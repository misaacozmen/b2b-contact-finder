"""The sales database: fairs we can quote, replies with their decision and draft, and each firm's stage (Talimat 51).

Talimat 53 numbers each reply (the user's command code), keeps the draft's
Message-ID, whether the draft may go by itself, who sent our answer, and one
random command code per day.

SQLite in the reply folder.  The rollback journal is kept between writes
(journal_mode=PERSIST), so after set-up no file is created or removed there;
the test suite's guard on the state folder stays unchanged while checks run.
"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
import secrets
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
    yanitlandi TEXT NOT NULL DEFAULT '',
    kod INTEGER,
    taslak_id TEXT NOT NULL DEFAULT '',
    otomatik INTEGER NOT NULL DEFAULT 0,
    taslak_zamani TEXT NOT NULL DEFAULT '',
    gonderim TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS komut_kodlari (
    gun TEXT PRIMARY KEY,
    kod TEXT NOT NULL
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
# Columns added by Talimat 53 to a database made before it.
NEW_COLUMNS = (
    ("kod", "INTEGER"), ("taslak_id", "TEXT NOT NULL DEFAULT ''"), ("otomatik", "INTEGER NOT NULL DEFAULT 0"),
    ("taslak_zamani", "TEXT NOT NULL DEFAULT ''"), ("gonderim", "TEXT NOT NULL DEFAULT ''"),
)
# The outcome of a reply.
DRAFT = "taslak hazır"
USER = "size"
CLOSED = "kapandı"
FAIR_STATES = ("hazir", "yok")
# Who sent our answer: '' the user from Zoho, or the tool on a command or by itself.
BY_COMMAND = "komut"
BY_ITSELF = "otomatik"
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(Path(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=PERSIST")
    connection.executescript(SCHEMA)
    _migrate(connection)
    return connection


def _migrate(connection) -> None:
    """Add the Talimat 53 columns to an older database and number its replies in the order they came."""
    present = {row[1] for row in connection.execute("PRAGMA table_info(yanitlar)")}
    missing = [(name, kind) for name, kind in NEW_COLUMNS if name not in present]
    unnumbered = connection.execute("SELECT COUNT(*) FROM yanitlar WHERE kod IS NULL").fetchone()[0] if not missing else 1
    if not missing and not unnumbered:
        return
    with connection:
        for name, kind in missing:
            connection.execute(f"ALTER TABLE yanitlar ADD COLUMN {name} {kind}")
        start = connection.execute("SELECT COALESCE(MAX(kod), 0) FROM yanitlar").fetchone()[0]
        rows = connection.execute("SELECT anahtar FROM yanitlar WHERE kod IS NULL ORDER BY alindi, anahtar").fetchall()
        for offset, row in enumerate(rows, start=1):
            connection.execute("UPDATE yanitlar SET kod = ? WHERE anahtar = ?", (start + offset, row["anahtar"]))


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


def save_reply(connection, entry: dict, *, niyet: str, guven: float | None, fuar: str, sonuc: str, neden: str,
               taslak_id: str = "", otomatik: bool = False, at: datetime | None = None) -> int:
    """Record a reply's outcome; each reply keeps one number, shown to the user as T12 (draft) or S12."""
    written = (at or datetime.now()).isoformat(timespec="seconds") if sonuc == DRAFT else ""
    with connection:
        old = connection.execute("SELECT kod FROM yanitlar WHERE anahtar = ?", (entry["key"],)).fetchone()
        kod = old["kod"] if old and old["kod"] else connection.execute(
            "SELECT COALESCE(MAX(kod), 0) + 1 FROM yanitlar").fetchone()[0]
        connection.execute(
            "INSERT OR REPLACE INTO yanitlar (anahtar, eposta, firma, takip, alindi, message_id, niyet, guven, fuar, "
            "sonuc, neden, kod, taslak_id, otomatik, taslak_zamani) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entry["key"], entry["email"], entry.get("company", ""), entry.get("tracker", ""),
             entry.get("received", entry["date"]), entry.get("message_id", ""), niyet, guven, fuar, sonuc, neden,
             kod, taslak_id, int(bool(otomatik)), written),
        )
    return kod


def by_code(connection, number: int) -> dict | None:
    row = connection.execute("SELECT * FROM yanitlar WHERE kod = ?", (int(number),)).fetchone()
    return dict(row) if row else None


def first_answer(connection, eposta: str, key: str) -> bool:
    """No other reply of this address is recorded, so our answer would be the first in the conversation."""
    return connection.execute(
        "SELECT COUNT(*) FROM yanitlar WHERE eposta = ? AND anahtar <> ?", (eposta, key)).fetchone()[0] == 0


def mark_sent(connection, key: str, *, day: date, how: str) -> None:
    """Our answer to the reply went out from the tool (on a command or by itself)."""
    with connection:
        connection.execute("UPDATE yanitlar SET yanitlandi = ?, gonderim = ? WHERE anahtar = ?",
                           (day.isoformat(), how, key))
        connection.execute(
            "UPDATE firmalar SET asama = 'yanıtımız gitti', guncellendi = ? WHERE eposta = "
            "(SELECT eposta FROM yanitlar WHERE anahtar = ?)", (_now(), key),
        )


def close_reply(connection, key: str, *, neden: str) -> None:
    with connection:
        connection.execute("UPDATE yanitlar SET sonuc = ?, neden = ?, otomatik = 0 WHERE anahtar = ?",
                           (CLOSED, neden, key))


def stop_auto(connection, key: str) -> None:
    with connection:
        connection.execute("UPDATE yanitlar SET otomatik = 0 WHERE anahtar = ?", (key,))


def auto_due(connection, *, before: datetime) -> list[dict]:
    """Drafts to send by themselves that were written before ``before`` and are still unanswered."""
    return [dict(row) for row in connection.execute(
        "SELECT * FROM yanitlar WHERE sonuc = ? AND otomatik = 1 AND yanitlandi = '' AND taslak_zamani <> '' "
        "AND taslak_zamani <= ? ORDER BY kod", (DRAFT, before.isoformat(timespec="seconds")),
    )]


def sent_by(connection, *, day: date, how: str) -> list[dict]:
    return [dict(row) for row in connection.execute(
        "SELECT * FROM yanitlar WHERE yanitlandi = ? AND gonderim = ? ORDER BY kod", (day.isoformat(), how),
    )]


def command_code(connection, day: date) -> str:
    """The day's command code; e-mails of that day carry it and commands must quote one of the last days."""
    with connection:
        row = connection.execute("SELECT kod FROM komut_kodlari WHERE gun = ?", (day.isoformat(),)).fetchone()
        if row:
            return row["kod"]
        code = "".join(secrets.choice(CODE_CHARS) for _ in range(8))
        connection.execute("INSERT INTO komut_kodlari (gun, kod) VALUES (?, ?)", (day.isoformat(), code))
        return code


def valid_command_codes(connection, today: date, days: int) -> set[str]:
    first = (today - timedelta(days=days)).isoformat()
    return {row["kod"] for row in connection.execute("SELECT kod FROM komut_kodlari WHERE gun >= ?", (first,))}


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
