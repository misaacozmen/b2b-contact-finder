from __future__ import annotations

import re
from typing import Callable
from urllib.parse import unquote, urlparse

import config
from modules import aliases, query_planner, scorer

DISCOVERY_ONLY_ROLES = {
    "directory", "fair_profile", "shared_listing", "marketplace", "news",
    "public_body",
}


def metadata_query_terms(metadata: dict | None) -> list[str]:
    return [config.METADATA_CONTEXTS[context]["query_term"] for context in scorer.metadata_contexts(metadata)[:2]]


def query_priority(query: str) -> int:
    normalized = scorer.normalize_text(query)
    if "official website" in normalized and any(
        term in normalized for term in (scorer.normalize_text(value) for value in config.TARGET_COUNTRY_QUERY_TERMS)
    ):
        return 3
    if "official website" in normalized or "resmi sitesi" in normalized:
        return 2
    if normalized.endswith(" contact") or normalized.endswith(" iletisim"):
        return 0
    return 1


def query_trust_bonus(query: str, *, query_priority_fn: Callable[[str], int] = query_priority) -> int:
    priority = query_priority_fn(query)
    if priority == 3:
        return config.TARGET_COUNTRY_OFFICIAL_QUERY_BONUS
    if priority == 2:
        return config.OFFICIAL_WEBSITE_QUERY_BONUS
    return 0


def metadata_context_match_count(metadata: dict | None, text: str) -> int:
    return sum(
        1
        for context in scorer.metadata_contexts(metadata)
        if scorer.page_matches_metadata_context(text, context)
    )


def candidate_rank_key(item: dict) -> tuple[int, ...]:
    role = item.get("role", "unknown")
    reason = item.get("reason", "")
    discovery_only = "discovery_only_not_identity_authority" in reason
    # A labelled outbound website is a high-value route to crawl even though
    # the listing/PDF that exposed it remains completely non-authoritative.
    outbound_evidence = item.get("_outbound_discovery_evidence", [])
    strong_outbound_route = bool(
        role == "company_candidate"
        and item.get("score", 0) >= 65
        and (
            item.get("query") == "search_bridge_profile"
            or any(
                urlparse(str(evidence.get("source_url", ""))).path.casefold().endswith(".pdf")
                for evidence in outbound_evidence
            )
        )
    )
    intrinsic_domain_identity = bool(
        item.get("_exact_brand_domain")
        or item.get("_public_brand_domain")
        or re.search(r"(?:^|;\s*)domain_hits:[1-9]\d*/", reason)
    )
    return (
        0 if role in DISCOVERY_ONLY_ROLES else 1,
        0 if discovery_only and not strong_outbound_route else 1,
        item.get("_ownership_evidence", 0),
        1 if role == "company_candidate" or item.get("_exact_brand_domain") or item.get("_public_brand_domain") else 0,
        1 if intrinsic_domain_identity else 0,
        item.get("_legal_name_evidence", 0),
        item["score"],
        item.get("_rare_token_signal", 0),
        item.get("_metadata_context_matches", 0),
        item.get("_official_query_evidence", 0),
        item.get("_query_trust_bonus", 0),
    )


def candidate_search_control_key(
    item: dict, *, candidate_rank_key_fn: Callable[[dict], tuple[int, ...]] = candidate_rank_key,
) -> tuple[int, ...]:
    """Keep corpus rarity from changing query expansion and cache seed sets."""
    key = candidate_rank_key_fn(item)
    return key[:7] + key[8:]


def candidate_role(company_name: str, url: str, title: str, snippet: str) -> str:
    """Classify entity-profile results before considering domain similarity."""
    domain = scorer.normalize_domain(url)
    if scorer.is_public_body_domain(domain):
        return "public_body"
    intrinsic_company_domain = scorer.domain_identity_match(company_name, url)[0]
    raw_path = unquote(urlparse(url).path).casefold()
    uuid_company_record = bool(re.search(
        r"/(?:company|firma|member|exhibitor)/"
        r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
        r"(?:/|$)", raw_path,
    ))
    if uuid_company_record:
        return "directory"
    path = scorer.normalize_text(unquote(urlparse(url).path.replace("/", " ")))
    raw_query = unquote(urlparse(url).query).casefold()
    text = scorer.normalize_text(f"{domain} {path} {title} {snippet}")
    directory_markers = (
        "directory", "firma rehberi", "company profile", "companies list",
        "supplier profile", "exporters", "marketplace", "yellow pages",
        "firmalar", "firma listesi", "company directory",
    )
    fair_markers = ("exhibitor", "katilimci", "trade fair", "expo profile")
    marketplace_markers = ("marketplace", "urunleri", "products", "supplier", "satici", "magaza")
    news_markers = ("haber", "news", "basin bulteni", "press release")
    directory_hits = sum(marker in text for marker in directory_markers)
    fair_host = any(marker in domain for marker in ("expo", "fuar", "exhibition"))
    fair_page = any(marker in text for marker in (
        "katilimci", "exhibitor", "trade fair", "fuari", "fuarı",
        "salon", "stant", "hall", "booth",
    ))
    if fair_host and fair_page:
        return "fair_profile"
    if any(marker in domain for marker in (
        "haber", "gazete", "news", "medya", "insesi",
    )):
        return "news"
    profile_path = any(marker in path.split() for marker in (
        "company", "companies", "firma", "firmalar", "profile", "supplier",
        "exhibitor", "katilimci", "listing", "member", "detail",
    ))
    generic_host = any(keyword in domain for keyword in config.GENERIC_DOMAIN_KEYWORDS)
    entity_query = bool(re.search(
        r"(?:^|&)(?:slug|company|companyid|company_id|firma|member|supplier|exhibitor)=",
        raw_query,
    ))
    entity_detail_variant = bool(re.search(
        r"/(?:company|firma|girisim|girişim|supplier|member|exhibitor)[-_]?(?:profile|profil|detail|detay)(?:/|$)",
        raw_path,
    ))
    numbered_company_record = bool(re.search(
        r"/(?:firma|company|member)[-_]\d+(?:[-_/]|$)", raw_path,
    ))
    if entity_query or entity_detail_variant or numbered_company_record:
        return "directory"
    if intrinsic_company_domain:
        return "company_candidate"
    if any(marker in path for marker in (
        "basin odasi", "basin bulteni", "press room", "press release", "news", "haber",
    )):
        return "news"
    entity_detail_path = bool(re.search(
        r"/(?:company|companies|firma|firmalar|supplier|exhibitor|katilimci|member)/(?:view/)?[^/]+",
        raw_path,
    ))
    generic_brand_detail = generic_host and bool(re.search(r"/(?:brand|detail)/[^/]+", raw_path))
    if directory_hits >= 2 or (
        directory_hits and any(keyword in domain for keyword in config.GENERIC_DOMAIN_KEYWORDS)
    ):
        return "directory"
    if entity_detail_path and directory_hits:
        return "directory"
    if generic_brand_detail:
        return "fair_profile" if any(marker in text for marker in ("fair", "fuar", "expo", "exhibition")) else "directory"
    if sum(marker in text for marker in fair_markers) >= 2:
        return "fair_profile"
    if any(marker in path for marker in ("exhibitor", "katilimci")) and any(
        marker in text for marker in ("fair", "fuar", "expo", "exhibition")
    ):
        return "fair_profile"
    if sum(marker in text for marker in marketplace_markers) >= 2 and (profile_path or generic_host):
        return "marketplace"
    if sum(marker in text for marker in news_markers) >= 2:
        return "news"
    return "unknown"


def primary_queries(
    company_name: str, metadata: dict | None, *,
    metadata_query_terms_fn: Callable[[dict | None], list[str]] = metadata_query_terms,
    query_priority_fn: Callable[[str], int] = query_priority,
) -> list[str]:
    queries: list[str] = []
    seen_queries = set()
    full_name = re.sub(r"\s+", " ", company_name).strip()
    if full_name:
        for query in (
            f'"{full_name}" Turkiye official website',
            f'"{full_name}" resmi sitesi',
        ):
            queries.append(query)
            seen_queries.add(query)
    query_inputs = scorer.search_name_variants(company_name)
    for alias in aliases.search_terms(company_name):
        query_inputs.extend(scorer.search_name_variants(alias))
    for query_input in dict.fromkeys(query_inputs):
        for template in config.SEARCH_QUERY_TEMPLATES:
            query = template.format(company=query_input)
            if query not in seen_queries:
                queries.append(query)
                seen_queries.add(query)
        for term in metadata_query_terms_fn(metadata):
            query = f"{query_input} {term}"
            if query not in seen_queries:
                queries.append(query)
                seen_queries.add(query)
        for country in config.TARGET_COUNTRY_QUERY_TERMS:
            for template in config.SEARCH_COUNTRY_QUERY_TEMPLATES:
                query = template.format(company=query_input, country=country)
                if query not in seen_queries:
                    queries.append(query)
                    seen_queries.add(query)
    if config.MAX_SEARCH_QUERIES_PER_COMPANY > 0:
        return sorted(queries, key=query_priority_fn, reverse=True)[: config.MAX_SEARCH_QUERIES_PER_COMPANY]
    return sorted(queries, key=query_priority_fn, reverse=True)


def query_covers_full_identity(company_name: str, query: str) -> bool:
    tokens = scorer.legal_identity_tokens(company_name)
    normalized_query = set(re.findall(
        r"[a-z0-9]+", scorer.normalize_text(query),
    ))
    return bool(tokens) and all(token in normalized_query for token in tokens)


def fallback_queries(
    company_name: str, metadata: dict | None, *,
    metadata_query_terms_fn: Callable[[dict | None], list[str]] = metadata_query_terms,
    query_priority_fn: Callable[[str], int] = query_priority,
) -> list[str]:
    full_name = " ".join(scorer._raw_company_tokens(company_name))
    if not full_name:
        return []
    quoted_name = f'"{full_name}"'
    contexts = metadata_query_terms_fn(metadata)
    queries = [
        f"{quoted_name} {contexts[0]} resmi sitesi" if contexts else "",
        f"{quoted_name} Turkiye official website",
        f"{quoted_name} iletisim",
    ]
    unique = list(dict.fromkeys(query for query in queries if query))
    return sorted(unique, key=query_priority_fn, reverse=True)[: config.MAX_FALLBACK_SEARCH_QUERIES]


def adaptive_queries(
    company_name: str, metadata: dict | None,
    already_run: set[str] | None = None, related_name_hints: list[str] | None = None,
    evidence_gaps: set[str] | None = None, *,
    metadata_query_terms_fn: Callable[[dict | None], list[str]] = metadata_query_terms,
) -> list[str]:
    """Build high-information queries only after the static plan is weak.

    These queries target public-brand/legal-name divergence and first-party
    disclosure pages. They do not carry identity authority; they only add
    search candidates that still pass the normal crawl and publication gates.
    """
    return query_planner.adaptive_queries(
        company_name,
        metadata,
        already_run=already_run,
        related_name_hints=related_name_hints,
        context_terms=metadata_query_terms_fn(metadata),
        evidence_gaps=evidence_gaps,
        limit=config.MAX_ADAPTIVE_SEARCH_QUERIES,
    )


def adaptive_discovery_gaps(
    company_name: str, candidates_by_domain: dict[str, dict],
    related_name_hints: list[str] | None = None, *,
    candidate_rank_key_fn: Callable[[dict], tuple[int, ...]] = candidate_rank_key,
) -> set[str]:
    """Describe unresolved discovery evidence without granting authority."""
    candidates = [
        item for item in candidates_by_domain.values()
        if item.get("role") not in DISCOVERY_ONLY_ROLES
        and not scorer.is_excluded_domain(item.get("url", ""))
    ]
    gaps: set[str] = set()
    if not candidates:
        gaps.add("no_candidates")
    ranked = sorted(candidates, key=candidate_rank_key_fn, reverse=True)
    if len(ranked) >= 2 and abs(ranked[0].get("score", 0) - ranked[1].get("score", 0)) <= config.AMBIGUOUS_CANDIDATE_MARGIN:
        gaps.add("ambiguous_candidates")
    brand_tokens = scorer.primary_brand_tokens(company_name, limit=1)
    # A single search hit is not uniqueness evidence for a short public brand;
    # explicitly seek the legal/full-name variant before accepting it.
    if brand_tokens and len(brand_tokens[0]) < 7 and ranked:
        gaps.add("ambiguous_candidates")
    if not any(
        (
            scorer.domain_identity_match(company_name, item.get("url", ""))[0]
            or scorer.public_brand_domain_match(company_name, item.get("url", ""))
        )
        and "search_text_identity:" not in item.get("reason", "")
        for item in candidates
    ):
        gaps.add("missing_intrinsic_domain")
    if not any(
        item.get("_legal_name_evidence") or item.get("_ownership_evidence")
        for item in candidates
    ):
        gaps.add("missing_legal_name")
    if not any(
        scorer.normalize_domain(item.get("url", "")).endswith(".tr")
        or item.get("_metadata_context_matches", 0) > 0
        for item in candidates
    ):
        gaps.add("missing_local_signal")
    if related_name_hints:
        gaps.add("relationship_hint")
    return gaps


def related_name_hints(company_name: str, title: str, snippet: str) -> list[str]:
    """Extract low-authority related-name hints from a legal-name result.

    Chamber and registry snippets sometimes expose a former/public company name
    inside an industrial-site or facility name.  The hint is used only to form
    another search query; it never contributes identity authority.
    """
    evidence_text = f"{title} {snippet}"
    if not scorer.legal_name_phrase_match(company_name, evidence_text):
        return []
    target_tokens = set(scorer._raw_company_tokens(company_name))
    pattern = re.compile(
        r"((?:[A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜ0-9-]{1,}[ \t]+){1,4}"
        r"[A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜ0-9-]{1,})[ \t]+S[İI]T(?:ES[İI])?\.?"
    )
    ignored = {
        scorer.normalize_text(word) for word in (
            *config.LEGAL_COMPANY_WORDS,
            "mahalle", "mahallesi", "cadde", "caddesi", "sokak", "bulvar",
            "organize", "sanayi", "sitesi", "site",
        )
    }
    hints: list[str] = []
    for match in pattern.finditer(evidence_text):
        raw_hint = match.group(1).replace("-", "")
        tokens = [
            token for token in scorer._raw_company_tokens(raw_hint)
            if token not in ignored and len(token) > 2
        ]
        if not 2 <= len(tokens) <= 4:
            continue
        shared = {token for token in tokens if token in target_tokens and len(token) >= 4}
        novel = [token for token in tokens if token not in target_tokens and len(token) >= 4]
        if not shared or not novel:
            continue
        hint = " ".join(tokens)
        if hint not in hints:
            hints.append(hint)
    return hints[:2]


def result_url(result: dict) -> str:
    return result.get("href") or result.get("url") or result.get("link") or ""


def canonical_site_url(raw_url: str) -> str:
    parsed = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
    if not parsed.netloc:
        return ""
    return f"{parsed.scheme or 'https'}://{parsed.netloc}"


def snippet_outbound_websites(
    result: dict, source_url: str, *,
    canonical_site_url_fn: Callable[[str], str] = canonical_site_url,
) -> list[str]:
    """Extract labelled or bare domains from listing/PDF search evidence."""
    snippet = result.get("body", "") or result.get("snippet", "")
    pattern = re.compile(
        r"(?i)(?:web\s*sitesi|web\s*site|website)\s*[:\-–—,]?\s*"
        r"((?:https?://|www\.)[a-z0-9][a-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)"
    )
    bare_pattern = re.compile(
        r"(?i)(?<![@\w.-])((?:https?://|www\.)?[a-z0-9](?:[a-z0-9-]{0,62}\.)+"
        r"(?:com\.tr|net\.tr|org\.tr|biz\.tr|info\.tr|web\.tr|gen\.tr|com|net|org|tr)"
        r"(?:/[a-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?)"
    )
    source_domain = scorer.normalize_domain(source_url)
    websites: list[str] = []
    pdf_source = urlparse(source_url).path.casefold().endswith(".pdf")
    matches = [*pattern.finditer(snippet)]
    if pdf_source:
        matches.extend(bare_pattern.finditer(snippet))
    for match in matches:
        raw_url = match.group(1).rstrip(".,;:)]}\"")
        website = raw_url if raw_url.startswith(("http://", "https://")) else f"https://{raw_url}"
        domain = scorer.normalize_domain(website)
        if (
            not scorer.is_valid_hostname(domain)
            or domain == source_domain
            or scorer.same_registrable_domain(domain, source_domain)
            or scorer.is_excluded_domain(domain)
            or scorer.is_foreign_country_domain(domain)
        ):
            continue
        canonical = canonical_site_url_fn(website)
        if canonical:
            websites.append(canonical)
    return list(dict.fromkeys(value for value in websites if value))


def can_early_stop(company_name: str, candidate: dict, metadata: dict | None = None) -> bool:
    if candidate.get("query") in {"verified_alias", "verified_entity"}:
        return True
    if candidate.get("query") == "source_profile":
        # Fair and directory links are discovery bridges. Continue searching so
        # stale or misassigned profile links are compared with other candidates.
        return False
    if (
        candidate.get("role") != "company_candidate"
        and not scorer.domain_identity_match(company_name, candidate.get("url", ""))[0]
    ):
        return False
    if not candidate.get("_official_query_evidence", 0):
        return False

    brand_tokens = scorer.domain_identity_tokens(company_name)
    exact_brand_domain = bool(brand_tokens) and scorer.compact_domain_core(candidate["domain"]) == "".join(brand_tokens)
    if not exact_brand_domain and candidate.get("_official_query_evidence", 0) < 2:
        return False

    # A single-token brand is especially prone to homonyms (AYSAN food,
    # electrical, plastic, heating...).  When sector metadata exists, do not
    # stop before a result carries that sector evidence.
    if len(brand_tokens) == 1:
        if scorer.metadata_contexts(metadata) and not candidate.get("_metadata_context_matches", 0):
            return False
        if (
            len(scorer.legal_identity_tokens(company_name)) > 1
            and not candidate.get("_legal_name_evidence")
            and not candidate.get("_ownership_evidence")
        ):
            return False
    return True


def discovery_needs_expansion(
    company_name: str, candidates_by_domain: dict[str, dict], metadata: dict | None, *,
    candidate_search_control_key_fn: Callable[[dict], tuple[int, ...]] = candidate_search_control_key,
    can_early_stop_fn: Callable[[str, dict, dict | None], bool] = can_early_stop,
) -> bool:
    """Return true when pre-crawl evidence is weak, ambiguous or bridge-led."""
    ranked = sorted(
        (
            item for item in candidates_by_domain.values()
            if item.get("role") not in DISCOVERY_ONLY_ROLES
        ),
        key=candidate_search_control_key_fn,
        reverse=True,
    )
    if not ranked:
        return True
    best = ranked[0]
    if best.get("score", 0) < config.EARLY_STOP_SCORE_THRESHOLD:
        return True
    if not can_early_stop_fn(company_name, best, metadata):
        return True
    if len(ranked) > 1:
        second = ranked[1]
        if (
            best.get("domain") != second.get("domain")
            and best.get("score", 0) - second.get("score", 0) <= config.AMBIGUOUS_CANDIDATE_MARGIN
        ):
            return True
    return False


def bridge_entity_anchor_supported(company_name: str, title: str, source_url: str) -> bool:
    """Require the search result to point at the target's own profile."""
    title_text = scorer.normalize_text(title)
    parsed = urlparse(source_url)
    url_text = scorer.normalize_text(f"{unquote(parsed.path)} {unquote(parsed.query)}")
    if scorer.legal_name_phrase_match(company_name, title):
        return True
    brand_tokens = scorer.primary_brand_tokens(company_name, limit=2)
    if not brand_tokens:
        return False
    title_words = set(title_text.split())
    url_words = set(url_text.split())
    if len(brand_tokens) >= 2:
        return all(token in title_words for token in brand_tokens) or all(
            token in url_words for token in brand_tokens
        )
    token = brand_tokens[0]
    return len(token) >= 5 and (token in title_words or token in url_words)


def bridge_identity_supported(
    company_name: str, title: str, snippet: str, metadata: dict | None, source_url: str = "", *,
    bridge_entity_anchor_supported_fn: Callable[[str, str, str], bool] = bridge_entity_anchor_supported,
) -> bool:
    """Require the bridge result itself to identify the requested company."""
    evidence_text = f"{title} {snippet}"
    if scorer.ownership_statement_match(company_name, evidence_text):
        return True
    if not bridge_entity_anchor_supported_fn(company_name, title, source_url):
        return False
    if scorer.legal_name_phrase_match(company_name, evidence_text):
        return True
    brand_tokens = scorer.primary_brand_tokens(company_name, limit=2)
    normalized = f" {scorer.normalize_text(evidence_text)} "
    brand_hits = sum(1 for token in brand_tokens if f" {token} " in normalized)
    if len(brand_tokens) >= 2:
        return brand_hits == len(brand_tokens)
    if not brand_tokens or len(brand_tokens[0]) < 5 or not brand_hits:
        return False
    contexts = scorer.metadata_contexts(metadata)
    return not contexts or any(
        scorer.page_matches_metadata_context(evidence_text, context) for context in contexts
    )


def best_candidate(
    candidates_by_domain: dict[str, dict], *,
    candidate_rank_key_fn: Callable[[dict], tuple[int, ...]] = candidate_rank_key,
) -> dict | None:
    return max(
        (item for item in candidates_by_domain.values() if item.get("role") not in DISCOVERY_ONLY_ROLES),
        key=candidate_rank_key_fn,
        default=None,
    )


ROLE_PRIORITY = {
    "company_candidate": 0, "unknown": 0, "news": 1, "marketplace": 2,
    "directory": 3, "shared_listing": 4, "fair_profile": 5, "public_body": 6,
}
_ROLE_PRIORITY = ROLE_PRIORITY


def strongest_candidate_role(*roles: str, role_priority: dict[str, int] = ROLE_PRIORITY) -> str:
    return max((role for role in roles if role), key=lambda role: role_priority.get(role, 0), default="unknown")


def collect_search_bridge_sources(
    target: dict[str, dict], company_name: str, query: str, results: list[dict], metadata: dict | None, *,
    result_url_fn: Callable[[dict], str] = result_url,
    candidate_role_fn: Callable[[str, str, str, str], str] = candidate_role,
    bridge_identity_supported_fn: Callable[[str, str, str, dict | None, str], bool] = bridge_identity_supported,
) -> None:
    blocked = {
        scorer.normalize_domain(domain) for domain in config.PROFILE_BRIDGE_BLOCKED_DOMAINS
    }
    for rank, result in enumerate(results, start=1):
        url = result_url_fn(result)
        domain = scorer.normalize_domain(url)
        if not domain or any(domain == item or domain.endswith(f".{item}") for item in blocked):
            continue
        title = result.get("title", "")
        snippet = result.get("body", "") or result.get("snippet", "")
        role = candidate_role_fn(company_name, url, title, snippet)
        path = scorer.normalize_text(unquote(urlparse(url).path.replace("/", " ")))
        profile_shaped = any(marker in path.split() for marker in (
            "company", "firma", "profile", "supplier", "exhibitor", "katilimci", "member", "detail",
        ))
        if role not in {"directory", "fair_profile", "shared_listing", "marketplace"} and not (
            scorer.is_excluded_domain(domain) and profile_shaped
        ):
            continue
        if not bridge_identity_supported_fn(company_name, title, snippet, metadata, url):
            continue
        current = target.get(url)
        record = {
            "url": url, "domain": domain, "query": query, "rank": rank,
            "title": title, "snippet": snippet, "role": role,
        }
        if current is None or rank < current.get("rank", 999):
            target[url] = record
