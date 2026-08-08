"""Run the fixed 30-company golden pipeline and print validation metrics."""

import argparse
from datetime import datetime
from pathlib import Path

import config
import main
from modules import checkpoint
from validate_golden_xlsx import FIELDS, evaluate


GOLDEN_DIR = Path(__file__).resolve().parent / "outputs" / "golden_manual_validation_20260713"
GOLDEN_INPUT = GOLDEN_DIR / "golden_pipeline_input_30.xlsx"
GOLDEN_EXPECTED = GOLDEN_DIR / "golden_manual_validation_30_v2.xlsx"


def _print_comparison(title: str, actual: Path, selected: set[str]) -> None:
    metrics, complete_matches = evaluate(GOLDEN_EXPECTED, actual, selected or None)
    print(f"\n{title}")
    print("=" * 24)
    for field in FIELDS:
        values = metrics[field]
        denominator = values["tp"] + values["fp"]
        precision = values["tp"] / denominator * 100 if denominator else 0.0
        print(
            f"{field}: TP {values['tp']}, FP {values['fp']}, FN {values['fn']}, "
            f"precision %{precision:.1f}"
        )
    denominator = len(selected) if selected else 30
    print(f"tam firma eslesmesi: {len(complete_matches)}/{denominator}")


def _handle_existing_checkpoint() -> None:
    if not checkpoint.has_progress():
        return
    while True:
        answer = input("Yarim kalmis golden kosusu bulundu. Devam / sifirdan? [devam/sifirdan]: ").strip().casefold()
        if answer in {"devam", "d"}:
            return
        if answer in {"sifirdan", "sıfırdan", "s"}:
            checkpoint.clear_progress()
            return
        print("Lutfen 'devam' veya 'sifirdan' yazin.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the fixed 30-company golden benchmark")
    parser.add_argument("--search-cache", choices=("use", "refresh", "off", "replay"), default="use")
    parser.add_argument("--crawl-cache", choices=("use", "refresh", "off", "replay"), default="use")
    parser.add_argument("--rerank-cache", action="store_true", help="Never make search or crawl network requests")
    parser.add_argument("--companies", default="", help="Comma-separated exact company names")
    parser.add_argument("--brightdata-budget", type=int, default=config.BRIGHTDATA_REQUEST_BUDGET)
    parser.add_argument("--google-places-budget", type=int, default=config.GOOGLE_PLACES_REQUEST_BUDGET)
    return parser.parse_args()


def main_cli() -> None:
    args = _parse_args()
    config.SEARCH_CACHE_MODE = "replay" if args.rerank_cache else args.search_cache
    config.CRAWL_CACHE_MODE = "replay" if args.rerank_cache else args.crawl_cache
    config.BRIGHTDATA_REQUEST_HARD_CAP = config.BRIGHTDATA_REQUEST_BUDGET = max(0, args.brightdata_budget)
    config.GOOGLE_PLACES_REQUEST_HARD_CAP = config.GOOGLE_PLACES_REQUEST_BUDGET = max(0, args.google_places_budget)
    if args.rerank_cache:
        config.MIN_DELAY_SEC = 0
        config.MAX_DELAY_SEC = 0
    _handle_existing_checkpoint()
    main.configure_apis_interactively()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = config.OUTPUT_DIR / f"golden_{timestamp}"
    print(f"Golden cikti klasoru: {output_dir}")
    selected_companies = {value.strip() for value in args.companies.split(",") if value.strip()}
    print(main.run(GOLDEN_INPUT, output_dir, selected_companies or None))

    _print_comparison("Golden - tum bulunan adaylar", output_dir / "contacts.xlsx", selected_companies)
    _print_comparison(
        "Golden - otomatik kullanima uygun dogrulanmis sonuclar",
        output_dir / "verified_contacts.xlsx",
        selected_companies,
    )
    print(f"Dosyalar: {output_dir}")


if __name__ == "__main__":
    main_cli()
