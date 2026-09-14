"""Shared fail-closed checks for explicit free-only finalization artifacts."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import copy
from contextlib import closing
from pathlib import Path


PROVIDERS = ("brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm")
SKIP_REASON = "disabled_by_explicit_free_only_finalization"
FREE_SEARCH_EXECUTION_NAMESPACE = "free_search_execution_v1"
DNS_ADDRESS_NAMESPACE = "dns_address_v1"
TRACE_SCHEMA_VERSION = 1
ACTIVITY_COUNTER_KEYS = (
    "provider_calls",
    "paid_attempts",
    "paid_attempt_calls",
    "provider_query_flights",
    "flight_consumers",
    "paid_budget_blocks",
)

THRESHOLD_FIELDS = (
    "review_score",
    "medium_confidence_score",
    "high_confidence_score",
    "publication_policy_min_safety_score",
    "safe_ok_min_score",
    "min_accept_score",
    "early_stop_score_threshold",
    "max_candidate_score_gap",
    "ambiguous_candidate_margin",
)
THRESHOLD_CONFIG_KEYS = frozenset({
    "HIGH_CONFIDENCE_SCORE",
    "MEDIUM_CONFIDENCE_SCORE",
    "REVIEW_SCORE",
})

USAGE_ACTIVITY_FIELDS = ("reserved_total", "reserved", "completed", "failed", "unknown")
_MISSING = object()


def _strict_zero_activity(value: object) -> bool:
    return (
        type(value) is dict
        and set(value) == set(ACTIVITY_COUNTER_KEYS)
        and all(type(value[key]) is int and value[key] == 0 for key in ACTIVITY_COUNTER_KEYS)
    )


def _offender_key(offender: dict) -> tuple[str, str, str, str]:
    try:
        value = json.dumps(offender.get("value"), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        value = repr(offender.get("value"))
    return (
        str(offender.get("source", "")),
        str(offender.get("provider", "")),
        str(offender.get("field", "")),
        value,
    )


def _dedupe_offenders(*collections: object) -> list[dict]:
    unique: dict[tuple[str, str, str, str], dict] = {}
    for collection in collections:
        if not isinstance(collection, list):
            continue
        for offender in collection:
            if isinstance(offender, dict):
                unique.setdefault(_offender_key(offender), copy.deepcopy(offender))
    return [unique[key] for key in sorted(unique)]


def inspect_budget_config(config_or_manifest: dict) -> dict:
    """Classify the canonical paid budget map without using exception truthiness."""
    config = config_or_manifest.get("run_config", config_or_manifest) if isinstance(config_or_manifest, dict) else None
    if not isinstance(config, dict):
        return {
            "budget_check_completed": False,
            "paid_budget_nonzero": False,
            "budget_offenders": [],
            "config_invalid_details": [{"field": "run_config", "reason": "missing_or_not_object"}],
            "failure_reason": "CONFIG_INVALID",
        }
    budgets = config.get("budgets")
    if not isinstance(budgets, dict):
        return {
            "budget_check_completed": False,
            "paid_budget_nonzero": False,
            "budget_offenders": [],
            "config_invalid_details": [{"field": "budgets", "reason": "missing_or_not_object"}],
            "failure_reason": "CONFIG_INVALID",
        }
    invalid: list[dict] = []
    missing = sorted(set(PROVIDERS) - set(budgets))
    extra = sorted(set(budgets) - set(PROVIDERS))
    invalid.extend({"provider": provider, "reason": "missing_provider"} for provider in missing)
    invalid.extend({"provider": provider, "reason": "unexpected_provider"} for provider in extra)
    for provider in sorted(set(budgets) & set(PROVIDERS)):
        value = budgets[provider]
        if type(value) is not int or value < 0:
            invalid.append({"provider": provider, "value": value, "reason": "invalid_value"})
    offenders = [
        {"provider": provider, "value": int(value), "source": "run_config.budgets"}
        for provider, value in sorted(budgets.items())
        if provider in PROVIDERS and type(value) is int and value > 0
    ]
    return {
        "budget_check_completed": True,
        "paid_budget_nonzero": bool(offenders),
        "budget_offenders": offenders,
        "config_invalid_details": invalid,
        "failure_reason": "CONFIG_INVALID" if invalid else ("PAID_BUDGET_NONZERO" if offenders else ""),
    }


def _config_differences(
    expected: object, actual: object, path: tuple[object, ...] = ("run_config",),
) -> list[tuple[tuple[object, ...], object, object]]:
    """Return exact recursive differences while retaining typed path tokens."""
    if expected is _MISSING or actual is _MISSING:
        return [(path, expected, actual)]
    if type(expected) is not type(actual):
        return [(path, expected, actual)]
    if type(expected) is dict:
        def entries(value: dict) -> dict[tuple[type, str], tuple[object, object]]:
            return {(type(key), repr(key)): (key, item) for key, item in value.items()}

        expected_entries = entries(expected)
        actual_entries = entries(actual)
        result: list[tuple[tuple[object, ...], object, object]] = []
        for key_token in sorted(set(expected_entries) | set(actual_entries), key=repr):
            expected_entry = expected_entries.get(key_token, (_MISSING, _MISSING))
            actual_entry = actual_entries.get(key_token, (_MISSING, _MISSING))
            key = expected_entry[0] if expected_entry[1] is not _MISSING else actual_entry[0]
            result.extend(_config_differences(
                expected_entry[1], actual_entry[1], path + (key,),
            ))
        return result
    if type(expected) in (list, tuple):
        result = []
        for index in range(max(len(expected), len(actual))):
            result.extend(_config_differences(
                expected[index] if index < len(expected) else _MISSING,
                actual[index] if index < len(actual) else _MISSING,
                path + (index,),
            ))
        return result
    return [] if expected == actual else [(path, expected, actual)]


def _render_config_path(path: tuple[object, ...]) -> str:
    rendered = ""
    for index, token in enumerate(path):
        if index == 0:
            rendered = str(token)
        elif type(token) is int:
            rendered += f"[{token}]"
        elif type(token) is str:
            rendered += f".{token}"
        else:
            rendered += f"[{token!r}]"
    return rendered


def _is_threshold_path(path: tuple[object, ...]) -> bool:
    return (
        len(path) == 3
        and path[0] == "run_config"
        and path[1] == "thresholds"
        and type(path[2]) is str
        and path[2] in THRESHOLD_CONFIG_KEYS
    ) or (
        len(path) == 3
        and path[0] == "run_config"
        and path[1] == "effective_settings"
        and type(path[2]) is str
        and path[2] in THRESHOLD_FIELDS
    )


def _config_semantic_details(config: object) -> list[dict]:
    """Report semantic score violations when a complete score section is present."""
    if type(config) is not dict:
        return []
    thresholds = config.get("thresholds")
    settings = config.get("effective_settings")
    if type(thresholds) is not dict or type(settings) is not dict:
        return []
    details: list[dict] = []
    for key, effective_key in (
        ("REVIEW_SCORE", "review_score"),
        ("MEDIUM_CONFIDENCE_SCORE", "medium_confidence_score"),
        ("HIGH_CONFIDENCE_SCORE", "high_confidence_score"),
    ):
        if key not in thresholds or effective_key not in settings:
            continue
        threshold = thresholds[key]
        effective = settings[effective_key]
        if type(threshold) is not int or not 0 <= threshold <= 100:
            details.append({"field": f"run_config.thresholds.{key}", "reason": "score_out_of_range_or_not_int"})
        if type(effective) is not int or not 0 <= effective <= 100:
            details.append({"field": f"run_config.effective_settings.{effective_key}", "reason": "score_out_of_range_or_not_int"})
        if type(threshold) is int and type(effective) is int and threshold != effective:
            details.append({"field": f"run_config.{key}_parity", "reason": "threshold_effective_mismatch"})
    for field in THRESHOLD_FIELDS:
        if field in settings and (type(settings[field]) is not int or not 0 <= settings[field] <= 100):
            details.append({"field": f"run_config.effective_settings.{field}", "reason": "score_out_of_range_or_not_int"})
    return details


def compare_run_configs(expected: dict, actual: dict) -> dict:
    """Compare config recursively, classifying only valid integer threshold drift."""
    def display(value: object) -> object:
        return {"missing": True} if value is _MISSING else copy.deepcopy(value)

    if type(expected) is not dict or type(actual) is not dict:
        config_details = [{
            "field": "run_config",
            "expected": display(expected),
            "actual": display(actual),
        }]
        threshold_details: list[dict] = []
    else:
        threshold_details = []
        config_details = []
        for path, expected_value, actual_value in _config_differences(expected, actual):
            field = _render_config_path(path)
            detail = {"field": field, "expected": display(expected_value), "actual": display(actual_value)}
            if (
                _is_threshold_path(path)
                and type(expected_value) is int
                and type(actual_value) is int
                and 0 <= expected_value <= 100
                and 0 <= actual_value <= 100
            ):
                threshold_details.append(detail)
            else:
                config_details.append(detail)
        threshold_order = {
            "run_config.thresholds": (0, 0),
            **{f"run_config.effective_settings.{field}": (1, index) for index, field in enumerate(THRESHOLD_FIELDS)},
        }
        threshold_details.sort(key=lambda item: threshold_order.get(item["field"], (2, item["field"])))
        config_details.extend(_config_semantic_details(expected))
        config_details.extend(_config_semantic_details(actual))
    return {
        "threshold_mismatch": bool(threshold_details),
        "threshold_mismatch_details": threshold_details,
        "config_mismatch": bool(config_details),
        "config_mismatch_details": config_details,
    }


def classify_replay_receipt(
    *,
    budget_checks: object,
    threshold_info: dict | None,
    database: dict | None,
    run_present: bool,
    pipeline_exit_code: int,
    network_event_count: int,
    config_invalid: bool = False,
    config_invalid_details: list[dict] | None = None,
    config_validation_error: str = "",
    db_validation_error: str = "",
    complete_run_missing: bool | None = None,
    base: dict | None = None,
) -> dict:
    """Derive replay receipt classification from typed, independent observations."""
    threshold_info = threshold_info if type(threshold_info) is dict else {}
    database = database if type(database) is dict else {}

    def _detail_list(value: object) -> bool:
        return type(value) is list and all(type(item) is dict for item in value)

    def _budget_offender_list(value: object) -> tuple[bool, list[dict]]:
        if type(value) is not list:
            return False, []
        valid: list[dict] = []
        well_formed = True
        for offender in value:
            if (
                type(offender) is not dict
                or not set(offender).issubset({"provider", "value", "field", "source"})
                or type(offender.get("provider")) is not str
                or offender.get("provider") not in PROVIDERS
                or type(offender.get("value")) is not int
                or offender.get("value") <= 0
                or ("field" in offender and type(offender.get("field")) is not str)
                or ("source" in offender and type(offender.get("source")) is not str)
            ):
                well_formed = False
                continue
            valid.append(copy.deepcopy(offender))
        return well_formed, valid

    invalid_details: list[dict] = []
    observation_config_invalid = False
    details_input = [] if config_invalid_details is None else config_invalid_details
    if type(details_input) is list and all(type(item) is dict for item in details_input):
        invalid_details.extend(copy.deepcopy(details_input))
    else:
        observation_config_invalid = True
        invalid_details.append({"field": "config_invalid_details", "reason": "not_a_list_of_objects"})

    budget_offender_sources: list[object] = []
    valid_budget_offenders: list[dict] = []
    budget_checks_valid = type(budget_checks) is list and bool(budget_checks)
    budget_checks_well_formed = budget_checks_valid
    budget_observation_invalid = not budget_checks_valid
    checks_to_validate = budget_checks if budget_checks_valid else []
    for check in checks_to_validate:
        if type(check) is not dict:
            budget_checks_well_formed = False
            budget_observation_invalid = True
            continue
        flags_valid = (
            type(check.get("budget_check_completed")) is bool
            and type(check.get("paid_budget_nonzero")) is bool
            and _detail_list(check.get("config_invalid_details"))
        )
        offenders_valid, offenders = _budget_offender_list(check.get("budget_offenders"))
        budget_checks_well_formed = budget_checks_well_formed and flags_valid and offenders_valid
        budget_observation_invalid = budget_observation_invalid or not flags_valid or not offenders_valid
        if flags_valid:
            invalid_details.extend(copy.deepcopy(check["config_invalid_details"]))
            if check["budget_check_completed"] is not True:
                budget_observation_invalid = True
                invalid_details.append({"field": "budget_check_completed", "reason": "must_be_true"})
        budget_offender_sources.append(offenders)
        valid_budget_offenders.extend(offenders)
        if flags_valid and offenders_valid:
            if check["paid_budget_nonzero"] is not (len(offenders) > 0):
                budget_observation_invalid = True
    if budget_observation_invalid:
        invalid_details.append({"field": "budget_observation", "reason": "malformed_or_flag_detail_mismatch"})

    database_shape_invalid = False
    paid_activity = database.get("paid_activity")
    paid_activity_nonzero = database.get("paid_activity_nonzero")
    database_activity = (
        type(paid_activity) is dict
        and set(paid_activity) == set(ACTIVITY_COUNTER_KEYS)
        and all(type(value) is int for value in paid_activity.values())
    )
    database_required = (
        type(database.get("check_completed")) is bool
        and type(database.get("budget_check_completed")) is bool
        and type(paid_activity_nonzero) is bool
        and database_activity
        and type(database.get("paid_provider_calls")) is int
        and type(database.get("paid_budget_nonzero")) is bool
        and type(database.get("database_validation_failed")) is bool
    )
    db_offenders_valid, db_offenders = _budget_offender_list(database.get("budget_offenders"))
    database_shape_invalid = not database_required or not db_offenders_valid
    database_activity_expected = database_activity and (
        any(value != 0 for value in paid_activity.values())
        or type(database.get("paid_provider_calls")) is int and database.get("paid_provider_calls") != 0
    )
    if database_activity and type(database.get("paid_provider_calls")) is int and database.get("paid_provider_calls") < 0:
        database_shape_invalid = True
    if database_activity and any(value < 0 for value in paid_activity.values()):
        database_shape_invalid = True
    if database_activity and paid_activity_nonzero is not database_activity_expected:
        database_shape_invalid = True
    if database_required and database.get("paid_budget_nonzero") is not (len(db_offenders) > 0):
        database_shape_invalid = True
    budget_offender_sources.append(db_offenders)
    valid_budget_offenders.extend(db_offenders)
    budget_offenders = _dedupe_offenders(*budget_offender_sources)
    paid_budget_nonzero = len(valid_budget_offenders) > 0
    if database_required and paid_budget_nonzero is not database.get("paid_budget_nonzero") and db_offenders:
        database_shape_invalid = True

    threshold_shape_valid = (
        type(threshold_info.get("threshold_mismatch")) is bool
        and _detail_list(threshold_info.get("threshold_mismatch_details"))
        and type(threshold_info.get("config_mismatch")) is bool
        and _detail_list(threshold_info.get("config_mismatch_details"))
    )
    threshold_mismatch = threshold_info.get("threshold_mismatch") if threshold_shape_valid else False
    config_mismatch = threshold_info.get("config_mismatch") if threshold_shape_valid else False
    threshold_consistent = threshold_shape_valid and (
        threshold_mismatch is (len(threshold_info["threshold_mismatch_details"]) > 0)
        and config_mismatch is (len(threshold_info["config_mismatch_details"]) > 0)
    )
    semantic_config_invalid = any(
        type(detail) is dict and detail.get("reason") in {
            "threshold_effective_mismatch", "score_out_of_range_or_not_int",
        }
        for detail in threshold_info.get("config_mismatch_details", [])
    ) if threshold_shape_valid else False
    if not threshold_consistent:
        observation_config_invalid = True
        invalid_details.append({"field": "threshold_or_config_observation", "reason": "flag_detail_mismatch"})
    if semantic_config_invalid:
        observation_config_invalid = True
        invalid_details.append({"field": "threshold_semantics", "reason": "invalid"})

    config_error_active = type(config_validation_error) is not str or config_validation_error != ""
    db_error_active = type(db_validation_error) is not str or db_validation_error != ""
    input_config_invalid = type(config_invalid) is not bool or config_invalid is True
    config_invalid = (
        input_config_invalid
        or config_error_active
        or observation_config_invalid
        or budget_observation_invalid
        or len(invalid_details) > 0
    )
    budget_check_completed = (
        budget_checks_well_formed
        and all(check.get("budget_check_completed") is True for check in checks_to_validate if type(check) is dict)
        and database.get("budget_check_completed") is True
    )
    paid_activity_check_completed = database.get("check_completed") is True and not database_shape_invalid
    database_validation_failed = (
        db_error_active
        or database_shape_invalid
        or database.get("database_validation_failed") is True
        or database.get("check_completed") is not True
        or database.get("budget_check_completed") is not True
    )
    base = dict(base or {})
    base_network_events = base.get("network_events", [])
    exception_value = base.get("exception", "")
    exception_active = type(exception_value) is not str or exception_value != ""
    pipeline_failed = type(pipeline_exit_code) is not int or pipeline_exit_code != 0 or exception_active
    network_active = (
        type(network_event_count) is not int
        or network_event_count != 0
        or type(base_network_events) is not list
        or base_network_events != []
    )
    if complete_run_missing is None:
        complete_run_missing = run_present is not True
    else:
        complete_run_missing = complete_run_missing is not False or run_present is not True

    independent_failures = {
        "PAID_BUDGET_NONZERO": paid_budget_nonzero,
        "CONFIG_INVALID": config_invalid,
        "NETWORK_ACTIVITY": network_active,
        "COMPLETE_RUN_MISSING": complete_run_missing,
        "REPLAY_PIPELINE_FAILURE": pipeline_failed,
        "PAID_ACTIVITY_NONZERO": paid_activity_nonzero is True or database_activity_expected,
        "RUN_CONFIG_MISMATCH": config_mismatch,
        "DATABASE_VALIDATION_FAILED": database_validation_failed,
    }
    failure_reasons = [reason for reason, active in independent_failures.items() if active]
    is_threshold_only = not failure_reasons and threshold_mismatch is True
    if is_threshold_only:
        failure_reasons = ["REJECTED_THRESHOLD_CANDIDATE"]
    elif threshold_mismatch is True:
        failure_reasons.append("THRESHOLD_MISMATCH")
    failure = len(failure_reasons) > 0
    if not failure:
        failure_reason = ""
        evidence_class = "REPLAY_PASS"
    elif is_threshold_only:
        failure_reason = "REJECTED_THRESHOLD_CANDIDATE"
        evidence_class = "REJECTED_THRESHOLD_CANDIDATE"
    else:
        failure_reason = failure_reasons[0]
        evidence_class = "REPLAY_FAILURE"
    normalized_pipeline_exit_code = pipeline_exit_code if type(pipeline_exit_code) is int else 1
    final_exit_code = 0 if not failure else (normalized_pipeline_exit_code or 3)
    eligible = (
        not failure
        and budget_check_completed
        and normalized_pipeline_exit_code == 0
        and type(final_exit_code) is int and final_exit_code == 0
        and paid_budget_nonzero is False
        and config_invalid is False
        and network_active is False
        and complete_run_missing is False
        and pipeline_failed is False
        and paid_activity_nonzero is False
        and config_mismatch is False
        and database_validation_failed is False
        and type(database.get("paid_provider_calls")) is int and database.get("paid_provider_calls") == 0
        and _strict_zero_activity(paid_activity)
        and type(base_network_events) is list and base_network_events == []
        and budget_offenders == []
        and invalid_details == []
        and threshold_info.get("threshold_mismatch_details", []) == []
        and threshold_info.get("config_mismatch_details", []) == []
    )
    if eligible:
        failure_reason = ""
        failure_reasons = []
        evidence_class = "REPLAY_PASS"
    else:
        if not failure_reasons:
            failure_reasons = ["DATABASE_VALIDATION_FAILED"] if database_validation_failed else ["CONFIG_INVALID"]
            failure_reason = failure_reasons[0]
            evidence_class = "REPLAY_FAILURE"
        if is_threshold_only and failure_reasons != ["REJECTED_THRESHOLD_CANDIDATE"]:
            is_threshold_only = False
    result = dict(base)
    result.update({
        "status": "PASS" if eligible else "FAIL",
        "exit_code": int(final_exit_code),
        "pipeline_exit_code": int(normalized_pipeline_exit_code),
        "paid_provider_calls": database.get("paid_provider_calls"),
        "paid_budget_nonzero": paid_budget_nonzero is True,
        "budget_check_completed": budget_check_completed is True,
        "budget_offenders": budget_offenders,
        "config_invalid_details": invalid_details,
        "config_invalid": config_invalid is True,
        "network_activity": network_active is True,
        "complete_run_missing": complete_run_missing is True,
        "replay_pipeline_failure": pipeline_failed is True,
        "database_validation_failed": database_validation_failed is True,
        "threshold_mismatch": threshold_mismatch is True,
        "threshold_mismatch_details": threshold_info.get("threshold_mismatch_details", []) if isinstance(threshold_info.get("threshold_mismatch_details", []), list) else [],
        "config_mismatch": config_mismatch is True,
        "config_mismatch_details": threshold_info.get("config_mismatch_details", []) if isinstance(threshold_info.get("config_mismatch_details", []), list) else [],
        "paid_activity": paid_activity,
        "paid_activity_nonzero": paid_activity_nonzero,
        "paid_activity_check_completed": paid_activity_check_completed,
        "failure_reason": failure_reason,
        "failure_reasons": failure_reasons,
        "evidence_class": evidence_class,
        "replay_evidence_eligible": eligible,
    })
    if "network_events" not in result or not isinstance(result.get("network_events"), list):
        result["network_events"] = []
    return result


def _database_observation(path: Path, run_id: str, expected_count: int, *, include_rows: bool = False) -> dict:
    """Read paid counters and provider limits independently of config comparison."""
    try:
        with closing(sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)) as connection:
            phase = connection.execute("SELECT phase FROM runs WHERE run_id=?", (run_id,)).fetchone()
            items = connection.execute(
                "SELECT paid_required,paid_state,paid_attempts FROM run_items WHERE run_id=? ORDER BY item_index",
                (run_id,),
            ).fetchall()
            results = connection.execute("SELECT payload FROM results WHERE run_id=? ORDER BY item_index", (run_id,)).fetchall()
            provider_call_rows = connection.execute(
                "SELECT provider,state FROM provider_calls WHERE run_id=? ORDER BY provider,state",
                (run_id,),
            ).fetchall()
            counters = {
                "provider_calls": connection.execute("SELECT COUNT(*) FROM provider_calls WHERE run_id=?", (run_id,)).fetchone()[0],
                "paid_attempts": connection.execute("SELECT COUNT(*) FROM paid_attempts WHERE run_id=?", (run_id,)).fetchone()[0],
                "paid_attempt_calls": connection.execute("SELECT COUNT(*) FROM paid_attempt_calls WHERE run_id=?", (run_id,)).fetchone()[0],
                "provider_query_flights": connection.execute("SELECT COUNT(*) FROM provider_query_flights WHERE run_id=?", (run_id,)).fetchone()[0],
                "flight_consumers": connection.execute("SELECT COUNT(*) FROM provider_query_flight_consumers WHERE run_id=?", (run_id,)).fetchone()[0],
                "paid_budget_blocks": connection.execute("SELECT COUNT(*) FROM provider_budget_blocks WHERE run_id=? AND provider<>'ddgs'", (run_id,)).fetchone()[0],
            }
            usage_rows = connection.execute(
                "SELECT provider,configured_limit,effective_limit,reserved_total,reserved,completed,failed,unknown FROM provider_usage WHERE run_id=? ORDER BY provider",
                (run_id,),
            ).fetchall()
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return {
            "check_completed": False,
            "budget_check_completed": False,
            "paid_activity": None,
            "paid_activity_nonzero": None,
            "paid_provider_calls": None,
            "budget_offenders": [],
            "paid_budget_nonzero": False,
            "database_validation_failed": True,
            "error": f"free_only_database_invalid:{type(exc).__name__}",
        }

    usage = {}
    usage_activity_nonzero = False
    budget_offenders = []
    database_structure_invalid = False
    provider_call_counts: dict[str, dict[str, int]] = {}
    for provider, state in provider_call_rows:
        if type(provider) is not str or type(state) is not str or provider not in PROVIDERS or state not in {"RESERVED", "RUNNING", "DONE", "FAILED", "UNKNOWN"}:
            database_structure_invalid = True
            continue
        states = provider_call_counts.setdefault(provider, {})
        states[state] = states.get(state, 0) + 1
    try:
        for provider, configured, effective, reserved_total, reserved, completed, failed, unknown in usage_rows:
            raw_values = (configured, effective, reserved_total, reserved, completed, failed, unknown)
            if type(provider) is not str or any(type(value) is not int for value in raw_values):
                database_structure_invalid = True
                continue
            values = {
                "configured_limit": configured, "effective_limit": effective,
                "reserved_total": reserved_total,
                "reserved": reserved, "completed": completed,
                "failed": failed, "unknown": unknown,
            }
            usage[str(provider)] = values
            if str(provider) in PROVIDERS:
                if (
                    configured < 0 or effective < 0 or reserved_total < 0
                    or reserved < 0 or completed < 0 or failed < 0 or unknown < 0
                    or effective > configured or reserved_total > effective
                ):
                    database_structure_invalid = True
                for field in ("configured_limit", "effective_limit"):
                    if values[field] > 0:
                        budget_offenders.append({
                            "provider": str(provider), "field": field,
                            "value": values[field], "source": "db.provider_usage",
                        })
                usage_activity_nonzero = usage_activity_nonzero or any(
                    values[field] > 0 for field in USAGE_ACTIVITY_FIELDS
                )
    except (TypeError, ValueError) as exc:
        return {
            "check_completed": False,
            "budget_check_completed": False,
            "paid_activity": {key: int(value) for key, value in counters.items()},
            "paid_activity_nonzero": None,
            "paid_provider_calls": int(counters["provider_calls"]),
            "budget_offenders": [],
            "paid_budget_nonzero": False,
            "database_validation_failed": True,
            "error": f"free_only_database_invalid:{type(exc).__name__}",
        }
    if len(usage_rows) != len(PROVIDERS) or set(usage) != set(PROVIDERS):
        database_structure_invalid = True
    for provider in PROVIDERS:
        values = usage.get(provider)
        if values is None:
            continue
        counts = provider_call_counts.get(provider, {})
        expected = {
            "reserved_total": sum(counts.get(state, 0) for state in ("RESERVED", "RUNNING", "DONE", "FAILED", "UNKNOWN")),
            "reserved": counts.get("RESERVED", 0) + counts.get("RUNNING", 0),
            "completed": counts.get("DONE", 0),
            "failed": counts.get("FAILED", 0),
            "unknown": counts.get("UNKNOWN", 0),
        }
        if any(values[field] != expected[field] for field in USAGE_ACTIVITY_FIELDS):
            database_structure_invalid = True
    item_status_violation_count = 0
    for required, state, attempts in items:
        if type(required) is not int or type(attempts) is not int:
            database_structure_invalid = True
            item_status_violation_count += 1
            continue
        item_status_violation_count += int(required) != 0 or str(state) != "NOT_REQUIRED" or int(attempts) != 0
    result_status_violation_count = 0
    for (payload_text,) in results:
        try:
            payload = json.loads(str(payload_text))
        except (TypeError, json.JSONDecodeError):
            result_status_violation_count += 1
            continue
        if not isinstance(payload, dict):
            result_status_violation_count += 1
            continue
        recommended = payload.get("paid_recommended") is True
        if payload.get("paid_required") is not False or payload.get("paid_state") != "NOT_REQUIRED":
            result_status_violation_count += 1
        elif not isinstance(payload.get("paid_recommended"), bool):
            result_status_violation_count += 1
        elif recommended and payload.get("paid_skipped_reason") != SKIP_REASON:
            result_status_violation_count += 1
    complete_shape = (
        phase is not None and str(phase[0]) == "COMPLETE"
        and len(items) == int(expected_count) and len(results) == int(expected_count)
        and len(usage_rows) == len(PROVIDERS)
        and set(usage) == set(PROVIDERS)
    )
    database_validation_failed = bool(
        database_structure_invalid
        or not complete_shape
        or item_status_violation_count
        or result_status_violation_count
    )
    check_completed = complete_shape and not database_validation_failed
    observation = {
        "check_completed": check_completed,
        "budget_check_completed": check_completed,
        "paid_activity": {key: int(value) for key, value in counters.items()},
        "paid_activity_nonzero": bool(usage_activity_nonzero or any(int(value) != 0 for value in counters.values())),
        "paid_budget_nonzero": bool(budget_offenders),
        "paid_provider_calls": int(counters["provider_calls"]),
        "budget_offenders": budget_offenders,
        "provider_usage": usage,
        "phase": str(phase[0]) if phase else None,
        "item_count": len(items),
        "result_count": len(results),
        "expected_count": int(expected_count),
        "item_status_violation_count": int(item_status_violation_count),
        "result_status_violation_count": int(result_status_violation_count),
        "database_validation_failed": database_validation_failed,
    }
    if include_rows:
        observation["items"] = items
        observation["results"] = results
    return observation


def inspect_database(path: Path, run_id: str, expected_count: int) -> dict:
    """Public read-only DB observation used by replay classification and migration."""
    return _database_observation(path, run_id, expected_count)


def validate_manifest(manifest: dict, *, require_complete: bool = True) -> dict:
    config = manifest.get("run_config")
    if not isinstance(config, dict):
        raise ValueError("free_only_run_config_missing")
    if config.get("finalize_without_paid") is not True:
        raise ValueError("free_only_finalization_not_enabled")
    if (
        config.get("paid_enabled") is not False
        or manifest.get("paid_enabled") is True
        or ("paid_enabled" in manifest and manifest.get("paid_enabled") is not False)
        or ("finalize_without_paid" in manifest and manifest.get("finalize_without_paid") is not True)
    ):
        raise ValueError("free_only_paid_enabled")
    _validate_config_semantics(config)
    budget = inspect_budget_config(config)
    if budget["failure_reason"] == "CONFIG_INVALID":
        raise ValueError("free_only_budget_config_invalid")
    if budget["paid_budget_nonzero"]:
        raise ValueError("free_only_paid_budget_nonzero")
    if require_complete and (
        manifest.get("complete") is not True
        or manifest.get("phase") != "COMPLETE"
        or manifest.get("finalized") is not True
        or manifest.get("status") != "complete_free_only"
    ):
        raise ValueError("free_only_manifest_not_complete")
    return config


def expected_offline_run_config(live_config: dict) -> dict:
    """Return the only permitted cache-mode transformation for replay."""
    if not isinstance(live_config, dict):
        raise ValueError("offline_config_source_invalid")
    expected = copy.deepcopy(live_config)
    if expected.get("search_cache_mode") != "refresh" or expected.get("crawl_cache_mode") != "refresh":
        raise ValueError("offline_config_source_modes_invalid")
    settings = expected.get("effective_settings")
    if not isinstance(settings, dict):
        raise ValueError("offline_config_effective_settings_invalid")
    expected["search_cache_mode"] = "replay"
    expected["crawl_cache_mode"] = "replay"
    settings["search_cache_mode"] = "replay"
    settings["crawl_cache_mode"] = "replay"
    return expected


def _validate_config_semantics(config: dict) -> None:
    """Reject typed-but-self-consistent configs with contradictory score semantics."""
    thresholds = config.get("thresholds")
    settings = config.get("effective_settings")
    if type(thresholds) is not dict or type(settings) is not dict:
        raise ValueError("free_only_config_semantic_invalid")
    if set(thresholds) != THRESHOLD_CONFIG_KEYS:
        raise ValueError("free_only_config_semantic_invalid")
    for key, effective_key in (
        ("REVIEW_SCORE", "review_score"),
        ("MEDIUM_CONFIDENCE_SCORE", "medium_confidence_score"),
        ("HIGH_CONFIDENCE_SCORE", "high_confidence_score"),
    ):
        threshold = thresholds.get(key)
        effective = settings.get(effective_key)
        if (
            type(threshold) is not int
            or type(effective) is not int
            or not 0 <= threshold <= 100
            or not 0 <= effective <= 100
            or threshold != effective
        ):
            raise ValueError("free_only_config_semantic_invalid")
    for field in THRESHOLD_FIELDS:
        value = settings.get(field)
        if type(value) is not int or not 0 <= value <= 100:
            raise ValueError("free_only_config_semantic_invalid")


def validate_manifest_config_sha(manifest: dict) -> None:
    """Verify a manifest's own RunConfig hash, without comparing run modes."""
    from modules import run_context

    config = manifest.get("run_config")
    recorded = manifest.get("config_sha256")
    if type(config) is not dict or type(recorded) is not str or not recorded:
        raise ValueError("free_only_config_sha_mismatch")
    try:
        reconstructed = run_context.RunConfig.from_dict(config)
        if _config_differences(config, reconstructed.as_dict()):
            raise ValueError("free_only_config_sha_mismatch")
        expected_types = {"config_schema_version": int}
        for name, canonical_name, kind in run_context.SEMANTIC_CONFIG_REGISTRY:
            if hasattr(__import__("config"), name):
                expected_types[canonical_name] = {
                    "bool": bool, "int": int, "float": float, "str": str,
                    "json": list,
                }[kind]
        expected_types.update({
            f"capability_{name}": bool
            for name in run_context.runtime_capability_profile()
        })
        settings = config.get("effective_settings")
        if (
            type(settings) is not dict
            or set(settings) != set(expected_types)
            or any(type(value) is not expected_types.get(key) for key, value in settings.items())
        ):
            raise ValueError("free_only_config_sha_mismatch")
        _validate_config_semantics(config)
        if reconstructed.sha256 != recorded:
            raise ValueError("free_only_config_sha_mismatch")
    except ValueError as exc:
        if str(exc) in {"free_only_config_sha_mismatch", "free_only_config_semantic_invalid"}:
            raise
        raise ValueError("free_only_config_sha_mismatch")
    except Exception as exc:
        raise ValueError("free_only_config_sha_mismatch") from exc


def validate_database(path: Path, run_id: str, expected_count: int) -> dict[str, int]:
    observed = _database_observation(path, run_id, expected_count, include_rows=True)
    if observed.get("error"):
        raise ValueError(observed["error"])
    phase = observed.get("phase")
    items = observed.get("items", [])
    results = observed.get("results", [])
    counters = observed.get("paid_activity") or {}
    usage = observed.get("provider_usage", {})
    if phase != "COMPLETE":
        raise ValueError("free_only_database_not_complete")
    if len(items) != int(expected_count) or len(results) != int(expected_count):
        raise ValueError("free_only_database_population_mismatch")
    if any(int(required) != 0 or str(state) != "NOT_REQUIRED" or int(attempts) != 0 for required, state, attempts in items):
        raise ValueError("free_only_paid_item_state_invalid")
    if any(int(value) != 0 for value in counters.values()):
        raise ValueError(f"free_only_paid_activity_nonzero:{counters}")
    if any(
        any(int(row.get(field, 0)) != 0 for field in ("configured_limit", "effective_limit", "reserved_total", "reserved", "completed", "failed", "unknown"))
        for row in usage.values()
    ) or len(usage) != len(PROVIDERS) or set(usage) != set(PROVIDERS):
        raise ValueError("free_only_provider_usage_nonzero_or_incomplete")
    for (payload_text,) in results:
        try:
            payload = json.loads(str(payload_text))
        except json.JSONDecodeError as exc:
            raise ValueError("free_only_result_payload_invalid") from exc
        recommended = payload.get("paid_recommended") is True
        if payload.get("paid_required") is not False or payload.get("paid_state") != "NOT_REQUIRED":
            raise ValueError("free_only_result_state_invalid")
        if not isinstance(payload.get("paid_recommended"), bool):
            raise ValueError("free_only_paid_recommendation_missing")
        if recommended and payload.get("paid_skipped_reason") != SKIP_REASON:
            raise ValueError("free_only_paid_skip_reason_invalid")
    return {key: int(value) for key, value in counters.items()}


def validate_behavioral_replay(path: Path, run_id: str, ordered_source_record_ids: list[str]) -> dict[str, int]:
    """Validate typed free-search/DNS replay closure against the live ledger."""
    try:
        with closing(sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)) as connection:
            item_rows = connection.execute(
                "SELECT item_index,source_record_id FROM run_items WHERE run_id=? ORDER BY item_index",
                (run_id,),
            ).fetchall()
            trace_rows = connection.execute(
                "SELECT store,namespace,key_sha256,schema_version,value_json FROM replay_entries WHERE run_id=? AND namespace IN (?,?) ORDER BY namespace,key_sha256",
                (run_id, "free_search_execution_v1", "dns_address_v1"),
            ).fetchall()
            attempt_rows = connection.execute(
                "SELECT item_index,bucket,provider,query_fingerprint,attempt_ordinal,state,error_class FROM free_provider_attempts WHERE run_id=? ORDER BY item_index,bucket,provider,query_fingerprint,attempt_ordinal",
                (run_id,),
            ).fetchall()
            usage_rows = connection.execute(
                "SELECT item_index,discovery_logical_used,targeted_logical_used,discovery_physical_used,targeted_physical_used,logical_blocked,physical_blocked FROM free_query_usage WHERE run_id=? ORDER BY item_index",
                (run_id,),
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        raise ValueError(f"behavioral_replay_database_invalid:{type(exc).__name__}") from exc

    id_to_index = {str(source_id): int(index) for index, source_id in item_rows}
    if len(item_rows) != len(ordered_source_record_ids) or [str(row[1]) for row in item_rows] != list(ordered_source_record_ids):
        raise ValueError("behavioral_replay_source_record_id_order_invalid")
    expected_attempts: list[tuple] = []
    logical_counts: dict[tuple[int, str], int] = {}
    blocked_logical_by_item: dict[int, int] = {}
    blocked_physical_by_item: dict[int, int] = {}
    dns_count = 0
    seen_trace_keys: set[str] = set()
    for store, namespace, key_sha, schema_version, value_json in trace_rows:
        if store != "replay" or int(schema_version) != TRACE_SCHEMA_VERSION:
            raise ValueError("behavioral_replay_namespace_invalid")
        try:
            value = json.loads(value_json)
        except json.JSONDecodeError as exc:
            raise ValueError("behavioral_replay_record_invalid_json") from exc
        if not isinstance(value, dict):
            raise ValueError("behavioral_replay_record_not_object")
        if namespace == FREE_SEARCH_EXECUTION_NAMESPACE:
            source_id = str(value.get("source_record_id") or "")
            bucket = str(value.get("bucket") or "")
            query_fingerprint = str(value.get("query_fingerprint") or "")
            if (
                not source_id or source_id not in id_to_index
                or bucket not in {"discovery", "targeted"}
                or len(query_fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in query_fingerprint)
                or value.get("provider") != "ddgs"
            ):
                raise ValueError("behavioral_replay_execution_identity_invalid")
            expected_key = hashlib.sha256(f"{source_id}\0{bucket}\0{query_fingerprint}".encode("utf-8")).hexdigest()
            if expected_key != str(key_sha):
                raise ValueError("behavioral_replay_execution_key_mismatch")
            if expected_key in seen_trace_keys:
                raise ValueError("behavioral_replay_execution_duplicate")
            seen_trace_keys.add(expected_key)
            logical = value.get("logical_reservation")
            attempts = value.get("backend_attempts")
            result = value.get("result")
            if not isinstance(logical, dict) or not isinstance(attempts, list) or not isinstance(result, dict):
                raise ValueError("behavioral_replay_execution_shape_invalid")
            logical_result = str(logical.get("result") or "")
            if logical_result not in {"ACCEPTED", "BLOCKED"}:
                raise ValueError("behavioral_replay_logical_result_invalid")
            if logical_result == "ACCEPTED":
                logical_counts[(id_to_index[source_id], bucket)] = logical_counts.get((id_to_index[source_id], bucket), 0) + 1
            else:
                item_index = id_to_index[source_id]
                blocked_logical_by_item[item_index] = blocked_logical_by_item.get(item_index, 0) + 1
            for attempt in attempts:
                if not isinstance(attempt, dict) or not attempt.get("backend"):
                    raise ValueError("behavioral_replay_backend_attempt_invalid")
                reservation = str(attempt.get("result") or "")
                if reservation not in {"ACCEPTED", "BLOCKED"}:
                    raise ValueError("behavioral_replay_backend_reservation_invalid")
                if reservation == "BLOCKED":
                    if attempt.get("outcome") != "BLOCKED":
                        raise ValueError("behavioral_replay_blocked_attempt_outcome_invalid")
                    item_index = id_to_index[source_id]
                    blocked_physical_by_item[item_index] = blocked_physical_by_item.get(item_index, 0) + 1
                    continue
                outcome = str(attempt.get("outcome") or "")
                if outcome not in {"DONE", "FAILED"}:
                    raise ValueError("behavioral_replay_attempt_outcome_invalid")
                expected_attempts.append((
                    id_to_index[source_id], bucket, str(attempt["backend"]).casefold(),
                    query_fingerprint, int(attempt.get("attempt_ordinal", 0)),
                    outcome, str(attempt.get("error_class") or ""),
                ))
        elif namespace == DNS_ADDRESS_NAMESPACE:
            source_id = str(value.get("source_record_id") or "")
            domain = str(value.get("domain") or "")
            if (
                not source_id or source_id not in id_to_index
                or not domain or domain != domain.casefold()
                or not isinstance(value.get("has_address"), bool)
                or value.get("schema_version") != TRACE_SCHEMA_VERSION
            ):
                raise ValueError("behavioral_replay_dns_record_invalid")
            expected_key = hashlib.sha256(f"{source_id}\0{domain}".encode("utf-8")).hexdigest()
            if expected_key != str(key_sha):
                raise ValueError("behavioral_replay_dns_key_mismatch")
            dns_count += 1
    actual_attempts = [
        (int(item_index), str(bucket), str(provider), str(query_fingerprint), int(attempt_ordinal), str(state), str(error_class or ""))
        for item_index, bucket, provider, query_fingerprint, attempt_ordinal, state, error_class in attempt_rows
    ]
    if sorted(expected_attempts) != actual_attempts:
        raise ValueError("behavioral_replay_backend_trace_ledger_mismatch")
    usage_map = {int(row[0]): row for row in usage_rows}
    if set(usage_map) != set(range(len(ordered_source_record_ids))):
        raise ValueError("behavioral_replay_usage_population_invalid")
    for item_index, row in usage_map.items():
        if logical_counts.get((item_index, "discovery"), 0) != int(row[1]) or logical_counts.get((item_index, "targeted"), 0) != int(row[2]):
            raise ValueError("behavioral_replay_logical_trace_ledger_mismatch")
        if sum(1 for attempt in expected_attempts if attempt[0] == item_index and attempt[1] == "discovery") != int(row[3]) or sum(1 for attempt in expected_attempts if attempt[0] == item_index and attempt[1] == "targeted") != int(row[4]):
            raise ValueError("behavioral_replay_physical_trace_ledger_mismatch")
        if (
            blocked_logical_by_item.get(item_index, 0) != int(row[5])
            or blocked_physical_by_item.get(item_index, 0) != int(row[6])
        ):
            raise ValueError("behavioral_replay_block_counter_mismatch")
    return {
        "free_search_execution_count": len(seen_trace_keys),
        "free_search_attempt_count": len(expected_attempts),
        "dns_address_count": dns_count,
        "blocked_logical_count": sum(blocked_logical_by_item.values()),
        "blocked_physical_count": sum(blocked_physical_by_item.values()),
    }
