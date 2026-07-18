"""Network boundary checks for URLs discovered from untrusted web content."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return bool(address.is_global)


def validate_public_http_url(url: str, resolver=socket.getaddrinfo) -> tuple[bool, str]:
    """Reject local/private/reserved destinations before every HTTP request."""
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False, "invalid_url"
    if parsed.scheme not in {"http", "https"}:
        return False, "unsupported_scheme"
    if parsed.username or parsed.password:
        return False, "userinfo_not_allowed"
    host = (parsed.hostname or "").rstrip(".").casefold()
    if not host:
        return False, "missing_host"
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False, "local_host"
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        return (True, "public_ip") if literal.is_global else (False, "non_public_ip")
    try:
        answers = resolver(host, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError, UnicodeError):
        return False, "dns_unresolved"
    addresses = {answer[4][0] for answer in answers if answer and len(answer) >= 5 and answer[4]}
    if not addresses:
        return False, "dns_no_address"
    if not all(_is_public_address(address) for address in addresses):
        return False, "dns_non_public_address"
    return True, "public_dns"
