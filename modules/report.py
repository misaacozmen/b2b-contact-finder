from datetime import timedelta
from statistics import mean
from modules import discovery_coverage, runtime


OK_STATUSES = {"OK_HIGH_CONFIDENCE", "OK_MEDIUM_CONFIDENCE"}


def failed_rows(rows: list[dict]) -> list[dict]:
    failed: list[dict] = []
    for row in rows:
        if row.get("status") in OK_STATUSES:
            continue
        failed.append(
            {
                "company": row.get("company", ""),
                "status": row.get("status", ""),
                "reason": row.get("reason", ""),
            }
        )
    return failed


def _pct(count: int, total: int) -> str:
    return f"{(count / total * 100):.1f}%" if total else "0.0%"


def build_report(rows: list[dict], elapsed_seconds: float) -> str:
    total = len(rows)
    website_count = sum(1 for row in rows if row.get("website"))
    email_count = sum(1 for row in rows if row.get("email"))
    verified_email_count = sum(1 for row in rows if row.get("email_verification") == "verified")
    phone_count = sum(1 for row in rows if row.get("phone"))
    complete_count = sum(1 for row in rows if row.get("website") and row.get("email") and row.get("phone"))
    verified_rows = [row for row in rows if row.get("status") in OK_STATUSES]
    verified_website_count = sum(1 for row in verified_rows if row.get("website"))
    verified_complete_count = sum(
        1 for row in verified_rows if row.get("website") and row.get("email") and row.get("phone")
    )
    high_confidence_count = sum(1 for row in rows if row.get("status") == "OK_HIGH_CONFIDENCE")
    medium_confidence_count = sum(1 for row in rows if row.get("status") == "OK_MEDIUM_CONFIDENCE")
    review_count = sum(1 for row in rows if row.get("status") in {"REVIEW_NEEDED", "WEBSITE_AMBIGUOUS"})
    ambiguous_count = sum(1 for row in rows if row.get("status") == "WEBSITE_AMBIGUOUS")
    scores = [int(row.get("score") or 0) for row in rows]
    average_score = mean(scores) if scores else 0
    elapsed = str(timedelta(seconds=int(elapsed_seconds)))
    counters = runtime.snapshot().get("counters", {})
    brightdata_requests = int(counters.get("api.brightdata.requests", 0))
    places_requests = int(counters.get("api.google_places.requests", 0))
    crawler_requests = int(counters.get("http.crawler.requests", 0))
    candidate_count = int(counters.get("pipeline.candidates_discovered", 0))
    identity_evaluations = int(counters.get("pipeline.identity_candidates_evaluated", 0))
    full_evaluations = int(counters.get("pipeline.full_candidates_evaluated", 0))
    source_5xx = int(counters.get("source_profile.http_5xx", 0))
    source_skips = int(counters.get("source_profile.circuit_skips", 0))
    static_attempts = int(counters.get("recovery.static_attempts", 0))
    static_successes = int(counters.get("recovery.static_successes", 0))
    browser_attempts = int(counters.get("recovery.browser_attempts", 0))
    browser_successes = int(counters.get("recovery.browser_successes", 0))
    pdf_attempts = int(counters.get("recovery.pdf_attempts", 0))
    pdf_successes = int(counters.get("recovery.pdf_text_successes", 0))
    snapshot_loaded = int(counters.get("snapshot.entries_loaded", 0))
    snapshot_hits = sum(
        int(value)
        for key, value in counters.items()
        if key.startswith("snapshot.") and key.endswith(".hit")
    )
    stale_hits = sum(
        int(value)
        for key, value in counters.items()
        if key.startswith("cache.") and key.endswith(".stale_hit")
    )
    email_field_allowed = int(counters.get("contact_policy.email.allowed", 0))
    email_field_suppressed = int(counters.get("contact_policy.email.suppressed", 0))
    phone_field_allowed = int(counters.get("contact_policy.phone.allowed", 0))
    phone_field_suppressed = int(counters.get("contact_policy.phone.suppressed", 0))
    coverage = discovery_coverage.payload()
    policy_downgrades = sum(
        1 for row in rows
        if row.get("publication_policy_action") == "downgrade_to_review"
    )
    low_risk_publications = sum(
        1 for row in verified_rows
        if row.get("publication_risk_tier") == "low"
    )
    controlled_risk_publications = sum(
        1 for row in verified_rows
        if row.get("publication_risk_tier") == "controlled"
    )

    return "\n".join(
        [
            "================================",
            "B2B Contact Finder - Sonuc Raporu",
            "================================",
            f"Toplam firma: {total}",
            f"Website bulundu: {website_count} ({_pct(website_count, total)})",
            f"Otomatik kullanima uygun dogrulanmis website: {verified_website_count} ({_pct(verified_website_count, total)})",
            f"E-posta bulundu: {email_count} ({_pct(email_count, total)})",
            f"MX/A kaydi bulunan e-posta domaini: {verified_email_count} ({_pct(verified_email_count, total)})",
            f"Telefon bulundu: {phone_count} ({_pct(phone_count, total)})",
            f"Tam iletisim bilgisi bulunan firma (website+email+phone): {complete_count} ({_pct(complete_count, total)})",
            f"Otomatik kullanima uygun dogrulanmis firma: {len(verified_rows)} ({_pct(len(verified_rows), total)})",
            f"Dogrulanmis tam iletisim: {verified_complete_count} ({_pct(verified_complete_count, total)})",
            f"Yuksek guvenli OK: {high_confidence_count} ({_pct(high_confidence_count, total)})",
            f"Orta guvenli OK: {medium_confidence_count} ({_pct(medium_confidence_count, total)})",
            f"Manuel kontrol gereken: {review_count} ({_pct(review_count, total)})",
            f"Website adayi belirsiz: {ambiguous_count} ({_pct(ambiguous_count, total)})",
            f"Yayinlanmayan/geri cekilen sonuc: {total - len(verified_rows)} ({_pct(total - len(verified_rows), total)})",
            f"P3 politika dusurmesi: {policy_downgrades}",
            f"P3 dusuk/kontrollu riskli yayin: {low_risk_publications}/{controlled_risk_publications}",
            "--------------------------------",
            f"Ortalama skor: {average_score:.1f}",
            f"Kesfedilen aday/firma: {(candidate_count / total):.1f}" if total else "Kesfedilen aday/firma: 0.0",
            f"Hafif kimlik taramasi/firma: {(identity_evaluations / total):.1f}" if total else "Hafif kimlik taramasi/firma: 0.0",
            f"Tam iletisim taramasi/firma: {(full_evaluations / total):.1f}" if total else "Tam iletisim taramasi/firma: 0.0",
            f"Crawler HTTP istegi/firma: {(crawler_requests / total):.1f}" if total else "Crawler HTTP istegi/firma: 0.0",
            f"Fuar profil 5xx: {source_5xx}; devre kesici atlamasi: {source_skips}",
            f"P4 statik kurtarma: {static_successes}/{static_attempts}",
            f"P4 browser kurtarma: {browser_successes}/{browser_attempts}",
            f"P4 PDF metin kurtarma: {pdf_successes}/{pdf_attempts}",
            f"P4 replay snapshot: yuklenen={snapshot_loaded}; isabet={snapshot_hits}; eski-cache-isabeti={stale_hits}",
            f"P5 e-posta alan karari: izin={email_field_allowed}; baskilanan={email_field_suppressed}",
            f"P5 telefon alan karari: izin={phone_field_allowed}; baskilanan={phone_field_suppressed}",
            (
                "P6 discovery kapsami: "
                f"cozulen={coverage['resolved_companies']}; "
                f"acik={coverage['unresolved_companies']}; "
                f"replay-eksigi={coverage['replay_miss_count']}; "
                f"edinim-plani={len(coverage['acquisition_plan'])}"
            ),
            f"Bright Data API istekleri: {brightdata_requests}",
            f"Google Places API istekleri: {places_requests}",
            f"Islem suresi: {elapsed}",
            "================================",
        ]
    )
