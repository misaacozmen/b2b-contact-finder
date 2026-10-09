"""Firms that asked to stay out of every delivery file (Talimat 39)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from modules import scorer


logger = logging.getLogger(__name__)

HEADER = (
    "# Ret listesi: buradaki firmalar hiçbir teslim dosyasına (sonuclar.xlsx) girmez.\n"
    "# Her satıra bir tane yazın:\n"
    "#   alan adı          firma.com.tr\n"
    "#   e-posta           biuro@firma.pl\n"
    "#   e-posta alanı     @firma.pl\n"
    "#   firma adı         fuar listesindeki gibi, tam haliyle\n"
    "# # ile başlayan satırlar açıklamadır.\n"
)


def load(path: Path) -> tuple[str, ...]:
    """Read the opt-out entries; a missing or unreadable file means no entries."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeDecodeError):
        logger.exception("opt-out list could not be read: %s", path)
        return ()
    entries = []
    for line in text.splitlines():
        value = " ".join(line.split()).casefold()
        if value and not value.startswith("#"):
            entries.append(value)
    return tuple(entries)


def matches(entries: Iterable[str], *, company: object, website: object, email: object) -> bool:
    name = " ".join(str(company or "").split()).casefold()
    site = str(website or "").strip()
    address = str(email or "").strip().casefold()
    mail_domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    for entry in entries:
        if entry.startswith("@"):
            if mail_domain and scorer.same_registrable_domain(mail_domain, entry[1:]):
                return True
        elif "@" in entry:
            if entry == address:
                return True
        elif "." in entry and " " not in entry and (
            (site and scorer.same_registrable_domain(site, entry))
            or (mail_domain and scorer.same_registrable_domain(mail_domain, entry))
        ):
            return True
        elif entry == name:
            return True
    return False


def ensure_file(path: Path) -> Path:
    """Create the opt-out file with its instructions when it does not exist yet; never overwrite."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(HEADER)
    except FileExistsError:
        pass
    return path
