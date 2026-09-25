from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("B2B_COMMAND_ID", "petzoo-p07-receipt-resume")
import conftest  # noqa: F401

network_log = Path(os.environ.get("B2B_SOCKET_DENY_JSONL", ""))
if str(network_log):
    network_log.parent.mkdir(parents=True, exist_ok=True)
    with network_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "kind": "guard_armed", "command_id": os.environ.get("B2B_COMMAND_ID", ""),
            "pid": os.getpid(), "child": True,
        }, sort_keys=True) + "\n")

import config
from modules import checkpoint, crawler, runtime


def forbidden_transport(_url):
    raise AssertionError("a durable terminal negative receipt was resent")


db_path = Path(os.environ["PETZOO_DB"]).resolve()
config.PROGRESS_DB_FILE = db_path
checkpoint._SCHEMA_READY.clear()
runtime.reset()
runtime.configure_durable_run(os.environ["PETZOO_RUN_ID"], {})
runtime.set_source_record_id("petzoo:test:000")
crawler._fetch = forbidden_transport
result = crawler._try_fetch(os.environ["PETZOO_RECEIPT_URL"])
Path(os.environ["PETZOO_RESULT"]).write_text(
    json.dumps({"result": list(result), "physical_transport_called": False}) + "\n",
    encoding="utf-8",
)
