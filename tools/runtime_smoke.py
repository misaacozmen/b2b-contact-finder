from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


def main() -> int:
    result: dict[str, object] = {
        "interpreter": str(Path(sys.executable).resolve()),
        "python": sys.version,
        "browser": {"ok": False},
        "ocr": {"ok": False},
    }

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto("data:text/html,<title>runtime smoke</title><p>A8 browser</p>")
            result["browser"] = {"ok": page.title() == "runtime smoke", "title": page.title()}
            browser.close()
    except Exception as exc:  # pragma: no cover - runtime capability probe
        result["browser"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        from PIL import Image, ImageDraw, ImageFont
        import pytesseract

        runtime_prefix = Path(sys.executable).resolve().parent
        bundled_tesseract = runtime_prefix / "Library" / "bin" / "tesseract.exe"
        tesseract = str(bundled_tesseract) if bundled_tesseract.is_file() else shutil.which("tesseract")
        if not tesseract:
            raise RuntimeError("tesseract binary not found")
        pytesseract.pytesseract.tesseract_cmd = tesseract
        tessdata = runtime_prefix / "share" / "tessdata"
        if tessdata.is_dir():
            os.environ["TESSDATA_PREFIX"] = str(tessdata)
        langs = pytesseract.get_languages(config="")
        if "eng" not in langs:
            raise RuntimeError("tesseract eng language data not found")
        smoke_path = Path(".runtime") / "runtime_ocr_smoke.png"
        image = Image.new("RGB", (640, 160), "white")
        font_path = Path("C:/Windows/Fonts/arial.ttf")
        font = ImageFont.truetype(str(font_path), 64) if font_path.is_file() else None
        ImageDraw.Draw(image).text((30, 35), "A8 OCR", fill="black", font=font)
        image.save(smoke_path)
        text = pytesseract.image_to_string(image, lang="eng")
        result["ocr"] = {
            "ok": "A8" in text.upper() and "OCR" in text.upper(),
            "tesseract": tesseract,
            "languages": sorted(langs),
            "observed_text": text.strip(),
        }
    except Exception as exc:  # pragma: no cover - runtime capability probe
        result["ocr"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["browser"]["ok"] and result["ocr"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
