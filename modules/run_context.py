"""Immutable run configuration, durable context and OS-level run lease."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import shutil
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from contextlib import closing

import config


PHASES = ("FREE", "PAID", "FINALIZING", "COMPLETE")
RUN_SCHEMA_VERSION = int(getattr(config, "RUN_SCHEMA_VERSION", 3))
CONFIG_SCHEMA_VERSION = int(getattr(config, "CONFIG_SCHEMA_VERSION", 3))

# One registry drives the recorded run configuration, its hash, and replay
# application.  Keep this list limited to non-secret values: credentials and
# provider tokens must never become part of a manifest.
SEMANTIC_CONFIG_REGISTRY: tuple[tuple[str, str, str], ...] = (
    ("SEARCH_PROVIDER", "search_provider", "str"),
    ("SEARCH_CACHE_MODE", "search_cache_mode", "str"),
    ("CRAWL_CACHE_MODE", "crawl_cache_mode", "str"),
    ("SEARCH_CACHE_TTL_DAYS", "search_cache_ttl_days", "int"),
    ("SEARCH_EMPTY_CACHE_TTL_DAYS", "search_empty_cache_ttl_days", "float"),
    ("CRAWL_CACHE_TTL_DAYS", "crawl_cache_ttl_days", "int"),
    ("CACHE_SCHEMA_VERSION", "cache_schema_version", "int"),
    ("CRAWL_CACHE_SCHEMA_VERSION", "crawl_cache_schema_version", "int"),
    ("CRAWL_CACHE_CAPABILITY_SCHEMA_VERSION", "crawl_cache_capability_schema_version", "int"),
    ("METADATA_SCHEMA_VERSION", "metadata_schema_version", "int"),
    ("EVIDENCE_SCHEMA_VERSION", "evidence_schema_version", "int"),
    ("MAX_WORKERS", "max_workers", "int"),
    ("GLOBAL_REQUESTS_PER_SECOND", "global_requests_per_second", "float"),
    ("MAX_ZUCHEX_PAGES", "max_zuchex_pages", "int"),
    ("MAX_TEXHIBITION_PAGES", "max_texhibition_pages", "int"),
    ("ZUCHEX_VIEW_ID", "zuchex_view_id", "str"),
    ("ZUCHEX_EVENT_ID", "zuchex_event_id", "str"),
    ("ZUCHEX_FILTER_ID", "zuchex_filter_id", "str"),
    ("ZUCHEX_FILTER_VALUE_ID", "zuchex_filter_value_id", "str"),
    ("MAX_AUTONOMOUS_RESOLUTION_ROUNDS", "max_autonomous_resolution_rounds", "int"),
    ("MAX_TARGETED_QUERIES_PER_ROUND", "max_targeted_queries_per_round", "int"),
    ("MAX_TARGETED_CRAWLS_PER_ROUND", "max_targeted_crawls_per_round", "int"),
    ("MAX_SEARCH_QUERIES_PER_COMPANY", "max_search_queries_per_company", "int"),
    ("DEFAULT_PAID_SEARCH_QUERY_LIMIT", "default_paid_search_query_limit", "int"),
    ("MAX_FALLBACK_SEARCH_QUERIES", "max_fallback_search_queries", "int"),
    ("MAX_ADAPTIVE_SEARCH_QUERIES", "max_adaptive_search_queries", "int"),
    ("MAX_CONTACT_PAGES", "max_contact_pages", "int"),
    ("MAX_CONTACT_ATTEMPTS", "max_contact_attempts", "int"),
    ("MAX_IDENTITY_PAGES", "max_identity_pages", "int"),
    ("MAX_FULL_CANDIDATE_EVALUATIONS", "max_full_candidate_evaluations", "int"),
    ("MAX_IDENTITY_EVIDENCE_RECRAWLS", "max_identity_evidence_recrawls", "int"),
    ("MAX_SITEMAPS", "max_sitemaps", "int"),
    ("MAX_SITEMAP_URLS", "max_sitemap_urls", "int"),
    ("MAX_DOCUMENT_LINKS", "max_document_links", "int"),
    ("MAX_STATIC_RECOVERY_PAGES", "max_static_recovery_pages", "int"),
    ("MAX_HOST_VARIANT_ATTEMPTS", "max_host_variant_attempts", "int"),
    ("REQUEST_TIMEOUT_SEC", "request_timeout_sec", "int"),
    ("BRIGHTDATA_TIMEOUT_SEC", "brightdata_timeout_sec", "int"),
    ("GOOGLE_PLACES_TIMEOUT_SEC", "google_places_timeout_sec", "int"),
    ("HUNTER_TIMEOUT_SEC", "hunter_timeout_sec", "int"),
    ("BRANDFETCH_TIMEOUT_SEC", "brandfetch_timeout_sec", "int"),
    ("LINKEDIN_COMPANY_TIMEOUT_SEC", "linkedin_company_timeout_sec", "int"),
    ("LLM_ARBITER_TIMEOUT_SEC", "llm_arbiter_timeout_sec", "int"),
    ("MAX_HTTP_REDIRECTS", "max_http_redirects", "int"),
    ("MAX_RETRIES", "max_retries", "int"),
    ("MAX_RETRY_AFTER_SEC", "max_retry_after_sec", "int"),
    ("ENABLE_JS_FALLBACK", "enable_js_fallback", "bool"),
    ("ENABLE_JS_PROFILE_FALLBACK", "enable_js_profile_fallback", "bool"),
    ("MAX_BROWSER_RENDER_WORKERS", "max_browser_render_workers", "int"),
    ("JS_RENDER_TIMEOUT_SEC", "js_render_timeout_sec", "int"),
    ("ENABLE_PDF_OCR", "enable_pdf_ocr", "bool"),
    ("PDF_OCR_MAX_PAGES", "pdf_ocr_max_pages", "int"),
    ("PDF_OCR_DPI", "pdf_ocr_dpi", "int"),
    ("PDF_MIN_TEXT_CHARS", "pdf_min_text_chars", "int"),
    ("ENABLE_GOOGLE_PLACES", "enable_google_places", "bool"),
    ("ENABLE_BRANDFETCH_DOMAIN_SEARCH", "enable_brandfetch", "bool"),
    ("ENABLE_HUNTER_FALLBACK", "enable_hunter_fallback", "bool"),
    ("ENABLE_HUNTER_DOMAIN_FINDER", "enable_hunter", "bool"),
    ("ENABLE_LINKEDIN_COMPANY_LOOKUP", "enable_linkedin", "bool"),
    ("ENABLE_LLM_ARBITER", "enable_llm", "bool"),
    ("COMPANY_RESOLVER_MAX_RESULTS", "company_resolver_max_results", "int"),
    ("LLM_ARBITER_MODEL", "llm_model", "str"),
    ("TARGET_COUNTRY", "target_country", "str"),
    ("TARGET_COUNTRY_QUERY_TERMS", "target_country_query_terms", "json"),
    ("LOCALE", "locale", "str"),
    ("PHONE_DEFAULT_COUNTRY", "phone_default_country", "str"),
    ("PHONE_OUTPUT_FORMAT", "phone_output_format", "str"),
    ("PHONE_ALLOWED_COUNTRIES", "phone_allowed_countries", "json"),
    ("REVIEW_SCORE", "review_score", "int"),
    ("MEDIUM_CONFIDENCE_SCORE", "medium_confidence_score", "int"),
    ("HIGH_CONFIDENCE_SCORE", "high_confidence_score", "int"),
    ("PUBLICATION_POLICY_MODE", "publication_policy_mode", "str"),
    ("PUBLICATION_POLICY_MIN_SAFETY_SCORE", "publication_policy_min_safety_score", "int"),
    ("SAFE_OK_MIN_SCORE", "safe_ok_min_score", "int"),
    ("MIN_ACCEPT_SCORE", "min_accept_score", "int"),
    ("EARLY_STOP_SCORE_THRESHOLD", "early_stop_score_threshold", "int"),
    ("MAX_CANDIDATE_EVALUATIONS", "max_candidate_evaluations", "int"),
    ("MAX_CANDIDATE_SCORE_GAP", "max_candidate_score_gap", "int"),
    ("AMBIGUOUS_CANDIDATE_MARGIN", "ambiguous_candidate_margin", "int"),
)


def runtime_capability_profile() -> dict[str, bool]:
    """Report optional local capabilities without invoking network/providers."""
    browser_dependency = importlib.util.find_spec("playwright") is not None
    ocr_dependencies = all(
        importlib.util.find_spec(name) is not None
        for name in ("fitz", "pytesseract", "PIL")
    )
    return {
        "browser_dependency_available": browser_dependency,
        "browser_enabled": bool(getattr(config, "ENABLE_JS_FALLBACK", False)),
        "ocr_dependency_available": ocr_dependencies and bool(shutil.which("tesseract")),
        "ocr_enabled": bool(getattr(config, "ENABLE_PDF_OCR", False)),
    }


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def source_tree_sha256(root: Path | None = None) -> str:
    root = Path(root or config.BASE_DIR).resolve()
    digest = hashlib.sha256()
    candidates = [root / "config.py", root / "main.py", root / "scrape_exhibitors.py"]
    candidates.extend(sorted(root.glob("requirements*.txt")))
    candidates.extend(sorted((root / "modules").glob("*.py")))
    for path in sorted({path.resolve() for path in candidates}):
        if not path.is_file():
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def canonical_run_id(*, input_sha256: str, ordered_source_record_ids: list[str], effective_config: dict,
                     runtime_source_tree_hash: str, lineage: dict | None = None,
                     run_schema_version: int = RUN_SCHEMA_VERSION) -> str:
    payload = {
        "input_sha256": str(input_sha256),
        "ordered_selected_source_record_ids": list(ordered_source_record_ids),
        "complete_effective_run_config": effective_config,
        "runtime_source_tree_sha256": str(runtime_source_tree_hash),
        "run_schema_version": int(run_schema_version),
        "lineage": lineage or {"type": "fresh"},
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def source_record_identity(record: dict, *, default_source: str = "input") -> tuple[str, str]:
    source = str(record.get("source") or default_source).strip() or default_source
    qualified = record.get("source_record_id")
    if qualified:
        value = str(qualified).strip()
        return (value if ":" in value else f"{source}:{value}"), "upstream"
    upstream = next((record.get(key) for key in ("_id", "upstream_id", "exhibitor_id", "profile_id") if record.get(key)), None)
    if upstream:
        value = str(upstream).strip()
        return (value if ":" in value else f"{source}:{value}"), "upstream"
    basis = {
        "source": source,
        "company": str(record.get("company", "")).strip(),
        "profile_url": str(record.get("profile_url", "")).strip(),
        "listing_url": str(record.get("listing_url", "")).strip(),
        "hall": str(record.get("hall", "")).strip(),
        "stand": str(record.get("stand", "")).strip(),
    }
    return f"{source}:{hashlib.sha256(canonical_json(basis).encode('utf-8')).hexdigest()}", "derived"


@dataclass(frozen=True)
class RunConfig:
    search_provider: str
    search_cache_mode: str
    crawl_cache_mode: str
    paid_enabled: bool
    brightdata_budget: int
    google_places_budget: int
    brandfetch_budget: int
    hunter_budget: int
    linkedin_budget: int
    llm_budget: int
    model: str
    thresholds: tuple[tuple[str, int], ...]
    policy_versions: tuple[tuple[str, str], ...]
    effective_settings: tuple[tuple[str, object], ...] = ()

    @staticmethod
    def _semantic_settings() -> tuple[tuple[str, object], ...]:
        result: list[tuple[str, object]] = [("config_schema_version", CONFIG_SCHEMA_VERSION)]
        for name, canonical_name, kind in SEMANTIC_CONFIG_REGISTRY:
            if not hasattr(config, name):
                continue
            value = getattr(config, name)
            if kind == "bool":
                value = bool(value)
            elif kind == "int":
                value = int(value)
            elif kind == "float":
                value = float(value)
            elif kind == "str":
                value = str(value)
            result.append((canonical_name, value))
        for name, value in runtime_capability_profile().items():
            result.append((f"capability_{name}", bool(value)))
        return tuple(sorted(result))

    @classmethod
    def from_config(cls, *, paid_enabled: bool) -> "RunConfig":
        import modules.publication_policy as publication_policy

        thresholds = tuple(sorted(
            (name, int(getattr(config, name)))
            for name in ("REVIEW_SCORE", "MEDIUM_CONFIDENCE_SCORE", "HIGH_CONFIDENCE_SCORE")
            if hasattr(config, name)
        ))
        budget = lambda name: max(0, int(getattr(config, name))) if paid_enabled else 0
        return cls(
            search_provider=str(config.SEARCH_PROVIDER),
            search_cache_mode=str(config.SEARCH_CACHE_MODE),
            crawl_cache_mode=str(config.CRAWL_CACHE_MODE),
            paid_enabled=bool(paid_enabled),
            brightdata_budget=budget("BRIGHTDATA_REQUEST_BUDGET"),
            google_places_budget=budget("GOOGLE_PLACES_REQUEST_BUDGET"),
            brandfetch_budget=budget("BRANDFETCH_REQUEST_BUDGET"),
            hunter_budget=budget("HUNTER_REQUEST_BUDGET"),
            linkedin_budget=budget("LINKEDIN_COMPANY_REQUEST_BUDGET"),
            llm_budget=budget("LLM_ARBITER_BUDGET"),
            model=str(config.LLM_ARBITER_MODEL),
            thresholds=thresholds,
            policy_versions=(
                ("publication", str(publication_policy.POLICY_VERSION)),
                ("cache", str(config.CACHE_SCHEMA_VERSION)),
            ),
            effective_settings=cls._semantic_settings(),
        )

    def as_dict(self) -> dict:
        return {
            "search_provider": self.search_provider,
            "search_cache_mode": self.search_cache_mode,
            "crawl_cache_mode": self.crawl_cache_mode,
            "paid_enabled": self.paid_enabled,
            "budgets": {
                "brightdata": self.brightdata_budget,
                "google_places": self.google_places_budget,
                "brandfetch": self.brandfetch_budget,
                "hunter": self.hunter_budget,
                "linkedin": self.linkedin_budget,
                "llm": self.llm_budget,
            },
            "model": self.model,
            "thresholds": dict(self.thresholds),
            "policy_versions": dict(self.policy_versions),
            "effective_settings": dict(self.effective_settings),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "RunConfig":
        if not isinstance(payload, dict) or not isinstance(payload.get("budgets"), dict):
            raise ValueError("manifest run_config is invalid")
        budgets = payload["budgets"]
        thresholds = tuple(sorted((str(k), int(v)) for k, v in dict(payload.get("thresholds", {})).items()))
        policies = tuple(sorted((str(k), str(v)) for k, v in dict(payload.get("policy_versions", {})).items()))
        settings = tuple(sorted(dict(payload.get("effective_settings", {})).items()))
        return cls(
            search_provider=str(payload["search_provider"]),
            search_cache_mode=str(payload["search_cache_mode"]),
            crawl_cache_mode=str(payload["crawl_cache_mode"]),
            paid_enabled=bool(payload["paid_enabled"]),
            brightdata_budget=int(budgets["brightdata"]),
            google_places_budget=int(budgets["google_places"]),
            brandfetch_budget=int(budgets["brandfetch"]),
            hunter_budget=int(budgets["hunter"]),
            linkedin_budget=int(budgets["linkedin"]),
            llm_budget=int(budgets["llm"]),
            model=str(payload["model"]), thresholds=thresholds,
            policy_versions=policies, effective_settings=settings,
        )

    def apply_effective_settings(self) -> None:
        """Apply only recorded, non-secret typed settings to the process config."""
        reverse = {canonical_name: (name, kind) for name, canonical_name, kind in SEMANTIC_CONFIG_REGISTRY}
        for key, value in self.effective_settings:
            target = reverse.get(key)
            if target and hasattr(config, target[0]):
                name, kind = target
                if kind == "bool":
                    value = bool(value)
                elif kind == "int":
                    value = int(value)
                elif kind == "float":
                    value = float(value)
                elif kind == "str":
                    value = str(value)
                elif kind == "json" and isinstance(value, tuple):
                    value = list(value)
                setattr(config, name, value)
        budget_attrs = {
            "brightdata": "BRIGHTDATA_REQUEST_BUDGET", "google_places": "GOOGLE_PLACES_REQUEST_BUDGET",
            "brandfetch": "BRANDFETCH_REQUEST_BUDGET", "hunter": "HUNTER_REQUEST_BUDGET",
            "linkedin": "LINKEDIN_COMPANY_REQUEST_BUDGET", "llm": "LLM_ARBITER_BUDGET",
        }
        for provider, target in budget_attrs.items():
            if hasattr(config, target):
                setattr(config, target, int(self.as_dict()["budgets"][provider]))

    @property
    def sha256(self) -> str:
        canonical = json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RunContext:
    run_id: str
    input_hash: str
    code_revision: str
    phase: str
    started_at: str
    resume_history: tuple[dict, ...] = ()

    def __post_init__(self) -> None:
        if self.phase not in PHASES:
            raise ValueError(f"invalid run phase: {self.phase}")

    def with_phase(self, phase: str) -> "RunContext":
        return replace(self, phase=phase)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "input_hash": self.input_hash,
            "code_revision": self.code_revision,
            "phase": self.phase,
            "started_at": self.started_at,
            "resume_history": list(self.resume_history),
        }


def new_context(input_hash: str, run_config: RunConfig, *, run_id: str | None = None,
                ordered_source_record_ids: list[str] | None = None,
                lineage: dict | None = None) -> RunContext:
    rid = run_id or canonical_run_id(
        input_sha256=input_hash,
        ordered_source_record_ids=ordered_source_record_ids or [],
        effective_config=run_config.as_dict(),
        runtime_source_tree_hash=source_tree_sha256(),
        lineage=lineage,
    )
    return RunContext(
        run_id=rid,
        input_hash=input_hash,
        code_revision=os.getenv("B2B_CODE_REVISION", "working-tree"),
        phase="FREE",
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


class RunLease:
    """Create-or-fail lease; a second process cannot enter the same run."""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / ".run.lease"
        self._handle = None

    def acquire(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._handle = self.path.open("a+b")
            self._handle.seek(0)
            self._handle.write(b"0")
            self._handle.flush()
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._handle.seek(0)
            self._handle.write(f"pid={os.getpid()} started={time.time()}\n".encode())
            self._handle.flush()
        except (OSError, BlockingIOError) as exc:
            handle = self._handle
            self._handle = None
            if handle is not None:
                handle.close()
            raise RuntimeError(f"run already leased: {self.run_dir}") from exc
        except BaseException:
            handle = self._handle
            self._handle = None
            if handle is not None:
                handle.close()
            raise
        _ACTIVE_LEASES.add(self)

    def release(self) -> None:
        if self._handle is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None
                _ACTIVE_LEASES.discard(self)

    def __enter__(self) -> "RunLease":
        self.acquire()
        return self

    def __exit__(self, *_args) -> None:
        self.release()


_ACTIVE_LEASES: set[RunLease] = set()


def active_leases() -> tuple[RunLease, ...]:
    return tuple(_ACTIVE_LEASES)


def validate_run_bundle(
    run_root: Path,
    *,
    expected_input_hash: str | None = None,
    expected_config_hash: str | None = None,
    expected_source_ids: list[str] | None = None,
    require_artifacts: bool = True,
    profile: str | None = None,
) -> dict:
    """Validate one run's immutable identity, scheduler, payloads and artifacts."""
    run_root = Path(run_root).resolve()
    manifest_path = run_root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("run_root/manifest.json is required")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    profile = profile or ("COMPLETE" if manifest.get("complete") else "FROZEN_RECOVERY" if manifest.get("provisional") else "ACTIVE_RESUME")
    if profile not in {"ACTIVE_RESUME", "FROZEN_RECOVERY", "FINALIZING", "COMPLETE"}:
        raise ValueError(f"unknown run validation profile: {profile}")
    run_id = str(manifest.get("run_id", ""))
    if not run_id or run_root.name != run_id:
        raise ValueError("manifest/run directory identity mismatch")
    source_ids = manifest.get("ordered_source_record_ids")
    item_count = int(manifest.get("item_count", -1))
    if not isinstance(source_ids, list) or item_count != len(source_ids) or any(not str(value).strip() for value in source_ids):
        raise ValueError("manifest source ID list is invalid")
    if len(set(source_ids)) != item_count or expected_source_ids is not None and source_ids != expected_source_ids:
        raise ValueError("manifest source ID list mismatch")
    if expected_input_hash is not None and manifest.get("input_sha256") != expected_input_hash:
        raise ValueError("manifest input identity mismatch")
    if expected_config_hash is not None and manifest.get("config_sha256") != expected_config_hash:
        raise ValueError("manifest config identity mismatch")
    db_path = run_root / "state" / "progress.sqlite3"
    if not db_path.is_file():
        raise ValueError("run checkpoint is missing")
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("run checkpoint integrity failure")
        runs = connection.execute("SELECT run_id,input_hash FROM runs").fetchall()
        if len(runs) != 1 or runs[0][0] != run_id or runs[0][1] != manifest.get("input_sha256"):
            raise ValueError("checkpoint run identity mismatch")
        item_columns = {row[1] for row in connection.execute("PRAGMA table_info(run_items)")}
        quarantine_columns = {"quarantine_state", "quarantine_status", "publication_blockers"}
        if quarantine_columns.issubset(item_columns):
            items = connection.execute(
                "SELECT item_index,source_record_id,free_state,paid_required,paid_state,payload_sha256,quarantine_state,quarantine_status,publication_blockers FROM run_items WHERE run_id=? ORDER BY item_index",
                (run_id,),
            ).fetchall()
        else:
            items = [tuple(row) + ("", "", "") for row in connection.execute(
                "SELECT item_index,source_record_id,free_state,paid_required,paid_state,payload_sha256 FROM run_items WHERE run_id=? ORDER BY item_index",
                (run_id,),
            )]
        if len(items) != item_count or [row[0] for row in items] != list(range(item_count)):
            raise ValueError("checkpoint index coverage mismatch")
        ids = [row[1] for row in items]
        if ids != source_ids or len(set(ids)) != item_count:
            raise ValueError("checkpoint source ID mismatch")
        payloads = connection.execute("SELECT item_index,payload FROM results WHERE run_id=? ORDER BY item_index", (run_id,)).fetchall()
        payload_indexes = [row[0] for row in payloads]
        if len(set(payload_indexes)) != len(payload_indexes) or any(index < 0 or index >= item_count for index in payload_indexes):
            raise ValueError("checkpoint payload coverage mismatch")
        if profile in {"FROZEN_RECOVERY", "FINALIZING", "COMPLETE"} and (len(payloads) != item_count or payload_indexes != list(range(item_count))):
            raise ValueError("checkpoint payload coverage mismatch")
        payload_map = dict(payloads)
        for index, _source_id, free_state, paid_required, paid_state, payload_hash, quarantine_state, quarantine_status, publication_blockers in items:
            if free_state not in {"PENDING", "RUNNING", "DONE", "FAILED", "UNKNOWN", "BLOCKED_BUDGET", "NOT_REQUIRED"}:
                raise ValueError("invalid free scheduler state")
            if paid_state not in {"PENDING", "RUNNING", "DONE", "FAILED", "UNKNOWN", "BLOCKED_BUDGET", "NOT_REQUIRED"}:
                raise ValueError("invalid paid scheduler state")
            if index not in payload_map:
                if profile == "ACTIVE_RESUME" and free_state in {"PENDING", "RUNNING"} and not payload_hash:
                    if manifest.get("provisional"):
                        raise ValueError(f"quarantine metadata is unavailable: {index}")
                    continue
                raise ValueError(f"missing payload: {index}")
            if hashlib.sha256(str(payload_map[index]).encode("utf-8")).hexdigest() != payload_hash:
                raise ValueError(f"payload hash mismatch: {index}")
            try:
                payload = json.loads(payload_map[index])
            except json.JSONDecodeError as exc:
                raise ValueError(f"payload JSON invalid: {index}") from exc
            if payload.get("source_record_id") != _source_id:
                raise ValueError(f"payload source ID mismatch: {index}")
            if not (quarantine_state or quarantine_status or publication_blockers):
                quarantine_state = str(payload.get("quarantine_state", "") or "")
                quarantine_status = str(payload.get("quarantine_status", "") or "")
                publication_blockers = str(payload.get("publication_blockers", "") or "")
            if manifest.get("provisional") and not (quarantine_state or quarantine_status or "legacy_recovery_provisional" in publication_blockers or "HANDOFF_PENDING" in publication_blockers):
                raise ValueError(f"quarantine metadata is unavailable: {index}")
            if quarantine_state or quarantine_status or "legacy_recovery_provisional" in str(publication_blockers) or "HANDOFF_PENDING" in str(publication_blockers):
                payload_blockers = str(payload.get("publication_blockers", ""))
                if payload.get("quarantine_state") != quarantine_state or payload.get("quarantine_status") != quarantine_status or payload.get("publication_eligible") is not False or not ({"legacy_recovery_provisional", "HANDOFF_PENDING"} & set(value.strip() for value in payload_blockers.replace(",", ";").split(";") if value.strip())):
                    raise ValueError(f"payload quarantine mismatch: {index}")
        provider_calls = connection.execute("SELECT provider,state FROM provider_calls WHERE run_id=?", (run_id,)).fetchall()
        for provider, state in provider_calls:
            if provider not in {"brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm"} or state not in {"RESERVED", "RUNNING", "DONE", "FAILED", "UNKNOWN"}:
                raise ValueError("provider ledger state is invalid")
        usage = connection.execute("SELECT provider,configured_limit,effective_limit,reserved,completed,failed FROM provider_usage WHERE run_id=?", (run_id,)).fetchall()
        usage_by_provider = {str(row[0]): row[1:] for row in usage}
        recorded_budgets = ((manifest.get("run_config") or {}).get("budgets") or {})
        for provider in {"brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm"}:
            if provider not in usage_by_provider:
                raise ValueError(f"provider ledger budget is missing: {provider}")
            configured, effective, reserved, completed, failed = usage_by_provider[provider]
            if provider not in {"brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm"} or min(configured, effective, reserved, completed, failed) < 0 or effective > configured:
                raise ValueError("provider ledger budget is invalid")
            if provider in recorded_budgets and int(effective) != int(recorded_budgets[provider]):
                raise ValueError(f"provider ledger budget does not match recorded config: {provider}")
            actual = connection.execute("SELECT state,COUNT(*) FROM provider_calls WHERE run_id=? AND provider=? GROUP BY state", (run_id, provider)).fetchall()
            counts = {str(state): int(count) for state, count in actual}
            if int(reserved) != counts.get("RESERVED", 0) + counts.get("RUNNING", 0):
                raise ValueError(f"provider reserved counter mismatch: {provider}")
            if int(completed) != counts.get("DONE", 0):
                raise ValueError(f"provider completed counter mismatch: {provider}")
            if int(failed) != counts.get("FAILED", 0) + counts.get("UNKNOWN", 0):
                raise ValueError(f"provider failed counter mismatch: {provider}")
    artifact_required = profile in {"FROZEN_RECOVERY", "COMPLETE"} or profile == "FINALIZING" and bool(manifest.get("complete"))
    if artifact_required and require_artifacts:
        artifact_hash = str(manifest.get("artifact_set_sha256", ""))
        files = manifest.get("files")
        artifact_dir = run_root / "output" / "artifacts" / artifact_hash
        if not artifact_hash or not isinstance(files, dict) or not files or not artifact_dir.is_dir():
            raise ValueError("immutable artifact set is missing")
        aggregate = []
        for name, info in sorted(files.items()):
            artifact = artifact_dir / str(name)
            if artifact.parent != artifact_dir or not artifact.is_file() or int(info.get("bytes", -1)) != artifact.stat().st_size:
                raise ValueError(f"artifact metadata mismatch: {name}")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            if digest != info.get("sha256"):
                raise ValueError(f"artifact hash mismatch: {name}")
            aggregate.append(f"{artifact.name}:{digest}\n")
        if hashlib.sha256("".join(aggregate).encode("utf-8")).hexdigest() != artifact_hash:
            raise ValueError("aggregate artifact hash mismatch")
        compatibility = run_root / "output" / "migration_report.json"
        immutable_report = artifact_dir / "migration_report.json"
        if compatibility.exists() and immutable_report.exists() and compatibility.read_bytes() != immutable_report.read_bytes():
            raise ValueError("compatibility report is not byte-identical")
        if profile == "FROZEN_RECOVERY":
            checkpoint_artifact = artifact_dir / "recovery_state.sqlite3"
            if not checkpoint_artifact.is_file() or checkpoint_artifact.read_bytes() != db_path.read_bytes():
                raise ValueError("frozen checkpoint is not byte-identical")
    if profile in {"FINALIZING", "COMPLETE"}:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as connection:
            phase = connection.execute("SELECT phase FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not phase or str(phase[0]) != ("COMPLETE" if profile == "COMPLETE" else "FINALIZING"):
                raise ValueError("run/database phase mismatch")
            unresolved = connection.execute("SELECT COUNT(*) FROM provider_calls WHERE run_id=? AND state IN ('RESERVED','RUNNING')", (run_id,)).fetchone()[0]
            if unresolved:
                raise ValueError("unresolved provider calls remain")
            terminal = connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND (free_state IN ('PENDING','RUNNING','UNKNOWN','BLOCKED_BUDGET') OR (paid_required=1 AND paid_state IN ('PENDING','RUNNING','UNKNOWN','BLOCKED_BUDGET')))", (run_id,)).fetchone()[0]
            if terminal:
                raise ValueError("nonterminal scheduler items remain")
    return {"run_id": run_id, "item_count": item_count, "source_ids": source_ids, "manifest": manifest}


def write_manifest(path: Path, context: RunContext, run_config: RunConfig, *, complete: bool = False, extra: dict | None = None) -> None:
    payload = {}
    if path.exists():
        try:
            payload.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    payload.update({
        "manifest_schema_version": 4,
        "complete": bool(complete),
        "run_id": context.run_id,
        "input_sha256": context.input_hash,
        "phase": context.phase,
        "paid_enabled": run_config.paid_enabled,
        "context": context.as_dict(),
        "run_config": run_config.as_dict(),
        "config_sha256": run_config.sha256,
    })
    if extra:
        payload.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
