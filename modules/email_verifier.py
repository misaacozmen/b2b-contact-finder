from functools import lru_cache

try:
    import dns.exception
    import dns.resolver
except ImportError:
    DNS_AVAILABLE = False
else:
    DNS_AVAILABLE = True

import config
from modules import cache_store


def _implicit_mx_status(resolver, domain: str) -> tuple[str, str]:
    for record_type in ("A", "AAAA"):
        try:
            answers = resolver.resolve(domain, record_type)
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            continue
        except (dns.resolver.Timeout, dns.exception.DNSException) as exc:
            return "unverified", f"implicit_mx_lookup_{exc.__class__.__name__.lower()}"
        if answers:
            return "verified", f"implicit_mx_{record_type.lower()}"
    return "invalid_domain", "mx_and_address_missing"


@lru_cache(maxsize=4096)
def _domain_mx_status(domain: str) -> tuple[str, str]:
    if not DNS_AVAILABLE:
        return "not_checked", "dnspython_not_installed"
    resolver = dns.resolver.Resolver()
    resolver.timeout = config.EMAIL_DNS_TIMEOUT_SEC
    resolver.lifetime = config.EMAIL_DNS_TIMEOUT_SEC
    try:
        answers = resolver.resolve(domain, "MX")
    except dns.resolver.NXDOMAIN:
        return "invalid_domain", "mx_nxdomain"
    except dns.resolver.NoAnswer:
        return _implicit_mx_status(resolver, domain)
    except (dns.resolver.Timeout, dns.exception.DNSException) as exc:
        return "unverified", f"mx_lookup_{exc.__class__.__name__.lower()}"
    if not answers:
        return _implicit_mx_status(resolver, domain)
    if all(str(getattr(answer, "exchange", "")).rstrip(".") == "" for answer in answers):
        return "invalid_domain", "null_mx"
    return "verified", "mx_present"


def verify_email(email: str) -> dict[str, str]:
    if not email or "@" not in email:
        return {"status": "not_checked", "reason": "no_email"}
    if not config.VERIFY_EMAIL_MX:
        return {"status": "not_checked", "reason": "mx_check_disabled"}
    domain = email.rsplit("@", 1)[1].strip().lower()
    if not domain:
        return {"status": "invalid_domain", "reason": "missing_domain"}
    if config.CRAWL_CACHE_MODE in {"use", "replay"}:
        cached = cache_store.load(
            config.EMAIL_CACHE_DIR, "mx", domain,
            config.CRAWL_CACHE_TTL_DAYS, config.CACHE_SCHEMA_VERSION,
        )
        if cached is not None:
            return cached
        if config.CRAWL_CACHE_MODE == "replay":
            return {"status": "not_checked", "reason": "mx_replay_cache_miss"}
    status, reason = _domain_mx_status(domain)
    result = {"status": status, "reason": reason}
    if config.CRAWL_CACHE_MODE in {"use", "refresh"}:
        cache_store.save(
            config.EMAIL_CACHE_DIR, "mx", domain, result,
            config.CACHE_SCHEMA_VERSION,
        )
    return result
