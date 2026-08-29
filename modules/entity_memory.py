"""Versioned discovery memory for previously revalidated official domains."""

from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import config
from modules import publication_policy, redaction, scorer


_LOCK = threading.Lock()
SCHEMA_VERSION = 1


def _load() -> list[dict]:
    try:
        lines = config.VERIFIED_ENTITY_MEMORY_FILE.read_text(
            encoding="utf-8"
        ).splitlines()
    except (FileNotFoundError, OSError):
        return []
    values: list[dict] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(value, dict)
            and value.get("schema_version") == SCHEMA_VERSION
        ):
            values.append(value)
    return values


def candidates(company: str) -> list[dict]:
    """Return non-authoritative domain hints for an exact remembered entity."""
    normalized = scorer.normalize_text(company).strip()
    by_domain: dict[str, dict] = {}
    for record in _load():
        if scorer.normalize_text(record.get("company", "")).strip() != normalized:
            continue
        domain = scorer.normalize_domain(record.get("domain", ""))
        if domain:
            by_domain[domain] = record
    return list(by_domain.values())


def remember(rows: list[dict]) -> int:
    """Persist only published, conflict-free, same-site-contact resolutions."""
    additions: list[dict] = []
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for row in rows:
        if (
            not publication_policy.is_publishable_row(row)
            or row.get("quarantine_state")
            or row.get("quarantine_status")
            or "legacy_recovery_provisional" in str(row.get("publication_blockers", ""))
        ):
            continue
        evaluation = row.get("__evaluation", {})
        if not isinstance(evaluation, dict):
            continue
        resolution = str(evaluation.get("_identity_resolution", ""))
        if not resolution.startswith(("candidate_resolved_", "profile_route_resolved_")):
            continue
        if evaluation.get("identity_assessment", {}).get("conflicts"):
            continue
        domain = scorer.normalize_domain(row.get("website", ""))
        contact_urls = [
            row.get("email_source_url", ""),
            row.get("phone_source_url", ""),
        ]
        if not domain or not any(
            scorer.same_registrable_domain(domain, value)
            for value in contact_urls if value
        ):
            continue
        additions.append({
            "schema_version": SCHEMA_VERSION,
            "receipt_key": str(row.get("__memory_receipt_key", "")),
            "company": row.get("company", ""),
            "domain": domain,
            "resolution": resolution,
            "evidence_urls": list(dict.fromkeys(
                str(page)
                for page in evaluation.get("crawl", {}).get("pages", [])
                if page
            ))[:5],
            "observed_at": observed_at,
            "authority": "discovery_hint_revalidate_every_run",
        })
    if not additions:
        return 0
    with _LOCK, _file_lock(config.VERIFIED_ENTITY_MEMORY_FILE):
        existing = _load()
        existing_receipts = {
            str(item.get("receipt_key", ""))
            for item in existing
            if item.get("receipt_key")
        }
        additions = [
            item for item in additions
            if not item.get("receipt_key") or item["receipt_key"] not in existing_receipts
        ]
        if not additions:
            return 0
        keyed = {
            (
                scorer.normalize_text(item.get("company", "")).strip(),
                scorer.normalize_domain(item.get("domain", "")),
            ): item for item in existing
        }
        for item in additions:
            keyed[(
                scorer.normalize_text(item["company"]).strip(),
                item["domain"],
            )] = item
        config.VERIFIED_ENTITY_MEMORY_FILE.parent.mkdir(
            parents=True, exist_ok=True,
        )
        target = config.VERIFIED_ENTITY_MEMORY_FILE
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            for item in keyed.values():
                handle.write(json.dumps(redaction.sanitize(item), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    return len(additions)


@contextmanager
def _file_lock(target):
    """Serialize read/merge/replace across processes without losing entries."""
    lock_path = target.with_name(f".{target.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
