"""Conservative business-entity type comparison from first-party text."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from modules import scorer


TAXONOMY = {
    "metal_industry": (
        "celik", "steel", "metal", "demir", "dokum", "foundry", "hadde",
        "profil", "sac", "paslanmaz", "ferro", "metalurji", "metallurgy",
    ),
    "machinery": (
        "makine", "machinery", "machine", "otomasyon", "automation",
        "robotik", "robotics", "endustriyel ekipman",
    ),
    "finance": (
        "banka", "bank", "banking", "finans", "finance", "kredi",
        "yatirim hizmetleri", "investment services",
    ),
    "hospitality_travel": (
        "otel", "hotel", "resort", "konaklama", "hospitality", "turizm",
        "travel agency", "seyahat acentesi",
    ),
    "education": (
        "universite", "university", "okul", "school", "akademi", "academy",
        "egitim kurumu",
    ),
    "government": (
        "belediye", "bakanlik", "ministry", "municipality", "kamu kurumu",
        "valilik",
    ),
    "media": (
        "gazete", "newspaper", "haber", "news media", "televizyon", "radio",
        "yayincilik",
    ),
    "retail_marketplace": (
        "marketplace", "pazaryeri", "online magaza", "e ticaret",
        "perakende", "retail store",
    ),
    "construction": (
        "insaat", "construction", "mimarlik", "architecture", "muteahhit",
        "yapi malzemeleri",
    ),
    "chemicals": (
        "kimya", "chemical", "chemicals", "boya", "paint", "polimer",
        "polymer",
    ),
    "logistics": (
        "lojistik", "logistics", "nakliye", "transportation", "freight",
        "depolama",
    ),
}

INCOMPATIBLE = {
    frozenset(("metal_industry", "finance")),
    frozenset(("metal_industry", "hospitality_travel")),
    frozenset(("metal_industry", "education")),
    frozenset(("metal_industry", "government")),
    frozenset(("metal_industry", "media")),
    frozenset(("machinery", "finance")),
    frozenset(("machinery", "hospitality_travel")),
    frozenset(("machinery", "education")),
    frozenset(("machinery", "government")),
}


def _normalized_visible_text(pages: list[dict]) -> str:
    values: list[str] = []
    for page in pages[:8]:
        html = str(page.get("html", ""))
        values.append(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
    return scorer.normalize_text(" ".join(values))


def classify(text: str) -> dict[str, int]:
    normalized = scorer.normalize_text(text)
    result: dict[str, int] = {}
    for kind, markers in TAXONOMY.items():
        hits = {
            marker for marker in markers
            if re.search(
                rf"(?<![a-z0-9]){re.escape(scorer.normalize_text(marker))}(?![a-z0-9])",
                normalized,
            )
        }
        if hits:
            result[kind] = len(hits)
    return result


def assess(company: str, metadata: dict | None, pages: list[dict]) -> dict:
    """Return support/conflict only when both classifications are explicit.

    Fair metadata may describe the requested target, but never proves the
    candidate.  Candidate type comes exclusively from first-party page text.
    """
    metadata = metadata or {}
    target_text = " ".join((
        company,
        str(metadata.get("sector", "")),
        str(metadata.get("description", "")),
        str(metadata.get("product_group", "")),
    ))
    target_scores = classify(target_text)
    site_scores = classify(_normalized_visible_text(pages))
    target_strong = {kind for kind, score in target_scores.items() if score >= 1}
    site_strong = {kind for kind, score in site_scores.items() if score >= 2}
    matches = sorted(target_strong & site_strong)
    # A group/corporate site may explicitly cover several sectors.  A matching
    # target activity outranks unrelated words elsewhere on the same site;
    # conflict is safe only when the strong sets have no overlap at all.
    conflicts = [] if matches else sorted(
        f"{left}:{right}"
        for left in target_strong
        for right in site_strong
        if frozenset((left, right)) in INCOMPATIBLE
    )
    return {
        "target_types": sorted(target_strong),
        "site_types": sorted(site_strong),
        "matches": matches,
        "conflicts": conflicts,
        "decision": (
            "conflict" if conflicts
            else "match" if matches
            else "unknown"
        ),
    }
