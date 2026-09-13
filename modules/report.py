import json
from statistics import mean
from modules import checkpoint, discovery_coverage, runtime
from modules.publication_policy import OK_STATUSES, is_publishable_row


def failed_rows(rows: list[dict]) -> list[dict]:
    failed: list[dict] = []
    for row in rows:
        if is_publishable_row(row):
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


def build_report(rows: list[dict], elapsed_seconds: float | None, *, runtime_snapshot: dict | None = None) -> str:
    if runtime_snapshot is None:
        runtime_snapshot = runtime.snapshot()
    total = len(rows)
    website_count = sum(1 for row in rows if row.get("website"))
    email_count = sum(1 for row in rows if row.get("email"))
    verified_email_count = sum(1 for row in rows if row.get("email_verification") == "verified")
    phone_count = sum(1 for row in rows if row.get("phone"))
    complete_count = sum(1 for row in rows if row.get("website") and row.get("email") and row.get("phone"))
    verified_rows = [row for row in rows if is_publishable_row(row)]
    publication_eligible_count = sum(
        1 for row in rows if is_publishable_row(row)
    )
    complete_held_count = sum(
        1 for row in rows
        if row.get("website") and row.get("email") and row.get("phone")
        and not is_publishable_row(row)
    )
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
    # Wall-clock time is execution-specific and would make live/replay
    # artifacts differ despite identical evidence.  Runtime timing remains in
    # logs; the published report is deliberately deterministic.
    elapsed = "deterministic"
    counters = (runtime_snapshot or {}).get("counters", {})
    brightdata_requests = int(counters.get("api.brightdata.requests", 0))
    linkedin_company_requests = int(counters.get("api.linkedin_company.requests", 0))
    linkedin_company_blocked = int(counters.get("api.linkedin_company.budget_blocked", 0))
    linkedin_company_matches = int(counters.get("api.linkedin_company.matches", 0))
    llm_arbiter_requests = int(counters.get("api.llm_arbiter.requests", 0))
    llm_arbiter_blocked = int(counters.get("api.llm_arbiter.budget_blocked", 0))
    llm_arbiter_tokens = int(counters.get("api.llm_arbiter.total_tokens", 0))
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
    identity_unproved_count = sum(
        1 for row in rows
        if row.get("publication_blockers") == "no_candidate_proved_target_fingerprint"
        or row.get("reason") == "no_candidate_proved_target_fingerprint"
    )
    fetch_failed_count = sum(
        1 for row in rows if row.get("status") == "WEBSITE_FETCH_FAILED"
    )
    not_found_count = sum(
        1 for row in rows if row.get("status") == "WEBSITE_NOT_FOUND"
    )
    brightdata_queries = int(counters.get("api.brightdata.queries", 0))
    brightdata_retries = int(counters.get("api.brightdata.retries", 0))
    brightdata_cooldowns = int(counters.get("api.brightdata.cooldown_retries", 0))
    brightdata_blocked = int(counters.get("api.brightdata.budget_blocked", 0))
    search_provider_failures = int(counters.get("search.provider_failures", 0))
    crawler_blocked = int(counters.get("http.crawler.budget_blocked", 0))
    static_skips = int(counters.get("recovery.static_skips", 0))
    host_variant_attempts = int(counters.get("recovery.host_variant_attempts", 0))
    host_variant_successes = int(counters.get("recovery.host_variant_successes", 0))
    durable_scheduler = (
        runtime_snapshot if (runtime_snapshot or {}).get("receipt_schema_version")
        else (runtime_snapshot or {}).get("durable_scheduler")
    )
    paid_query_limit = (
        int(durable_scheduler["paid_query_limit_per_company"])
        if isinstance(durable_scheduler, dict) and "paid_query_limit_per_company" in durable_scheduler
        else "UNAVAILABLE"
    )
    unique_counts = (runtime_snapshot or {}).get("unique_counts", {})
    browser_recovered_companies = int(unique_counts.get("recovery.browser_recovered_companies", 0))
    browser_publication_companies = int(unique_counts.get("recovery.browser_publication_companies", 0))
    interstitial_live_pages_rejected = int(counters.get("live.site.security_interstitial_rejected", 0))
    interstitial_cache_pages_rejected = int(counters.get("cache.site.security_interstitial_rejected", 0))
    interstitial_hosts = int(unique_counts.get("recovery.security_interstitial_hosts", 0))
    browser_root_attempts = int(counters.get("recovery.browser.root.attempts", 0))
    browser_root_successes = int(counters.get("recovery.browser.root.successes", 0))
    browser_root_errors = int(counters.get("recovery.browser.root.errors", 0))
    browser_identity_attempts = int(counters.get("recovery.browser.identity.attempts", 0))
    browser_identity_successes = int(counters.get("recovery.browser.identity.successes", 0))
    browser_identity_errors = int(counters.get("recovery.browser.identity.errors", 0))
    browser_contact_attempts = int(counters.get("recovery.browser.contact.attempts", 0))
    browser_contact_successes = int(counters.get("recovery.browser.contact.successes", 0))
    browser_contact_errors = int(counters.get("recovery.browser.contact.errors", 0))
    durable_scheduler = (
        runtime_snapshot if (runtime_snapshot or {}).get("receipt_schema_version")
        else (runtime_snapshot or {}).get("durable_scheduler", {})
    )
    provider_budgets = durable_scheduler.get("provider_budgets", {})
    if not provider_budgets:
        provider_budgets = (runtime_snapshot or {}).get("provider_budgets", {})
    durable_claim = bool((runtime_snapshot or {}).get("receipt_schema_version")) or "durable_scheduler" in (runtime_snapshot or {})
    if durable_claim and set(provider_budgets) != checkpoint.CANONICAL_PROVIDERS:
        raise checkpoint.EvidenceInvariant("durable provider telemetry set is missing or extra")
    budget_lines = []
    required_provider_fields = {
        "population_count", "ratio", "explicit_cap", "effective_limit", "reserved_total",
        "done", "failed", "unknown", "reserved", "running", "physical_http_attempts",
        "retry_attempts", "inherited_uses", "budget_blocked_items",
    }
    for provider, details in sorted(provider_budgets.items()):
        missing = required_provider_fields.difference(details)
        if missing:
            raise checkpoint.EvidenceInvariant(f"durable provider telemetry fields missing for {provider}: {sorted(missing)}")
        budget_lines.append(
            f"Butce {provider}: population={details.get('population_count', 0)}; "
            f"ratio={details['ratio']}; explicit_cap={details['explicit_cap']}; "
            f"effective_limit={details['effective_limit']}; reserved_total={details['reserved_total']}; "
            f"done={details['done']}; failed={details['failed']}; unknown={details['unknown']}; "
            f"reserved={details['reserved']}; running={details['running']}; "
            f"physical_http_attempts={details['physical_http_attempts']}; retry_attempts={details['retry_attempts']}; "
            f"inherited_uses={details['inherited_uses']}; budget_blocked_items={details['budget_blocked_items']}"
        )

    scheduler_receipt = json.dumps(durable_scheduler or {"provider_budgets": provider_budgets}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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
            f"Yayin politikasina uygun firma: {publication_eligible_count} ({_pct(publication_eligible_count, total)})",
            f"Dogrulanmis tam iletisim: {verified_complete_count} ({_pct(verified_complete_count, total)})",
            f"Tam iletisim bulundu fakat kimlik incelemesinde: {complete_held_count} ({_pct(complete_held_count, total)})",
            f"Yuksek guvenli OK: {high_confidence_count} ({_pct(high_confidence_count, total)})",
            f"Orta guvenli OK: {medium_confidence_count} ({_pct(medium_confidence_count, total)})",
            f"Manuel kontrol gereken: {review_count} ({_pct(review_count, total)})",
            f"Website adayi belirsiz: {ambiguous_count} ({_pct(ambiguous_count, total)})",
            f"Yayinlanmayan/geri cekilen sonuc: {total - len(verified_rows)} ({_pct(total - len(verified_rows), total)})",
            f"Kimlik izi kanitlanamayan: {identity_unproved_count}; website erisim hatasi: {fetch_failed_count}; aday bulunamayan: {not_found_count}",
            f"P3 politika dusurmesi: {policy_downgrades}",
            f"P3 dusuk/kontrollu riskli yayin: {low_risk_publications}/{controlled_risk_publications}",
            f"LLM hakem istek/butce engeli/token: {llm_arbiter_requests}/{llm_arbiter_blocked}/{llm_arbiter_tokens}",
            "--------------------------------",
            f"SCHEDULER_RECEIPT_JSON={scheduler_receipt}",
            f"Ortalama skor: {average_score:.1f}",
            f"Kesfedilen aday/firma: {(candidate_count / total):.1f}" if total else "Kesfedilen aday/firma: 0.0",
            f"Hafif kimlik taramasi/firma: {(identity_evaluations / total):.1f}" if total else "Hafif kimlik taramasi/firma: 0.0",
            f"Tam iletisim taramasi/firma: {(full_evaluations / total):.1f}" if total else "Tam iletisim taramasi/firma: 0.0",
            f"Crawler HTTP istegi/firma: {(crawler_requests / total):.1f}" if total else "Crawler HTTP istegi/firma: 0.0",
            f"Kaynak profil 5xx: {source_5xx}; devre kesici atlamasi: {source_skips}",
            f"P4 statik kurtarma: {static_successes}/{static_attempts}; gereksiz deneme atlamasi={static_skips}",
            f"P4 host varyanti: {host_variant_successes}/{host_variant_attempts}",
            f"P4 browser kurtarma: {browser_successes}/{browser_attempts}",
            f"JS interstitial reddi: canlı_sayfa={interstitial_live_pages_rejected}; cache_sayfa={interstitial_cache_pages_rejected}; benzersiz_host={interstitial_hosts}",
            f"JS root deneme/basari/hata: {browser_root_attempts}/{browser_root_successes}/{browser_root_errors}",
            f"JS identity deneme/basari/hata: {browser_identity_attempts}/{browser_identity_successes}/{browser_identity_errors}",
            f"JS contact deneme/basari/hata: {browser_contact_attempts}/{browser_contact_successes}/{browser_contact_errors}",
            f"Browser kurtarilan benzersiz firma: {browser_recovered_companies}; sonrasinda yayinlanabilir: {browser_publication_companies}",
            f"P4 PDF metin kurtarma: {pdf_successes}/{pdf_attempts}",
            f"P4 replay snapshot: yuklenen={snapshot_loaded}; isabet={snapshot_hits}; eski-cache-isabeti={stale_hits}",
            f"P5 aday e-posta alan karari: izin={email_field_allowed}; baskilanan={email_field_suppressed}",
            f"P5 aday telefon alan karari: izin={phone_field_allowed}; baskilanan={phone_field_suppressed}",
            (
                "P6 discovery kapsami: "
                f"cozulen={coverage['resolved_companies']}; "
                f"acik={coverage['unresolved_companies']}; "
                f"kaynak={coverage.get('source_count', 0)}; "
                f"islenmemis={sum(1 for row in coverage.get('source_coverage', []) if row.get('status') == 'unprocessed')}; "
                f"replay-eksigi={coverage['replay_miss_count']}; "
                f"edinim-plani={len(coverage['acquisition_plan'])}"
            ),
            f"Firma basi ucretli sorgu siniri: {paid_query_limit}",
            *budget_lines,
            f"Bright Data: sorgu={brightdata_queries}; HTTP={brightdata_requests}; retry={brightdata_retries}; cooldown={brightdata_cooldowns}; butce-engeli={brightdata_blocked}; saglayici-hatasi={search_provider_failures}",
            f"LinkedIn Company: eslesme={linkedin_company_matches}; HTTP={linkedin_company_requests}; butce-engeli={linkedin_company_blocked}",
            f"Crawler butce engeli: {crawler_blocked}",
            f"Google Places API istekleri: {places_requests}",
            f"Islem suresi: {elapsed}",
            "================================",
        ]
    )
