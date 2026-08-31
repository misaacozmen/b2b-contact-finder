import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "prune_disposable_workspaces.ps1"
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed")


def _check_target(target, parent, *, protected=(), worktrees=()):
    assert SCRIPT.is_file(), "prune safety script must be included in the validation package"
    environment = os.environ.copy()
    environment["B2B_PRUNE_TEST_SCRIPT"] = str(SCRIPT)
    environment["B2B_PRUNE_TEST_SPEC"] = json.dumps({
        "target": str(target), "parent": str(parent),
        "protected": [str(path) for path in protected],
        "worktrees": [str(path) for path in worktrees],
    })
    # SafetyFunctionsOnly returns before any incident discovery, hashing or deletion.
    command = """
$ErrorActionPreference = 'Stop'
. $env:B2B_PRUNE_TEST_SCRIPT -SafetyFunctionsOnly
$spec = $env:B2B_PRUNE_TEST_SPEC | ConvertFrom-Json
try {
    Assert-PruneTarget -Path $spec.target -Parent $spec.parent -ProtectedPaths @($spec.protected) -WorktreePaths @($spec.worktrees) | Out-Null
    exit 0
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
"""
    return subprocess.run(
        [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        env=environment, capture_output=True, text=True, timeout=15, check=False,
    )


@pytest.mark.parametrize("relationship", ["equal", "inside", "ancestor"])
def test_prune_rejects_worktree_overlap(tmp_path, relationship):
    target = tmp_path / "candidate"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    worktree = {"equal": target, "inside": target / "nested", "ancestor": tmp_path}[relationship]
    result = _check_target(target, tmp_path, worktrees=[worktree])
    assert result.returncode == 1
    assert "protected/worktree overlap" in result.stderr
    assert marker.read_text(encoding="utf-8") == "keep"


def test_prune_accepts_sibling_prefix_and_rejects_wrong_parent(tmp_path):
    target = tmp_path / "candidate"
    target.mkdir()
    accepted = _check_target(target, tmp_path, worktrees=[tmp_path / "candidate-other"])
    assert accepted.returncode == 0, accepted.stderr
    rejected = _check_target(target, tmp_path / "other")
    assert rejected.returncode == 1
    assert "not a direct Documents child" in rejected.stderr
    assert target.is_dir()


def test_prune_rejects_protected_root(tmp_path):
    target = tmp_path / "candidate"
    target.mkdir()
    result = _check_target(target, tmp_path, protected=[target])
    assert result.returncode == 1
    assert "protected/worktree overlap" in result.stderr
    assert target.is_dir()


def test_prune_rejects_root_symlink_without_touching_target(tmp_path):
    protected = tmp_path / "keep"
    protected.mkdir()
    marker = protected / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    target = tmp_path / "candidate"
    try:
        target.symlink_to(protected, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    result = _check_target(target, tmp_path)
    assert result.returncode == 1
    assert "reparse point at prune root" in result.stderr
    assert marker.read_text(encoding="utf-8") == "keep"
