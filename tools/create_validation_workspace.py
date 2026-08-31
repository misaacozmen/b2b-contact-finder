"""Create a bounded, source-only validation workspace.

The copier is deliberately allow-list based and refuses to inspect through
Windows reparse points (including junctions and symlinks).  Workspaces are
registered for process-exit cleanup; only a small receipt remains in the
source project.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import shutil
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


MAX_WORKSPACE_BYTES = 25 * 1024 * 1024
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_ACTIVE: dict[Path, tuple[tuple[int, int], tuple[int, int]]] = {}


class ReparsePointError(RuntimeError):
    pass


def _is_reparse(path: Path) -> bool:
    info = os.lstat(path)
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT) or stat.S_ISLNK(info.st_mode)


def _walk_without_dereference(root: Path) -> None:
    if _is_reparse(root):
        raise ReparsePointError(f"reparse point is not allowed: {root}")
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(dirs) + list(files):
            path = current_path / name
            if _is_reparse(path):
                raise ReparsePointError(f"reparse point is not allowed: {path}")


def _validate_allowed_trees(source: Path) -> None:
    """Inspect only trees that the allow-list can copy.

    Excluded trees are intentionally opaque: their contents are never copied
    or dereferenced, while reparse points inside selected trees fail closed.
    """
    for path in source.iterdir():
        if path.is_file() and (path.suffix.casefold() == ".py" or path.name.casefold() == "pytest.ini" or path.match("requirements*.txt") or path.suffix.casefold() == ".md"):
            if _is_reparse(path):
                raise ReparsePointError(f"reparse point is not allowed: {path}")
    for relative in (Path("modules"), Path("tests"), Path("tools")):
        base = source / relative
        if base.is_dir():
            _walk_without_dereference(base)


def _allowed_files(source: Path) -> list[Path]:
    selected: set[Path] = set()
    for path in source.iterdir():
        if path.is_file() and (path.suffix.casefold() == ".py" or path.name.casefold() == "pytest.ini" or path.match("requirements*.txt") or path.suffix.casefold() == ".md"):
            selected.add(path)
    for relative in (Path("modules"), Path("tests"), Path("tools")):
        base = source / relative
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and ((relative in (Path("modules"), Path("tools")) and path.suffix.casefold() == ".py") or path.relative_to(source) == Path("tools/prune_disposable_workspaces.ps1") or (relative == Path("tests") and (path.suffix.casefold() == ".py" or Path("fixtures") in path.relative_to(base).parents))):
                selected.add(path)
    return sorted(selected, key=lambda path: path.relative_to(source).as_posix())


def _receipt_path(source: Path, receipt: Path | None) -> Path:
    return (receipt or source / "recovery_validation" / "validation_workspace_receipt.json").resolve()


def _write_receipt(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _directory_identity(path: Path) -> tuple[int, int]:
    info = os.lstat(path)
    if bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT) or stat.S_ISLNK(info.st_mode):
        raise ReparsePointError(f"reparse point is not allowed: {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"workspace path is not a directory: {path}")
    return info.st_dev, info.st_ino


def cleanup_validation_workspace(workspace: Path) -> None:
    # Do not resolve an untrusted link into a different, potentially protected tree.
    workspace = Path(os.path.abspath(os.fspath(workspace)))
    if workspace not in _ACTIVE:
        raise ValueError(f"workspace is not registered for cleanup: {workspace}")
    expected_workspace, expected_parent = _ACTIVE[workspace]
    if workspace.parent.name != "b2b_validation" or workspace.parent.resolve(strict=True) != workspace.parent:
        raise ValueError(f"workspace parent changed: {workspace.parent}")
    if _directory_identity(workspace.parent) != expected_parent:
        raise ValueError(f"workspace parent identity changed: {workspace.parent}")
    try:
        current_workspace = _directory_identity(workspace)
    except FileNotFoundError:
        del _ACTIVE[workspace]
        return
    if current_workspace != expected_workspace:
        raise ValueError(f"workspace identity changed: {workspace}")
    _walk_without_dereference(workspace)
    if _directory_identity(workspace.parent) != expected_parent or _directory_identity(workspace) != expected_workspace:
        raise ValueError(f"workspace changed before cleanup: {workspace}")
    shutil.rmtree(workspace)
    del _ACTIVE[workspace]


def _cleanup_at_exit() -> None:
    for workspace in list(_ACTIVE):
        try:
            cleanup_validation_workspace(workspace)
        except (OSError, ValueError, ReparsePointError):
            pass


atexit.register(_cleanup_at_exit)


def create_validation_workspace(
    source_root: Path | None = None,
    *,
    temp_root: Path | None = None,
    max_bytes: int = MAX_WORKSPACE_BYTES,
    receipt: Path | None = None,
) -> Path:
    source = Path(source_root or Path(__file__).resolve().parents[1]).resolve()
    if not source.is_dir():
        raise FileNotFoundError(source)
    _validate_allowed_trees(source)
    files = _allowed_files(source)
    total_bytes = sum(path.stat().st_size for path in files)
    if total_bytes > int(max_bytes):
        raise ValueError(f"validation workspace exceeds {max_bytes} bytes: {total_bytes}")
    base = Path(temp_root or tempfile.gettempdir()).resolve() / "b2b_validation"
    base.mkdir(parents=True, exist_ok=True)
    parent_identity = _directory_identity(base)
    workspace = base / uuid.uuid4().hex
    workspace.mkdir(parents=False, exist_ok=False)
    _ACTIVE[workspace] = (_directory_identity(workspace), parent_identity)
    try:
        for source_path in files:
            relative = source_path.relative_to(source)
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, target)
        actual_bytes = sum(path.stat().st_size for path in workspace.rglob("*") if path.is_file())
        if actual_bytes > int(max_bytes):
            raise ValueError(f"validation workspace exceeds {max_bytes} bytes after copy: {actual_bytes}")
        receipt_path = _receipt_path(source, receipt)
        _write_receipt(receipt_path, {
            "source_root": str(source),
            "workspace": str(workspace),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "bytes": actual_bytes,
            "file_count": len(files),
            "max_bytes": int(max_bytes),
            "removed_at": None,
        })
        return workspace
    except Exception:
        cleanup_validation_workspace(workspace)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--max-bytes", type=int, default=MAX_WORKSPACE_BYTES)
    args = parser.parse_args()
    workspace = create_validation_workspace(args.source_root, max_bytes=args.max_bytes, receipt=args.receipt)
    print(workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
