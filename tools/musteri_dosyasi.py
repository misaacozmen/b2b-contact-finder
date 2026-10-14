"""Write the customer copy of one or more completed runs (Talimat 43).

Examples:
    python tools\\musteri_dosyasi.py --run-dir runs\\<id> --out teslim\\musteri\\FUAR.xlsx
    python tools\\musteri_dosyasi.py --run-dir runs\\<id1>="Fuar A" --run-dir runs\\<id2>="Fuar B" --out teslim\\musteri\\iki_fuar.xlsx
    python tools\\musteri_dosyasi.py --run-dir runs\\<id> --out teslim\\musteri\\FUAR_ornek.xlsx --sample 20

Run databases are only read. A Polish run keeps only firms whose name shows a
company form; any other run leaves out firms whose name shows a person.
A British run keeps a firm without a company form in its name only when its
website or Companies House shows it is a company (Talimat 44).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from modules import company_register, country_profile, crawler, customer_file, legal_form, opt_out  # noqa: E402
from tools.recompute_run_report import _load_database, _read_json  # noqa: E402


def _split_run(value: str) -> tuple[Path, str]:
    path, _, fair = value.partition("=")
    return Path(path.strip().strip('"')), fair.strip().strip('"')


def _keep_firm(country: str, checks: dict[str, dict] | None = None):
    profile = country_profile.PROFILES.get(country, {})
    if profile.get("COMPANY_REGISTER"):
        checks = checks or {}
        return lambda name: bool(checks.get(name, {}).get("company"))
    if profile.get("DELIVERY_COMPANIES_ONLY"):
        return legal_form.is_company
    return lambda name: not legal_form.names_a_person(name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", action="append", required=True,
                        help="run folder; add =\"Fair name\" when the run has no fair name")
    parser.add_argument("--out", required=True, type=Path, help="customer .xlsx file to write")
    parser.add_argument("--lang", choices=customer_file.LANGUAGES,
                        help="header language; default pl for a Polish and en for a British first run, otherwise tr")
    parser.add_argument("--sample", type=int, help="write a sample of this many firms with contacts partly hidden")
    args = parser.parse_args()

    entries = opt_out.load(config.OPT_OUT_FILE)
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    summary = []
    company_checks = []
    first_country = ""
    for value in args.run_dir:
        run_dir, fair = _split_run(value)
        run_dir = run_dir.resolve()
        if args.out.resolve().is_relative_to(run_dir):
            parser.error("--out must be outside the run folders")
        manifest = _read_json(run_dir / "manifest.json", required=False)
        settings = (manifest.get("run_config") or {}).get("effective_settings") or {}
        country = str(settings.get("target_country") or "TR")
        first_country = first_country or country
        run_rows, _context, _output, _elapsed = _load_database(run_dir / "state" / "progress.sqlite3", run_dir.name)
        checks = None
        if country_profile.PROFILES.get(country, {}).get("COMPANY_REGISTER"):
            key = company_register.api_key()
            checks = company_register.check_firms(
                customer_file.firm_sites(run_rows), fetch_page=crawler.fetch_page, key=key,
            )
            company_checks.append({"run_id": run_dir.name, **company_register.summary(checks, register_on=bool(key))})
        firms, left_out, opted_out = customer_file.customer_rows(
            run_rows, keep_firm=_keep_firm(country, checks), opt_out_entries=entries, fair=fair,
        )
        added = 0
        for firm in firms:
            key = (firm["Firma"].casefold(), firm["Fuar"].casefold())
            if key in seen:
                continue
            seen.add(key)
            rows.append(firm)
            added += 1
        summary.append({
            "run_id": run_dir.name, "country": country, "firms": added,
            "left_out_by_name": left_out, "left_out_by_opt_out": opted_out,
        })
    lang = args.lang or {"PL": "pl", "GB": "en"}.get(first_country, "tr")
    if args.sample:
        rows = customer_file.sample_rows(rows, args.sample)
    companies_only = bool(country_profile.PROFILES.get(first_country, {}).get("DELIVERY_COMPANIES_ONLY"))
    written = customer_file.write_customer_file(
        args.out, rows, lang=lang, sample=bool(args.sample), companies_only=companies_only,
    )
    print(json.dumps({
        "files": [str(path) for path in written],
        "firms": len(rows),
        "website": sum(1 for row in rows if row["Web sitesi"]),
        "email": sum(1 for row in rows if row["E-posta"]),
        "phone": sum(1 for row in rows if row["Telefon"]),
        "runs": summary,
        **({"company_check": company_checks} if company_checks else {}),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())