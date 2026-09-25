"""Deterministic transport-only harness for PETZOO acceptance runs."""

from __future__ import annotations

import hashlib
import io
import json
import os
import random
import socket
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import requests
from openpyxl import Workbook

import config
import main
from modules import checkpoint, crawler, network_guard, runtime, search

try:
    import dns.resolver
except ImportError:
    dns_resolver = None
else:
    dns_resolver = dns.resolver


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "petzoo_acceptance"
PROVIDER_BUDGETS = {
    "brightdata": 535,
    "google_places": 35,
    "brandfetch": 35,
    "hunter": 14,
    "linkedin": 1085,
    "llm": 478,
}
FIXED_RUNTIME_LIMITS = {
    "min_delay_seconds": 1.0,
    "max_delay_seconds": 3.0,
    "global_requests_per_second": 3.0,
    "brightdata_requests_per_minute": 13.0,
    "max_retries": 2,
    "max_retry_after_seconds": 30,
}


def load_fixture() -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    manifest_path = FIXTURE_ROOT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("providers") != PROVIDER_BUDGETS:
        raise RuntimeError("fixture provider budgets differ from the frozen harness contract")
    if manifest.get("runtime_limits") != FIXED_RUNTIME_LIMITS:
        raise RuntimeError("fixture runtime limits differ from the frozen harness contract")
    records: list[dict[str, Any]] = []
    for group in manifest["groups"]:
        subcases = []
        for name, count in group.get("subcases", {}).items():
            subcases.extend([name] * int(count))
        for ordinal in range(int(group["count"])):
            subcase = subcases[ordinal] if ordinal < len(subcases) else ""
            index = len(records)
            company_pattern = str(group.get("company_pattern") or "")
            company = (
                company_pattern.format(pair=ordinal // int(group.get("pair_size", 1)), ordinal=ordinal)
                if company_pattern else f"PETZOO {group['id']}-{ordinal:03d}"
            )
            listed_by_ordinal = group.get("listed_website_by_ordinal", {})
            listed_website = str(
                listed_by_ordinal.get(f"{ordinal:03d}", group.get("listed_website") or "")
            ).format(ordinal=ordinal)
            record = {
                "item_index": index,
                "source_record_id": f"petzoo:{group['id']}:{ordinal:03d}",
                "company": company,
                "group": str(group["id"]),
                "scenario": str(group["scenario"]),
                "subcase": subcase,
                "website": (
                    f"https://petzoo-a-{ordinal:03d}.example/"
                    if group["id"] == "A" else
                    f"https://company-h-{ordinal:03d}.example/"
                    if group["id"] == "H" else ""
                ),
                "listed_website": listed_website,
            }
            if group.get("brand_pattern"):
                record["brands"] = str(group["brand_pattern"]).format(ordinal=ordinal)
                record["legal_name"] = company
            if group.get("sector"):
                record["sector"] = str(group["sector"])
            records.append(record)
    execution_rank = {
        str(group_id): index
        for index, group_id in enumerate(manifest.get("execution_order", []))
    }
    records.sort(key=lambda row: (
        execution_rank.get(
            row["source_record_id"],
            execution_rank.get(row["group"], len(execution_rank)),
        ),
        row["source_record_id"],
    ))
    for index, record in enumerate(records):
        record["item_index"] = index
    canonical = json.dumps(
        {"manifest": manifest, "records": records},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return manifest, records, hashlib.sha256(canonical).hexdigest()


def write_input_book(path: Path, records: list[dict[str, Any]]) -> None:
    book = Workbook()
    sheet = book.active
    sheet.append(["company", "source_record_id", "website", "listed_website", "sector", "legal_name", "brands"])
    for row in records:
        sector = str(row.get("sector") or ("pet food" if row["group"] == "H" else "pet products"))
        sheet.append([
            row["company"], row["source_record_id"], row["website"], row.get("listed_website", ""),
            sector, str(row.get("legal_name") or row["company"]), str(row.get("brands") or ""),
        ])
    book.save(path)
    book.close()


class FakeResponse(requests.Response):
    def __init__(self, status: int = 200, payload: Any = None,
                 headers: dict[str, str] | None = None, *, url: str = ""):
        super().__init__()
        self.status_code = int(status)
        self.url = str(url)
        self.headers.update(headers or {})
        self.headers.setdefault("content-type", "application/json; charset=utf-8")
        self._json_payload = payload
        if isinstance(payload, (dict, list)):
            self._content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, bytes):
            self._content = payload
        else:
            self._content = str(payload or "").encode("utf-8")
        self._content_consumed = True
        self.raw = io.BytesIO()
        self.encoding = "utf-8"

    def json(self):
        if self._json_payload is not None and isinstance(self._json_payload, (dict, list)):
            return self._json_payload
        return json.loads(self.text)


def _company_html(company: str, *, website: str, include_email: bool = True,
                  conflicting_sector: bool = False,
                  identify_company: bool = True,
                  structured_identity: bool = True,
                  include_contact: bool = True) -> str:
    activity = (
        "industrial packaging machinery and packaging systems"
        if conflicting_sector else "pet food and animal nutrition products"
    )
    host = (urlparse(website).hostname or "").casefold()
    contact_email = (
        f"<p>info@{host}</p>"
        if include_email and identify_company and include_contact and host else ""
    )
    identity_markup = (
        "<script type='application/ld+json'>"
        + json.dumps({
            "@context": "https://schema.org", "@type": "Organization",
            "name": company, "url": website,
            "address": {"addressCountry": "TR"},
        }, ensure_ascii=False)
        + "</script>"
        if identify_company and structured_identity else ""
    )
    page_title = company if identify_company else "Welcome"
    page_heading = company if identify_company else "Welcome"
    page_description = company if identify_company else "Independent product information and support."
    phone = (
        "<p>Telefon +90 212 555 12 12</p>"
        if identify_company and include_contact else ""
    )
    return (
        "<html><head><title>" + page_title + "</title>"
        + identity_markup + "</head><body><h1>" + page_heading + "</h1><p>"
        + page_description + " resmi şirket sitesi Türkiye İstanbul. " + activity
        + "</p>" + phone + contact_email
        + "<a href='/contact'>İletişim</a></body></html>"
    )


class FixtureSession:
    """A HTTP transport replacement; URL policy/DNS validation still runs."""

    def __init__(self, records: list[dict[str, Any]], journal_path: Path | None = None):
        self.records_by_host = {
            (urlparse(str(row.get("website") or (
                f"https://petzoo-{row['group'].lower()}-"
                f"{int(row['source_record_id'].rsplit(':', 1)[1]):03d}.example/"
            ))).hostname or "").casefold(): row
            for row in records
        }
        for row in records:
            if row.get("group") == "H" and row.get("subcase") in {
                "linkedin_resolution", "llm_context_conflict",
            }:
                source = str(row.get("source_record_id", "petzoo:H:000"))
                ordinal = int(source.rsplit(":", 1)[1])
                self.records_by_host[f"petzoo{ordinal:03d}.example"] = row
        self.journal_path = journal_path
        self._lock = threading.Lock()
        self.counts: dict[str, int] = {}

    def get(self, url: str, **_kwargs):
        host = (urlparse(str(url)).hostname or "").casefold()
        record = self.records_by_host.get(host)
        if record is None:
            response = FakeResponse(404, "not found", {"content-type": "text/plain"}, url=str(url))
            return response
        key = f"{host}{urlparse(str(url)).path or '/'}"
        with self._lock:
            ordinal = self.counts.get(key, 0) + 1
            self.counts[key] = ordinal
        if self.journal_path:
            _append_jsonl(self.journal_path, {
                "kind": "http", "url": str(url), "host": host,
                "attempt_ordinal": ordinal, "pid": os.getpid(),
                "source_record_id": record["source_record_id"],
            })
        root = urlparse(str(url)).path in {"", "/"}
        if root and record["group"] == "D":
            return FakeResponse(403, "blocked", {"content-type": "text/html"}, url=str(url))
        if root and record["group"] == "E":
            subcase = record["subcase"]
            if subcase == "timeout":
                raise requests.Timeout("fixture timeout")
            if subcase == "http_404":
                return FakeResponse(404, "missing", {"content-type": "text/plain"}, url=str(url))
            if subcase == "http_410":
                return FakeResponse(410, "gone", {"content-type": "text/plain"}, url=str(url))
            if subcase == "unsupported":
                return FakeResponse(200, b"image bytes", {"content-type": "image/png"}, url=str(url))
            if subcase == "transient_then_success" and ordinal == 1:
                return FakeResponse(503, "temporary", {"Retry-After": "0", "content-type": "text/plain"}, url=str(url))
            if subcase in {"http_429", "http_503"}:
                status = 429 if subcase == "http_429" else 503
                return FakeResponse(status, "transient", {"Retry-After": "0", "content-type": "text/plain"}, url=str(url))
        company = str(record["company"])
        source_parts = str(record["source_record_id"]).split(":")
        website = str(record.get("website") or f"https://petzoo-{source_parts[-2].lower()}-{int(source_parts[-1]):03d}.example/")
        return FakeResponse(
            200, _company_html(
                company, website=website, include_email=record["group"] != "A",
                conflicting_sector=(
                    record["group"] == "H"
                    and record.get("subcase") == "llm_context_conflict"
                ),
                identify_company=not (
                    record["group"] == "H"
                    and record.get("subcase") == "linkedin_resolution"
                ),
                # The LinkedIn subcase deliberately has a reachable but
                # identity-weak page; LinkedIn must be the evidence that
                # resolves it, not a strong title/meta hit from the fixture.
                structured_identity=not (
                    record["group"] == "H"
                    and record.get("subcase") == "linkedin_resolution"
                ),
                include_contact=not (
                    record["group"] == "H"
                    and record.get("subcase") == "linkedin_resolution"
                ),
            ),
            {"content-type": "text/html; charset=utf-8"}, url=str(url),
        )


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+b") as lock:
        if os.name == "nt":
            import msvcrt
            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                with open(path, "ab") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                with open(path, "ab") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class ReplayPaidTransport:
    """Route by durable request fingerprint and attempt ordinal, never pop order."""

    def __init__(self, records: list[dict[str, Any]], journal_path: Path,
                 route_overrides: dict[tuple[str, str, int], Any] | None = None,
                 db_path_getter=None):
        self.records = {int(row["item_index"]): row for row in records}
        self.journal_path = journal_path
        self.route_overrides = dict(route_overrides or {})
        self.db_path_getter = db_path_getter or (lambda: config.PROGRESS_DB_FILE)
        self._lock = threading.Lock()
        self.counts: dict[tuple[str, str], int] = {}

    def _call_receipt(self, call_id: str) -> dict[str, Any]:
        try:
            with sqlite3.connect(f"file:{Path(self.db_path_getter()).resolve()}?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                row = db.execute(
                    "SELECT run_id,item_index,provider,operation,phase,request_fingerprint,"
                    "flight_fingerprint,attempt_ordinal FROM provider_calls WHERE call_id=?",
                    (call_id,),
                ).fetchone()
                if not row:
                    return {}
                receipt = dict(row)
                consumer = db.execute(
                    "SELECT query_fingerprint,execution_generation "
                    "FROM provider_query_flight_consumers WHERE provider_call_id=? "
                    "ORDER BY linked_at LIMIT 1", (call_id,),
                ).fetchone()
                if consumer:
                    receipt["query_fingerprint"] = str(consumer[0] or "")
                    receipt["execution_generation"] = int(consumer[1] or 0)
                    receipt["query_fingerprint_basis"] = "durable_flight_consumer"
                else:
                    work = db.execute(
                        "SELECT query_fingerprint,execution_generation FROM provider_work_items "
                        "WHERE run_id=? AND item_index=? AND provider=? AND operation=? "
                        "AND request_fingerprint=? ORDER BY created_at LIMIT 1",
                        (receipt["run_id"], receipt["item_index"], receipt["provider"],
                         receipt["operation"], receipt["request_fingerprint"]),
                    ).fetchone()
                    query_fingerprint = str(work[0] or "") if work else ""
                    if query_fingerprint:
                        receipt["query_fingerprint_basis"] = "durable_provider_work_item"
                    else:
                        query_fingerprint = str(receipt.get("flight_fingerprint") or receipt["request_fingerprint"])
                        receipt["query_fingerprint_basis"] = (
                            "flight_fingerprint" if receipt.get("flight_fingerprint")
                            else "request_fingerprint_for_non_query_operation"
                        )
                    receipt["query_fingerprint"] = query_fingerprint
                    receipt["execution_generation"] = int(work[1] or 0) if work else 0
                return receipt
        except (OSError, sqlite3.Error):
            return {}

    def __call__(self, envelope):
        provider = str(envelope.provider)
        fingerprint = str(envelope.request_fingerprint)
        attempt = int(envelope.attempt_ordinal)
        identity = (provider, fingerprint, attempt)
        with self._lock:
            ordinal_key = (provider, fingerprint)
            invocation_ordinal = self.counts.get(ordinal_key, 0) + 1
            self.counts[ordinal_key] = invocation_ordinal
        item_index = int(envelope.item_index)
        record = self.records.get(item_index, {})
        call_id = str(envelope.provider_call_id)
        call_receipt = self._call_receipt(call_id)
        entry = {
            "kind": "paid_transport", "provider": provider,
            "request_fingerprint": fingerprint, "attempt_ordinal": attempt,
            "invocation_ordinal": invocation_ordinal, "item_index": item_index,
            "source_record_id": record.get("source_record_id", ""),
            "operation": str(call_receipt.get("operation", "")),
            "query_fingerprint": str(call_receipt.get("query_fingerprint", "")),
            "query_fingerprint_basis": str(call_receipt.get("query_fingerprint_basis", "")),
            "execution_generation": int(call_receipt.get("execution_generation", 0) or 0),
            "flight_fingerprint": str(call_receipt.get("flight_fingerprint", "")),
            "phase": str(call_receipt.get("phase", "")),
            "call_id": call_id,
            "http_started": True, "pid": os.getpid(),
        }
        _append_jsonl(self.journal_path, entry)
        response = None
        try:
            if identity in self.route_overrides:
                route = self.route_overrides[identity]
                response = route() if callable(route) else route
            else:
                group = str(record.get("group", ""))
                operation = str(call_receipt.get("operation", ""))
                if provider == "brightdata":
                    if group == "G" and record.get("subcase") == "post_send_unknown":
                        raise requests.ReadTimeout("controlled post-send uncertainty")
                    elif group == "G":
                        response = FakeResponse(200, {"organic": []}, {
                            "x-brd-err-code": "CAPTCHA", "x-brd-error": "captcha challenge",
                        })
                    elif group == "F":
                        response = FakeResponse(200, {"organic": [{
                            "link": self._website(record), "title": record.get("company", ""),
                            "description": f"{record.get('company', '')} resmi şirket sitesi Türkiye pet ürünleri",
                        }]})
                    elif group == "H" and record.get("subcase") in {
                        "linkedin_resolution", "llm_context_conflict",
                    }:
                        primary_queries = search._primary_queries(record.get("company", ""), record)
                        primary_fingerprint = (
                            search.brightdata_request_fingerprint(primary_queries[0])
                            if primary_queries else ""
                        )
                        response = (
                            FakeResponse(200, {"organic": [{
                                "link": self._candidate_website(record), "title": record.get("company", ""),
                                "description": f"{record.get('company', '')} resmi şirket sitesi Türkiye pet ürünleri",
                            }]})
                            if fingerprint == primary_fingerprint
                            else FakeResponse(200, {"organic": []})
                        )
                    else:
                        response = FakeResponse(200, {"organic": [{
                            "link": self._website(record), "title": record.get("company", ""),
                            "description": f"{record.get('company', '')} resmi şirket sitesi Türkiye pet ürünleri",
                        }]})
                elif provider == "google_places":
                    response = FakeResponse(200, {"places": [] if group == "C" else [{
                        "id": f"place-{item_index}",
                        "displayName": {"text": record.get("company", "")},
                        "websiteUri": self._website(record),
                        "internationalPhoneNumber": "+90 212 555 12 12",
                        "businessStatus": "OPERATIONAL",
                    }]})
                elif provider == "brandfetch":
                    response = FakeResponse(200, [] if group in {"C", "F"} else [{
                        "domain": urlparse(self._website(record)).hostname or "",
                        "name": record.get("company", ""),
                    }])
                elif provider == "hunter":
                    response = FakeResponse(200, {"data": [{
                        "domain": urlparse(self._website(record)).hostname or "",
                        "organization": record.get("company", ""),
                    }]})
                elif provider == "linkedin":
                    if operation == "scrape":
                        response = FakeResponse(200, [{
                            "name": record.get("company", ""),
                            "website": (
                                self._candidate_website(record)
                                if record.get("group") == "H"
                                and record.get("subcase") == "linkedin_resolution"
                                else self._website(record)
                            ),
                            "linkedin_url": f"https://www.linkedin.com/company/petzoo-{item_index}",
                        }])
                    else:
                        response = FakeResponse(200, {"organic": [{
                            "link": f"https://www.linkedin.com/company/petzoo-{item_index}",
                            "title": record.get("company", ""),
                            "description": f"{record.get('company', '')} official company page",
                        }]})
                elif provider == "llm":
                    payload = {
                        "verdict": "match", "reason": "The page identifies the requested organization.",
                        "detected_sector": "pet food and animal nutrition products",
                        "expected_sector": "industrial animal nutrition",
                    }
                    response = FakeResponse(200, {"choices": [{"message": {"content": json.dumps(payload)}}], "usage": {"total_tokens": 17}})
                else:
                    raise AssertionError(f"unrouted provider transport: {provider}")
        except BaseException as exc:
            _append_jsonl(self.journal_path, {
                **entry, "kind": "paid_transport_terminal", "outcome": "exception",
                "exception_type": type(exc).__name__, "response_sha256": "",
                "terminal_event_id": uuid.uuid4().hex,
            })
            raise
        body = getattr(response, "content", b"")
        if not isinstance(body, bytes):
            body = str(body or "").encode("utf-8")
        _append_jsonl(self.journal_path, {
            **entry, "kind": "paid_transport_terminal", "outcome": "response",
            "http_status": int(getattr(response, "status_code", 0) or 0),
            "response_sha256": hashlib.sha256(body).hexdigest(),
            "terminal_event_id": uuid.uuid4().hex,
        })
        return response

    @staticmethod
    def _website(record: dict[str, Any]) -> str:
        if record.get("website"):
            return str(record["website"])
        source = str(record.get("source_record_id", "petzoo:x:000"))
        group, ordinal = source.split(":")[-2:]
        return f"https://petzoo-{group.lower()}-{int(ordinal):03d}.example/"

    @classmethod
    def _candidate_website(cls, record: dict[str, Any]) -> str:
        if record.get("group") == "H" and record.get("subcase") in {
            "linkedin_resolution", "llm_context_conflict",
        }:
            source = str(record.get("source_record_id", "petzoo:H:000"))
            ordinal = int(source.rsplit(":", 1)[1])
            return f"https://petzoo{ordinal:03d}.example/"
        return cls._website(record)


class _VirtualWaitClock:
    """Advance only harness-controlled waits; production limits stay intact."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._now = time.monotonic()

    def monotonic(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += max(0.0, float(seconds))


def install_harness(setter, tmp_path: Path, records: list[dict[str, Any]],
                    *, workers: int = 1, router: ReplayPaidTransport | None = None,
                    http_journal: Path | None = None) -> tuple[Path, ReplayPaidTransport, FixtureSession]:
    observed_limits = {
        "min_delay_seconds": float(config.MIN_DELAY_SEC),
        "max_delay_seconds": float(config.MAX_DELAY_SEC),
        "global_requests_per_second": float(config.GLOBAL_REQUESTS_PER_SECOND),
        "brightdata_requests_per_minute": float(config.BRIGHTDATA_REQUESTS_PER_MINUTE),
        "max_retries": int(config.MAX_RETRIES),
        "max_retry_after_seconds": int(config.MAX_RETRY_AFTER_SEC),
    }
    if observed_limits != FIXED_RUNTIME_LIMITS:
        raise RuntimeError(f"fixture runtime limits differ from frozen contract: {observed_limits}")
    base_dir = Path(config.BASE_DIR).resolve()
    test_root = Path(tmp_path).resolve()
    for name, value in vars(config).items():
        if name == "BASE_DIR" or not isinstance(value, Path):
            continue
        try:
            relative = value.resolve().relative_to(base_dir)
        except ValueError:
            continue
        setter.setattr(config, name, test_root / relative)
    runs_dir = tmp_path / "runs"
    setter.setattr(config, "RUNS_DIR", runs_dir)
    setter.setattr(config, "MAX_WORKERS", workers)
    setter.setattr(config, "SEARCH_PROVIDER", "brightdata")
    setter.setattr(config, "SEARCH_CACHE_MODE", "off")
    setter.setattr(config, "CRAWL_CACHE_MODE", "off")
    setter.setattr(config, "BRIGHTDATA_API_KEY", "fake-petzoo-brightdata")
    setter.setattr(config, "BRIGHTDATA_REQUEST_BUDGET", PROVIDER_BUDGETS["brightdata"])
    setter.setattr(config, "BRIGHTDATA_REQUEST_HARD_CAP", PROVIDER_BUDGETS["brightdata"])
    setter.setattr(config, "GOOGLE_PLACES_API_KEY", "fake-petzoo-places")
    setter.setattr(config, "GOOGLE_PLACES_REQUEST_BUDGET", PROVIDER_BUDGETS["google_places"])
    setter.setattr(config, "ENABLE_GOOGLE_PLACES", True)
    setter.setattr(config, "BRANDFETCH_CLIENT_ID", "fake-petzoo-brandfetch")
    setter.setattr(config, "ENABLE_BRANDFETCH_DOMAIN_SEARCH", True)
    setter.setattr(config, "BRANDFETCH_REQUEST_BUDGET", PROVIDER_BUDGETS["brandfetch"])
    setter.setattr(config, "HUNTER_API_KEY", "fake-petzoo-hunter")
    setter.setattr(config, "ENABLE_HUNTER_DOMAIN_FINDER", True)
    setter.setattr(config, "HUNTER_REQUEST_BUDGET", PROVIDER_BUDGETS["hunter"])
    setter.setattr(config, "ENABLE_LINKEDIN_COMPANY_LOOKUP", True)
    setter.setattr(config, "LINKEDIN_COMPANY_REQUEST_BUDGET", PROVIDER_BUDGETS["linkedin"])
    setter.setattr(config, "ENABLE_LLM_ARBITER", True)
    setter.setattr(config, "OPENROUTER_API_KEY", "fake-petzoo-llm")
    setter.setattr(config, "LLM_ARBITER_BUDGET", PROVIDER_BUDGETS["llm"])
    # Preserve configured rate/retry thresholds and production call paths. Only
    # advance time for waits, as permitted by the transport-only test harness.
    virtual_clock = _VirtualWaitClock()
    runtime._NEXT_REQUEST_AT = 0.0
    search._BRIGHTDATA_NEXT_REQUEST_AT = 0.0
    original_search_time = search.time
    setter.setattr(search, "time", SimpleNamespace(
        monotonic=virtual_clock.monotonic,
        time=original_search_time.time,
        sleep=original_search_time.sleep,
    ))

    def virtual_paid_wait(seconds: float, lease_keeper=None) -> None:
        virtual_clock.advance(seconds)
        if lease_keeper is not None:
            lease_keeper.heartbeat()

    setter.setattr(search, "_paid_wait", virtual_paid_wait)

    def virtual_request_slot(*, waiter=None, clock=None) -> None:
        del clock
        rate = max(float(config.GLOBAL_REQUESTS_PER_SECOND), 0.0)
        if rate <= 0:
            return
        with runtime._LOCK:
            now = virtual_clock.monotonic()
            wait = max(0.0, runtime._NEXT_REQUEST_AT - now)
            runtime._NEXT_REQUEST_AT = max(now, runtime._NEXT_REQUEST_AT) + 1.0 / rate
        if wait:
            if waiter is None:
                virtual_clock.advance(wait)
            else:
                waiter(wait)

    setter.setattr(runtime, "wait_for_request_slot", virtual_request_slot)
    delay_rng = random.Random(0)
    delay_lock = threading.Lock()

    def virtual_random_delay(min_sec=None, max_sec=None) -> None:
        low = float(min_sec or config.MIN_DELAY_SEC)
        high = float(max_sec or config.MAX_DELAY_SEC)
        with delay_lock:
            delay = delay_rng.uniform(low, high)
        virtual_clock.advance(delay)

    setter.setattr(main, "random_delay", virtual_random_delay)

    class EmptyDDGS:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def text(self, *_args, **_kwargs): return []

    setter.setattr(search, "DDGS", EmptyDDGS)
    # Test DNS answers are public documentation IPs; every other hostname is
    # deliberately unresolved. The URL policy continues to reject private IPs,
    # invalid schemes, credentials, and unsafe hosts before the fake transport.
    def resolver(host, port=0, *args, **kwargs):
        del args, kwargs
        if str(host).casefold().endswith(".example"):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", int(port or 443)))]
        raise socket.gaierror("offline fixture resolver blocked non-fixture DNS")

    real_resolve = network_guard.resolve_public_http_url

    def validate_fixture_url(url: str):
        target, reason = real_resolve(url, resolver=resolver)
        return target is not None, reason

    def resolve_fixture_url(url: str):
        return real_resolve(url, resolver=resolver)

    setter.setattr(network_guard, "validate_public_http_url", validate_fixture_url)
    setter.setattr(network_guard, "resolve_public_http_url", resolve_fixture_url)
    # Candidate-domain discovery uses socket.getaddrinfo directly. Give the
    # fixture zone a deterministic public answer and fail every other host
    # closed so neither the OS resolver nor the audit hook's fsync journal is
    # exercised by a fake transport run.
    setter.setattr(socket, "getaddrinfo", resolver)
    if dns_resolver is not None:
        def no_external_dns_answer(*_args, **_kwargs):
            raise dns_resolver.NoAnswer()

        setter.setattr(dns_resolver.Resolver, "resolve", no_external_dns_answer)
    session = FixtureSession(records, http_journal)
    setter.setattr(crawler, "_http_session", lambda: session)
    def render_fixture(url: str):
        host = (urlparse(str(url)).hostname or "").casefold()
        record = session.records_by_host.get(host)
        if not record or record.get("group") != "D":
            return None, "fixture_browser_route_missing"
        source_parts = str(record["source_record_id"]).split(":")
        website = str(record.get("website") or f"https://petzoo-{source_parts[-2].lower()}-{int(source_parts[-1]):03d}.example/")
        return _company_html(str(record["company"]), website=website), None

    setter.setattr(crawler, "_try_render", render_fixture)
    if router is None:
        route_path = tmp_path / "transport.jsonl"
        router = ReplayPaidTransport(records, route_path)
    setter.setattr(runtime, "_PAID_TRANSPORT", router)
    return runs_dir, router, session


def petzoo_group_c_transport(records: list[dict[str, Any]], journal_path: Path) -> tuple[ReplayPaidTransport, dict[str, Any]]:
    """Freeze C responses by exact Bright Data request fingerprint and attempt ordinal."""
    by_fingerprint: dict[str, dict[str, Any]] = {}
    for record in records:
        if str(record.get("group", "")) != "C":
            continue
        for query in search._primary_queries(str(record["company"]), record):
            fingerprint = search.brightdata_request_fingerprint(query)
            entry = by_fingerprint.setdefault(fingerprint, {"sources": set(), "record": record, "query": query})
            entry["sources"].add(str(record["source_record_id"]))
    overrides = {}
    for fingerprint, entry in by_fingerprint.items():
        record = entry["record"]
        if len(entry["sources"]) > 1:
            response = FakeResponse(200, {"organic": []})
            route_kind = "shared_terminal_empty"
        else:
            website = ReplayPaidTransport._website(record)
            response = FakeResponse(200, {"organic": [{
                "link": website, "title": str(record["company"]),
                "description": f"{record['company']} resmi şirket sitesi Türkiye pet ürünleri",
            }]})
            route_kind = "distinct_query_candidate"
        overrides[("brightdata", fingerprint, 1)] = response
        entry["sources"] = sorted(entry["sources"])
        entry["route_kind"] = route_kind
    route_report = {
        "routes": [
            {"request_fingerprint": fingerprint, "query": entry["query"],
             "source_record_ids": entry["sources"], "attempt_ordinal": 1,
             "route_kind": entry["route_kind"]}
            for fingerprint, entry in sorted(by_fingerprint.items())
        ],
        "shared_terminal_fingerprint_count": sum(len(entry["sources"]) > 1 for entry in by_fingerprint.values()),
        "distinct_query_fingerprint_count": sum(len(entry["sources"]) == 1 for entry in by_fingerprint.values()),
    }
    return ReplayPaidTransport(records, journal_path, route_overrides=overrides), route_report
