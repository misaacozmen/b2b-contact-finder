"""Run the fixed-seed Task 2 live validation against a chosen worktree."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--keys", type=Path, required=True)
    parser.add_argument("--label", required=True)
    return parser.parse_args()


def main_cli() -> None:
    args = _parse_args()
    repo = args.repo.resolve()
    os.chdir(repo)
    sys.path.insert(0, str(repo))

    import config  # noqa: PLC0415
    import main  # noqa: PLC0415

    config.SAVED_API_KEYS_FILE = args.keys.resolve()
    saved = main._load_saved_api_keys()
    required = ("brightdata", "google_places", "brandfetch", "hunter")
    missing = [name for name in required if not saved.get(name)]
    if missing:
        raise SystemExit("Missing saved API keys: " + ", ".join(missing))

    main._set_run_state_dir(args.state.resolve())
    config.SEARCH_PROVIDER = "brightdata"
    config.SEARCH_CACHE_MODE = "off"
    config.CRAWL_CACHE_MODE = "off"
    config.REPLAY_SNAPSHOT_INPUT = None
    config.BRIGHTDATA_API_KEY = saved["brightdata"]
    config.GOOGLE_PLACES_API_KEY = saved["google_places"]
    config.BRANDFETCH_CLIENT_ID = saved["brandfetch"]
    config.HUNTER_API_KEY = saved["hunter"]
    config.ENABLE_GOOGLE_PLACES = True
    config.ENABLE_BRANDFETCH_DOMAIN_SEARCH = True
    config.ENABLE_HUNTER_DOMAIN_FINDER = True
    config.ENABLE_HUNTER_FALLBACK = False
    config.BRIGHTDATA_REQUEST_BUDGET = 120
    config.GOOGLE_PLACES_REQUEST_BUDGET = 60
    config.BRANDFETCH_REQUEST_BUDGET = 60
    config.HUNTER_REQUEST_BUDGET = 60
    for name, value in (
        ("BRIGHTDATA_REQUEST_HARD_CAP", 120),
        ("GOOGLE_PLACES_REQUEST_HARD_CAP", 60),
        ("BRANDFETCH_REQUEST_HARD_CAP", 60),
        ("HUNTER_REQUEST_HARD_CAP", 60),
    ):
        if hasattr(config, name):
            setattr(config, name, value)
    config.SEARCH_HTTP_REQUEST_BUDGET = 600
    config.CRAWLER_HTTP_REQUEST_BUDGET = 12_000
    config.MAX_WORKERS = 12
    config.MIN_DELAY_SEC = 0
    config.MAX_DELAY_SEC = 0
    config.ENABLE_JS_FALLBACK = True
    config.ENABLE_JS_PROFILE_FALLBACK = False
    config.MAX_BROWSER_RENDER_WORKERS = 1

    args.output.mkdir(parents=True, exist_ok=True)
    metadata = {
        "label": args.label,
        "repo": str(repo),
        "revision": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "input": str(args.input.resolve()),
        "search_cache_mode": config.SEARCH_CACHE_MODE,
        "crawl_cache_mode": config.CRAWL_CACHE_MODE,
        "budgets": {
            "brightdata": config.BRIGHTDATA_REQUEST_BUDGET,
            "google_places": config.GOOGLE_PLACES_REQUEST_BUDGET,
            "brandfetch": config.BRANDFETCH_REQUEST_BUDGET,
            "hunter": config.HUNTER_REQUEST_BUDGET,
        },
    }
    (args.output / "validation_run.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(main.run(args.input.resolve(), args.output.resolve()))


if __name__ == "__main__":
    main_cli()
