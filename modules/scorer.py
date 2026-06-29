import re
from urllib.parse import urlparse

from rapidfuzz import fuzz

import config


TRANSLATION_TABLE = str.maketrans(
    {
        "\u00e7": "c",
        "\u011f": "g",
        "\u0131": "i",
        "\u00f6": "o",
        "\u015f": "s",
        "\u00fc": "u",
        "\u00c7": "c",
        "\u011e": "g",
        "\u0130": "i",
        "I": "i",
        "\u00d6": "o",
        "\u015e": "s",
        "\u00dc": "u",
    }
)


def normalize_text(value: str) -> str:
    return (value or "").translate(TRANSLATION_TABLE).lower()


def normalize_domain(url_or_domain: str) -> str:
    value = (url_or_domain or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    domain = parsed.netloc.lower() or parsed.path.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return normalize_text(domain.split(":")[0].rstrip("/"))


def domain_core(domain: str) -> str:
    domain = normalize_domain(domain)
    for suffix in (".com.tr", ".net.tr", ".org.tr", ".co.uk"):
        if domain.endswith(suffix):
            return domain[: -len(suffix)]
    return re.sub(r"\.[a-z]{2,}$", "", domain)


def compact_domain_core(domain: str) -> str:
    return re.sub(r"[^a-z0-9]", "", domain_core(domain))


def is_excluded_domain(domain: str) -> bool:
    domain = normalize_domain(domain)
    return any(domain == excluded or domain.endswith(f".{excluded}") for excluded in config.EXCLUDED_DOMAINS)


def is_foreign_country_domain(domain: str) -> bool:
    domain = normalize_domain(domain)
    if not domain:
        return False
    parts = domain.split(".")
    if len(parts) < 2:
        return False
    last = parts[-1]
    if last in config.FOREIGN_COUNTRY_TLDS:
        return True
    if len(parts) >= 3 and parts[-2] in {"com", "net", "org", "gov", "edu", "co"} and last in config.FOREIGN_COUNTRY_TLDS:
        return True
    return False


def _raw_company_tokens(company_name: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", normalize_text(company_name))


def _company_tokens(company_name: str, include_sector_words: bool = True) -> list[str]:
    stopwords = {normalize_text(word) for word in config.LEGAL_COMPANY_WORDS}
    if not include_sector_words:
        stopwords |= {normalize_text(word) for word in config.SECTOR_GENERIC_WORDS}
        stopwords |= {normalize_text(word) for word in config.BUSINESS_GENERIC_WORDS}
    return [token for token in _raw_company_tokens(company_name) if token not in stopwords and len(token) > 1]


def distinctive_tokens(company_name: str) -> list[str]:
    tokens = _company_tokens(company_name, include_sector_words=False)
    if tokens:
        return tokens
    return _company_tokens(company_name, include_sector_words=True)


def primary_brand_tokens(company_name: str, limit: int = 2) -> list[str]:
    tokens = distinctive_tokens(company_name)
    if not tokens:
        return []
    return tokens[:limit]


def context_tokens(company_name: str) -> list[str]:
    raw_tokens = _raw_company_tokens(company_name)
    tokens = [token for token in raw_tokens if token in config.CONTEXT_VALIDATION_WORDS]
    seen = set()
    unique = []
    for token in tokens:
        if token not in seen:
            unique.append(token)
            seen.add(token)
    return unique


def search_name_variants(company_name: str) -> list[str]:
    variants: list[str] = []
    brand_tokens = primary_brand_tokens(company_name, limit=2)
    if brand_tokens:
        variants.append(" ".join(brand_tokens))
        variants.append(brand_tokens[0])

    normalized_full = " ".join(_raw_company_tokens(company_name))
    if normalized_full:
        variants.append(normalized_full)

    unique: list[str] = []
    seen = set()
    for variant in variants:
        key = variant.strip().lower()
        if key and key not in seen:
            unique.append(variant.strip())
            seen.add(key)
    return unique


def _abbreviation_match(tokens: list[str], domain_compact: str) -> bool:
    if len(tokens) < 2:
        return False

    initials = "".join(token[0] for token in tokens)
    if len(initials) >= 2 and initials in domain_compact:
        return True

    first_plus_initials = tokens[0] + "".join(token[0] for token in tokens[1:])
    if len(first_plus_initials) >= 4 and first_plus_initials in domain_compact:
        return True

    for prefix_len in (3, 4):
        merged = "".join(token[:prefix_len] for token in tokens if len(token) >= prefix_len)
        if len(merged) >= 4 and merged in domain_compact:
            return True
    return False


def domain_identity_match(company_name: str, url_or_domain: str) -> tuple[bool, int, int]:
    domain_compact = compact_domain_core(url_or_domain)
    tokens = distinctive_tokens(company_name)
    if not tokens or not domain_compact:
        return False, 0, len(tokens)

    hits = sum(1 for token in tokens if token in domain_compact)
    required_hits = max(
        config.MIN_DISTINCTIVE_DOMAIN_HITS,
        int(len(tokens) * config.MIN_DISTINCTIVE_DOMAIN_HIT_RATIO + 0.999),
    )

    company_compact = "".join(tokens)
    fuzzy = fuzz.token_set_ratio(company_compact, domain_compact)
    exact_compact_match = len(company_compact) >= 5 and fuzzy >= 88
    brand_tokens = primary_brand_tokens(company_name, limit=2)
    primary_brand_match = bool(brand_tokens) and len(brand_tokens[0]) >= 4 and brand_tokens[0] in domain_compact

    abbrev_match = _abbreviation_match(tokens, domain_compact)
    if not abbrev_match:
        abbrev_match = _abbreviation_match(_company_tokens(company_name, include_sector_words=True), domain_compact)

    return hits >= required_hits or exact_compact_match or abbrev_match or primary_brand_match, hits, len(tokens)


def score_domain(company_name: str, url_or_domain: str, title: str = "", snippet: str = "") -> int:
    return score_domain_details(company_name, url_or_domain, title=title, snippet=snippet)["score"]


def score_domain_details(company_name: str, url_or_domain: str, title: str = "", snippet: str = "") -> dict:
    domain = normalize_domain(url_or_domain)
    if not domain or is_excluded_domain(domain):
        return {"score": 0, "reason": "excluded_or_empty_domain", "domain_hits": 0, "token_count": 0}
    if is_foreign_country_domain(domain):
        return {"score": 0, "reason": "foreign_country_domain", "domain_hits": 0, "token_count": 0}
    if any(keyword in domain for keyword in config.NON_COMPANY_DOMAIN_KEYWORDS):
        return {"score": 0, "reason": "non_company_domain_keyword", "domain_hits": 0, "token_count": 0}

    identity_ok, domain_hits, token_count = domain_identity_match(company_name, domain)
    if not identity_ok:
        return {
            "score": 0,
            "reason": f"no_distinctive_domain_match:{domain_hits}/{token_count}",
            "domain_hits": domain_hits,
            "token_count": token_count,
        }

    tokens = _company_tokens(company_name, include_sector_words=True)
    core_compact = compact_domain_core(domain)
    searchable = normalize_text(f"{domain_core(domain)} {title} {snippet}")
    if not tokens:
        return {"score": 0, "reason": "no_company_tokens", "domain_hits": 0, "token_count": 0}

    token_score = int((domain_hits / max(token_count, 1)) * 32)
    company_compact = "".join(distinctive_tokens(company_name) or tokens)
    fuzzy_score = int(fuzz.partial_ratio(company_compact, core_compact) * 0.24)

    text_bonus = 0
    for token in tokens:
        if token in searchable:
            text_bonus += 2
    text_bonus = min(text_bonus, 8)

    tld_bonus = max((bonus for suffix, bonus in config.COUNTRY_TLD_BONUSES.items() if domain.endswith(suffix)), default=0)
    generic_penalty = 30 if any(keyword in domain for keyword in config.GENERIC_DOMAIN_KEYWORDS) else 0
    brand_tokens = primary_brand_tokens(company_name, limit=2)
    primary_brand_bonus = 0
    if brand_tokens and brand_tokens[0] in core_compact:
        primary_brand_bonus = 14
    support_tokens = context_tokens(company_name)
    support_hits = sum(1 for token in support_tokens if token in searchable)
    context_bonus = 0
    context_penalty = 0
    if support_tokens:
        if support_hits:
            context_bonus = min(8, support_hits * 4)
        else:
            context_penalty = 30

    abbrev_bonus = 0
    if domain_hits == 0 and _abbreviation_match(_company_tokens(company_name, include_sector_words=True), core_compact):
        abbrev_bonus = 24

    score = token_score + fuzzy_score + text_bonus + tld_bonus + primary_brand_bonus + context_bonus + abbrev_bonus - generic_penalty - context_penalty
    reasons = [
        f"domain_hits:{domain_hits}/{token_count}",
        f"fuzzy:{fuzzy_score}",
        f"text_bonus:{text_bonus}",
        f"tld_bonus:{tld_bonus}",
    ]
    if primary_brand_bonus:
        reasons.append(f"primary_brand_bonus:{primary_brand_bonus}")
    if context_bonus:
        reasons.append(f"context_bonus:{context_bonus}")
    if context_penalty:
        reasons.append(f"context_penalty:{context_penalty}")
    if abbrev_bonus:
        reasons.append(f"abbrev_bonus:{abbrev_bonus}")
    if generic_penalty:
        reasons.append(f"generic_penalty:{generic_penalty}")
    distinctive = distinctive_tokens(company_name)
    if token_count == 1 and len((distinctive or [''])[0]) <= 3:
        score = min(score, config.SHORT_COMPANY_MIN_SCORE - 1)
        reasons.append("short_name_capped")

    return {
        "score": max(0, min(100, score)),
        "reason": "; ".join(reasons),
        "domain_hits": domain_hits,
        "token_count": token_count,
    }
