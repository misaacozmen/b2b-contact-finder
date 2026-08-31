import gzip
import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from modules import checkpoint, replay_snapshot


def test_large_nested_bodies_are_externalized_and_round_trip(tmp_path):
    db = tmp_path / "progress.sqlite3"
    replay_snapshot.reset()
    replay_snapshot.configure_run_store(db, "run-1")
    body = "<html>" + ("x" * (5 * 1024 * 1024)) + "</html>"
    value = {"nested": {"html": body, "body": body}, "raw_body": body}
    replay_snapshot.record("crawl", "pages", "same", 1, value)
    replay_snapshot.record("crawl", "pages", "same-2", 1, {"body": body})

    with sqlite3.connect(db) as connection:
        stored = [row[0] for row in connection.execute("SELECT value_json FROM replay_entries")]
    assert all(body not in item for item in stored)
    assert len(list((tmp_path / "replay_shards").glob("*.json.gz"))) == 1
    assert replay_snapshot.lookup("crawl", "pages", "same", 1)[1] == value

    export = tmp_path / "export"
    manifest = replay_snapshot.export_shards(export, run_id="run-1", config_hash="config-1")
    assert len(manifest["replay_shards"]) == 1
    replay_snapshot.reset()
    replay_snapshot.load_shards(export / "manifest.json", expected_run_id="run-1", expected_config_hash="config-1")
    assert replay_snapshot.lookup("crawl", "pages", "same", 1)[1] == value


def test_handoff_snapshot_removes_replay_entries_and_preserves_payloads(tmp_path):
    db = tmp_path / "active.sqlite3"
    payload = json.dumps({"source_record_id": "input:1", "company": "Example"}, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    checkpoint.seed_recovered_run(
        path=db,
        run_id="run-1",
        input_hash="input-hash",
        run_signature="signature",
        context={"phase": "FREE"},
        budgets={},
        items=[{"item_index": 0, "source_record_id": "input:1", "free_state": "DONE", "paid_required": False, "paid_state": "NOT_REQUIRED", "payload_sha256": digest}],
        results=[{"item_index": 0, "payload": payload}],
    )
    replay_snapshot.reset()
    replay_snapshot.configure_run_store(db, "run-1")
    replay_snapshot.record("crawl", "pages", "one", 1, {"html": "cached"})
    handoff = tmp_path / "handoff.sqlite3"
    result = checkpoint.create_handoff_snapshot(db, handoff, run_id="run-1", expected_count=1)
    assert result["replay_entries"] == 0
    with sqlite3.connect(handoff) as connection:
        assert connection.execute("SELECT COUNT(*) FROM replay_entries").fetchone()[0] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT payload FROM results").fetchone()[0] == payload


def _replacement_pair(tmp_path, journal_mode="DELETE"):
    active = tmp_path / "active.sqlite3"
    snapshot = tmp_path / "snapshot.sqlite3"
    for path, value in ((active, "original"), (snapshot, "replacement")):
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(f"PRAGMA journal_mode={journal_mode}")
            connection.execute("CREATE TABLE payloads (value TEXT)")
            connection.execute("INSERT INTO payloads VALUES (?)", (value,))
            connection.commit()
    return active, snapshot, {
        "expected_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "expected_bytes": snapshot.stat().st_size,
    }


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
def test_handoff_checkpoint_replacement_is_atomic_and_validated(tmp_path, journal_mode):
    active, snapshot, expected = _replacement_pair(tmp_path, journal_mode)
    checkpoint.replace_handoff_checkpoint(snapshot, active, **expected)
    assert active.read_bytes() == snapshot.read_bytes()
    with closing(sqlite3.connect(active)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT value FROM payloads").fetchone()[0] == "replacement"
    assert not list(tmp_path.glob(".*.staging"))


@pytest.mark.parametrize("failure", ["copy", "replace"])
def test_handoff_checkpoint_failure_preserves_original(tmp_path, monkeypatch, failure):
    active, snapshot, expected = _replacement_pair(tmp_path)
    before = hashlib.sha256(active.read_bytes()).hexdigest()

    def fail_copy(source, target):
        target.write(source.read(128))
        raise OSError("simulated disk full")

    def fail_replace(source, destination):
        raise PermissionError("simulated open checkpoint handle")

    if failure == "copy":
        monkeypatch.setattr(checkpoint.shutil, "copyfileobj", fail_copy)
    else:
        monkeypatch.setattr(checkpoint.os, "replace", fail_replace)
    with pytest.raises(OSError):
        checkpoint.replace_handoff_checkpoint(snapshot, active, **expected)
    assert hashlib.sha256(active.read_bytes()).hexdigest() == before
    assert not list(tmp_path.glob(".*.staging"))


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("location", ["active", "snapshot"])
def test_handoff_checkpoint_rejects_sidecars_without_removing_them(tmp_path, suffix, location):
    active, snapshot, expected = _replacement_pair(tmp_path)
    before = active.read_bytes()
    owner = active if location == "active" else snapshot
    sidecar = owner.with_name(owner.name + suffix)
    sidecar.write_bytes(b"must survive")
    with pytest.raises(RuntimeError, match="sidecars"):
        checkpoint.replace_handoff_checkpoint(snapshot, active, **expected)
    assert active.read_bytes() == before
    assert sidecar.read_bytes() == b"must survive"
    assert not list(tmp_path.glob(".*.staging"))


def test_handoff_checkpoint_rejects_open_read_transaction(tmp_path):
    active, snapshot, expected = _replacement_pair(tmp_path)
    before = active.read_bytes()
    with closing(sqlite3.connect(active)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT value FROM payloads").fetchall()
        with pytest.raises(RuntimeError, match="idle checkpoint"):
            checkpoint.replace_handoff_checkpoint(snapshot, active, **expected)
        assert active.read_bytes() == before
    assert not list(tmp_path.glob(".*.staging"))


@pytest.mark.parametrize("field", ["expected_sha256", "expected_bytes"])
def test_handoff_checkpoint_rejects_invalid_snapshot_identity(tmp_path, field):
    active, snapshot, expected = _replacement_pair(tmp_path)
    before = active.read_bytes()
    expected[field] = "0" * 64 if field == "expected_sha256" else expected[field] + 1
    with pytest.raises(RuntimeError, match="hash or size mismatch"):
        checkpoint.replace_handoff_checkpoint(snapshot, active, **expected)
    assert active.read_bytes() == before


def test_handoff_checkpoint_rejects_corrupt_snapshot_before_replacement(tmp_path):
    active, snapshot, expected = _replacement_pair(tmp_path)
    before = active.read_bytes()
    snapshot.write_bytes(b"not a SQLite database")
    expected = {
        "expected_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "expected_bytes": snapshot.stat().st_size,
    }
    with pytest.raises(sqlite3.DatabaseError):
        checkpoint.replace_handoff_checkpoint(snapshot, active, **expected)
    assert active.read_bytes() == before
    assert not list(tmp_path.glob(".*.staging"))


def test_handoff_snapshot_rejects_active_destination(tmp_path):
    active, snapshot, expected = _replacement_pair(tmp_path)
    before = active.read_bytes()
    with pytest.raises(ValueError, match="active source"):
        checkpoint.create_handoff_snapshot(active, active)
    with pytest.raises(ValueError, match="differ"):
        checkpoint.replace_handoff_checkpoint(active, active, **expected)
    assert active.read_bytes() == before


def test_nested_body_secrets_are_redacted_before_local_and_export_shards(tmp_path):
    replay_snapshot.reset()
    replay_snapshot.configure_run_store(tmp_path / "progress.sqlite3", "run-1")
    secret = "REPLAY_BODY_SECRET_3497"
    body = f'<a href="https://example.test/?token={secret}">contact</a>'
    value = {"nested": [{"html": body, "body": body}], "raw_body": body}
    replay_snapshot.record("crawl", "pages", "secret-body", 1, value)
    export = tmp_path / "export"
    manifest = replay_snapshot.export_shards(export, run_id="run-1", config_hash="config-1")
    assert manifest["replay_shards"]
    for root in (tmp_path / "replay_shards", export / "replay_shards"):
        paths = list(root.glob("*.json.gz"))
        assert paths
        for path in paths:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                text = handle.read()
            assert secret not in text
            assert "[REDACTED]" in text
    replay_snapshot.reset()
    replay_snapshot.load_shards(export / "manifest.json", expected_run_id="run-1", expected_config_hash="config-1")
    found, hydrated = replay_snapshot.lookup("crawl", "pages", "secret-body", 1)
    assert found
    assert secret not in json.dumps(hydrated)
