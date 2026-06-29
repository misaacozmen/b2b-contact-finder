import logging
from urllib.parse import urlparse

from ddgs import DDGS
from ddgs.exceptions import DDGSException

import config
from modules import scorer


LOGGER = logging.getLogger("contact_finder")
PREFERRED_BACKENDS = ["duckduckgo", "google", "brave", "yahoo", "yandex"]
FALLBACK_BACKENDS = ["mojeek", "grokipedia"]


class SearchBackendError(RuntimeError):
    pass


def _result_url(result: dict) -> str:
    return result.get("href") or result.get("url") or result.get("link") or ""


def _canonical_site_url(raw_url: str) -> str:
    parsed = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
    if not parsed.netloc:
        return ""
    return f"{parsed.scheme or 'https'}://{parsed.netloc}"


def _ddgs_text(query: str) -> list[dict]:
    had_non_error_response = False
    last_hard_error: Exception | None = None

    for backend in PREFERRED_BACKENDS + FALLBACK_BACKENDS:
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=config.SEARCH_RESULTS_PER_QUERY, backend=backend))
            had_non_error_response = True
            if results:
                return results
            LOGGER.debug("DDGS backend '%s' returned 0 results for '%s'", backend, query)
        except DDGSException as exc:
            message = str(exc).lower()
            if "no results" in message:
                had_non_error_response = True
                LOGGER.debug("DDGS backend '%s' no results for '%s'", backend, query)
                continue
            LOGGER.debug("DDGS backend '%s' error for '%s': %s", backend, query, exc)
            last_hard_error = exc
        except Exception as exc:
            LOGGER.debug("DDGS backend '%s' failed for '%s': %s", backend, query, exc)
            last_hard_error = exc

    if last_hard_error and not had_non_error_response:
        raise SearchBackendError(f"All DDGS backends failed for '{query}': {last_hard_error}")
    return []


def find_candidate_domains(company_name: str) -> list[dict]:
    candidates_by_domain: dict[str, dict] = {}
    query_inputs = scorer.search_name_variants(company_name)
    queries: list[str] = []
    seen_queries = set()
    for query_input in query_inputs:
        for template in config.SEARCH_QUERY_TEMPLATES:
            query = template.format(company=query_input)
            if query not in seen_queries:
                queries.append(query)
                seen_queries.add(query)
        for country in config.TARGET_COUNTRY_QUERY_TERMS:
            for template in config.SEARCH_COUNTRY_QUERY_TEMPLATES:
                query = template.format(company=query_input, country=country)
                if query not in seen_queries:
                    queries.append(query)
                    seen_queries.add(query)

    for query in queries:
        results = _ddgs_text(query)

        for rank, result in enumerate(results, start=1):
            url = _result_url(result)
            domain = scorer.normalize_domain(url)
            if not domain or scorer.is_excluded_domain(domain):
                continue

            title = result.get("title", "")
            snippet = result.get("body", "") or result.get("snippet", "")
            score_details = scorer.score_domain_details(company_name, url, title=title, snippet=snippet)
            rank_bonus = max(config.RESULT_RANK_BONUS_MAX - (rank - 1) * 2, 0)
            score = min(config.PRE_CRAWL_SCORE_CAP, score_details["score"] + rank_bonus)
            if score <= 0:
                continue

            existing = candidates_by_domain.get(domain)
            candidate = {
                "domain": domain,
                "url": _canonical_site_url(url),
                "score": score,
                "title": title,
                "snippet": snippet,
                "query": query,
                "rank": rank,
                "reason": f"{score_details['reason']}; rank_bonus:{rank_bonus}; rank:{rank}",
            }
            if existing is None or score > existing["score"]:
                candidates_by_domain[domain] = candidate

        best = max(candidates_by_domain.values(), key=lambda item: item["score"], default=None)
        if best and best["score"] >= config.EARLY_STOP_SCORE_THRESHOLD:
            break

    return sorted(candidates_by_domain.values(), key=lambda item: item["score"], reverse=True)
