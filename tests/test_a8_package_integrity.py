from __future__ import annotations

import hashlib
import json
from pathlib import Path

from openpyxl import Workbook
import pytest

from tools.assemble_a8_package import PACKAGE_ID_ALGORITHM, canonical_hash, file_spec
from validate_benchmark_suite import _validate_actual_manifest, validate_package_integrity


def _package(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "staging"
    root.mkdir()
    data = root / "runtime-lock.txt"
    data.write_text("https://example.invalid/pkg.conda\n", encoding="utf-8")
    manifest_without_id = {
        "schema_version": 3,
        "benchmark": "A8",
        "package_id_algorithm": PACKAGE_ID_ALGORITHM,
        "base_commit": "a" * 40,
        "uncommitted": False,
        "files": [file_spec(data, "runtime-lock.txt")],
    }
    package_id = canonical_hash(manifest_without_id)
    package = tmp_path / f"a8_benchmark_{package_id}"
    root.rename(package)
    (package / "benchmark_manifest.json").write_text(json.dumps({**manifest_without_id, "package_id_sha256": package_id}, separators=(",", ":")), encoding="utf-8")
    return package, package / "benchmark_manifest.json"


def test_valid_v3_package_passes_integrity():
    package, manifest = _package(Path(__import__("tempfile").mkdtemp()))
    assert package.name.endswith(json.loads(manifest.read_text())["package_id_sha256"])
    assert validate_package_integrity(manifest) == []


def test_one_byte_tamper_and_undeclared_file_fail_integrity(tmp_path: Path):
    package, manifest = _package(tmp_path)
    (package / "runtime-lock.txt").write_text("tampered\n", encoding="utf-8")
    (package / "undeclared.txt").write_text("x", encoding="utf-8")
    issues = validate_package_integrity(manifest)
    assert any("hash mismatch" in issue for issue in issues)
    assert any("undeclared" in issue for issue in issues)


def test_path_traversal_and_package_suffix_mismatch_fail(tmp_path: Path):
    package, manifest = _package(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["files"][0]["path"] = "../escape.txt"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    issues = validate_package_integrity(manifest)
    assert any("escapes root" in issue for issue in issues)
    assert any("package_id_sha256" in issue for issue in issues)


def test_handoff_actual_is_structural_failure(tmp_path: Path):
    expected = tmp_path / "expected.xlsx"
    actual = tmp_path / "actual.xlsx"
    workbook = Workbook()
    workbook.active.append(["source_record_id"])
    workbook.active.append(["src:1"])
    workbook.save(expected)
    workbook.close()
    workbook = Workbook()
    workbook.active.append(["source_record_id", "publication_eligible"])
    workbook.active.append(["src:1", False])
    workbook.save(actual)
    workbook.close()
    manifest = tmp_path / "actual_manifest.json"
    manifest.write_text(json.dumps({"status": "handoff_checkpoint_materialized", "finalized": False, "complete": False, "phase": "PAID"}), encoding="utf-8")
    issues = _validate_actual_manifest(manifest, expected, actual, "diagnostic")
    assert any("not a finalized" in issue for issue in issues)


def test_assembler_requires_clean_worktree(monkeypatch):
    from tools.assemble_a8_package import _git_identity
    import tools.assemble_a8_package as assembler
    from types import SimpleNamespace

    root = Path(assembler.__file__).resolve().parents[1]

    def fake_run(command, **_kwargs):
        if command[1:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(stdout="deadbeef\n")
        return SimpleNamespace(stdout=" M test-fixture\n")

    monkeypatch.setattr(assembler.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="clean worktree"):
        _git_identity(root)
