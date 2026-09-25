from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import config
import main
from modules import checkpoint, company_resolvers, discovery_coverage, publication_policy, runtime, search
from tools import measurement_audit


def _bound_record(*, target: str = "source:target", domain: str = "target.test") -> dict:
    digest = hashlib.sha256(b"target page").hexdigest()
    return {
        "evidence_id": "proof:target",
        "target_source_record_id": target,
        "evidence_source_record_id": "crawl:target",
        "observed_business": "Target Company",
        "url": f"https://{domain}/about",
        "final_url": f"https://{domain}/about",
        "content_sha256": digest,
        "retrieval_method": "http",
        "observation_type": "legal_name",
        "observation_value": "Target Company Sanayi A.S.",
        "location": {"kind": "page_text", "selector": "body"},
        "relation": "first_party_identity",
        "identity_route": "LEGAL_NAME",
        "first_party": True,
        "distinctive_token_count": 2,
        "legal_name_match": True,
        "full_name_match": True,
        "contact_fields": ["email", "phone"],
        "observed_contacts": {"email": "info@target.test", "phone": "+902121112233"},
    }


def _content_row(records: list[dict] | None = None) -> dict:
    records = records or [_bound_record(), {
        "evidence_id": "proof:country",
        "target_source_record_id": "source:target",
        "evidence_source_record_id": "crawl:country",
        "observed_business": "Target Company",
        "url": "https://target.test/about",
        "final_url": "https://target.test/about",
        "content_sha256": hashlib.sha256(b"country page").hexdigest(),
        "retrieval_method": "http",
        "observation_type": "country",
        "observation_value": "TR",
        "location": {"kind": "page_text", "selector": "body"},
        "relation": "country",
    }]
    decision = publication_policy.evaluate_content({
        "company": "Target Company",
        "source_record_id": "source:target",
        "website": "https://target.test",
        "identity_route": "LEGAL_NAME",
        "identity_assessment": {"publishable": True, "conflicts": []},
        "country_supported": True,
        "country_evidence_ids": ["proof:country"],
        "evidence_records": records,
        "support_evidence_ids": ["proof:target"],
        "email": "info@target.test",
        "phone": "+902121112233",
        "email_publication_status": "allowed",
        "phone_publication_status": "allowed",
    }).as_dict()
    return {
        "source_record_id": "source:target",
        "website": "https://target.test",
        "email": "info@target.test",
        "phone": "+902121112233",
        "status": "OK_HIGH_CONFIDENCE",
        "free_state": "DONE",
        "paid_required": False,
        "paid_state": "NOT_REQUIRED",
        "content_decision": decision,
        "content_evidence_records": records,
    }


def test_content_evidence_rejects_other_target_and_other_domain() -> None:
    record = _bound_record(target="source:other", domain="other.test")
    decision = publication_policy.evaluate_content({
        "company": "Target Company",
        "source_record_id": "source:target",
        "website": "https://target.test",
        "identity_route": "LEGAL_NAME",
        "country_supported": True,
        "country_evidence_ids": ["proof:target"],
        "evidence_records": [record],
        "support_evidence_ids": ["proof:target"],
        "email": "info@target.test",
        "email_publication_status": "allowed",
    })
    assert not decision.website_allowed
    assert "ownership_route_evidence_missing" in decision.missing_evidence


def test_missing_persisted_content_binding_requires_reevaluation() -> None:
    row = _content_row()
    row["content_decision"].pop("evidence_fingerprint")
    result = publication_policy.decide_row(row)
    assert not result["publishable"]
    assert "CONTENT_REEVALUATION_REQUIRED" in result["blockers"]


def test_reason_text_and_unrelated_html_do_not_materialize_identity() -> None:
    records, route, country_ids, _ = main._content_evidence_records(
        "Target Company",
        {
            "reasons": ["legal_name_phrase_match:2", "country_identity_tr_tld"],
            "crawl_result": {"pages": [{
                "url": "https://unrelated.test",
                "final_url": "https://unrelated.test",
                "html": "<p>Completely unrelated content</p>",
                "retrieval_method": "http",
            }]},
        },
        {"source_record_id": "source:target", "listed_legal_name": "Actual Legal Entity"},
    )
    assert route == ""
    assert country_ids == []
    assert records and not any(item.get("first_party") or item.get("legal_name_match") for item in records)


def test_advisory_flag_cannot_stop_insufficient_content() -> None:
    row = {
        "status": "OK_HIGH_CONFIDENCE",
        "publication_advisory_eligible": True,
        "content_decision": {"website_allowed": False, "email_allowed": False, "phone_allowed": False},
    }
    ready = __import__("modules.pipeline_runner", fromlist=["content_decision_ready"]).content_decision_ready(row)
    states = __import__("modules.pipeline_runner", fromlist=["classify_scheduler_states"]).classify_scheduler_states(
        row, attempt_number=1, publication_gate=ready,
    )
    assert not ready
    assert states["paid_required"] is True
    assert states["paid_state"] == "PENDING"


def test_name_compatible_brandfetch_without_crawl_still_runs_hunter() -> None:
    brandfetch = [{"domain": "target.test", "name": "Target Company", "provider": "brandfetch", "claimed": False}]
    with patch.object(company_resolvers, "brandfetch_domains", return_value=brandfetch), \
         patch.object(company_resolvers, "hunter_domains", return_value=[]) as hunter:
        company_resolvers.resolve_company_domains("Target Company")
    hunter.assert_called_once_with("Target Company")


class _RedirectResponse:
    status_code = 200
    headers = {}
    url = "https://target.test/"

    def close(self):
        return None


def test_opaque_ddgs_hits_are_resolved_before_receipt_and_survive_replay(monkeypatch, tmp_path: Path) -> None:
    class OpaqueDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def text(self, *args, **kwargs):
            return [{"href": "https://www.google.com/goto?url=CAESopaque", "title": "Target Company"}]

    runtime.reset()
    runtime.set_item_context(0, "free")
    runtime.set_source_record_id("")
    runtime.set_search_bucket("discovery")
    monkeypatch.setattr(search, "DDGS", OpaqueDDGS)
    monkeypatch.setattr(runtime, "wait_for_request_slot", lambda: None)
    monkeypatch.setattr(search, "_serp_redirect_transport", lambda *_args, **_kwargs: _RedirectResponse())
    monkeypatch.setattr(search.network_guard, "validate_public_http_url", lambda _url: (True, ""))
    monkeypatch.setattr(search, "_record_free_search_execution", lambda **_kwargs: None)
    found = search._ddgs_text("Target Company official")
    assert found.result_state == "COMPLETED"
    assert found and found[0]["resolution_status"] == "resolved"

    with patch.object(config, "SEARCH_CACHE_DIR", tmp_path), patch.object(config, "SEARCH_CACHE_MODE", "refresh"), \
         patch.object(search, "_search_text_live", return_value=search.SearchResults([
             {"href": "https://www.google.com/goto?url=CAESopaque", "title": "Target Company"},
         ], "live", "ddgs", result_state="COMPLETED")), \
         patch.object(search, "_serp_redirect_transport", lambda *_args, **_kwargs: _RedirectResponse()):
        live = search._search_text("Target Company cache")
        cached = search.cache_store.load(tmp_path, "serp", search._search_cache_key("Target Company cache"), config.SEARCH_CACHE_TTL_DAYS, config.CACHE_SCHEMA_VERSION)
        assert live[0]["resolution_status"] == "resolved"
        assert cached["values"][0]["resolution_status"] == "resolved"
        with patch.object(config, "SEARCH_CACHE_MODE", "replay"), patch.object(search, "_search_text_live", side_effect=AssertionError("live")):
            replayed = search._search_text("Target Company cache")
    assert replayed[0]["resolved_url"] == "https://target.test/"


def test_opaque_first_backend_is_preserved_when_second_backend_is_usable(monkeypatch) -> None:
    class MixedDDGS:
        calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def text(self, *args, **kwargs):
            type(self).calls += 1
            if type(self).calls == 1:
                return [{"href": "https://www.google.com/goto?url=CAESopaque", "title": "Target Company"}]
            return [{"href": "https://target.test", "title": "Target Company"}]

    runtime.reset()
    runtime.set_item_context(0, "free")
    runtime.set_source_record_id("")
    runtime.set_search_bucket("discovery")
    monkeypatch.setattr(search, "DDGS", MixedDDGS)
    monkeypatch.setattr(runtime, "wait_for_request_slot", lambda: None)
    monkeypatch.setattr(search, "_serp_redirect_transport", lambda *_args, **_kwargs: _RedirectResponse())
    monkeypatch.setattr(search.network_guard, "validate_public_http_url", lambda _url: (True, ""))
    monkeypatch.setattr(search, "_record_free_search_execution", lambda **_kwargs: None)
    found = search._ddgs_text("Target Company mixed")
    assert found.result_state == "COMPLETED"
    assert len(found) == 2
    assert any(item.get("resolution_status") == "resolved" for item in found)
    assert any(item.get("href") == "https://target.test" for item in found)


def test_discovery_attempt_retry_is_append_only_and_idempotent(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "attempts.sqlite3"
    monkeypatch.setattr(config, "PROGRESS_DB_FILE", database)
    checkpoint.initialize_schema(database)
    checkpoint.initialize_run_items("run-a", [{"source_record_id": "source:target"}])
    first = checkpoint.discovery_event_id("fetch:target", outcome="FAILED")
    second = checkpoint.discovery_event_id("fetch:target", outcome="DONE")
    checkpoint.record_discovery_attempt(
        run_id="run-a", source_record_id="source:target", attempt_id=first,
        stage="fetch", transport_outcome="FAILED",
    )
    checkpoint.record_discovery_attempt(
        run_id="run-a", source_record_id="source:target", attempt_id=second,
        stage="fetch", transport_outcome="DONE",
    )
    checkpoint.record_discovery_attempt(
        run_id="run-a", source_record_id="source:target", attempt_id=first,
        stage="fetch", transport_outcome="FAILED",
    )
    assert [row["transport_outcome"] for row in checkpoint.load_discovery_attempts("run-a")] == ["FAILED", "DONE"]
    with pytest.raises(checkpoint.EvidenceInvariant, match="reused with different payload"):
        checkpoint.record_discovery_attempt(
            run_id="run-a", source_record_id="source:target", attempt_id=first,
            stage="fetch", transport_outcome="DONE",
        )


def test_coverage_rebuilds_source_from_durable_run_after_ram_reset(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "coverage.sqlite3"
    monkeypatch.setattr(config, "PROGRESS_DB_FILE", database)
    checkpoint.initialize_schema(database)
    checkpoint.initialize_run_items("run-c", [{"source_record_id": "source:target"}])
    checkpoint.record_discovery_attempt(
        run_id="run-c", source_record_id="source:target",
        attempt_id="attempt-1", stage="fetch", transport_outcome="DONE",
    )
    discovery_coverage.reset()
    runtime.configure_durable_run("run-c", {provider: 1 for provider in checkpoint.CANONICAL_PROVIDERS})
    payload = discovery_coverage.payload()
    assert payload["source_count"] == 1
    assert payload["company_count"] == 1
    assert payload["attempt_count"] == 1


def test_measurement_never_uses_truth_as_prediction_and_deduplicates_companies() -> None:
    fixture = measurement_audit.build_synthetic_fixture(1)
    for company in fixture["companies"]:
        for row in company["records"]:
            for field in measurement_audit.FIELDS:
                row[f"predicted_{field}"] = {"state": "present", "value": "wrong.example"}
    fixture["companies"].append(dict(fixture["companies"][0]))
    wrong = measurement_audit.evaluate_fixture(fixture)
    assert wrong["company_count"] == 1
    assert all(wrong["fields"][field]["correct_published"] == 0 for field in measurement_audit.FIELDS)
    assert wrong["company_metrics"]["full_coverage"] == 0
    assert wrong["company_metrics"]["partial_coverage"] == 0

    for row in fixture["companies"][0]["records"]:
        for field in measurement_audit.FIELDS:
            row.pop(f"predicted_{field}", None)
    missing = measurement_audit.evaluate_fixture({"companies": [fixture["companies"][0]]})
    assert all(missing["fields"][field]["correct_published"] == 0 for field in measurement_audit.FIELDS)
    assert all(missing["fields"][field]["missing_prediction"] == 2 for field in measurement_audit.FIELDS)
