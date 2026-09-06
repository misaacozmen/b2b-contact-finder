"""Resolve and configure the bundled Tesseract runtime consistently."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path


REQUIRED_LANGUAGES = ("eng", "tur")


def _runtime_prefix() -> Path:
    return Path(sys.executable).resolve().parent


def resolve_tesseract() -> Path | None:
    """Prefer the Tesseract shipped beside the active Python interpreter."""
    prefix = _runtime_prefix()
    candidates = (
        prefix / "Library" / "bin" / "tesseract.exe",
        prefix / "bin" / "tesseract",
        prefix / "bin" / "tesseract.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    for name in ("tesseract", "tesseract.exe"):
        resolved = shutil.which(name)
        if resolved:
            return Path(resolved).resolve()
    return None


def resolve_tessdata(tesseract: Path | None = None) -> Path | None:
    prefix = _runtime_prefix()
    candidates = (
        prefix / "share" / "tessdata",
        prefix / "Library" / "share" / "tessdata",
        (tesseract.parent.parent / "share" / "tessdata") if tesseract else Path(),
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    return None


def configure_tesseract(*, require_languages: bool = True) -> dict[str, object]:
    """Configure pytesseract and return a local capability receipt."""
    tesseract = resolve_tesseract()
    receipt: dict[str, object] = {
        "available": False,
        "path": str(tesseract) if tesseract else None,
        "languages": [],
        "required_languages": list(REQUIRED_LANGUAGES),
    }
    if tesseract is None or importlib.util.find_spec("pytesseract") is None:
        return receipt
    try:
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = str(tesseract)
        tessdata = resolve_tessdata(tesseract)
        if tessdata is not None:
            os.environ["TESSDATA_PREFIX"] = str(tessdata)
        languages = sorted(str(value) for value in pytesseract.get_languages(config=""))
        receipt["languages"] = languages
        receipt["tessdata"] = str(tessdata) if tessdata else None
        receipt["available"] = all(language in languages for language in REQUIRED_LANGUAGES)
        if require_languages and not receipt["available"]:
            return receipt
    except Exception as exc:  # pragma: no cover - local runtime dependent
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        return receipt
    return receipt


def ocr_dependency_available() -> bool:
    if any(importlib.util.find_spec(name) is None for name in ("fitz", "pytesseract", "PIL")):
        return False
    return bool(configure_tesseract().get("available"))
