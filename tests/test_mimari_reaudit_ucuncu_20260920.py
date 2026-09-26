from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

import main
from modules import checkpoint, company_resolvers, discovery_coverage, discovery_rules, output_artifacts, publication_policy, replay_snapshot, runtime, search
from tools import measurement_audit


def _html_evidence(*, anchor: bool = False, anchor_only: bool = False):
    html = "<html><body><h1>Target Company Sanayi A.S.</h1><p>Türkiye Istanbul</p><p>0212 111 22 33</p><a href='mailto:info@target.test'>info@target.test</a></body></html>"
    metadata = {
        "source_record_id": "source:target",
        "target_identity": {
            "legal_name": "Target" if anchor_only else "Target Company Sanayi A.S.",
            "brands": ["Target" if anchor_only else "Target Company"],
        },
    }
    if anchor:
        metadata.update({
            "listed_phone": "02121112233",
            "source_detail_url": "https://fair.test/target",
            "source_detail_content_sha256": hashlib.sha256(b"fair evidence").hexdigest(),
            "source_evidence": "fair:target",
        })
    evaluation = {
        "crawl_result": {"url": "https://target.test", "pages": [{
            "url": "https://target.test/about", "final_url": "https://target.test/about",
            "html": html, "retrieval_method": "http",
        }]},
        "email": "info@target.test", "phone": "02121112233",
        "structured_identity": {},
    }
    return metadata, evaluation


def _decision(records, country_ids, *, email="info@target.test", phone="02121112233"):
    return publication_policy.evaluate_content({
        "company": "Target Company Sanayi A.S.", "source_record_id": "source:target",
        "website": "https://target.test", "identity_route": "LEGAL_NAME",
        "evidence_records": records,
        "support_evidence_ids": [item["evidence_id"] for item in records if item.get("identity_route") in {"LEGAL_NAME", "TARGET_ANCHOR", "BRAND_OWNER"}],
        "country_supported": bool(country_ids), "country_evidence_ids": country_ids,
        "email": email, "phone": phone,
        "email_publication_status": "allowed", "phone_publication_status": "allowed",
    })


def test_e01_real_producer_keeps_legal_route_when_anchor_is_added() -> None:
    metadata, evaluation = _html_evidence(anchor=False)
    records, route, country_ids, _ = main._content_evidence_records("Target Company Sanayi A.S.", evaluation, metadata)
    before = _decision(records, country_ids)
    metadata, evaluation = _html_evidence(anchor=True)
    records, route, country_ids, anchor = main._content_evidence_records("Target Company Sanayi A.S.", evaluation, metadata)
    after = _decision(records, country_ids)
    assert before.website_allowed and after.website_allowed and route == "LEGAL_NAME" and anchor
    assert after.identity_route == "LEGAL_NAME"


def test_e01_target_anchor_requires_three_bound_records() -> None:
    metadata, evaluation = _html_evidence(anchor=True)
    records, _, country_ids, _ = main._content_evidence_records("Target Company Sanayi A.S.", evaluation, metadata)
    anchor = next(item for item in records if item.get("identity_route") == "TARGET_ANCHOR" and item.get("match_kind"))
    broken = [item for item in records if item["evidence_id"] != anchor["evidence_id"] and item["evidence_id"] != anchor["relation_evidence_id"] and item.get("identity_route") != "LEGAL_NAME"]
    assert not _decision(broken, country_ids).website_allowed
    assert _decision(records, country_ids).website_allowed


def test_e01_anchor_only_producer_route_is_publishable_from_real_html() -> None:
    metadata, evaluation = _html_evidence(anchor=True, anchor_only=True)
    records, route, country_ids, anchor = main._content_evidence_records(
        "Target", evaluation, metadata,
    )
    decision = _decision(records, country_ids)
    assert route == "TARGET_ANCHOR" and anchor
    assert decision.website_allowed
    assert sum(item.get("role") == "candidate_first_party_observation" for item in records) == 1


def test_e01_unbranded_single_token_target_name_can_support_anchor_only() -> None:
    metadata, evaluation = _html_evidence(anchor=True, anchor_only=True)
    metadata["target_identity"]["brands"] = []
    records, route, country_ids, anchor = main._content_evidence_records(
        "Target", evaluation, metadata,
    )
    decision = _decision(records, country_ids)
    assert route == "TARGET_ANCHOR" and anchor
    assert decision.website_allowed


def test_e01_target_anchor_rejects_role_value_domain_and_cycle_tampering() -> None:
    metadata, evaluation = _html_evidence(anchor=True, anchor_only=True)
    records, _, country_ids, _ = main._content_evidence_records(
        "Target", evaluation, metadata,
    )
    anchor = next(item for item in records if item.get("role") == "independent_observation")
    candidate_id = anchor["candidate_evidence_id"]
    relation_id = anchor["relation_evidence_id"]
    for mutation in (
        {"role": "candidate_first_party_observation"},
        {"observation_value": "+902121119999"},
        {"candidate_evidence_id": relation_id},
    ):
        mutated = [dict(item) for item in records]
        target_id = candidate_id if "observation_value" in mutation else anchor["evidence_id"]
        if "candidate_evidence_id" in mutation:
            target_id = relation_id
        target = next(item for item in mutated if item["evidence_id"] == target_id)
        target.update(mutation)
        assert not _decision(mutated, country_ids).website_allowed
    candidate = next(item for item in records if item["evidence_id"] == candidate_id)
    candidate["final_url"] = "https://agency.test/contact"
    assert not _decision(records, country_ids).website_allowed


def test_e02_invalid_contact_provenance_is_not_publishable() -> None:
    metadata, evaluation = _html_evidence(anchor=False)
    records, _, country_ids, _ = main._content_evidence_records("Target Company Sanayi A.S.", evaluation, metadata)
    identity = next(item for item in records if item.get("identity_route") == "LEGAL_NAME")
    identity["contact_fields"] = []
    identity["observed_contacts"] = {}
    invalid = dict(identity)
    invalid.update({
        "evidence_id": "invalid-contact", "url": "https://agency.test/contact", "final_url": "https://agency.test/contact",
        "content_sha256": "", "retrieval_method": "unknown", "contact_fields": ["email"],
        "observed_contacts": {"email": "info@target.test"}, "observation_type": "contact", "observation_value": "info@target.test",
    })
    decision = _decision(records + [invalid], country_ids)
    assert decision.website_allowed and not decision.email_allowed
    assert "email_evidence_missing" in decision.missing_evidence
    assert "acquire_email" in decision.next_actions


def test_e03_execution_ordinal_keeps_equal_outcomes_separate(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "events.sqlite3"
    monkeypatch.setattr("config.PROGRESS_DB_FILE", database)
    checkpoint.initialize_schema(database)
    first = checkpoint.reserve_discovery_execution(run_id="run-e03", source_record_id="source:x", stage="fetch", execution_kind="https://target.test")
    second = checkpoint.reserve_discovery_execution(run_id="run-e03", source_record_id="source:x", stage="fetch", execution_kind="https://target.test")
    assert first["ordinal"] == 1 and second["ordinal"] == 2 and first["execution_id"] != second["execution_id"]
    for execution in (first, second, first):
        checkpoint.record_discovery_attempt(
            run_id="run-e03", source_record_id="source:x",
            attempt_id=checkpoint.discovery_event_id("fetch:target", execution_id=execution["execution_id"]),
            execution_id=execution["execution_id"], parent_attempt_id=execution["parent_execution_id"],
            stage="fetch", transport_outcome="FAILED", reason="redirect_resolution_timeout",
        )
    assert len(checkpoint.load_discovery_attempts("run-e03")) == 2


def test_e03_unpersisted_execution_is_reported_unknown(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "crash.sqlite3"
    monkeypatch.setattr("config.PROGRESS_DB_FILE", database)
    checkpoint.initialize_schema(database)
    checkpoint.reserve_discovery_execution(run_id="run-crash", source_record_id="source:x", stage="fetch", execution_kind="target")
    attempts = checkpoint.load_discovery_attempts("run-crash")
    assert len(attempts) == 1 and attempts[0]["transport_outcome"] == "UNKNOWN"


def test_e04_measurement_dedupes_fields_and_rejects_conflicts() -> None:
    fixture = measurement_audit.build_synthetic_fixture(2)
    duplicate = {"source_record_id": fixture["companies"][0]["source_record_id"], "company": fixture["companies"][0]["company"], "records": [dict(fixture["companies"][0]["records"][0])]}
    base = measurement_audit.evaluate_fixture(fixture)
    repeated = measurement_audit.evaluate_fixture({**fixture, "companies": [*fixture["companies"], duplicate]})
    assert repeated["company_count"] == base["company_count"] == 2
    assert repeated["fields"]["website"]["precision"] == base["fields"]["website"]["precision"]
    conflict = measurement_audit.build_synthetic_fixture(1)
    conflict["companies"][0]["records"][0]["predicted_email"] = {"state": "present", "value": "other.test"}
    conflict["companies"][0]["records"][0]["prediction"] = {"email": {"state": "present", "value": "third.test"}}
    with pytest.raises(measurement_audit.MeasurementInputError):
        measurement_audit.evaluate_fixture(conflict)


def test_e04_wrong_website_invalidates_unfrozen_contact_relationship() -> None:
    fixture = {
        "dataset_version": "fixture-v1", "companies": [{"source_record_id": "source:wrong-site", "records": [{
            "website": {"state": "present", "value": "https://right.test"},
            "email": {"state": "present", "value": "info@right.test"},
            "phone": {"state": "present", "value": "+902121112233"},
            "predicted_website": {"state": "present", "value": "https://wrong.test"},
            "predicted_email": {"state": "present", "value": "info@right.test"},
            "predicted_phone": {"state": "present", "value": "+902121112233"},
        }]}],
    }
    result = measurement_audit.evaluate_fixture(fixture)
    assert result["fields"]["website"]["correct_published"] == 0
    assert result["fields"]["email"]["correct_published"] == 0
    assert result["fields"]["phone"]["correct_published"] == 0


def test_e05_resolver_uses_post_crawl_identity_gate() -> None:
    raw = [{"domain": "target.test", "name": "Target Company", "claimed": False}]
    with pytest.MonkeyPatch.context() as patch:
        hunter = lambda company: [{"provider": "hunter_domain_finder", "domain": "hunter.test", "resolved_name": company, "claimed": False}]
        patch.setattr(company_resolvers, "hunter_domains", hunter)
        sufficient = company_resolvers.resolve_company_domains("Target Company", brandfetch_results=company_resolvers._clean_results(raw, "brandfetch"), candidate_evaluator=lambda item: True)
        assert not any(item["domain"] == "hunter.test" for item in sufficient)
        insufficient = company_resolvers.resolve_company_domains("Target Company", brandfetch_results=company_resolvers._clean_results(raw, "brandfetch"), candidate_evaluator=lambda item: False)
        assert any(item["domain"] == "hunter.test" for item in insufficient)
    assert all("validated_identity" not in item for item in company_resolvers._clean_results(raw, "brandfetch"))


def test_e05_dispatch_round_prioritizes_need_then_input_order_and_resume_idempotent(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "allocations.sqlite3"
    monkeypatch.setattr("config.PROGRESS_DB_FILE", database)
    checkpoint.initialize_schema(database)
    checkpoint.initialize_run(
        run_id="run-e05", input_hash="input", run_signature="dispatch",
        context={"phase": "PAID"},
        budgets={provider: 3 if provider == "hunter" else 0 for provider in checkpoint.CANONICAL_PROVIDERS},
        items=[
            {"item_index": 2, "source_record_id": "source:z", "free_state": "DONE", "paid_required": True, "paid_state": "RUNNING"},
            {"item_index": 4, "source_record_id": "source:m", "free_state": "DONE", "paid_required": True, "paid_state": "RUNNING"},
            {"item_index": 9, "source_record_id": "source:a", "free_state": "DONE", "paid_required": True, "paid_state": "RUNNING"},
            {"item_index": 12, "source_record_id": "source:b", "free_state": "DONE", "paid_required": True, "paid_state": "RUNNING"},
        ],
    )
    candidates = [
        {"item_index": 12, "source_record_id": "source:b", "need_class": "contact"},
        {"item_index": 9, "source_record_id": "source:a", "need_class": "identity"},
        {"item_index": 2, "source_record_id": "source:z", "need_class": "identity"},
        {"item_index": 4, "source_record_id": "source:m", "need_class": "website"},
    ]
    first = checkpoint.reserve_provider_dispatch_round(run_id="run-e05", provider="hunter", round_ordinal=0, candidates=candidates, cap=3)
    second = checkpoint.reserve_provider_dispatch_round(run_id="run-e05", provider="hunter", round_ordinal=0, candidates=list(reversed(candidates)), cap=3)
    assert [item["source_record_id"] for item in first] == ["source:m", "source:z", "source:a"]
    assert first == second


def test_e05_dispatch_rejects_source_drift_and_unallocated_provider_call(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "allocation-drift.sqlite3"
    monkeypatch.setattr("config.PROGRESS_DB_FILE", database)
    checkpoint.initialize_schema(database)
    checkpoint.initialize_run(
        run_id="run-e05-drift", input_hash="input", run_signature="dispatch",
        context={"phase": "PAID"},
        budgets={provider: 1 if provider == "hunter" else 0 for provider in checkpoint.CANONICAL_PROVIDERS},
        items=[{"item_index": 0, "source_record_id": "source:a", "free_state": "DONE", "paid_required": True, "paid_state": "RUNNING"}],
    )
    with pytest.raises(checkpoint.ResumeInvariant):
        checkpoint.reserve_provider_dispatch_round(
            run_id="run-e05-drift", provider="hunter", round_ordinal=0,
            candidates=[
                {"item_index": 0, "source_record_id": "source:a", "need_class": "website"},
                {"item_index": 1, "source_record_id": "source:a", "need_class": "contact"},
            ], cap=1,
        )
    checkpoint.reserve_provider_dispatch_round(
        run_id="run-e05-drift", provider="hunter", round_ordinal=0,
        candidates=[{"item_index": 0, "source_record_id": "source:a", "need_class": "website"}], cap=1,
    )
    runtime.reset()
    runtime.configure_durable_run("run-e05-drift", {provider: 1 if provider == "hunter" else 0 for provider in checkpoint.CANONICAL_PROVIDERS})
    runtime.set_phase("PAID")
    runtime.set_item_context(1, "paid")
    runtime.set_source_record_id("source:missing")
    runtime.set_provider_dispatch_rounds({"hunter": 0})
    assert not runtime.reserve_api("hunter", operation="test", request_fingerprint="unallocated")


def test_e07_durable_free_replay_uses_source_id_and_snapshot(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "free.sqlite3"
    replay_database = tmp_path / "replay.sqlite3"
    monkeypatch.setattr("config.PROGRESS_DB_FILE", database)
    checkpoint.initialize_schema(database)
    run_id = "run-free-replay"
    checkpoint.initialize_run(
        run_id=run_id, input_hash="input-hash", run_signature="test", context={"phase": "FREE"},
        budgets={provider: 0 for provider in checkpoint.CANONICAL_PROVIDERS},
        items=[{"item_index": 0, "source_record_id": "source:free", "free_state": "PENDING", "paid_required": False, "paid_state": "NOT_REQUIRED"}],
    )
    query_fingerprint = search._query_fingerprint("Target Company official")
    results = search.SearchResults([{"url": "https://target.test", "title": "Target Company"}], "ddgs", result_state="COMPLETED")
    replay_snapshot.reset()
    replay_snapshot.configure_run_store(replay_database, run_id)
    search._record_free_search_execution(
        source_record_id="source:free", bucket="discovery", query_fingerprint=query_fingerprint,
        logical={"accepted": True, "reason": "", "logical_used": 1, "logical_limit": 10, "physical_used": 0, "physical_limit": 20},
        backend_attempts=[{"backend": "ddgs", "result": "ACCEPTED", "transport_outcome": "DONE", "attempt_ordinal": 1, "error_class": ""}],
        results=results,
    )
    replay_snapshot.reset()
    replay_snapshot.configure_run_store(replay_database, run_id, read_only=True)
    runtime.reset()
    runtime.configure_durable_run(run_id, {provider: 0 for provider in checkpoint.CANONICAL_PROVIDERS})
    runtime.set_item_context(0, "free")
    runtime.set_source_record_id("source:free")
    runtime.set_search_bucket("discovery")
    found, payload = replay_snapshot.lookup("replay", search.FREE_SEARCH_EXECUTION_NAMESPACE, search._replay_record_key("source:free", "discovery", query_fingerprint), search.FREE_SEARCH_EXECUTION_SCHEMA_VERSION)
    replayed = search._replay_free_search_execution(payload, source_record_id="source:free", bucket="discovery", query_fingerprint=query_fingerprint)
    assert found and replayed[0]["url"] == "https://target.test" and replayed.origin == search.SearchOrigin.REPLAY


def test_e06_late_redirect_response_is_typed_timeout(monkeypatch) -> None:
    class Late:
        status_code = 200
        headers = {}
        url = "https://target.test/"
        def close(self):
            self.closed = True

    def late_transport(_url, *, timeout):
        time.sleep(0.02)
        return Late()

    monkeypatch.setattr(discovery_rules.network_guard, "validate_public_http_url", lambda _url: (True, ""))
    result = discovery_rules.resolve_serp_target({"raw_url": "https://wrapper.test", "resolution_method": "wrapper_opaque"}, transport=late_transport, timeout_seconds=0.005)
    assert result.get("resolution_status") != "resolved"
    assert result["resolution_reason"] == "redirect_resolution_timeout"


def test_e07_real_html_checkpoint_export_survives_runtime_reset(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "chain.sqlite3"
    monkeypatch.setattr("config.PROGRESS_DB_FILE", database)
    checkpoint.initialize_schema(database)
    checkpoint.initialize_run_items("run-e07", [{"source_record_id": "source:target"}])
    metadata, evaluation = _html_evidence(anchor=True)
    records, route, country_ids, anchor = main._content_evidence_records("Target Company Sanayi A.S.", evaluation, metadata)
    decision = _decision(records, country_ids)
    assert decision.website_allowed and decision.email_allowed and anchor and route == "LEGAL_NAME"
    runtime.reset()
    runtime.configure_durable_run("run-e07", {provider: 1 for provider in checkpoint.CANONICAL_PROVIDERS})
    runtime.set_phase("PAID")
    runtime.set_item_context(0, "paid")
    runtime.set_source_record_id("source:target")
    execution = checkpoint.reserve_discovery_execution(run_id="run-e07", source_record_id="source:target", stage="publication", execution_kind="target.test")
    checkpoint.record_discovery_attempt(
        run_id="run-e07", source_record_id="source:target",
        attempt_id=checkpoint.discovery_event_id("publication:target.test", execution_id=execution["execution_id"]),
        execution_id=execution["execution_id"], stage="publication", candidate_url="https://target.test",
        transport_outcome="DONE", semantic_result="PUBLICATION_EVALUATED", evidence_refs=list(decision.support_evidence_ids),
    )
    runtime.record("recovery.browser_attempts", 2)
    runtime.record_unique("recovery.browser_recovered_companies", "source:target")
    row = {
        "company": "Target Company Sanayi A.S.",
        "source_record_id": "source:target",
        "website": "https://target.test",
        "website_source": "fixture_html",
        "email": "info@target.test",
        "phone": "02121112233",
        "email_publication_status": "allowed",
        "phone_publication_status": "allowed",
        "status": "OK_HIGH_CONFIDENCE",
        "score": 100,
        "identity_assessment": {"publishable": True, "conflicts": []},
        "content_decision": decision.as_dict(),
        "__content_decision": decision.as_dict(),
        "content_evidence_records": records,
        "content_target_source_record_id": "source:target",
    }
    checkpoint.save_item_transaction(
        run_id="run-e07", item_index=0, source_record_id="source:target", payload=row,
        free_state="DONE", paid_state="NOT_REQUIRED", paid_required=False,
        free_attempts=1, paid_attempts=0,
    )
    runtime.record("pipeline.batch_regression", amount=2)
    runtime.record("pipeline.batch_regression", amount=3)
    operational_before_reset = checkpoint.operational_metrics_snapshot("run-e07")
    assert operational_before_reset["counters"]["pipeline.batch_regression"] == 5
    runtime.reset()
    loaded = checkpoint.load_results_by_id("run-e07")[0]
    operational_after_reset = checkpoint.operational_metrics_snapshot("run-e07")
    assert operational_after_reset["counters"]["recovery.browser_attempts"] == 2
    assert operational_after_reset["unique_counts"]["recovery.browser_recovered_companies"] == 1
    assert operational_after_reset["counters"] == operational_before_reset["counters"]
    discovery_coverage.reset()
    output_root = tmp_path / "output"
    monkeypatch.setattr("config.OUTPUT_DIR", output_root)
    for name in (
        "CONTACTS_FILE", "VERIFIED_CONTACTS_FILE", "REVIEW_QUEUE_FILE", "FAILED_FILE",
        "CANDIDATES_FILE", "EVIDENCE_FILE", "ENTITY_RELATIONSHIPS_FILE", "QUALITY_AUDIT_FILE",
        "DISCOVERY_COVERAGE_FILE", "REPORT_FILE", "TELEMETRY_FILE",
    ):
        monkeypatch.setattr("config." + name, output_root / getattr(__import__("config"), name).name)
    artifact_result = output_artifacts.write_outputs(
        [loaded], 0, telemetry_snapshot=checkpoint.canonical_scheduler_receipt("run-e07"),
        operational_metrics=operational_after_reset,
    )
    artifact_dir = Path(artifact_result.artifacts["artifact_dir"])
    assert (artifact_dir / "report.txt").is_file()
    report_text = (artifact_dir / "report.txt").read_text(encoding="utf-8")
    assert "P4 browser kurtarma" in report_text
    assert "/2" in report_text
    runtime.configure_durable_run("run-e07", {provider: 1 for provider in checkpoint.CANONICAL_PROVIDERS})
    exported = discovery_coverage.payload()
    assert exported["coverage_complete"] and exported["attempt_count"] == 1
    assert exported["discovery_attempts"][0]["reservation"].get("execution_id") == execution["execution_id"]
