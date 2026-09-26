from __future__ import annotations

import logging

from modules import field_merge, pipeline_runner, runtime


def _settings(**overrides):
    values = {
        "search_provider": "ddgs",
        "google_places": False,
        "hunter_domain": False,
        "brightdata_budget": 0,
        "google_places_budget": 0,
        "hunter_budget": 0,
    }
    values.update(overrides)
    return values


def _ready_row() -> dict:
    row = {
        "company": "Acme",
        "website": "https://acme.com/",
        "website_source": "OWN_SEARCH",
        "status": "OK_HIGH_CONFIDENCE",
        "email": "info@acme.com",
        "email_source_tier": "SITE",
        "phone": "+90 212 555 12 34",
        "phone_source_tier": "SITE",
    }
    field_merge.annotate(row, None)
    return row


def test_complete_row_not_sent_to_paid():
    row = _ready_row()
    gaps = pipeline_runner.paid_fillable_gaps(row, _settings(
        search_provider="brightdata", brightdata_budget=5,
        google_places=True, google_places_budget=5,
        hunter_domain=True, hunter_budget=5,
    ))
    assert gaps == set()
    assert pipeline_runner.classify_scheduler_states(
        row, attempt_number=1, paid_gaps=gaps,
    )["paid_required"] is False


def test_phone_gap_with_places_enabled_goes_paid():
    row = _ready_row()
    row["phone"] = ""
    settings = _settings(google_places=True, google_places_budget=1)
    gaps = pipeline_runner.paid_fillable_gaps(row, settings)
    assert gaps == {"phone"}
    assert pipeline_runner.classify_scheduler_states(
        row, attempt_number=1, paid_gaps=gaps,
    ) == {"free_state": "DONE", "paid_state": "PENDING", "paid_required": True}


def test_email_gap_without_website_is_not_fillable():
    row = {"company": "Acme", "website": "", "email": "", "phone": ""}
    settings = _settings(hunter_domain=True, hunter_budget=1)
    gaps = pipeline_runner.paid_fillable_gaps(row, settings)
    assert gaps == set()
    assert pipeline_runner.classify_scheduler_states(
        row, attempt_number=1, paid_gaps=gaps,
    )["paid_state"] == "NOT_REQUIRED"


def test_website_gap_goes_paid_with_brightdata():
    row = {"company": "Acme", "website": "", "email": "", "phone": ""}
    settings = _settings(
        search_provider="brightdata", brightdata_budget=1,
        google_places=True, google_places_budget=1,
        hunter_domain=True, hunter_budget=1,
    )
    gaps = pipeline_runner.paid_fillable_gaps(row, settings)
    assert gaps == {"website", "email", "phone"}
    assert pipeline_runner.classify_scheduler_states(
        row, attempt_number=1, paid_gaps=gaps,
    )["paid_required"] is True


def test_content_decision_no_longer_gates_scheduler():
    row = _ready_row()
    row["content_decision"] = {
        "website_allowed": False, "email_allowed": False, "phone_allowed": False,
    }
    settings = _settings(
        search_provider="brightdata", brightdata_budget=1,
        google_places=True, google_places_budget=1,
        hunter_domain=True, hunter_budget=1,
    )
    assert not pipeline_runner.content_decision_ready(row)
    assert pipeline_runner.paid_fillable_gaps(row, settings) == set()
    assert pipeline_runner.classify_scheduler_states(
        row, attempt_number=1, paid_gaps=set(),
    )["paid_required"] is False


def test_paid_gap_fill_skips_search_when_website_known(monkeypatch):
    import main

    prior = _ready_row()
    prior["email"] = ""
    prior["phone"] = ""
    monkeypatch.setattr(main, "_process_company_core", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("search should be skipped")))
    monkeypatch.setattr(main.google_places, "is_enabled", lambda: True)
    monkeypatch.setattr(main.google_places, "search_company", lambda _company: runtime.provider_result(
        [{"website": "https://acme.com/", "phone": "+90 212 555 12 34"}],
        state="COMPLETED", call_ids=("places-call",),
    ))
    monkeypatch.setattr(main.hunter, "email_gap_fill_enabled", lambda: True)
    monkeypatch.setattr(main.hunter, "find_domain_emails", lambda _domain: runtime.provider_result(
        [
            {"email": "other@acme.com", "confidence": 99},
            {"email": "info@acme.com", "confidence": 85},
            {"email": "sales@acme.com", "confidence": 90},
        ],
        state="COMPLETED", call_ids=("hunter-call",),
    ))
    _, row = main.paid_gap_fill(
        0, "Acme", logging.getLogger("ip7-test"), "",
        {"_prior_row": prior},
    )
    assert row["website"] == prior["website"]
    assert row["phone"]
    assert row["email"] == "info@acme.com"
    assert [item["provider"] for item in row["provider_results"]] == ["google_places", "hunter"]


def test_paid_merge_keeps_free_values_when_paid_fails(monkeypatch):
    import main

    prior = _ready_row()
    prior["phone"] = ""
    monkeypatch.setattr(main, "_process_company_core", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("search should be skipped")))
    monkeypatch.setattr(main.google_places, "is_enabled", lambda: True)
    monkeypatch.setattr(main.google_places, "search_company", lambda _company: runtime.provider_result(
        [], state="FAILED", call_ids=("places-failed",),
    ))
    monkeypatch.setattr(main.hunter, "email_gap_fill_enabled", lambda: False)
    _, row = main.paid_gap_fill(
        0, "Acme", logging.getLogger("ip7-test"), "",
        {"_prior_row": prior},
    )
    assert row["website"] == prior["website"]
    assert row["email"] == prior["email"]
    assert row["phone"] == ""
    assert row["website_confidence"] == "HIGH"
    assert row["email_confidence"] == "HIGH"


def test_prepare_work_items_only_for_gaps(monkeypatch):
    from modules import checkpoint

    complete = _ready_row()
    needs_contacts = {
        "company": "Acme", "website": "https://acme.com/",
        "website_source": "OWN_SEARCH", "status": "OK_HIGH_CONFIDENCE",
        "email": "", "phone": "",
    }
    needs_website = {"company": "New Co", "website": "", "email": "", "phone": ""}
    records = [
        {"company": "Complete", "source_record_id": "source:complete"},
        {"company": "Acme", "source_record_id": "source:acme"},
        {"company": "New Co", "source_record_id": "source:new"},
    ]
    captured = []
    monkeypatch.setattr(checkpoint, "ensure_provider_work_item", lambda **kwargs: captured.append(kwargs) or {})
    monkeypatch.setattr(checkpoint, "load_paid_query_plan", lambda _run_id, idx: ["New Co official website"] if idx == 2 else [])
    monkeypatch.setattr(checkpoint, "load_provider_query_flight", lambda **_kwargs: None)
    monkeypatch.setattr(pipeline_runner.search, "brightdata_request_fingerprint", lambda query: f"bd:{query}")
    monkeypatch.setattr(pipeline_runner.search, "_brightdata_flight_fingerprint", lambda query: f"flight:{query}")
    pipeline_runner._prepare_provider_work_items(
        run_id="run-ip7", company_records=records,
        results_by_index={0: complete, 1: needs_contacts, 2: needs_website}, item_indexes=[0, 1, 2],
        paid_settings=_settings(
            search_provider="brightdata", brightdata_budget=1,
            google_places=True, google_places_budget=1,
            hunter_domain=True, hunter_budget=1,
        ),
    )
    assert {(item["item_index"], item["provider"], item["operation"], item["need_class"]) for item in captured} == {
        (1, "google_places", "text_search", "contact"),
        (1, "hunter", "domain_search", "identity"),
        (2, "brightdata", "search", "website"),
    }
    assert all(item["request_fingerprint"] for item in captured)
