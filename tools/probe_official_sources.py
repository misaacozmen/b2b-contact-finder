from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


ALLOWED_HOSTS = {
    "hometex.com.tr",
    "www.hometex.com.tr",
    "ambiente.messefrankfurt.com",
    "exhibitorsearch.messefrankfurt.com",
    "api.messefrankfurt.com",
}


def get_bytes(url: str, *, reject_replacement: bool = True) -> bytes:
    host = (urlparse(url).hostname or "").casefold()
    if host not in ALLOWED_HOSTS:
        raise RuntimeError(f"host not allowlisted: {host}")
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    body = response.content
    body.decode("utf-8", "strict")
    if reject_replacement and b"\xef\xbf\xbd" in body:
        raise RuntimeError(f"replacement character in response: {url}")
    return body


def main() -> None:
    hometex_url = "https://hometex.com.tr/en/2026-exhibitor-list"
    hometex = BeautifulSoup(get_bytes(hometex_url).decode("utf-8"), "html.parser")
    hrefs = []
    for link in hometex.select('div.list ul li a[href^="/en/"]'):
        href = link.get("href", "").strip()
        if href in {"/en/exhibitor", "/en/exhibitor-profile", "/en/2026-exhibitor-list"}:
            continue
        hrefs.append(href.split("?", 1)[0].rstrip("/"))
    slugs = sorted({href.rsplit("/", 1)[-1] for href in hrefs if href.count("/") >= 2})

    ambiente_url = "https://ambiente.messefrankfurt.com/frankfurt/en/exhibitor-search.html"
    ambiente = BeautifulSoup(get_bytes(ambiente_url).decode("utf-8"), "html.parser")
    scripts = []
    for script in ambiente.find_all("script", src=True):
        url = urljoin(ambiente_url, script["src"])
        if (urlparse(url).hostname or "").casefold() in ALLOWED_HOSTS:
            scripts.append(url)
    main_scripts = [url for url in scripts if any(token in url.casefold() for token in ("main", "app"))]
    summaries = []
    api_probe: dict[str, object] = {}
    for url in main_scripts:
        body = get_bytes(url, reject_replacement=False).decode("utf-8")
        marker_matches = list(re.finditer(r"(?i)(?:api[-_]?key|x[-_]?api[-_]?key|apikey)", body))
        masked_contexts = []
        for match in marker_matches:
            fragment = body[max(0, match.start() - 120): match.end() + 180]
            fragment = re.sub(r'"(?:\\.|[^"\\])*"', '"<str>"', fragment)
            fragment = re.sub(r"'(?:\\.|[^'\\])*'", "'<str>'", fragment)
            masked_contexts.append(fragment)
        key_matches = re.findall(r'(?i)externalDataUrlApiKey="([^"]+)"', body)
        production: list[tuple[str, str]] = []
        for key_match in re.finditer(r'externalDataUrlApiKey="([^"]+)"', body):
            preceding = body[max(0, key_match.start() - 2000):key_match.start()]
            urls = re.findall(r'externalDataUrl="(https://api[.]messefrankfurt[.]com[^"]+)"', preceding)
            if urls:
                production.append((urls[-1], key_match.group(1)))
        summaries.append({
            "url": url,
            "bytes": len(body.encode("utf-8")),
            "key_marker_count": len(marker_matches),
            "search_endpoint_present": "/exhibitor-service/api/2.1/public/exhibitor/search" in body,
            "external_key_assignment_count": len(key_matches),
            "production_key_match_count": len(production),
        })
        if production:
            filters_url = "https://api.messefrankfurt.com/service/esb_api/exhibitor-service/api/2.1/public/exhibitor/searchfilters/en-GB/AMBIENTE"
            headers = {"Apikey": production[0][1]}
            filters_response = requests.get(filters_url, headers=headers, timeout=20)
            api_probe["filters_status"] = filters_response.status_code
            api_probe["filters_bytes"] = len(filters_response.content)
            search_url = "https://api.messefrankfurt.com/service/esb_api/exhibitor-service/api/2.1/public/exhibitor/search"
            categories = [
                "mf_ppx_import_9789309", "mf_ppx_import_9789424", "mf_ppx_import_9789553",
                "mf_ppx_import_9789677", "mf_ppx_import_9789939", "mf_ppx_import_9790106",
                "mf_ppx_import_9790189", "mf_ppx_import_9790250",
            ]
            params: list[tuple[str, str | int | bool]] = [
                ("country", "TUR"), ("language", "en-GB"), ("q", ""), ("orderBy", "name"),
                ("pageNumber", 1), ("pageSize", 100), ("orSearchFallback", "true"),
                ("showJumpLabels", "true"), ("findEventVariable", "AMBIENTE"),
            ] + [("categoryId", category) for category in categories]
            search_response = requests.get(search_url, params=params, headers=headers, timeout=20)
            api_probe["search_status"] = search_response.status_code
            api_probe["search_bytes"] = len(search_response.content)
            if search_response.ok:
                search_json = json.loads(search_response.content.decode("utf-8", "strict"))
                api_probe["hits_total"] = search_json.get("metaData", {}).get("hitsTotal")
                api_probe["top_level_keys"] = sorted(search_json.keys())
                result = search_json.get("result")
                api_probe["success"] = search_json.get("success")
                api_probe["result_type"] = type(result).__name__
                api_probe["result_keys"] = sorted(result.keys()) if isinstance(result, dict) else None
                if isinstance(result, dict):
                    api_probe["result_meta_keys"] = sorted(result.get("metaData", {}).keys()) if isinstance(result.get("metaData"), dict) else None
                    api_probe["result_hits_total"] = result.get("metaData", {}).get("hitsTotal") if isinstance(result.get("metaData"), dict) else None
                    api_probe["result_hit_count"] = len(result.get("hits", [])) if isinstance(result.get("hits"), list) else None
                    if result.get("hits"):
                        first_hit = result["hits"][0]
                        api_probe["first_hit_keys"] = sorted(first_hit.keys()) if isinstance(first_hit, dict) else None
                        first_exhibitor = first_hit.get("exhibitor") if isinstance(first_hit, dict) else None
                        api_probe["first_exhibitor_keys"] = sorted(first_exhibitor.keys()) if isinstance(first_exhibitor, dict) else None
                        if isinstance(first_exhibitor, dict):
                            api_probe["first_exhibitor_scalar_keys"] = sorted(
                                key for key, value in first_exhibitor.items() if isinstance(value, (str, int, float, bool, type(None)))
                            )

    print({"hometex_unique_slugs": len(slugs), "ambiente_script_urls": scripts, "ambiente_main_scripts": summaries, "api_probe": api_probe})


if __name__ == "__main__":
    main()
