"""Validate Dev/Validation/Blind benchmark isolation and optional run outputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
from pathlib import Path
from urllib.parse import urlparse

import phonenumbers

from modules import excel, scorer
from modules import phone as phone_normalizer
from validate_golden_xlsx import FIELDS, _sheet_rows, assertion_coverage, evaluate, readiness_issues


BASE_DIR = Path(__file__).resolve().parent
INVALID_PACKAGE_REGISTRY = BASE_DIR / "data" / "invalid_benchmark_packages.json"
EXPECTED_PRIVATE_SEEN_UNIQUE_COMPANIES = 71
SOURCE_REVIEW_FIELDS = (
    "source_record_id", "Company", "source", "official_profile_url",
    "listed_legal_name", "listed_address", "listed_phone", "expected_website",
    "website_verified", "expected_email", "email_verified", "expected_phone",
    "phone_verified", "expected_publication", "identity_evidence_urls",
    "contact_evidence_urls", "observed_at", "evidence_content_sha256",
    "reviewer_pass_1", "reviewer_pass_2", "disagreement_reason", "label_status",
)
SOURCE_REVIEW_V3_FIELDS = SOURCE_REVIEW_FIELDS + (
    "source_listed_website", "source_listed_website_status",
    "expected_website_domains_json", "expected_emails_json", "expected_phones_e164_json",
    "expected_publication_reason_codes_json",
    "website_field_evidence_json", "email_field_evidence_json", "phone_field_evidence_json",
    "reviewer_provenance_json",
)
_STATES = {"present", "absent", "unknown"}
_PUBLICATION_STATES = {"publishable", "abstain", "unknown"}
_SOURCE_LISTED_STATES = {"present", "absent", "rejected"}
REQUIRED_ACCEPTANCE = {
    "max_false_publication": 0,
    "max_published_unknown_identity": 0,
    "require_full_source_id_coverage": True,
    "require_all_rows_review_frozen": True,
    "require_nonzero_known_identity_denominator": True,
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_INVALID_REASON_CODES = {
    "actual_input_profile_url_misclassified_as_company_website",
    "ground_truth_non_company_links_labeled_as_website",
    "review_passes_not_independent",
    "actual_not_finalized",
    "package_integrity_not_verified",
    "review_passes_same_decision_implementation",
    "review_passes_same_tool_hash",
    "website_label_web_and_colon_not_parsed",
    "schemeless_official_homepage_rejected",
    "source_listed_field_conflated_with_verified_ground_truth",
    "single_contact_value_rejected_valid_alternative",
    "crawler_budget_starved_later_records",
}


def _validate_invalid_registry(registry_path: Path = INVALID_PACKAGE_REGISTRY) -> list[dict]:
    """Load and validate the deny-list before any benchmark quality work starts."""
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid benchmark registry cannot be read: {exc}") from exc
    if type(payload) is not dict or payload.get("schema_version") not in {1, 2}:
        raise ValueError("invalid benchmark registry schema_version")
    packages = payload.get("packages")
    if type(packages) is not list:
        raise ValueError("invalid benchmark registry packages")
    seen: set[str] = set()
    seen_artifacts: set[str] = set()
    seen_artifact_hashes: set[str] = set()
    validated: list[dict] = []
    for entry in packages:
        if type(entry) is not dict:
            raise ValueError("invalid benchmark registry entry")
        package_id = str(entry.get("package_id") or "")
        package_hash = str(entry.get("package_id_sha256") or "")
        manifest_hash = str(entry.get("benchmark_manifest_file_sha256") or "")
        expected = entry.get("expected_sha256")
        reasons = entry.get("invalid_reason_codes")
        if not _SHA256_RE.fullmatch(package_hash) or not _SHA256_RE.fullmatch(manifest_hash):
            raise ValueError(f"invalid benchmark registry SHA-256 for {package_id}")
        if package_id in seen:
            raise ValueError(f"duplicate invalid benchmark package ID: {package_id}")
        if not package_id.endswith(package_hash) or type(expected) is not list or not expected:
            raise ValueError(f"malformed invalid benchmark package entry: {package_id}")
        if any(not isinstance(value, str) or not _SHA256_RE.fullmatch(value) for value in expected):
            raise ValueError(f"malformed expected SHA-256 for {package_id}")
        if type(reasons) is not list or not reasons or any(value not in _INVALID_REASON_CODES for value in reasons):
            raise ValueError(f"unknown invalidation reason for {package_id}")
        seen.add(package_id)
        validated.append(entry)
    if payload.get("schema_version") == 2:
        artifacts = payload.get("artifacts")
        if type(artifacts) is not list:
            raise ValueError("invalid benchmark registry artifacts")
        for artifact in artifacts:
            if type(artifact) is not dict:
                raise ValueError("invalid benchmark registry artifact entry")
            artifact_id = str(artifact.get("artifact_id") or "")
            hashes = artifact.get("artifact_sha256")
            reasons = artifact.get("invalid_reason_codes")
            if not artifact_id or artifact_id in seen_artifacts:
                raise ValueError(f"duplicate invalid benchmark artifact: {artifact_id}")
            if type(hashes) is not dict or not hashes:
                raise ValueError(f"malformed invalid benchmark artifact: {artifact_id}")
            if any(
                not isinstance(key, str)
                or not key
                or not isinstance(value, str)
                or not _SHA256_RE.fullmatch(value)
                for key, value in hashes.items()
            ):
                raise ValueError(f"malformed artifact SHA-256 for {artifact_id}")
            duplicate_hashes = set(hashes.values()) & seen_artifact_hashes
            if duplicate_hashes:
                raise ValueError(f"duplicate invalid benchmark artifact hash: {artifact_id}")
            if type(reasons) is not list or not reasons or any(
                value not in _INVALID_REASON_CODES for value in reasons
            ):
                raise ValueError(f"unknown artifact invalidation reason: {artifact_id}")
            seen_artifacts.add(artifact_id)
            seen_artifact_hashes.update(hashes.values())
            validated.append({"artifact_id": artifact_id, **artifact})
    return validated


def _sha256_strings(value: object) -> set[str]:
    """Collect declared hashes from a manifest without trusting field names."""
    found: set[str] = set()
    if isinstance(value, dict):
        for nested in value.values():
            found.update(_sha256_strings(nested))
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found.update(_sha256_strings(nested))
    elif isinstance(value, str) and _SHA256_RE.fullmatch(value):
        found.add(value)
    return found


def _invalid_package_issues(
    manifest_path: Path,
    manifest_payload: dict,
    registry_path: Path | None = None,
) -> list[str]:
    entries = _validate_invalid_registry(registry_path or INVALID_PACKAGE_REGISTRY)
    path_name = manifest_path.resolve().parent.name
    package_id = str(manifest_payload.get("package_id_sha256") or manifest_payload.get("manifest_sha256") or "")
    expected_hashes = {
        str(item.get("expected_sha256") or "")
        for item in manifest_payload.get("sets", [])
        if isinstance(item, dict)
    }
    manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest() if manifest_path.is_file() else ""
    declared_hashes = _sha256_strings(manifest_payload)
    if manifest_hash:
        declared_hashes.add(manifest_hash)
    issues: list[str] = []
    for entry in entries:
        if "artifact_id" in entry:
            entry_id = str(entry["artifact_id"])
            matched = bool(declared_hashes & set((entry.get("artifact_sha256") or {}).values()))
        else:
            entry_id = str(entry["package_id"])
            entry_hash = str(entry["package_id_sha256"])
            matched = path_name == entry_id or package_id in {entry_id, entry_hash} or bool(
                expected_hashes & set(entry["expected_sha256"])
            ) or bool(declared_hashes & {str(value) for value in entry["expected_sha256"]})
        if matched:
            issues.append(f"invalid benchmark package registry match: {entry_id}")
    return issues


def _companies(path: Path) -> set[str]:
    return {
        scorer.normalize_text(str(row.get("Company") or "")).strip()
        for row in _sheet_rows(path, "Manual Report")
        if str(row.get("Company") or "").strip()
    }


def resolve_item_paths(item: dict, manifest_path: Path) -> dict[str, Path | None]:
    """Resolve v1/v2 manifest paths once for every validator loop."""
    manifest_path = Path(manifest_path).resolve()
    schema_version = int(item.get("schema_version", 1) or 1)
    if schema_version >= 2 or item.get("manifest_schema_version", 1) >= 2:
        base = manifest_path.parent
    else:
        base = manifest_path.parent.parent if manifest_path.name == "benchmark_splits.json" else BASE_DIR
    expected = item.get("expected")
    actual = item.get("actual")
    return {
        "expected": (base / str(expected)).resolve() if expected else None,
        "actual": (base / str(actual)).resolve() if actual else None,
    }


def _source_rows(path: Path) -> list[dict]:
    rows = _sheet_rows(Path(path))
    if not rows:
        raise ValueError("workbook has no rows")
    headers = {str(key).strip().casefold() for key in rows[0]}
    required = {value.casefold() for value in SOURCE_REVIEW_FIELDS}
    missing = required - headers
    if missing:
        raise ValueError(f"source-reviewed workbook missing fields: {sorted(missing)}")
    return rows


def _json_array_cell(row: dict, field: str, *, sort_key=None) -> list[str]:
    raw = row.get(field, "")
    if isinstance(raw, list):
        values = raw
    else:
        try:
            values = json.loads(str(raw or ""))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{field} must be a JSON array") from exc
    if type(values) is not list or any(type(value) is not str or not value for value in values):
        raise ValueError(f"{field} must be a non-null string array")
    if len(values) != len(set(values)):
        raise ValueError(f"{field} contains duplicate values")
    key = sort_key or (lambda value: value.casefold())
    if values != sorted(values, key=key):
        raise ValueError(f"{field} is not canonically ordered")
    if isinstance(raw, str) and json.dumps(values, ensure_ascii=False, separators=(",", ":")) != raw:
        raise ValueError(f"{field} is not canonical JSON")
    return values


def _evidence_array_cell(row: dict, field: str) -> list[dict]:
    raw = row.get(field, "")
    try:
        values = raw if isinstance(raw, list) else json.loads(str(raw or ""))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} must be a JSON array") from exc
    if type(values) is not list or any(type(value) is not dict for value in values):
        raise ValueError(f"{field} must be an array of objects")
    return values


def _validate_v3_ground_truth(rows: list[dict], label: str) -> None:
    required = {value.casefold() for value in SOURCE_REVIEW_V3_FIELDS}
    headers = {str(key).strip().casefold() for key in rows[0]} if rows else set()
    missing = required - headers
    if missing:
        raise ValueError(f"{label}: v3 workbook missing fields: {sorted(missing)}")
    for row in rows:
        source_id = _source_id_key(row)
        if not source_id:
            raise ValueError(f"{label}: missing source_record_id")
        for field in ("website_field_evidence_json", "email_field_evidence_json", "phone_field_evidence_json", "reviewer_provenance_json"):
            if not _evidence_array_cell(row, field):
                raise ValueError(f"{label}: {field} is empty: {source_id}")
        listed_status = str(row.get("source_listed_website_status", "") or "").strip().casefold()
        if listed_status not in _SOURCE_LISTED_STATES:
            raise ValueError(f"{label}: invalid source_listed_website_status: {source_id}")
        domains = _json_array_cell(row, "expected_website_domains_json")
        for domain in domains:
            if scorer.registrable_domain(domain) != domain.casefold() or "://" in domain or "/" in domain:
                raise ValueError(f"{label}: expected website domain is not canonical: {source_id}")
        emails = _json_array_cell(row, "expected_emails_json")
        if len({value.casefold() for value in emails}) != len(emails):
            raise ValueError(f"{label}: expected email set contains duplicate values: {source_id}")
        if any(value != value.casefold() or not re.fullmatch(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", value) for value in emails):
            raise ValueError(f"{label}: expected email set is not canonical: {source_id}")
        phones = _json_array_cell(row, "expected_phones_e164_json", sort_key=lambda value: value)
        for value in phones:
            if not re.fullmatch(r"\+[1-9][0-9]{6,14}", value) or phone_normalizer.normalize_phone(value, default_country="TR") == "":
                raise ValueError(f"{label}: invalid E.164 phone: {source_id}")
        reasons = _json_array_cell(row, "expected_publication_reason_codes_json")
        compatibility_values = (
            ("expected_website", domains, lambda value: scorer.registrable_domain(value)),
            ("expected_email", emails, lambda value: value.casefold()),
            ("expected_phone", phones, lambda value: value.strip()),
        )
        for field, values, canonicalize in compatibility_values:
            compatibility = str(row.get(field, "") or "").strip()
            if compatibility and (not values or canonicalize(compatibility) != values[0]):
                raise ValueError(f"{label}: compatibility field does not project first canonical value: {field}:{source_id}")
        website_state = _state(row, "website_verified")
        email_state = _state(row, "email_verified")
        phone_state = _state(row, "phone_verified")
        publication = _expected_publication(row)
        if not website_state or not email_state or not phone_state or not publication:
            raise ValueError(f"{label}: invalid field/publication state: {source_id}")
        for state, values, field in (
            (website_state, domains, "expected_website_domains_json"),
            (email_state, emails, "expected_emails_json"),
            (phone_state, phones, "expected_phones_e164_json"),
        ):
            if state == "present" and not values:
                raise ValueError(f"{label}: present field has empty values: {field}:{source_id}")
            if state == "absent" and values:
                raise ValueError(f"{label}: absent field has populated values: {field}:{source_id}")
        if publication == "publishable" and not domains:
            raise ValueError(f"{label}: publishable row has no expected domain: {source_id}")
        if publication == "publishable" and not (emails or phones):
            raise ValueError(f"{label}: publishable row has no verified contact: {source_id}")
        if publication == "abstain" and not reasons:
            raise ValueError(f"{label}: abstain row has no reason code: {source_id}")


def _source_id_key(row: dict) -> str:
    return str(row.get("source_record_id") or row.get("Source_Record_ID") or "").strip()


def _validated_id_map(rows: list[dict], label: str) -> dict[str, dict]:
    result: dict[str, dict] = {}
    missing = 0
    for row in rows:
        source_id = _source_id_key(row)
        if not source_id:
            missing += 1
            continue
        if source_id in result:
            raise ValueError(f"{label}: duplicate source_record_id: {source_id}")
        result[source_id] = row
    if missing:
        raise ValueError(f"{label}: {missing} rows have missing source_record_id")
    return result


def _state(row: dict, field: str) -> str:
    value = str(row.get(field, "") or "").strip().casefold()
    return value if value in _STATES else ""


def _host(value: object) -> str:
    from urllib.parse import urlparse
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return (parsed.hostname or "").casefold().removeprefix("www.")


def _emails(value: object) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        values = [str(item) for item in value]
    else:
        raw = str(value or "")
        try:
            parsed = json.loads(raw)
            values = [str(item) for item in parsed] if isinstance(parsed, list) else [raw]
        except json.JSONDecodeError:
            values = re.split(r"[;\n,]+", raw)
    return set(re.findall(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", " ".join(values).casefold()))


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _phones(value: object) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        values = [str(item) for item in value]
    else:
        raw = str(value or "")
        try:
            parsed = json.loads(raw)
            values = [str(item) for item in parsed] if isinstance(parsed, list) else [raw]
        except json.JSONDecodeError:
            values = re.split(r"[;\n,]+", raw)
    result: set[str] = set()
    for value in values:
        candidate = value.strip()
        if not candidate:
            continue
        try:
            parsed = phonenumbers.parse(candidate, "TR")
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_possible_number(parsed) and phonenumbers.is_valid_number(parsed):
            result.add(phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164))
    return result


def _array_values(row: dict, field: str) -> list[str]:
    value = row.get(field, "")
    try:
        parsed = json.loads(str(value or ""))
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _expected_domains(row: dict) -> set[str]:
    values = _array_values(row, "expected_website_domains_json")
    if values:
        return {scorer.registrable_domain(value) for value in values if scorer.registrable_domain(value)}
    fallback = _host(row.get("expected_website"))
    return {scorer.registrable_domain(fallback)} if fallback else set()


def _expected_emails(row: dict) -> set[str]:
    values = _array_values(row, "expected_emails_json")
    return {value.casefold() for value in values} if values else _emails(row.get("expected_email"))


def _expected_phones(row: dict) -> set[str]:
    values = _array_values(row, "expected_phones_e164_json")
    return _phones(values) if values else _phones(row.get("expected_phone"))


def _actual_values(row: dict, primary: str, alternatives: str) -> object:
    values = [row.get(primary, ""), row.get(alternatives, "")]
    return values


def _published(row: dict) -> bool:
    decision = row.get("publication_decision")
    if isinstance(decision, str):
        try:
            decision = json.loads(decision)
        except json.JSONDecodeError:
            decision = None
    if isinstance(decision, dict) and "publishable" in decision:
        return bool(decision["publishable"])
    return row.get("publication_eligible") is True or str(row.get("publication_eligible", "")).casefold() == "true"


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_package_integrity(manifest_path: Path) -> list[str]:
    """Validate an immutable V3 package without reading quality as authority."""
    root = manifest_path.resolve().parent
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    issues: list[str] = []
    if payload.get("package_id_algorithm") != "sha256(canonical-json(manifest_without_package_id_sha256))":
        issues.append("package_id_algorithm is missing or unsupported")
    package_id = str(payload.get("package_id_sha256") or "")
    if not _SHA256_RE.fullmatch(package_id):
        issues.append("package_id_sha256 is missing or malformed")
    else:
        preimage = dict(payload)
        preimage.pop("package_id_sha256", None)
        computed = _canonical_hash(preimage)
        if computed != package_id:
            issues.append("package_id_sha256 does not match canonical manifest preimage")
        if not root.name.endswith(package_id):
            issues.append("package directory suffix does not match package_id_sha256")
    declared = payload.get("files")
    if type(declared) is not list or not declared:
        issues.append("package files declaration is missing")
        return issues
    paths: list[str] = []
    declared_by_path: dict[str, dict] = {}
    for item in declared:
        if type(item) is not dict or not isinstance(item.get("path"), str) or not item.get("path"):
            issues.append("package file declaration is malformed")
            continue
        relative = item["path"].replace("\\", "/")
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            issues.append(f"package path escapes root: {relative}")
        if relative == "benchmark_manifest.json" or relative in declared_by_path:
            issues.append(f"duplicate or self-declared package path: {relative}")
        paths.append(relative)
        declared_by_path[relative] = item
        actual = root / relative
        try:
            actual.resolve().relative_to(root)
        except ValueError:
            issues.append(f"package path escapes root: {relative}")
            continue
        if actual.is_symlink():
            issues.append(f"package symlink is not allowed: {relative}")
            continue
        if not actual.is_file():
            issues.append(f"declared package file is missing: {relative}")
            continue
        if item.get("sha256") != _file_sha256(actual):
            issues.append(f"package file hash mismatch: {relative}")
        if type(item.get("bytes")) is not int or item["bytes"] != actual.stat().st_size:
            issues.append(f"package file byte mismatch: {relative}")
    if paths != sorted(set(paths)):
        issues.append("package files must be sorted and unique")
    actual_paths = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "benchmark_manifest.json" and not path.is_symlink()
    )
    if actual_paths != sorted(declared_by_path):
        issues.append("package has missing or undeclared files")
    descriptor_refs = {
        "selection_manifest_sha256": "selection_manifest.json",
        "runtime_lock_sha256": "runtime-lock.txt",
        "runtime_report_sha256": "runtime_report.json",
    }
    for field, relative in descriptor_refs.items():
        if field in payload and relative in declared_by_path and payload[field] != declared_by_path[relative].get("sha256"):
            issues.append(f"descriptor hash mismatch: {field}")
    for descriptor in payload.get("sets", []):
        if not isinstance(descriptor, dict):
            continue
        for field, relative_key in (("expected_sha256", "expected"), ("actual_output_sha256", "actual"), ("actual_manifest_sha256", "actual_manifest")):
            value = descriptor.get(field)
            relative = descriptor.get(relative_key)
            if value and relative:
                spec = declared_by_path.get(str(relative).replace("\\", "/"))
                if not spec or value != spec.get("sha256"):
                    issues.append(f"descriptor hash mismatch: {descriptor.get('role')}:{field}")
    return sorted(set(issues))


def _validate_actual_manifest(actual_manifest: Path, expected: Path, actual: Path, role: str) -> list[str]:
    payload = json.loads(actual_manifest.read_text(encoding="utf-8"))
    issues: list[str] = []
    if type(payload) is not dict:
        return [f"{role}: actual manifest must be an object"]
    if payload.get("status") not in {"complete", "complete_free_only"} or payload.get("finalized") is not True or payload.get("complete") is not True or payload.get("phase") != "COMPLETE":
        issues.append(f"{role}: actual manifest is not a finalized COMPLETE artifact")
    if payload.get("status") == "handoff_checkpoint_materialized" or payload.get("checkpoint_materialized"):
        issues.append(f"{role}: handoff/checkpoint actual is not production final")
    expected_ids = [_source_id_key(row) for row in _sheet_rows(expected)]
    actual_rows = _sheet_rows(actual)
    actual_ids = [_source_id_key(row) for row in actual_rows]
    if actual_ids != expected_ids:
        issues.append(f"{role}: actual source ID order/set does not match expected")
    if payload.get("source_record_ids") != expected_ids:
        issues.append(f"{role}: actual manifest source IDs do not match expected")
    if payload.get("source_record_ids_sha256") != _canonical_hash(expected_ids):
        issues.append(f"{role}: actual source ID hash mismatch")
    files = payload.get("files") or {}
    output_file = (files.get(actual.name) or files.get("all_results.xlsx")) if isinstance(files, dict) else None
    if not isinstance(output_file, dict) or output_file.get("sha256") != _file_sha256(actual) or output_file.get("bytes") != actual.stat().st_size:
        issues.append(f"{role}: actual workbook hash/byte declaration mismatch")
    if payload.get("expected_sha256") != _file_sha256(expected):
        issues.append(f"{role}: actual expected hash mismatch")
    for field in ("config_sha256", "runtime_source_tree_sha256"):
        if not _SHA256_RE.fullmatch(str(payload.get(field) or "")):
            issues.append(f"{role}: actual manifest {field} missing or malformed")
    for field in ("provider_calls", "physical_http_requests", "logical_request_count", "browser_page_count", "ocr_page_count"):
        if type(payload.get(field)) not in {int, float} or payload[field] < 0:
            issues.append(f"{role}: actual telemetry field {field} is missing or untyped")
    if type(payload.get("elapsed_seconds")) not in {int, float} or payload["elapsed_seconds"] < 0:
        issues.append(f"{role}: actual duration is missing or untyped")
    if type(payload.get("cost")) not in {int, float} or payload["cost"] < 0:
        issues.append(f"{role}: actual cost is missing or untyped")
    budgets = payload.get("paid_provider_budgets")
    if payload.get("paid_enabled") is not False or not isinstance(budgets, dict) or any(type(value) not in {int, float} or value != 0 for value in budgets.values()) or payload.get("provider_calls") != 0:
        issues.append(f"{role}: paid provider safety contract failed")
    capability = payload.get("capability_profile")
    if not isinstance(capability, dict):
        issues.append(f"{role}: capability_profile is missing")
    for row in actual_rows:
        decision = row.get("publication_decision")
        if isinstance(decision, str):
            try:
                decision = json.loads(decision)
            except json.JSONDecodeError:
                decision = None
        source_id = _source_id_key(row)
        if not isinstance(decision, dict):
            issues.append(f"{role}: frozen publication decision missing: {source_id}")
            continue
        if decision.get("source_record_id") != source_id or decision.get("run_id") not in {None, "", payload.get("run_id")} or decision.get("config_sha256") not in {None, "", payload.get("config_sha256")}:
            issues.append(f"{role}: frozen decision lineage mismatch: {source_id}")
        if bool(row.get("publication_eligible")) != bool(decision.get("publishable")):
            issues.append(f"{role}: flat publication projection mismatch: {source_id}")
    return issues


def _expected_publication(row: dict) -> str:
    value = str(row.get("expected_publication", "") or "").strip().casefold()
    aliases = {
        "publish": "publishable", "yes": "publishable", "true": "publishable", "allow": "publishable",
        "do_not_publish": "abstain", "not_publishable": "abstain", "no": "abstain", "false": "abstain",
        "unresolved": "unknown", "uncertain": "unknown",
    }
    value = aliases.get(value, value)
    return value if value in _PUBLICATION_STATES else ""


def evaluate_source_review(expected_path: Path, actual_path: Path, actual_manifest: Path | None = None) -> dict:
    expected = _validated_id_map(_source_rows(expected_path), "expected")
    actual = _validated_id_map(_sheet_rows(actual_path), "actual")
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    if missing or unexpected:
        raise ValueError(f"source ID coverage mismatch: missing={len(missing)} unexpected={len(unexpected)}")
    metrics = {
        "identity_correct": 0, "false_publication": 0, "published_unknown_identity": 0,
        "publication_precision": 0.0, "publication_recall": 0.0,
        "publication_coverage": 0.0, "safe_yield": 0.0,
        "expected_publishable_count": 0, "total_expected_records": len(expected),
        "unknown_record_count": 0, "unknown_label_count": 0,
        "website_tp": 0, "website_fp": 0, "website_fn": 0,
        "email_tp": 0, "email_fp": 0, "email_fn": 0,
        "phone_tp": 0, "phone_fp": 0, "phone_fn": 0,
        "website_precision": 0.0, "website_recall": 0.0,
        "email_precision": 0.0, "email_recall": 0.0,
        "phone_precision": 0.0, "phone_recall": 0.0,
        "verified_complete_contacts": 0, "verified_complete_contact_recall": 0.0,
        "expected_complete_contact_count": 0,
        "abstain_count": 0, "published_count": 0, "correct_published": 0,
        "provider_calls": 0, "physical_http_requests": 0,
        "run_elapsed_seconds": None, "records_per_minute": None,
        "p50_item_seconds": None, "p95_item_seconds": None,
        "p50_seconds": None, "p95_seconds": None, "item_latency_count": 0,
        "cost": None,
        "denominators": {
            "publication_coverage": len(expected), "safe_yield": len(expected),
            "publication_precision": 0, "publication_recall": 0,
            "known_identity": 0, "website": 0, "email": 0, "phone": 0,
            "verified_complete_contact_recall": 0,
        },
    }
    published = 0
    correct_published = 0
    durations: list[float] = []
    for source_id, expected_row in expected.items():
        row = actual[source_id]
        website_state = _state(expected_row, "website_verified")
        email_state = _state(expected_row, "email_verified")
        phone_state = _state(expected_row, "phone_verified")
        expected_publication = _expected_publication(expected_row)
        if not website_state or not email_state or not phone_state or not expected_publication:
            raise ValueError(f"expected label state missing/invalid: {source_id}")
        if expected_publication == "publishable":
            metrics["expected_publishable_count"] += 1
        identity_states = (website_state, email_state, phone_state)
        metrics["unknown_label_count"] += sum(state == "unknown" for state in identity_states) + (expected_publication == "unknown")
        if expected_publication == "unknown" or any(state == "unknown" for state in identity_states):
            metrics["unknown_record_count"] += 1
        is_pub = _published(row)
        actual_domains = {scorer.registrable_domain(_host(row.get("website")))} - {""}
        expected_domains = _expected_domains(expected_row)
        website_hit = bool(actual_domains & expected_domains)
        if website_state == "present":
            metrics["denominators"]["website"] += 1
            if website_hit:
                metrics["website_tp"] += 1
            else:
                metrics["website_fn"] += 1
                if actual_domains:
                    metrics["website_fp"] += 1
        elif website_state == "absent" and actual_domains:
            metrics["website_fp"] += 1
        if website_state == "present" and website_hit:
            metrics["identity_correct"] += 1
        if is_pub:
            published += 1
            if expected_publication == "abstain":
                metrics["false_publication"] += 1
            elif expected_publication == "unknown" or website_state == "unknown":
                metrics["published_unknown_identity"] += 1
            elif website_state == "present" and website_hit:
                correct_published += 1
            else:
                metrics["false_publication"] += 1
        else:
            metrics["abstain_count"] += 1
        expected_email = _expected_emails(expected_row)
        actual_email = _emails(_actual_values(row, "email", "alternative_emails"))
        if email_state == "present":
            metrics["denominators"]["email"] += 1
            if expected_email & actual_email:
                metrics["email_tp"] += 1
            else:
                metrics["email_fn"] += 1
                if actual_email:
                    metrics["email_fp"] += 1
        elif email_state == "absent" and actual_email:
            metrics["email_fp"] += 1
        expected_phone = _expected_phones(expected_row)
        actual_phone = _phones(_actual_values(row, "phone", "alternative_phones"))
        if phone_state == "present":
            metrics["denominators"]["phone"] += 1
            if expected_phone & actual_phone:
                metrics["phone_tp"] += 1
            else:
                metrics["phone_fn"] += 1
                if actual_phone:
                    metrics["phone_fp"] += 1
        elif phone_state == "absent" and actual_phone:
            metrics["phone_fp"] += 1
        if email_state == "present" and phone_state == "present":
            metrics["expected_complete_contact_count"] += 1
        if email_state == "present" and phone_state == "present" and expected_email & actual_email and expected_phone & actual_phone:
            metrics["verified_complete_contacts"] += 1
        for key in ("elapsed_seconds", "duration_seconds"):
            if row.get(key) not in (None, ""):
                try:
                    value = float(row[key])
                    if value >= 0:
                        durations.append(value)
                except (TypeError, ValueError):
                    pass
    metrics["denominators"]["publication_precision"] = published
    metrics["denominators"]["publication_recall"] = metrics["expected_publishable_count"]
    metrics["denominators"]["known_identity"] = sum(
        1 for row in expected.values() if _expected_publication(row) != "unknown"
    )
    metrics["denominators"]["verified_complete_contact_recall"] = metrics["expected_complete_contact_count"]
    metrics["publication_precision"] = round(correct_published / published, 4) if published else 0.0
    metrics["publication_recall"] = round(
        correct_published / metrics["expected_publishable_count"], 4
    ) if metrics["expected_publishable_count"] else 0.0
    metrics["safe_yield"] = round(
        correct_published / metrics["total_expected_records"], 4
    ) if metrics["total_expected_records"] else 0.0
    metrics["publication_coverage"] = metrics["safe_yield"]
    metrics["published_count"] = published
    metrics["correct_published"] = correct_published
    for field in ("website", "email", "phone"):
        tp = metrics[f"{field}_tp"]
        fp = metrics[f"{field}_fp"]
        denominator = metrics["denominators"][field]
        metrics[f"{field}_precision"] = round(tp / (tp + fp), 4) if tp + fp else 0.0
        metrics[f"{field}_recall"] = round(tp / denominator, 4) if denominator else 0.0
    complete_denominator = metrics["expected_complete_contact_count"]
    metrics["verified_complete_contact_recall"] = round(
        metrics["verified_complete_contacts"] / complete_denominator, 4
    ) if complete_denominator else 0.0
    if actual_manifest and Path(actual_manifest).exists():
        payload = json.loads(Path(actual_manifest).read_text(encoding="utf-8"))
        for field, aliases in {
            "provider_calls": ("provider_calls", "provider_calls_new"),
            "physical_http_requests": ("physical_http_requests", "physical_http_requests_new"),
        }.items():
            value = next((payload[key] for key in aliases if key in payload), None)
            if value is not None and (type(value) not in {int, float} or value < 0):
                raise ValueError(f"actual manifest {field} is not a non-negative number")
            metrics[field] = value
        cost = payload.get("cost")
        if cost is not None and (type(cost) not in {int, float} or cost < 0):
            raise ValueError("actual manifest cost is not a non-negative number")
        metrics["cost"] = float(cost) if cost is not None else None
        duration = payload.get("elapsed_seconds", payload.get("duration_seconds"))
        if duration is not None and (type(duration) not in {int, float} or duration < 0):
            raise ValueError("actual manifest duration is not a non-negative number")
        if duration is not None:
            metrics["run_elapsed_seconds"] = float(duration)
            if metrics["total_expected_records"]:
                metrics["records_per_minute"] = round(metrics["total_expected_records"] / (float(duration) / 60), 4) if duration else None
    if durations:
        metrics["item_latency_count"] = len(durations)
        metrics["p50_item_seconds"] = statistics.median(durations)
        metrics["p95_item_seconds"] = statistics.quantiles(durations, n=20, method="inclusive")[18] if len(durations) > 1 else durations[0]
        metrics["p50_seconds"] = metrics["p50_item_seconds"]
        metrics["p95_seconds"] = metrics["p95_item_seconds"]
    return metrics


def validate_manifest(
    path: Path,
    private_seen_workbook: Path | None = None,
    require_all_rows_review_frozen: bool = False,
) -> tuple[list[dict], list[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    issues: list[str] = []
    sets = payload.get("sets", [])
    policy = payload.get("policy", {})
    manifest_schema_version = int(payload.get("schema_version", payload.get("version", 1)) or 1)
    for item in sets:
        if isinstance(item, dict):
            item.setdefault("manifest_schema_version", manifest_schema_version)

    policy_count = policy.get("private_seen_expected_unique_companies")
    if manifest_schema_version < 2 and (type(policy_count) is not int or policy_count != EXPECTED_PRIVATE_SEEN_UNIQUE_COMPANIES):
        issues.append(
            f"manifest policy error: private_seen_expected_unique_companies must be {EXPECTED_PRIVATE_SEEN_UNIQUE_COMPANIES}"
        )

    seen_roles = set()
    company_sets: dict[str, set[str]] = {}
    has_private_flag = False

    for item in sets:
        role = item.get("role", "")
        if role in seen_roles:
            issues.append(f"duplicate benchmark role: {role}")
        seen_roles.add(role)

        if "private_seen_check" in item:
            val = item["private_seen_check"]
            if type(val) is not bool:
                issues.append(f"{role}: private_seen_check must be a boolean")

        is_private_checked = (item.get("private_seen_check") is True)
        if is_private_checked:
            has_private_flag = True

        if role == "blind" or role.startswith("blind_"):
            if not is_private_checked:
                issues.append(f"{role}: blind role requires private_seen_check=true")

        expected_text = item.get("expected", "")
        if not expected_text:
            if role != "blind" and not (
                manifest_schema_version >= 2 and item.get("status") in {"blocked", "not_run"}
            ):
                issues.append(f"{role}: expected workbook missing")
            continue
        expected = resolve_item_paths(item, path)["expected"]
        assert expected is not None
        if not expected.exists():
            issues.append(f"{role}: workbook not found: {expected}")
            continue
        if manifest_schema_version >= 2:
            try:
                acceptance = item.get("acceptance", payload.get("acceptance"))
                if acceptance != REQUIRED_ACCEPTANCE:
                    issues.append(f"{role}: v2 acceptance contract is missing or invalid")
                source_rows = _source_rows(expected)
                if manifest_schema_version >= 3:
                    _validate_v3_ground_truth(source_rows, role)
                _validated_id_map(source_rows, f"{role} expected")
                if any(str(row.get("label_status", "")).casefold() not in {"frozen", "unknown"} for row in source_rows):
                    issues.append(f"{role}: label_status must be frozen or unknown")
                if require_all_rows_review_frozen and any(
                    str(row.get("label_status", "")).casefold() != "frozen" for row in source_rows
                ):
                    issues.append(f"{role}: every expected row must have label_status=frozen under --require-actual")
            except Exception as exc:
                issues.append(f"{role}: source-reviewed schema error: {exc}")
        elif item.get("readiness_mode") != "legacy" and item.get("status") != "manual_validation_pending":
            issues.extend(f"{role}: {value}" for value in readiness_issues(expected))
        company_sets[role] = (
            set(_validated_id_map(_source_rows(expected), f"{role} expected"))
            if manifest_schema_version >= 2 else _companies(expected)
        )

    if manifest_schema_version < 2 and not has_private_flag:
        issues.append("manifest policy error: at least one set must have private_seen_check=true")

    for first_role, first in company_sets.items():
        for second_role, second in company_sets.items():
            if first_role >= second_role:
                continue
            overlap = first & second
            if overlap:
                issues.append(f"company overlap {first_role}/{second_role}: {len(overlap)}")

    if manifest_schema_version < 2 and private_seen_workbook is not None:
        wb_path = Path(private_seen_workbook).resolve()
        if not wb_path.exists():
            issues.append(f"private seen workbook not found: {wb_path}")
        else:
            try:
                records = excel.read_company_records(wb_path)
                private_seen = {
                    scorer.normalize_text(str(r.get("company") or "")).strip()
                    for r in records
                    if str(r.get("company") or "").strip()
                }
                if len(private_seen) != EXPECTED_PRIVATE_SEEN_UNIQUE_COMPANIES:
                    issues.append(
                        f"private seen workbook count mismatch: actual {len(private_seen)} != expected {EXPECTED_PRIVATE_SEEN_UNIQUE_COMPANIES}"
                    )

                checked_private_roles: list[str] = []
                for item in sets:
                    if item.get("private_seen_check") is True:
                        role = item.get("role", "")
                        if role in company_sets and company_sets[role]:
                            checked_private_roles.append(role)
                            overlap = company_sets[role] & private_seen
                            if overlap:
                                issues.append(f"{role}: private seen overlap: {len(overlap)}")
                        else:
                            issues.append(f"{role}: private seen check target workbook missing or empty")

                if not checked_private_roles:
                    issues.append("private seen gate error: no private_seen_check sets evaluated")

            except Exception:
                issues.append("private seen workbook read error")

    return sets, issues


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=BASE_DIR / "data" / "benchmark_splits.json")
    parser.add_argument("--actual", action="append", default=[], help="role=contacts.xlsx")
    parser.add_argument(
        "--require-actual", action="store_true",
        help="Fail unless every manifest role with expected labels has an evaluated actual output.",
    )
    parser.add_argument("--private-seen-workbook", type=Path, default=None, help="Path to private seen firms workbook")
    args = parser.parse_args()

    try:
        manifest_payload = json.loads(args.manifest.read_text(encoding="utf-8"))
        registry_issues = _invalid_package_issues(args.manifest, manifest_payload)
    except Exception as exc:
        print(f"Benchmark suite issues:\n- {exc}")
        raise SystemExit(2)
    if registry_issues:
        print("Benchmark suite issues:")
        for issue in registry_issues:
            print(f"- {issue}")
        raise SystemExit(2)

    package_issues = validate_package_integrity(args.manifest) if manifest_payload.get("package_id_algorithm") else []
    if package_issues:
        print("Benchmark suite issues:")
        for issue in package_issues:
            print(f"- {issue}")
        raise SystemExit(2)

    sets, issues = validate_manifest(
        args.manifest,
        private_seen_workbook=args.private_seen_workbook,
        require_all_rows_review_frozen=args.require_actual,
    )
    actuals: dict[str, Path] = {}
    for value in args.actual:
        if "=" not in value:
            issues.append(f"invalid --actual value: {value}")
            continue
        role, actual_path = value.split("=", 1)
        actuals[role] = Path(actual_path).resolve()
    manifest_schema_version = int(manifest_payload.get("schema_version", manifest_payload.get("version", 1)) or 1)
    all_expected_roles = [item.get("role", "") for item in sets if item.get("expected", "")]
    if args.require_actual:
        for role in all_expected_roles:
            if role not in actuals:
                configured = next(item for item in sets if item.get("role") == role)
                configured_path = resolve_item_paths(configured, args.manifest)["actual"]
                if configured_path is not None:
                    actuals[role] = configured_path
                else:
                    issues.append(f"{role}: actual output required (--require-actual)")
            if role in actuals and not actuals[role].exists():
                issues.append(f"{role}: actual output not found: {actuals[role]}")
            configured = next(item for item in sets if item.get("role") == role)
            actual_manifest_text = configured.get("actual_manifest")
            if not actual_manifest_text:
                issues.append(f"{role}: actual manifest is required")
            else:
                actual_manifest_path = (args.manifest.resolve().parent / str(actual_manifest_text)).resolve()
                if not actual_manifest_path.exists():
                    issues.append(f"{role}: actual manifest not found: {actual_manifest_path}")
                elif role in actuals and actuals[role].exists():
                    try:
                        issues.extend(_validate_actual_manifest(actual_manifest_path, resolve_item_paths(configured, args.manifest)["expected"], actuals[role], role))
                    except Exception as exc:
                        issues.append(f"{role}: actual manifest structural validation error: {exc}")
    if issues:
        print("Benchmark suite issues:")
        for issue in issues:
            print(f"- {issue}")
        raise SystemExit(2)
    quality_failures: list[str] = []
    quality_thresholds = manifest_payload.get("quality_thresholds", {})
    evaluated_roles: set[str] = set()
    for item in sets:
        role = item.get("role", "")
        paths = resolve_item_paths(item, args.manifest)
        expected = paths["expected"]
        actual = actuals.get(role)
        if role not in actuals or expected is None or actual is None or not actual.exists():
            continue
        try:
            if manifest_schema_version >= 2:
                actual_manifest = None
                manifest_text = item.get("actual_manifest")
                if not manifest_text:
                    issues.append(f"{role}: actual manifest is required")
                    continue
                if manifest_text:
                    actual_manifest = (args.manifest.resolve().parent / str(manifest_text)).resolve()
                if actual_manifest is None or not actual_manifest.exists():
                    raise ValueError("actual manifest is required and must exist")
                metrics = evaluate_source_review(expected, actual, actual_manifest)
                evaluated_roles.add(role)
                print(f"[{role}] source_id_count={len(_validated_id_map(_source_rows(expected), 'expected'))}")
                print(f"  metrics: {json.dumps(metrics, ensure_ascii=False, sort_keys=True)}")
                if metrics["false_publication"]:
                    quality_failures.append(f"{role}: confirmed false publication={metrics['false_publication']}")
                if metrics["published_unknown_identity"]:
                    quality_failures.append(f"{role}: published unknown identity={metrics['published_unknown_identity']}")
                if metrics["total_expected_records"] <= 0 or metrics["denominators"]["known_identity"] <= 0 or metrics["expected_publishable_count"] <= 0:
                    quality_failures.append(f"{role}: zero expected, publishable, or known-identity denominator")
                if metrics["denominators"]["publication_precision"] <= 0:
                    quality_failures.append(f"{role}: zero published denominator")
                if item.get("acceptance", manifest_payload.get("acceptance")) != REQUIRED_ACCEPTANCE:
                    quality_failures.append(f"{role}: acceptance contract failed")
                if metrics["provider_calls"] is None or metrics["physical_http_requests"] is None or metrics["cost"] is None:
                    quality_failures.append(f"{role}: actual manifest has unknown telemetry/cost")
                thresholds = dict(quality_thresholds)
                thresholds.update(item.get("quality_thresholds", {}) or {})
                thresholds.setdefault("min_publication_precision", 1.0)
                thresholds.setdefault("min_publication_recall", 0.75)
                thresholds.setdefault("min_verified_complete_contact_recall", 0.70)
                if "min_publication_precision" in thresholds and metrics["publication_precision"] < float(thresholds["min_publication_precision"]):
                    quality_failures.append(f"{role}: publication precision below threshold")
                if "min_publication_recall" in thresholds and metrics["publication_recall"] < float(thresholds["min_publication_recall"]):
                    quality_failures.append(f"{role}: publication recall below threshold")
                if "min_verified_complete_contact_recall" in thresholds and metrics["verified_complete_contact_recall"] < float(thresholds["min_verified_complete_contact_recall"]):
                    quality_failures.append(f"{role}: verified complete contact recall below threshold")
                if "min_publication_coverage" in thresholds and metrics["safe_yield"] < float(thresholds["min_publication_coverage"]):
                    quality_failures.append(f"{role}: safe yield below threshold")
            else:
                metrics, complete = evaluate(expected, actual)
                evaluated_roles.add(role)
                print(f"[{role}] complete={len(complete)}")
                print(f"  coverage: {assertion_coverage(expected)}")
                for field in FIELDS:
                    print(f"  {field}: {metrics[field]}")
        except Exception as exc:
            issues.append(f"{role}: actual evaluation error: {exc}")
    if issues:
        print("Benchmark suite issues:")
        for issue in issues:
            print(f"- {issue}")
        raise SystemExit(2)
    print("Benchmark suite manifest: OK")
    if args.require_actual and set(all_expected_roles) == evaluated_roles:
        if quality_failures:
            print("quality_status: failed")
            for failure in quality_failures:
                print(f"- {failure}")
            raise SystemExit(3)
        print("quality_status: pass")
    elif args.require_actual:
        print("quality_status: schema_only; quality_not_evaluated")
    else:
        print("quality_status: schema_only; quality_not_evaluated")
    if args.private_seen_workbook is not None:
        print("private_seen_gate: OK")
    else:
        print("private_seen_gate: NOT_REQUESTED")


if __name__ == "__main__":
    main()
