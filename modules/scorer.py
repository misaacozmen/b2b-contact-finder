import re
import threading
from collections import Counter
from urllib.parse import urlparse

from rapidfuzz import fuzz

import config


_TOKEN_FREQUENCY_LOCK = threading.Lock()
_TOKEN_DOCUMENT_FREQUENCIES: Counter = Counter()
_TOKEN_DOCUMENT_COUNT = 0


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


def is_valid_hostname(url_or_domain: str) -> bool:
    """Reject paths and malformed resolver/snippet values before crawling."""
    domain = normalize_domain(url_or_domain)
    if not domain or len(domain) > 253 or "." not in domain:
        return False
    labels = domain.split(".")
    if labels[-1] in {"asp", "aspx", "php", "html", "htm", "jsp"}:
        return False
    return all(
        label and len(label) <= 63
        and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    ) and len(labels[-1]) >= 2 and labels[-1].isalpha()


def configure_company_token_frequencies(company_names: list[str]) -> None:
    """Build per-run document frequencies for auditable rare-token ranking."""
    global _TOKEN_DOCUMENT_FREQUENCIES, _TOKEN_DOCUMENT_COUNT
    documents = [set(legal_identity_tokens(name)) for name in company_names if name]
    with _TOKEN_FREQUENCY_LOCK:
        _TOKEN_DOCUMENT_FREQUENCIES = Counter(token for tokens in documents for token in tokens)
        _TOKEN_DOCUMENT_COUNT = len(documents)


def rare_identity_token_bonus(company_name: str, evidence_text: str) -> tuple[int, list[str]]:
    """Reward uncommon legal-name tokens only when the candidate contains them."""
    evidence = f" {normalize_text(evidence_text)} "
    compact = re.sub(r"[^a-z0-9]", "", normalize_text(evidence_text))
    with _TOKEN_FREQUENCY_LOCK:
        frequencies = dict(_TOKEN_DOCUMENT_FREQUENCIES)
        document_count = _TOKEN_DOCUMENT_COUNT
    if document_count < 2:
        return 0, []
    matched = []
    for token in legal_identity_tokens(company_name):
        if len(token) < 4 or frequencies.get(token, document_count) > max(1, document_count // 10):
            continue
        if f" {token} " in evidence or token in compact:
            matched.append(token)
    matched = list(dict.fromkeys(matched))
    return min(len(matched) * 4, 8), matched


MULTI_PART_PUBLIC_SUFFIXES = {
    "com.tr", "net.tr", "org.tr", "biz.tr", "info.tr", "web.tr", "gen.tr",
    "av.tr", "bel.tr", "gov.tr", "edu.tr", "k12.tr", "pol.tr", "tsk.tr",
    "co.uk", "org.uk", "gov.uk", "ac.uk",
}


def registrable_domain(url_or_domain: str) -> str:
    """Return a conservative registrable-domain approximation without network I/O."""
    domain = normalize_domain(url_or_domain)
    parts = domain.split(".")
    if len(parts) < 2:
        return domain
    suffix = ".".join(parts[-2:])
    if suffix in MULTI_PART_PUBLIC_SUFFIXES and len(parts) >= 3:
        return ".".join(parts[-3:])
    return suffix


def same_registrable_domain(first: str, second: str) -> bool:
    first_domain = registrable_domain(first)
    second_domain = registrable_domain(second)
    return bool(first_domain and first_domain == second_domain)


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


def legal_identity_tokens(company_name: str) -> list[str]:
    """Keep the public/legal name while dropping only corporate boilerplate."""
    ignored = {normalize_text(word) for word in config.LEGAL_COMPANY_WORDS}
    ignored.update({"ve", "and", "sirket", "sirketi"})
    return [
        token for token in _raw_company_tokens(company_name)
        if token not in ignored and len(token) > 1
    ]


def legal_name_phrase_match(company_name: str, text: str) -> bool:
    """Require the name words to appear together, not scattered across a page."""
    tokens = legal_identity_tokens(company_name)
    if not tokens:
        return False
    ignored = {normalize_text(word) for word in config.LEGAL_COMPANY_WORDS}
    ignored.update({"ve", "and", "sirket", "sirketi"})
    haystack_tokens = [
        token for token in _raw_company_tokens(text)
        if token not in ignored and len(token) > 1
    ]
    # Very long legal names often differ only in trailing activity clauses.
    # Four consecutive meaningful words are still substantially stronger than
    # independent token hits elsewhere in a catalogue or retailer page.
    needle = tokens if len(tokens) <= 4 else tokens[:4]
    width = len(needle)
    return any(haystack_tokens[index:index + width] == needle for index in range(len(haystack_tokens) - width + 1))


def legal_name_full_phrase_match(company_name: str, text: str) -> bool:
    """Require every meaningful legal-name token to appear contiguously.

    This stricter companion is only a ranking signal. It distinguishes a full
    first-party legal title from a page that repeats a generic name prefix.
    """
    needle = legal_identity_tokens(company_name)
    if not needle:
        return False
    ignored = {normalize_text(word) for word in config.LEGAL_COMPANY_WORDS}
    ignored.update({"ve", "and", "sirket", "sirketi"})
    haystack = [
        token for token in _raw_company_tokens(text)
        if token not in ignored and len(token) > 1
    ]
    width = len(needle)
    return any(
        haystack[index:index + width] == needle
        for index in range(len(haystack) - width + 1)
    )


def ownership_statement_match(company_name: str, text: str) -> bool:
    """Detect an explicit legal-name/brand ownership statement on the page."""
    tokens = legal_identity_tokens(company_name)
    if not tokens:
        return False
    normalized = " ".join(_raw_company_tokens(text))
    markers = (
        "ait bir marka", "markasidir", "markasi", "bunyesinde", "ticari unvani",
        "resmi unvani", "bir markadir", "brand of", "a brand of", "owned by",
        "belongs to", "operated by", "trading name of", "part of",
    )
    positions = [normalized.find(marker) for marker in markers if marker in normalized]
    if not positions:
        return False
    # The legal/public name and relationship wording must occur in the same
    # local statement. A marker in an unrelated footer must not resolve an
    # otherwise contradictory structured owner.
    needle = " ".join(tokens if len(tokens) <= 4 else tokens[:4])
    for position in positions:
        window = normalized[max(0, position - 240): position + 240]
        if needle in window:
            return True
    return False


def domain_identity_tokens(company_name: str) -> list[str]:
    """Return brand-like tokens used only for company/domain matching."""
    tokens = distinctive_tokens(company_name)
    generic = {normalize_text(word) for word in config.DOMAIN_IDENTITY_GENERIC_WORDS}
    brand_tokens = [token for token in tokens if token not in generic]
    return brand_tokens or tokens


def primary_brand_tokens(company_name: str, limit: int = 2) -> list[str]:
    tokens = distinctive_tokens(company_name)
    if not tokens:
        return []
    return tokens[:limit]


_IDENTITY_TOKEN_EQUIVALENTS = {
    "ileri": ("advanced",),
    "advanced": ("ileri",),
    "malzeme": ("material", "materials"),
    "malzemeler": ("material", "materials"),
    "material": ("malzeme", "malzemeler"),
    "materials": ("malzeme", "malzemeler"),
    "teknoloji": ("technology", "technologies"),
    "teknolojileri": ("technology", "technologies"),
    "technology": ("teknoloji", "teknolojileri"),
    "technologies": ("teknoloji", "teknolojileri"),
    "saglik": ("health", "healthcare"),
    "health": ("saglik",),
    "healthcare": ("saglik",),
    "tibbi": ("medical",),
    "medikal": ("medical",),
    "medical": ("tibbi", "medikal"),
    "makine": ("machine", "machinery"),
    "makina": ("machine", "machinery"),
    "machine": ("makine", "makina"),
    "machinery": ("makine", "makina"),
}


def primary_brand_text_hits(company_name: str, text: str, limit: int = 2) -> tuple[int, int, int]:
    """Match a public brand in first-party text with guarded TR/EN aliases.

    Translation aliases are considered only after the first, distinctive brand
    token appears exactly.  Thus a generic word such as ``medical`` or
    ``advanced`` can never establish identity by itself.
    """
    tokens = primary_brand_tokens(company_name, limit=limit)
    if not tokens:
        return 0, 0, 0
    words = set(_raw_company_tokens(text))
    exact_hits = sum(1 for token in tokens if token in words)
    anchored = len(tokens[0]) >= 5 and tokens[0] in words
    translated_hits = 0
    matched = 0
    for token in tokens:
        if token in words:
            matched += 1
            continue
        if anchored and any(alias in words for alias in _IDENTITY_TOKEN_EQUIVALENTS.get(token, ())):
            matched += 1
            translated_hits += 1
    return matched, len(tokens), translated_hits


def public_brand_domain_match(company_name: str, url_or_domain: str) -> bool:
    """Match a public brand at the start of a domain without using legal-name tail words.

    Long exhibitor legal names commonly append activities, import/export wording
    and corporate boilerplate to a much shorter public brand. Requiring half of
    that entire legal name to occur in the domain hides otherwise exact official
    sites. A short first word is deliberately not sufficient here because it is
    much more likely to be a homonym.
    """
    core = compact_domain_core(url_or_domain)
    brand_tokens = primary_brand_tokens(company_name, limit=2)
    if not core or not brand_tokens:
        return False

    compound = "".join(brand_tokens)
    if len(brand_tokens) >= 2 and len(compound) >= 7 and core == compound:
        return True

    primary = brand_tokens[0]
    if len(primary) < 7 or not core.startswith(primary):
        return False
    # Exact long public brands (metafiz.com.tr) and an anchored long brand with
    # a meaningful descriptor (appsilonadvancedmaterials.com) are accepted.
    suffix = core[len(primary):]
    return not suffix or len(suffix) >= 3


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


def explicit_activity_qualifiers(name: str) -> list[str]:
    """Return explicit industry qualifiers suitable for owner-name conflicts.

    This set is intentionally broader than search context validation: it is
    used only when both names explicitly state different activities, never to
    penalize a site merely for omitting an activity from a legal title.
    """
    vocabulary = {
        normalize_text(word)
        for word in (
            *config.SECTOR_GENERIC_WORDS,
            *config.BUSINESS_GENERIC_WORDS,
            *config.CONTEXT_VALIDATION_WORDS,
        )
    }
    vocabulary -= {
        "ve", "dahili", "hizmetleri", "servis", "satis", "sistemleri",
        "endustri", "endustriyel", "maddeler",
    }
    return list(dict.fromkeys(
        token for token in _raw_company_tokens(name) if token in vocabulary
    ))


def metadata_contexts(metadata: dict | None) -> list[str]:
    if not metadata:
        return []
    text = " ".join(_raw_company_tokens(f"{metadata.get('sector', '')} {metadata.get('description', '')}"))
    if not text:
        return []

    contexts = []
    for context, details in config.METADATA_CONTEXTS.items():
        aliases = (" ".join(_raw_company_tokens(alias)) for alias in details["aliases"])
        if any(alias and f" {alias} " in f" {text} " for alias in aliases):
            contexts.append(context)
    return contexts


def page_matches_metadata_context(page_text: str, context: str) -> bool:
    details = config.METADATA_CONTEXTS.get(context)
    if not details:
        return False
    text = " ".join(_raw_company_tokens(page_text))
    haystack = f" {text} "
    return any(
        alias and f" {alias} " in haystack
        for alias in (" ".join(_raw_company_tokens(alias)) for alias in details["aliases"])
    )


def search_name_variants(company_name: str) -> list[str]:
    variants: list[str] = []
    # Fair catalogues sometimes put two independent brands in one cell. Search
    # slash/pipe-separated names independently before trying the combined text.
    # Do not split '&': it is often part of a single legal/brand name.
    segments = [part.strip() for part in re.split(r"\s*[/|]\s*", company_name) if part.strip()]
    if len(segments) > 1:
        for segment in segments:
            variants.extend(search_name_variants(segment))

    brand_tokens = primary_brand_tokens(company_name, limit=2)
    if brand_tokens:
        variants.append(" ".join(brand_tokens))

    # Keep a short second brand component (for example the "A" in
    # "BRANDS A") even though one-letter tokens are normally too noisy for
    # domain scoring.  As a quoted company-name component it is useful search
    # evidence and lets engines return punctuation variants such as brands-a.
    raw_non_legal = [
        token
        for token in _raw_company_tokens(company_name)
        if token not in {normalize_text(word) for word in config.LEGAL_COMPANY_WORDS}
    ]
    if len(raw_non_legal) >= 2 and len(raw_non_legal[1]) == 1:
        variants.append(" ".join(raw_non_legal[:2]))

    # Short corporate abbreviations are commonly paired with a sector word in
    # the public domain (ATÇ Kimya -> atckimya.com).  Keep that compact search
    # form even when the sector word is excluded from brand-only scoring.
    if raw_non_legal and len(raw_non_legal[0]) <= 4:
        context_word = next(
            (token for token in raw_non_legal[1:] if token in config.CONTEXT_VALIDATION_WORDS),
            "",
        )
        if context_word:
            variants.append(f"{raw_non_legal[0]} {context_word}")

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


def _clean_single_token_domain_match(tokens: list[str], domain_compact: str) -> tuple[bool, str]:
    if len(tokens) != 2:
        return False, ""
    hits = [token for token in tokens if token in domain_compact]
    if len(hits) != 1:
        return False, ""
    token = hits[0]
    if domain_compact == token:
        return True, token
    return False, token


def _token_matches_domain(token: str, domain_compact: str) -> bool:
    """Avoid treating short brand fragments as matches inside another name."""
    if len(token) <= 3:
        return domain_compact == token
    if token in domain_compact:
        return True
    # Small, explicit TR/EN equivalents cover legal-name/domain language
    # changes without making generic sector terms sufficient on their own.
    equivalents = {
        "chocolate": ("cikolata",),
        "cikolata": ("chocolate",),
        "candy": ("seker", "sekerleme"),
        "seker": ("candy",),
        "sekerleme": ("candy",),
    }
    return any(alias in domain_compact for alias in equivalents.get(token, ()))


def _short_prefix_long_token_match(tokens: list[str], domain_compact: str) -> bool:
    """Match compact domains such as dratabay without allowing arbitrary substrings."""
    for token in tokens:
        if len(token) < 5 or not domain_compact.endswith(token):
            continue
        prefix = domain_compact[: -len(token)]
        if prefix and prefix in tokens and len(prefix) <= 3:
            return True
    return False


def _near_single_brand_prefix_match(tokens: list[str], domain_compact: str) -> bool:
    """Allow a one-character typo at the start of a longer brand domain."""
    if len(tokens) != 1 or len(tokens[0]) < 5 or len(domain_compact) < len(tokens[0]):
        return False
    token = tokens[0]
    prefix = domain_compact[: len(token)]
    return token != prefix and token[:4] == prefix[:4] and fuzz.ratio(token, prefix) >= 83


def domain_identity_match(company_name: str, url_or_domain: str) -> tuple[bool, int, int]:
    domain_compact = compact_domain_core(url_or_domain)
    tokens = domain_identity_tokens(company_name)
    if not tokens or not domain_compact:
        return False, 0, len(tokens)

    hits = sum(1 for token in tokens if _token_matches_domain(token, domain_compact))
    clean_single_token_match, _ = _clean_single_token_domain_match(tokens, domain_compact)
    compact_brand_match = _short_prefix_long_token_match(tokens, domain_compact)
    near_brand_prefix_match = _near_single_brand_prefix_match(tokens, domain_compact)
    raw_tokens = _raw_company_tokens(company_name)
    anchored_primary_match = bool(
        raw_tokens
        and 3 <= len(raw_tokens[0]) <= 4
        and domain_compact.startswith(raw_tokens[0])
        and any(len(token) >= 4 and token in domain_compact for token in raw_tokens[1:])
    )
    if len(tokens) <= 2:
        required_hits = len(tokens)
    else:
        required_hits = max(
            config.MIN_DISTINCTIVE_DOMAIN_HITS,
            int(len(tokens) * config.MIN_DISTINCTIVE_DOMAIN_HIT_RATIO + 0.999),
        )

    company_compact = "".join(tokens)
    fuzzy = fuzz.token_set_ratio(company_compact, domain_compact)
    exact_compact_match = len(company_compact) >= 5 and fuzzy >= 88
    brand_tokens = domain_identity_tokens(company_name)[:2]
    primary_brand_match = (
        len(tokens) == 1
        and bool(brand_tokens)
        and len(brand_tokens[0]) >= 4
        and brand_tokens[0] in domain_compact
    )

    abbrev_match = _abbreviation_match(tokens, domain_compact)
    if not abbrev_match:
        abbrev_match = _abbreviation_match(_company_tokens(company_name, include_sector_words=True), domain_compact)

    return (
        hits >= required_hits
        or clean_single_token_match
        or compact_brand_match
        or near_brand_prefix_match
        or exact_compact_match
        or abbrev_match
        or primary_brand_match
        # Short abbreviations such as ATÇ are safe only when anchored at the
        # start and accompanied by another word from the legal company name.
        # This admits atckimya.com without making arbitrary "atc" substrings
        # valid.
        or anchored_primary_match,
        hits,
        len(tokens),
    )


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
    company_compact = "".join(domain_identity_tokens(company_name) or tokens)
    fuzzy_score = int(fuzz.partial_ratio(company_compact, core_compact) * 0.24)

    text_bonus = 0
    for token in tokens:
        if token in searchable:
            text_bonus += 2
    text_bonus = min(text_bonus, 8)

    tld_bonus = max((bonus for suffix, bonus in config.COUNTRY_TLD_BONUSES.items() if domain.endswith(suffix)), default=0)
    generic_penalty = 30 if any(keyword in domain for keyword in config.GENERIC_DOMAIN_KEYWORDS) else 0
    brand_tokens = domain_identity_tokens(company_name)[:2]
    primary_brand_bonus = 0
    if brand_tokens and _token_matches_domain(brand_tokens[0], core_compact):
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

    compact_brand_bonus = 0
    if _short_prefix_long_token_match(domain_identity_tokens(company_name), core_compact):
        compact_brand_bonus = 24

    near_brand_bonus = 0
    if _near_single_brand_prefix_match(domain_identity_tokens(company_name), core_compact):
        near_brand_bonus = 24

    raw_tokens = _raw_company_tokens(company_name)
    anchored_primary_match = bool(
        raw_tokens
        and 3 <= len(raw_tokens[0]) <= 4
        and core_compact.startswith(raw_tokens[0])
        and any(len(token) >= 4 and token in core_compact for token in raw_tokens[1:])
    )
    anchored_primary_bonus = 24 if anchored_primary_match else 0

    rare_token_bonus, rare_tokens = rare_identity_token_bonus(
        company_name, f"{domain_core(domain)} {title} {snippet}",
    )
    # Corpus rarity is a tie-break signal, not confidence.  Adding it to the
    # score would change early-stop/search-expansion decisions and could reduce
    # recall merely because the surrounding input batch changed.
    score = token_score + fuzzy_score + text_bonus + tld_bonus + primary_brand_bonus + context_bonus + abbrev_bonus + compact_brand_bonus + near_brand_bonus + anchored_primary_bonus - generic_penalty - context_penalty
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
    if compact_brand_bonus:
        reasons.append(f"compact_brand_bonus:{compact_brand_bonus}")
    if near_brand_bonus:
        reasons.append(f"near_brand_bonus:{near_brand_bonus}")
    if rare_token_bonus:
        reasons.append(f"rare_token_signal:{rare_token_bonus}:{','.join(rare_tokens)}")
    if anchored_primary_bonus:
        score = max(score, 72)
        reasons.append(f"anchored_primary_bonus:{anchored_primary_bonus}")
    if generic_penalty:
        reasons.append(f"generic_penalty:{generic_penalty}")
    clean_single_token_match, matched_token = _clean_single_token_domain_match(domain_identity_tokens(company_name), core_compact)
    if clean_single_token_match:
        score = max(score, 68)
        score = min(score, config.SAFE_OK_MIN_SCORE - 1)
        reasons.append(f"clean_single_token_domain:{matched_token}")
    distinctive = domain_identity_tokens(company_name)
    if token_count == 1 and len((distinctive or [''])[0]) <= 3:
        score = min(score, config.SHORT_COMPANY_MIN_SCORE - 1)
        reasons.append("short_name_capped")

    return {
        "score": max(0, min(100, score)),
        "reason": "; ".join(reasons),
        "domain_hits": domain_hits,
        "token_count": token_count,
        "rare_token_signal": rare_token_bonus,
    }
