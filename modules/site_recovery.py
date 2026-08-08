"""Safe same-entity URL variants for legacy or misconfigured websites."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from modules import scorer


def root_variants(url: str) -> list[str]:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = parsed.hostname or ""
    if not host:
        return []
    try:
        ipaddress.ip_address(host)
        host_variants = [host]
    except ValueError:
        alternate = host[4:] if host.casefold().startswith("www.") else f"www.{host}"
        host_variants = [host, alternate]
    values: list[str] = []
    for candidate_host in host_variants:
        if not scorer.same_registrable_domain(host, candidate_host):
            continue
        for scheme in ("https", "http"):
            port = f":{parsed.port}" if parsed.port else ""
            values.append(f"{scheme}://{candidate_host}{port}")
    return list(dict.fromkeys(values))
