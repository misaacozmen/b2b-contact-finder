"""Export the current ANKIROS exhibitor list into the project's input workbook."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import re
import shutil
import time
import unicodedata
from collections import Counter

import requests
from openpyxl import Workbook, load_workbook


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "input" / "firms.xlsx"
BACKUP = ROOT / "input" / "firms_before_ankiros.xlsx"
EXPO_ID = "a069ef48-11a1-405b-5070-08de2b499b9f"
API = "https://web.ankirosforms.com/api/v1.0/participant/"
LIST_URL = "https://www.ankiros.com/tr/exhibitor-list"
HEADERS = {"Origin": "https://www.ankiros.com", "Referer": LIST_URL}


def post(endpoint, payload):
    for attempt in range(3):
        try:
            response = requests.post(API + endpoint, json=payload, headers=HEADERS, timeout=30)
            response.raise_for_status()
            data = response.json()
            if data.get("hasError") or not isinstance(data.get("result"), (list, dict)):
                raise ValueError(f"API error: {data.get('message')}")
            return data["result"]
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def comparable(name):
    normalized = unicodedata.normalize("NFKC", name or "").casefold()
    return " ".join(re.findall(r"[^\W_]+", normalized, re.UNICODE))


def normalize_phone(value):
    raw = str(value or "").strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+90") and digits.startswith("90"):
        return "0" + digits[2:]
    return digits


def main():
    listing = post("getParticipantListByCountry", {"expoId": EXPO_ID, "country": "TÜRKİYE"})
    if not listing:
        raise RuntimeError("Exhibitor list is empty")
    country_slug_counts = Counter(item["slug"] for item in listing)
    unique_listing = []
    seen_slugs = set()
    seen_names = set()
    for item in listing:
        slug = item["slug"]
        name_key = comparable(item["companyName"])
        if slug in seen_slugs or name_key in seen_names:
            continue
        unique_listing.append(item)
        seen_slugs.add(slug)
        seen_names.add(name_key)
    listing = unique_listing
    all_listing = post("getAllParticipantList", {"expoId": EXPO_ID})
    slug_counts = Counter(item["slug"] for item in all_listing)
    slugs = sorted({item["slug"] for item in listing})
    details = {}
    failures = {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {
            pool.submit(post, "getCompanyDetailBySlug", {"expoId": EXPO_ID, "slug": slug}): slug
            for slug in slugs
        }
        for future in as_completed(futures):
            slug = futures[future]
            try:
                details[slug] = future.result()
            except Exception as exc:
                failures[slug] = str(exc)
    if failures:
        raise RuntimeError(f"Failed to fetch {len(failures)} profiles; workbook untouched")

    headers = [
        "company", "website", "listed_website", "listed_legal_name", "country",
        "source", "source_record_id", "listing_url", "listed_email", "listed_phone",
        "hall", "stand", "sector", "profile_url", "contact_match_status", "profile_company_name",
    ]
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "ANKIROS exhibitors"
    sheet.append(headers)
    matched = 0
    prefix_matched = 0
    mismatched = 0
    for item in listing:
        slug = item["slug"]
        name = item["companyName"].strip()
        detail = details[slug]
        listed_key = comparable(name)
        profile_key = comparable(detail.get("companyName"))
        exact_match = listed_key == profile_key
        prefix_match = (
            not exact_match and (slug_counts[slug] == 1 or country_slug_counts[slug] > 1)
            and len(profile_key) >= 6
            and listed_key.startswith(profile_key + " ")
        )
        is_match = exact_match or prefix_match
        if is_match:
            matched += 1
            if prefix_match:
                prefix_matched += 1
        else:
            mismatched += 1
        stand = str(detail.get("standNo") or "").strip() if is_match else ""
        hall = stand.split("-", 1)[0] if stand else ""
        profile_url = f"https://www.ankiros.com/tr/exhibitor/{slug}"
        sheet.append([
            name, "", "", name, "Türkiye", "ANKIROS katılımcı listesi", f"ankiros:{slug}",
            LIST_URL, (detail.get("email") or "").strip() if is_match else "",
            normalize_phone(detail.get("phone")) if is_match else "",
            hall, stand, "", profile_url,
            "matched_profile_prefix" if prefix_match else ("matched" if is_match else "profile_name_mismatch"),
            detail.get("companyName") or "",
        ])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for col, width in {"A": 58, "F": 25, "H": 48, "I": 34, "J": 22, "N": 65}.items():
        sheet.column_dimensions[col].width = width

    if INPUT.exists():
        if not BACKUP.exists():
            shutil.copy2(INPUT, BACKUP)
    temporary = INPUT.with_suffix(".tmp.xlsx")
    workbook.save(temporary)
    temporary.replace(INPUT)
    print(f"rows={len(listing)} matched={matched} prefix_matched={prefix_matched} mismatched={mismatched} backup={BACKUP}")


if __name__ == "__main__":
    main()
