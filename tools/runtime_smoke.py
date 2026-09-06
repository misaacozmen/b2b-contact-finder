from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from modules.ocr_runtime import REQUIRED_LANGUAGES, configure_tesseract


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

        receipt = configure_tesseract()
        if not receipt.get("available"):
            raise RuntimeError(f"Tesseract tur+eng capability unavailable: {receipt}")
        smoke_path = Path(".runtime") / "runtime_ocr_smoke.png"
        image = Image.new("RGB", (640, 160), "white")
        font_path = Path("C:/Windows/Fonts/arial.ttf")
        font = ImageFont.truetype(str(font_path), 64) if font_path.is_file() else None
        ImageDraw.Draw(image).text((30, 35), "A8 OCR", fill="black", font=font)
        image.save(smoke_path)
        text = pytesseract.image_to_string(image, lang="tur+eng")
        result["ocr"] = {
            "ok": "A8" in text.upper() and "OCR" in text.upper(),
            "tesseract": receipt.get("path"),
            "tessdata": receipt.get("tessdata"),
            "languages": receipt.get("languages", []),
            "required_languages": list(REQUIRED_LANGUAGES),
            "observed_text": text.strip(),
        }
    except Exception as exc:  # pragma: no cover - runtime capability probe
        result["ocr"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["browser"]["ok"] and result["ocr"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
