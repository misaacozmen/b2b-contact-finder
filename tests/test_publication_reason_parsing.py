from copy import deepcopy

from modules.publication_policy import is_publishable_row
from strict_fixtures import publishable_content_decision


def _row(**updates):
    row = {
        "source_record_id": "test:publication-reason",
        "free_state": "DONE",
        "paid_required": False,
        "paid_state": "NOT_REQUIRED",
        "status": "OK_MEDIUM_CONFIDENCE",
        "publication_eligible": True,
        "score": 100,
        "company": "Example Textiles",
        "website": "https://example-textiles.example",
        "email": "hello@example-textiles.example",
        "phone": "+90 212 000 00 00",
        "email_publication_status": "allowed",
        "phone_publication_status": "allowed",
        "reason": "",
        "publication_blockers": "",
        "quarantine_state": "",
        "quarantine_status": "",
        "__evaluation": {
            "identity_assessment": {"publishable": True, "conflicts": []},
            "reasons": [],
            "structured_domain_relation": {},
        },
    }
    row.update(updates)
    return row


def test_positive_legal_name_token_is_whitespace_position_independent():
    for token in (
        "legal_name_phrase_match:",
        "legal_name_full_match:",
        "legal_name_ownership_match:",
    ):
        for reason in (f"{token}1", f"page_identity_strong:1; {token}1"):
            row = _row(
                company="ACME",
                website="https://acme.com",
                reason=f"{reason}; country_identity_tr_tld; context_match:1/1",
            )
            row["content_decision"] = publishable_content_decision(
                source_record_id=row["source_record_id"], website=row["website"],
                email=row["email"], phone=row["phone"],
            )
            assert is_publishable_row(row)


def test_negative_or_unavailable_legal_name_tokens_do_not_authorize_short_name():
    for token in ("legal_name_phrase_missing:0/1", "legal_name_phrase_unavailable"):
        row = _row(
            company="ACME",
            website="https://acme.com",
            reason=f"{token}; country_identity_tr_tld; context_match:1/1",
        )
        assert not is_publishable_row(row)


def test_exact_resolved_context_token_is_not_a_conflict():
    context = {
        "legal_ownership_verified": True,
        "candidate_sector_compatible": True,
        "independent_matches": [{
            "kind": "phone",
            "url": "https://example-textiles.example/contact",
            "content_sha256": "a" * 64,
            "source_record_id": "source:example",
        }],
    }
    for field in ("reason", "publication_blockers", "__evaluation"):
        row = _row()
        row["content_decision"] = publishable_content_decision(source_record_id=row["source_record_id"], website=row["website"], email=row["email"], phone=row["phone"])
        if field == "__evaluation":
            row[field] = {
                "identity_assessment": {"publishable": True, "conflicts": []},
                "reasons": ["metadata_context_conflict_overridden_by_exact_compound_identity"],
                "structured_domain_relation": {},
                "context_resolution": context,
            }
        else:
            row[field] = "metadata_context_conflict_overridden_by_exact_compound_identity"
            row["__evaluation"]["context_resolution"] = context
        assert is_publishable_row(row)


def test_resolved_context_token_does_not_hide_a_real_conflict():
    for field in ("reason", "publication_blockers", "__evaluation"):
        row = _row()
        if field == "__evaluation":
            row[field] = {
                "identity_assessment": {"publishable": True, "conflicts": []},
                "reasons": [
                    "metadata_context_conflict_overridden_by_exact_compound_identity",
                    "context_conflict",
                ],
                "structured_domain_relation": {},
            }
        else:
            row[field] = "metadata_context_conflict_overridden_by_exact_compound_identity; context_conflict"
        assert not is_publishable_row(row)


def test_detailed_conflict_markers_are_rejected_in_each_reason_source():
    for marker in ("sector_conflict", "context_conflict", "country_conflict", "country_mismatch", "foreign_country"):
        for field in ("reason", "publication_blockers", "__evaluation"):
            row = _row()
            detail = f"{marker}:observed"
            if field == "__evaluation":
                row[field] = {
                    "identity_assessment": {"publishable": True, "conflicts": []},
                    "reasons": [f"evidence; {detail}"],
                    "structured_domain_relation": {},
                }
            else:
                row[field] = f"evidence; {detail}"
            assert not is_publishable_row(row)


def test_override_suffix_is_not_an_exception_and_list_elements_are_split():
    row = _row(
        __evaluation={
            "identity_assessment": {"publishable": True, "conflicts": []},
            "reasons": [
                "metadata_context_conflict_overridden_by_exact_compound_identity:suffix; context_conflict:real",
            ],
            "structured_domain_relation": {},
        },
    )
    assert not is_publishable_row(row)


def test_combined_list_tokens_and_detailed_cross_domain_marker_remain_blocked():
    row = _row(
        __evaluation={
            "identity_assessment": {"publishable": True, "conflicts": []},
            "reasons": [
                "first; second",
                "cross_domain_email_accepted_from_verified_official_page:detail",
            ],
            "structured_domain_relation": {},
        },
    )
    assert not is_publishable_row(row)


def test_existing_publication_safety_gates_remain_fail_closed():
    cases = (
        {"score": 60},
        {"__evaluation": {"identity_assessment": {"publishable": False, "conflicts": []}}},
        {"__evaluation": {"identity_assessment": {"publishable": True, "conflicts": [{"kind": "name"}]}}},
        {"paid_required": True, "paid_state": "PENDING"},
        {"quarantine_state": "LEGACY"},
        {
            "reason": "cross_domain_email_accepted_from_verified_official_page",
            "__evaluation": {
                "identity_assessment": {"publishable": True, "conflicts": []},
                "reasons": ["cross_domain_email_accepted_from_verified_official_page"],
                "structured_domain_relation": {},
            },
        },
        {
            "email_publication_status": "suppressed",
            "phone_publication_status": "suppressed",
        },
    )
    for updates in cases:
        assert not is_publishable_row(deepcopy(_row(**updates)))
