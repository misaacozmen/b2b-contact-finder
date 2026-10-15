"""Find firms that serve fair exhibitors with the free search engines (Talimat 46).

Example:
    python tools\\hedef_bul.py --ulke TR --aday hedef_adaylar.xlsx --girdi input\\Hedef_TR.xlsx --haric eski_takip.xlsx

The candidate workbook shows every website found, "evet" or "hayır" with the
reason.  The input workbook holds the "evet" rows (company, website, country)
for an ordinary free run.  No paid service is used.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import country_profile, prospect_search  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ulke", choices=tuple(prospect_search.CATEGORIES), default="TR")
    parser.add_argument("--aday", required=True, type=Path, help="candidate workbook to write")
    parser.add_argument("--girdi", required=True, type=Path, help="run input workbook to write")
    parser.add_argument("--haric", action="append", type=Path, default=[],
                        help="earlier list or tracking workbook; firms in it are left out (repeatable)")
    parser.add_argument("--kategori", action="append", help="only this category (repeatable)")
    args = parser.parse_args(argv)

    for path in (args.aday, args.girdi):
        if path.exists():
            print(f"HATA: dosya zaten var: {path}")
            return 1
    unknown = [name for name in args.kategori or [] if name not in prospect_search.CATEGORIES[args.ulke]]
    if unknown:
        print(f"HATA: bilinmeyen kategori: {', '.join(unknown)}")
        return 1
    country_profile.apply(args.ulke)
    known = prospect_search.domains_in(args.haric)
    rows, stats = prospect_search.find(
        args.ulke, categories=tuple(args.kategori) if args.kategori else None, known=known,
    )
    if not stats["answered"]:
        print("HATA: ücretsiz arama motorları hiçbir sorguya sonuç vermedi; daha sonra yeniden deneyin.")
        return 1
    args.aday.parent.mkdir(parents=True, exist_ok=True)
    args.girdi.parent.mkdir(parents=True, exist_ok=True)
    prospect_search.write_review(args.aday, rows)
    count = prospect_search.write_input(args.girdi, rows, args.ulke)
    per_category = Counter(row["categories"][0] for row in rows if row["ok"])
    reasons = Counter(row["reason"] for row in rows if not row["ok"])
    print(f"Sorgu: {stats['queries']} (sonuç veren {stats['answered']}); sonuç: {stats['results']}; site: {len(rows)}")
    print(f"Uygun firma: {count}")
    for category, number in per_category.most_common():
        print(f"  {category}: {number}")
    for reason, number in reasons.most_common():
        print(f"  hayır ({reason}): {number}")
    print(f"Aday dosyası: {args.aday}\nKoşu girdisi: {args.girdi}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
