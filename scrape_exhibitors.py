import argparse
from pathlib import Path

import config
from modules import excel
from modules.exhibitor_scraper import (
    dedupe_rows,
    scrape_beauty_eurasia,
    scrape_idos,
    scrape_ifco,
)
from modules.utils import ensure_directories


SCRAPERS = {
    "ifco": scrape_ifco,
    "idos": scrape_idos,
    "beauty": scrape_beauty_eurasia,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fuar katilimci listelerinden firma adi ve website ceker.")
    parser.add_argument(
        "--source",
        choices=["all", *SCRAPERS.keys()],
        default="all",
        help="Cekilecek kaynak. Varsayilan: all",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=config.INPUT_FILE,
        help="Olusacak Excel dosyasi. Varsayilan: input/firms.xlsx",
    )
    parser.add_argument(
        "--no-details",
        action="store_true",
        help="Detay sayfalarina girip website arama. Daha hizli calisir.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.4,
        help="Sayfalar arasi bekleme suresi. Varsayilan: 0.4 saniye",
    )
    return parser.parse_args()


def run(source: str, output: Path, fetch_details: bool, delay: float) -> list[dict]:
    ensure_directories()
    rows: list[dict] = []
    selected_sources = SCRAPERS.keys() if source == "all" else [source]

    for selected_source in selected_sources:
        scraper = SCRAPERS[selected_source]
        source_rows = scraper(fetch_details=fetch_details, delay_sec=delay)
        print(f"{selected_source}: {len(source_rows)} firma")
        rows.extend(source_rows)

    rows = dedupe_rows(rows)
    excel.write_company_records(output, rows)
    print(f"Toplam benzersiz firma: {len(rows)}")
    print(f"Website bulunan: {sum(1 for row in rows if row.get('website'))}")
    print(f"Yazildi: {output}")
    return rows


if __name__ == "__main__":
    args = parse_args()
    run(args.source, args.output, fetch_details=not args.no_details, delay=args.delay)
