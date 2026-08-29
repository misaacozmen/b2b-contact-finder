"""Network boundary checks for URLs discovered from untrusted web content."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

from modules import runtime


@dataclass(frozen=True)
class ResolvedTarget:
    scheme: str
    hostname: str
    port: int
    address: str


def safe_urlparse(value: str, *, metric: str = "url"):
    """Parse untrusted URL text and reject controls or malformed hosts."""
    raw = str(value or "")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        runtime.record(f"{metric}.parse_control_character")
        return None
    try:
        parsed = urlparse(raw)
        _ = parsed.hostname
        _ = parsed.port
    except (TypeError, ValueError, UnicodeError):
        runtime.record(f"{metric}.parse_error")
        return None
    if parsed.netloc.count("[") != parsed.netloc.count("]"):
        runtime.record(f"{metric}.parse_invalid_brackets")
        return None
    return parsed


class _PinnedHTTPAdapter(HTTPAdapter):
    """Connect to one validated IP while preserving HTTP Host and TLS SNI."""

    def __init__(self, target: ResolvedTarget):
        self.target = target
        super().__init__(max_retries=0)

    def get_connection_with_tls_context(
        self, request, verify, proxies=None, cert=None,
    ):
        host_params, pool_kwargs = self.build_connection_pool_key_attributes(
            request, verify, cert,
        )
        host_params["host"] = self.target.address
        if self.target.scheme == "https":
            pool_kwargs["assert_hostname"] = self.target.hostname
            pool_kwargs["server_hostname"] = self.target.hostname
        return self.poolmanager.connection_from_host(
            **host_params, pool_kwargs=pool_kwargs,
        )

    def send(self, request, **kwargs):
        default_port = 443 if self.target.scheme == "https" else 80
        host = self.target.hostname
        if ":" in host:
            host = f"[{host}]"
        request.headers["Host"] = (
            host if self.target.port == default_port
            else f"{host}:{self.target.port}"
        )
        # A proxy could resolve the hostname again and defeat address pinning.
        kwargs["proxies"] = {}
        return super().send(request, **kwargs)


class PublicOnlyHTTPAdapter(HTTPAdapter):
    """Resolve, validate and pin every request independently."""

    def send(self, request, **kwargs):
        target, reason = resolve_public_http_url(request.url)
        if target is None:
            raise requests.exceptions.InvalidURL(
                f"blocked_network_target:{reason}", request=request,
            )
        adapter = _PinnedHTTPAdapter(target)
        try:
            response = adapter.send(request, **kwargs)
        except Exception:
            adapter.close()
            raise
        original_close = response.close
        closed = False

        def close() -> None:
            nonlocal closed
            try:
                original_close()
            finally:
                if not closed:
                    closed = True
                    adapter.close()

        response.close = close
        return response


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return bool(address.is_global)


def resolve_public_http_url(
    url: str, resolver=socket.getaddrinfo,
) -> tuple[ResolvedTarget | None, str]:
    """Resolve a URL once and return a public address suitable for pinning."""
    try:
        parsed = safe_urlparse(url, metric="network")
        if parsed is None:
            return None, "invalid_url"
        port = parsed.port
    except ValueError:
        return None, "invalid_url"
    if parsed.scheme not in {"http", "https"}:
        return None, "unsupported_scheme"
    if parsed.username or parsed.password:
        return None, "userinfo_not_allowed"
    host = (parsed.hostname or "").rstrip(".").casefold()
    if not host:
        return None, "missing_host"
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return None, "local_host"
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            return None, "non_public_ip"
        return ResolvedTarget(
            parsed.scheme, host, port or (443 if parsed.scheme == "https" else 80), host,
        ), "public_ip"
    try:
        answers = resolver(host, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError, UnicodeError):
        return None, "dns_unresolved"
    addresses = {answer[4][0] for answer in answers if answer and len(answer) >= 5 and answer[4]}
    if not addresses:
        return None, "dns_no_address"
    if not all(_is_public_address(address) for address in addresses):
        return None, "dns_non_public_address"
    selected = min(
        addresses,
        key=lambda address: (
            ipaddress.ip_address(address.split("%", 1)[0]).version != 4,
            address,
        ),
    )
    return ResolvedTarget(
        parsed.scheme,
        host,
        port or (443 if parsed.scheme == "https" else 80),
        selected,
    ), "public_dns"


def validate_public_http_url(url: str, resolver=socket.getaddrinfo) -> tuple[bool, str]:
    """Reject local/private/reserved destinations before an HTTP request."""
    target, reason = resolve_public_http_url(url, resolver)
    return target is not None, reason


def harden_session(session: requests.Session) -> requests.Session:
    """Disable proxies and pin every HTTP(S) connection to a validated IP."""
    session.trust_env = False
    session.proxies.clear()
    session.mount("http://", PublicOnlyHTTPAdapter())
    session.mount("https://", PublicOnlyHTTPAdapter())
    return session
