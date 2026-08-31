import os
from pathlib import Path

import pytest

from tools import create_validation_workspace as workspace_module
from tools.create_validation_workspace import ReparsePointError, cleanup_validation_workspace, create_validation_workspace


def _minimal_source(root: Path) -> None:
    (root / "modules").mkdir()
    (root / "tools").mkdir()
    (root / "tests" / "fixtures").mkdir(parents=True)
    (root / "modules" / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tools" / "helper.py").write_text("VALUE = 2\n", encoding="utf-8")
    (root / "tools" / "prune_disposable_workspaces.ps1").write_text("# safety helper\n", encoding="utf-8")
    (root / "tools" / "private.txt").write_text("not source\n", encoding="utf-8")
    (root / "tests" / "example_test.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    (root / "tests" / "fixtures" / "fixture.bin").write_bytes(b"fixture")
    (root / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "README.md").write_text("docs\n", encoding="utf-8")
    (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    for name in ("input", "output", "state", "runs", "node_modules", "cache"):
        (root / name).mkdir()
        (root / name / "should_not_copy.txt").write_text("secret\n", encoding="utf-8")


def test_workspace_allowlist_is_bounded_and_cleans_up(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _minimal_source(source)
    receipt = source / "receipt.json"
    workspace = create_validation_workspace(source, temp_root=tmp_path / "temp", receipt=receipt)
    try:
        assert workspace.parent.name == "b2b_validation"
        assert (workspace / "modules" / "example.py").is_file()
        assert (workspace / "tools" / "helper.py").is_file()
        assert (workspace / "tools" / "prune_disposable_workspaces.ps1").is_file()
        assert not (workspace / "tools" / "private.txt").exists()
        assert (workspace / "tests" / "fixtures" / "fixture.bin").is_file()
        assert not (workspace / "input").exists()
        assert not (workspace / "node_modules").exists()
        assert sum(path.stat().st_size for path in workspace.rglob("*") if path.is_file()) < 25 * 1024 * 1024
        assert receipt.is_file()
    finally:
        cleanup_validation_workspace(workspace)
    assert not workspace.exists()


@pytest.mark.parametrize("tree", ["modules", "tools"])
def test_reparse_point_in_selected_tree_fails_closed(tmp_path, tree):
    source = tmp_path / "source"
    source.mkdir()
    _minimal_source(source)
    target = source / tree / "junction"
    try:
        os.symlink(source / "tests", target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ReparsePointError):
        create_validation_workspace(source, temp_root=tmp_path / "temp")


def test_cleanup_rejects_unregistered_directory(tmp_path):
    marker = tmp_path / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="not registered"):
        cleanup_validation_workspace(tmp_path)
    assert marker.read_text(encoding="utf-8") == "keep"


def test_cleanup_rejects_replaced_directory_identity(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _minimal_source(source)
    workspace = create_validation_workspace(source, temp_root=tmp_path / "temp")
    saved = workspace.with_name(workspace.name + "_saved")
    workspace.rename(saved)
    workspace.mkdir()
    marker = workspace / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="identity changed"):
            cleanup_validation_workspace(workspace)
        assert marker.read_text(encoding="utf-8") == "keep"
        assert workspace in workspace_module._ACTIVE
    finally:
        marker.unlink()
        workspace.rmdir()
        saved.rename(workspace)
        cleanup_validation_workspace(workspace)


def test_cleanup_rejects_registered_root_reparse_before_resolve(tmp_path, monkeypatch):
    from types import SimpleNamespace

    source = tmp_path / "source"
    source.mkdir()
    _minimal_source(source)
    workspace = create_validation_workspace(source, temp_root=tmp_path / "temp")
    original_lstat = os.lstat

    def changed_root(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if Path(path) == workspace:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    try:
        with monkeypatch.context() as patch:
            patch.setattr(workspace_module.os, "lstat", changed_root)
            with pytest.raises(ReparsePointError):
                cleanup_validation_workspace(workspace)
        assert (workspace / "modules" / "example.py").is_file()
        assert workspace in workspace_module._ACTIVE
    finally:
        cleanup_validation_workspace(workspace)


def test_cleanup_keeps_registration_after_delete_failure(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _minimal_source(source)
    workspace = create_validation_workspace(source, temp_root=tmp_path / "temp")

    def denied(path):
        raise PermissionError("synthetic deletion failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(workspace_module.shutil, "rmtree", denied)
            with pytest.raises(PermissionError):
                cleanup_validation_workspace(workspace)
        assert workspace in workspace_module._ACTIVE
        assert workspace.is_dir()
    finally:
        cleanup_validation_workspace(workspace)
