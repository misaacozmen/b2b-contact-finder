"""The sales database: fairs we can quote, replies with their decision and draft, and each firm's stage (Talimat 51).

SQLite in the reply folder.  The rollback journal is kept between writes
(journal_mode=PERSIST), so after set-up no file is created or removed there;
the test suite's guard on the state folder stays unchanged while checks run.
"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime
from pathlib import Path
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS fuarlar (
    id INTEGER PRIMARY KEY,
    ad TEXT NOT NULL UNIQUE,
    diger_adlar TEXT NOT NULL DEFAULT '',
    turk_firma INTEGER NOT NULL DEFAULT 0,
    liste_dosyasi TEXT NOT NULL DEFAULT '',
    iletisim_dosyasi TEXT NOT NULL DEFAULT '',
    durum TEXT NOT NULL DEFAULT 'hazir',
    guncellendi TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS yanitlar (
    anahtar TEXT PRIMARY KEY,
    eposta TEXT NOT NULL,
    firma TEXT NOT NULL DEFAULT '',
    takip TEXT NOT NULL DEFAULT '',
    alindi TEXT NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    niyet TEXT NOT NULL DEFAULT '',
    guven REAL,
    fuar TEXT NOT NULL DEFAULT '',
    sonuc TEXT NOT NULL,
    neden TEXT NOT NULL DEFAULT '',
    yanitlandi TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS firmalar (
    eposta TEXT PRIMARY KEY,
    firma TEXT NOT NULL DEFAULT '',
    takip TEXT NOT NULL DEFAULT '',
    asama TEXT NOT NULL,
    sonraki_tarih TEXT NOT NULL DEFAULT '',
    guncellendi TEXT NOT NULL
);
"""
# The outcome of a reply.
DRAFT = "taslak hazır"
USER = "size"
CLOSED = "kapandı"
FAIR_STATES = ("hazir", "yok")


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(Path(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=PERSIST")
    connection.executescript(SCHEMA)
    return connection


def create(path: Path) -> None:
    """Make the database and its journal file once, at set-up."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path)) as connection, connection:
        connection.execute("CREATE TABLE IF NOT EXISTS kurulum (zaman TEXT)")
        connection.execute("DELETE FROM kurulum")
        connection.execute("INSERT INTO kurulum VALUES (?)", (datetime.now().isoformat(timespec="seconds"),))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def save_fair(connection, *, ad: str, diger_adlar: str, turk_firma: int, liste_dosyasi: str,
              iletisim_dosyasi: str, durum: str) -> None:
    if durum not in FAIR_STATES:
        raise ValueError(f"durum: {durum}")
    with connection:
        connection.execute(
            "INSERT INTO fuarlar (ad, diger_adlar, turk_firma, liste_dosyasi, iletisim_dosyasi, durum, guncellendi) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(ad) DO UPDATE SET diger_adlar=excluded.diger_adlar, "
            "turk_firma=excluded.turk_firma, liste_dosyasi=excluded.liste_dosyasi, "
            "iletisim_dosyasi=excluded.iletisim_dosyasi, durum=excluded.durum, guncellendi=excluded.guncellendi",
            (ad, diger_adlar, int(turk_firma), liste_dosyasi, iletisim_dosyasi, durum, _now()),
        )


def fairs(connection) -> list[dict]:
    return [dict(row) for row in connection.execute("SELECT * FROM fuarlar ORDER BY ad")]


def has_reply(connection, key: str) -> bool:
    return connection.execute("SELECT 1 FROM yanitlar WHERE anahtar = ?", (key,)).fetchone() is not None


def save_reply(connection, entry: dict, *, niyet: str, guven: float | None, fuar: str, sonuc: str, neden: str) -> None:
    with connection:
        connection.execute(
            "INSERT OR REPLACE INTO yanitlar (anahtar, eposta, firma, takip, alindi, message_id, niyet, guven, fuar, "
            "sonuc, neden) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entry["key"], entry["email"], entry.get("company", ""), entry.get("tracker", ""),
             entry.get("received", entry["date"]), entry.get("message_id", ""), niyet, guven, fuar, sonuc, neden),
        )


def reply(connection, key: str) -> dict | None:
    row = connection.execute("SELECT * FROM yanitlar WHERE anahtar = ?", (key,)).fetchone()
    return dict(row) if row else None


def set_stage(connection, *, eposta: str, firma: str, takip: str, asama: str, sonraki: date | None = None) -> None:
    with connection:
        connection.execute(
            "INSERT INTO firmalar (eposta, firma, takip, asama, sonraki_tarih, guncellendi) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(eposta) DO UPDATE SET asama=excluded.asama, sonraki_tarih=excluded.sonraki_tarih, "
            "guncellendi=excluded.guncellendi",
            (eposta, firma, takip, asama, sonraki.isoformat() if sonraki else "", _now()),
        )


def _received(row: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(row["alindi"])
    except (TypeError, ValueError):
        return None


def mark_answered(connection, *, message_ids: set[str], addresses: set[str], sent: datetime) -> int:
    """Close the replies the user answered: the answer quotes their Message-ID, or goes to their address later."""
    rows = [dict(row) for row in connection.execute("SELECT * FROM yanitlar WHERE yanitlandi = ''")]
    done = []
    for row in rows:
        received = _received(row)
        later = received is not None and received.tzinfo is not None and sent.tzinfo is not None and received < sent
        if (row["message_id"] and row["message_id"] in message_ids) or (row["eposta"] in addresses and later):
            done.append(row)
    with connection:
        for row in done:
            connection.execute("UPDATE yanitlar SET yanitlandi = ? WHERE anahtar = ?",
                               (sent.date().isoformat(), row["anahtar"]))
            connection.execute(
                "UPDATE firmalar SET asama = 'yanıtımız gitti', guncellendi = ? WHERE eposta = ?", (_now(), row["eposta"]),
            )
    return len(done)


def open_replies(connection) -> list[dict]:
    """Replies still waiting for the user: a draft to send, or a reply with no draft."""
    return [dict(row) for row in connection.execute(
        "SELECT * FROM yanitlar WHERE yanitlandi = '' AND sonuc IN (?, ?) ORDER BY alindi", (DRAFT, USER),
    )]
