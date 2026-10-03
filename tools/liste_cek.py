"""Pull a fair's exhibitor list into a run input workbook (Talimat 23).

Examples:
    python tools/liste_cek.py --url https://fuar.example/katilimcilar --name "Ornek Fuari"
    python tools/liste_cek.py --file kayitli_sayfa.html --url https://fuar.example/katilimcilar --name "Ornek Fuari"

With --file nothing is downloaded; --url is then only the address of the saved page.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import list_extractor, run_launcher  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fuar katılımcı listesini koşu girdisine çevirir.")
    parser.add_argument("--url", default="", help="Liste sayfasının adresi")
    parser.add_argument("--file", action="append", type=Path, help="Kaydedilmiş liste sayfası (.html) ya da veri dosyası (.json)")
    parser.add_argument("--name", required=True, help="Fuar adı")
    parser.add_argument("--output", type=Path, help="Yazılacak .xlsx (varsayılan: input klasörü)")
    args = parser.parse_args(argv)
    if not args.url and not args.file:
        parser.error("--url ya da --file gerekli")
    output = args.output or run_launcher.list_output_path(args.name)
    try:
        counts = list_extractor.build_input(args.url, args.file, args.name, output, progress=print)
    except (ValueError, OSError) as exc:
        print(f"HATA: {exc}")
        return 2
    print(
        f"LISTE_OK {output} firma={counts['firms']} web={counts['website']} "
        f"telefon={counts['phone']} eposta={counts['email']} sayfa={counts['pages']} profil={counts['profiles']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
