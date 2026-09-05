from __future__ import annotations

from modules import publication_policy


def frozen_row(row: dict, source_id: str, *, publishable: bool | None = None) -> dict:
    row["source_record_id"] = source_id
    requested = row.get("publication_eligible")
    decision = publication_policy.freeze_publication_decision(row, row.get("__evaluation", {}))
    desired = publishable if publishable is not None else requested
    if desired is not None and bool(desired) != bool(decision["publishable"]):
        if desired:
            raise AssertionError(f"fixture policy unexpectedly withheld {source_id}")
        decision = publication_policy.with_blocker(decision, "fixture_forced_review")
    row["publication_decision"] = decision
    row["publication_eligible"] = decision["publishable"]
    row["publication_blockers"] = "; ".join(decision["blockers"])
    return publication_policy.apply_frozen_decision_fields(row, decision)
