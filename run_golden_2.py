"""Run Golden 2 after its manual workbook is completely verified."""

import argparse
from datetime import datetime
from pathlib import Path

import config
import main
from modules import checkpoint
from validate_golden_xlsx import FIELDS, evaluate, readiness_issues


GOLDEN_DIR = Path(__file__).resolve().parent / "outputs" / "golden_2_20260714"
GOLDEN_INPUT = GOLDEN_DIR / "golden_2_pipeline_input_30.xlsx"
GOLDEN_EXPECTED = GOLDEN_DIR / "golden_2_manual_validation_30.xlsx"


def _print_comparison(title: str, expected: Path, actual: Path, selected: set[str]) -> None:
    metrics, complete_matches = evaluate(expected, actual, selected or None)
    print(f"\n{title}")
    print("=" * 24)
    for field in FIELDS:
        values = metrics[field]
        denominator = values["tp"] + values["fp"]
        precision = values["tp"] / denominator * 100 if denominator else 0.0
        print(f"{field}: TP {values['tp']}, FP {values['fp']}, FN {values['fn']}, precision %{precision:.1f}")
    print(f"tam firma eslesmesi: {len(complete_matches)}/{len(selected) if selected else 30}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the independent Golden 2 benchmark")
    parser.add_argument("--search-cache", choices=("use", "refresh", "off", "replay"), default="use")
    parser.add_argument("--crawl-cache", choices=("use", "refresh", "off", "replay"), default="use")
    parser.add_argument("--rerank-cache", action="store_true", help="Never make search or crawl network requests")
    parser.add_argument("--companies", default="", help="Comma-separated exact company names")
    parser.add_argument("--brightdata-budget", type=int, default=config.BRIGHTDATA_REQUEST_BUDGET)
    parser.add_argument("--google-places-budget", type=int, default=config.GOOGLE_PLACES_REQUEST_BUDGET)
    return parser.parse_args()


def _handle_existing_checkpoint() -> None:
    if not checkpoint.has_progress():
        return
    while True:
        answer = input("Yarim kalmis kosu bulundu. Devam / sifirdan? [devam/sifirdan]: ").strip().casefold()
        if answer in {"devam", "d"}:
            return
        if answer in {"sifirdan", "sıfırdan", "s"}:
            checkpoint.clear_progress()
            return
        print("Lutfen 'devam' veya 'sifirdan' yazin.")


def main_cli() -> None:
    args = _parse_args()
    selected = {value.strip() for value in args.companies.split(",") if value.strip()}
    issues = readiness_issues(GOLDEN_EXPECTED, selected or None)
    if issues:
        print("Golden 2 manuel dogrulamasi tamamlanmamis; API harcanmadan kosu durduruldu.")
        for issue in issues[:30]:
            print(f"- {issue}")
        if len(issues) > 30:
            print(f"- ... ve {len(issues) - 30} eksik daha")
        raise SystemExit(2)

    config.SEARCH_CACHE_MODE = "replay" if args.rerank_cache else args.search_cache
    config.CRAWL_CACHE_MODE = "replay" if args.rerank_cache else args.crawl_cache
    config.BRIGHTDATA_REQUEST_BUDGET = max(0, args.brightdata_budget)
    config.GOOGLE_PLACES_REQUEST_BUDGET = max(0, args.google_places_budget)
    if args.rerank_cache:
        config.MIN_DELAY_SEC = 0
        config.MAX_DELAY_SEC = 0
    _handle_existing_checkpoint()
    main.configure_apis_interactively()
    output_dir = config.OUTPUT_DIR / f"golden2_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    print(f"Golden 2 cikti klasoru: {output_dir}")
    print(main.run(GOLDEN_INPUT, output_dir, selected or None))

    _print_comparison("Golden 2 - tum bulunan adaylar", GOLDEN_EXPECTED, output_dir / "contacts.xlsx", selected)
    _print_comparison(
        "Golden 2 - otomatik kullanima uygun dogrulanmis sonuclar",
        GOLDEN_EXPECTED,
        output_dir / "verified_contacts.xlsx",
        selected,
    )
    print(f"Dosyalar: {output_dir}")


if __name__ == "__main__":
    main_cli()
