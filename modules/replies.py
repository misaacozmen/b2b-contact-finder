"""Replies to the cold e-mails, read from the Zoho mailbox (Talimat 50).

The mailbox is opened read-only, so every message stays unread in Zoho.  Each
folder is read from the last message seen before (state file).  A message is
matched to a sent e-mail by its In-Reply-To or References header (the
Message-ID the send log kept), else by the sender's address or company domain
as found in the send log and the tracking workbooks.

- A returned e-mail (bounce) marks the address in the workbook.
- An automatic reply (out of office) is only noted.
- A person's reply is written to the workbook and sent to the user's own
  address as a short notice; a reply that asks to stop also puts the address
  on the opt-out list.
"""

from __future__ import annotations

from datetime import date, datetime
import email
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, parsedate_to_datetime
import html
import imaplib
import json
from pathlib import Path
import re
import subprocess
import sys

import config
from modules import opt_out, outreach, scorer


BOUNCE = "geri döndü"
AUTO = "otomatik yanıt"
OPT_OUT = "istemiyor"
REPLY = "yanıt"
NOTIFY_KINDS = (REPLY, OPT_OUT)
BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail-daemon")
AUTO_SUBJECTS = (
    "otomatik yanıt", "otomatik cevap", "automatic reply", "auto reply", "autoreply", "auto:", "out of office",
    "ofis dışında", "işyeri dışında", "abwesenheit", "automatische antwort",
)
OPT_OUT_PHRASES = (
    "istemiyorum", "istemiyoruz", "listeden çıkar", "listenizden çıkar", "listeden sil", "listenizden sil",
    "adresimizi çıkar", "adresimizi sil", "adresimi çıkar", "adresimi sil", "göndermeyin", "göndermeyiniz",
    "mail atmayın", "e-posta atmayın", "unsubscribe", "remove us", "remove me", "take us off",
)
# Lines that start the quoted original under a reply.
QUOTE_START_RE = re.compile(
    r"^(>|-{2,}\s*(original message|özgün ileti|orijinal ileti|ileti)|"
    r"(from|kimden|gönderen|sent|gönderildi|tarih|date|to|kime|subject|konu)\s*:|"
    r"on .+ wrote:|.+ (yazdı|şunu yazdı):$)",
    re.IGNORECASE,
)
SNIPPET_CHARS = 120
NOTICE_CHARS = 1500


class ReplyError(Exception):
    """A problem that stops the reply check."""


# --- settings and state --------------------------------------------------------------

def load_settings() -> dict:
    try:
        return json.loads(config.REPLY_SETTINGS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_settings(settings: dict) -> None:
    """Save the settings and make every file of the check now, so later checks only rewrite them."""
    config.REPLY_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.REPLY_SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    for path in (config.REPLY_LOG_FILE, config.REPLY_RUN_LOG_FILE):
        path.touch(exist_ok=True)
    if not config.REPLY_STATE_FILE.exists():
        _save_state(_load_state())


def _load_state() -> dict:
    try:
        state = json.loads(config.REPLY_STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    state.setdefault("folders", {})
    state.setdefault("notified", [])
    return state


def _save_state(state: dict) -> None:
    config.REPLY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.REPLY_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_reply_log() -> list[dict]:
    try:
        lines = config.REPLY_LOG_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("key"):
            entries.append(entry)
    return entries


def _append_reply_log(entry: dict) -> None:
    config.REPLY_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with config.REPLY_LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write_run_log(now: datetime, line: str, keep: int = 500) -> None:
    """One line per check; the scheduled check runs without a window, so this is where it reports."""
    path = config.REPLY_RUN_LOG_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    lines.append(f"{now:%Y-%m-%d %H:%M} {line}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")


def task_arguments(*, remove: bool = False) -> list[str]:
    """The schtasks command that runs the check every half hour from 08:00 to 22:00, without a window."""
    if remove:
        return ["schtasks", "/Delete", "/TN", config.REPLY_TASK_NAME, "/F"]
    runtime = Path(sys.executable)
    windowless = runtime.with_name("pythonw.exe")
    interpreter = windowless if windowless.is_file() else runtime
    tool = config.BASE_DIR / "tools" / "eposta_gonder.py"
    return [
        "schtasks", "/Create", "/TN", config.REPLY_TASK_NAME, "/SC", "DAILY", "/ST", "08:00",
        "/RI", str(config.REPLY_TASK_EVERY_MIN), "/DU", "14:00", "/TR", f'"{interpreter}" "{tool}" yanitlar', "/F",
    ]


def schedule_task(*, remove: bool = False, runner=subprocess.run) -> tuple[int, str]:
    done = runner(task_arguments(remove=remove), capture_output=True, text=True, errors="replace")
    return done.returncode, (done.stdout or done.stderr or "").strip()


def quiet_now(window: str, now: datetime) -> bool:
    """True on a weekday inside a "HH:MM-HH:MM" window (the user's hours without runs)."""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", window or "")
    if not match or now.weekday() >= 5:
        return False
    start_h, start_m, end_h, end_m = (int(part) for part in match.groups())
    minute = now.hour * 60 + now.minute
    return start_h * 60 + start_m <= minute < end_h * 60 + end_m


# --- sent e-mails ------------------------------------------------------------------------

def sent_index(log: list[dict], tracker_dir: Path | None) -> dict:
    """Where each sent e-mail went: by Message-ID, address and company domain."""
    by_id, by_email, by_domain = {}, {}, {}
    free = {*config.NON_CORPORATE_EMAIL_DOMAINS, *config.EXTRA_FREE_MAIL_DOMAINS}

    def add(address: str, record: dict) -> None:
        address = address.strip().casefold()
        if not address or "@" not in address:
            return
        by_email.setdefault(address, record)
        domain = scorer.registrable_domain(address.rsplit("@", 1)[-1])
        if domain and domain not in free:
            by_domain.setdefault(domain, record)

    for entry in log:
        if entry.get("result") == outreach.TEST:
            continue
        record = {"email": entry["email"], "company": entry.get("company", ""), "tracker": entry.get("tracker", "")}
        if entry.get("message_id"):
            by_id[entry["message_id"].strip()] = record
        add(entry["email"], record)
    if tracker_dir and Path(tracker_dir).is_dir():
        # E-mails sent by hand before the tool kept a log are only in the workbooks.
        for path in sorted(Path(tracker_dir).glob("*_takip.xlsx")):
            try:
                rows = outreach.Tracker(path).rows()
            except Exception:  # noqa: BLE001 - an unreadable workbook only loses its matches
                continue
            for row in rows:
                address = outreach._text(row.get("E-posta")).casefold()
                if address and outreach._text(row.get("Gönderim tarihi")):
                    add(address, {"email": address, "company": outreach._text(row.get("Firma")), "tracker": path.name})
    return {"id": by_id, "email": by_email, "domain": by_domain}


# --- reading a message ----------------------------------------------------------------------

def _addresses(value: str) -> list[str]:
    return [address.casefold() for _name, address in getaddresses([value or ""]) if "@" in address]


def _plain_text(message) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, UnicodeError):
        text = part.get_payload(decode=True).decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return text.replace("\r\n", "\n")


def reply_text(message, account: str) -> str:
    """The new text of a reply: the quoted original and everything after it are cut."""
    kept = []
    for line in _plain_text(message).split("\n"):
        stripped = line.strip()
        if QUOTE_START_RE.match(stripped) or (account and account in stripped.casefold()):
            break
        kept.append(stripped)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _bounced_address(message, known: dict) -> str:
    for part in message.walk():
        if part.get_content_type() in {"message/delivery-status", "text/plain"}:
            raw = part.as_string() if part.get_content_type() == "message/delivery-status" else _safe_text(part)
            for match in re.finditer(r"(?:final|original)-recipient:\s*rfc822;\s*<?([^\s>]+@[^\s>]+)", raw, re.I):
                return match.group(1).casefold()
    text = message.as_string().casefold()
    for address in known:
        if address in text:
            return address
    return ""


def _safe_text(part) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeError, KeyError):
        return ""


def is_bounce(message) -> bool:
    sender = " ".join(_addresses(message.get("From", "")))
    return message.get_content_type() == "multipart/report" or any(word in sender for word in BOUNCE_SENDERS)


def is_automatic(message) -> bool:
    auto = (message.get("Auto-Submitted") or "").strip().casefold()
    if auto and auto != "no":
        return True
    if message.get("X-Autoreply") or message.get("X-Autorespond"):
        return True
    if (message.get("Precedence") or "").strip().casefold() in {"auto_reply", "auto-reply"}:
        return True
    subject = (message.get("Subject") or "").strip().casefold()
    return subject.startswith(AUTO_SUBJECTS)


def classify(message, index: dict, account: str) -> dict | None:
    """What the message means for the outreach, or None when it is not about a sent e-mail."""
    sender = next(iter(_addresses(message.get("From", ""))), "")
    if not sender or sender == account:
        return None
    if is_bounce(message):
        address = _bounced_address(message, index["email"])
        record = index["email"].get(address)
        return {**record, "kind": BOUNCE, "from": sender, "text": ""} if record else None
    record = None
    for message_id in re.findall(r"<[^<>]+>", f"{message.get('In-Reply-To', '')} {message.get('References', '')}"):
        record = record or index["id"].get(message_id)
    record = record or index["email"].get(sender)
    domain = scorer.registrable_domain(sender.rsplit("@", 1)[-1])
    record = record or index["domain"].get(domain)
    if not record:
        return None
    if is_automatic(message):
        return {**record, "kind": AUTO, "from": sender, "text": ""}
    text = reply_text(message, account)
    folded = text.casefold()
    kind = OPT_OUT if any(phrase in folded for phrase in OPT_OUT_PHRASES) else REPLY
    return {**record, "kind": kind, "from": sender, "text": text}


# --- mailbox ----------------------------------------------------------------------------------

def _imap_date(day: date) -> str:
    return f"{day.day}-{('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec')[day.month - 1]}-{day.year}"


def fetch_new(imap, folders: tuple[str, ...], state: dict, since: date) -> list[tuple[str, str, bytes]]:
    """New messages of each folder as (key, folder, raw bytes); the folders stay unread."""
    found = []
    for folder in folders:
        status, data = imap.select(f'"{folder}"', readonly=True)
        if status != "OK":
            continue
        _name, validity = imap.response("UIDVALIDITY")
        uid_validity = validity[0].decode() if validity and validity[0] else "0"
        seen = state["folders"].get(folder, {})
        last = int(seen.get("last_uid", 0)) if seen.get("uidvalidity") == uid_validity else 0
        query = ("UID", f"{last + 1}:*") if last else ("SINCE", _imap_date(since))
        status, data = imap.uid("SEARCH", None, *query)
        uids = sorted(int(uid) for uid in (data[0] or b"").split()) if status == "OK" else []
        for uid in (uid for uid in uids if uid > last):
            status, parts = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            raw = next((part[1] for part in parts or [] if isinstance(part, tuple)), None)
            if status == "OK" and raw:
                found.append((f"{folder}:{uid_validity}:{uid}", folder, raw))
            last = max(last, uid)
        state["folders"][folder] = {"uidvalidity": uid_validity, "last_uid": last}
    return found


# --- workbook, opt-out list and notice ----------------------------------------------------------

def _column(tracker: outreach.Tracker, name: str) -> None:
    if name not in tracker.columns:
        index = tracker.sheet.max_column + 1
        tracker.sheet.cell(row=1, column=index, value=name)
        tracker.columns[name] = index


def sync_trackers(entries: list[dict], tracker_dir: Path) -> dict[str, int]:
    """Write each reply into its workbook once; a workbook open in Excel waits for the next run."""
    counts = {"yazılan": 0, "açık dosya": 0}
    by_tracker: dict[str, list[dict]] = {}
    for entry in entries:
        if entry.get("tracker"):
            by_tracker.setdefault(entry["tracker"], []).append(entry)
    for name, items in by_tracker.items():
        path = Path(tracker_dir) / name
        if not path.is_file():
            continue
        if not outreach.writable(path):
            counts["açık dosya"] += 1
            continue
        tracker = outreach.Tracker(path)
        for column in ("Yanıt", "Yanıt tarihi", "Not"):
            _column(tracker, column)
        rows = {outreach._text(row.get("E-posta")).casefold(): row for row in tracker.rows()}
        changed = False
        for entry in items:
            row = rows.get(entry["email"])
            if not row:
                continue
            day = date.fromisoformat(entry["date"])
            if entry["kind"] == BOUNCE:
                if outreach._text(row.get("Gönderim sonucu")) != BOUNCE:
                    tracker.set(row["row"], "Gönderim sonucu", BOUNCE)
                    if not outreach._text(row.get("Not")):
                        tracker.set(row["row"], "Not", "adres geri döndü")
                    row["Gönderim sonucu"] = BOUNCE
                    changed = True
            elif entry["kind"] == AUTO:
                if not outreach._text(row.get("Not")) and not outreach._text(row.get("Yanıt")):
                    tracker.set(row["row"], "Not", AUTO)
                    row["Not"] = AUTO
                    changed = True
            elif not outreach._text(row.get("Yanıt")):
                label = "İstemiyor" if entry["kind"] == OPT_OUT else "Yanıt"
                snippet = " ".join(entry.get("text", "").split())[:SNIPPET_CHARS]
                tracker.set(row["row"], "Yanıt", f"{label}: {snippet}" if snippet else label)
                tracker.set(row["row"], "Yanıt tarihi", day)
                row["Yanıt"] = label
                changed = True
                counts["yazılan"] += 1
        if changed:
            try:
                tracker.save()
            except PermissionError:
                counts["açık dosya"] += 1
    return counts


def add_opt_outs(entries: list[dict]) -> int:
    """Put the address of each "stop" reply on the opt-out list once."""
    path = opt_out.ensure_file(config.OPT_OUT_FILE)
    present = list(opt_out.load(path))
    added = []
    for entry in entries:
        if entry["kind"] == OPT_OUT and not opt_out.matches(present, company="", website="", email=entry["email"]):
            present.append(entry["email"])
            added.append(f"# {entry['date']} yanıt: istemiyor ({entry.get('company', '')})\n{entry['email']}\n")
    if added:
        text = path.read_text(encoding="utf-8-sig")
        path.write_text(text + ("" if text.endswith("\n") else "\n") + "".join(added), encoding="utf-8")
    return len(added)


def notice(entry: dict, *, account: str, to: str) -> EmailMessage:
    message = EmailMessage()
    company = entry.get("company") or entry["from"]
    first = "İstemiyor (ret listesine eklendi)" if entry["kind"] == OPT_OUT else "Yanıt geldi"
    message["From"] = formataddr(("Yanıt bildirimi", account))
    message["To"] = to
    message["Subject"] = f"{first}: {company}"
    message.set_content("\n".join([
        f"Firma: {company}",
        f"Kimden: {entry['from']}",
        f"Konu: {entry.get('subject', '')}",
        f"Tarih: {entry.get('received', entry['date'])}",
        f"Takip dosyası: {entry.get('tracker') or '-'}",
        "",
        (entry.get("text") or "")[:NOTICE_CHARS] or "(metin yok)",
        "",
        "Yanıtı Zoho'dan siz verin. Bu e-posta otomatik bildirimdir.",
    ]) + "\n")
    return message


# --- one check ----------------------------------------------------------------------------------

def check(*, account: str, password: str, notify_to: str, tracker_dir: Path, notify_since: date | None = None,
          imap_factory=None, smtp_factory=None) -> dict:
    """Read the new messages, record them, update the workbooks and the opt-out list, send notices.

    Replies received before ``notify_since`` (the day the checks were set up) get no notice.
    """
    account = account.casefold()
    log = outreach.load_log()
    sent = [entry for entry in log if entry.get("result") != outreach.TEST]
    result = {"yeni": 0, BOUNCE: 0, AUTO: 0, OPT_OUT: 0, REPLY: 0, "bildirim": 0, "bildirim hatası": 0}
    state = _load_state()
    known = {entry["key"] for entry in load_reply_log()}
    if sent:
        since = min(date.fromisoformat(entry["date"]) for entry in sent)
        index = sent_index(log, tracker_dir)
        factory = imap_factory or (lambda: imaplib.IMAP4_SSL(config.OUTREACH_IMAP_HOST, config.OUTREACH_IMAP_PORT))
        try:
            imap = factory()
            imap.login(account, password)
        except imaplib.IMAP4.error:
            raise ReplyError("Zoho IMAP girişi reddedildi: Zoho Mail ayarlarında IMAP erişimini açın ve uygulama şifresini kontrol edin.") from None
        except OSError as error:
            raise ReplyError(f"Zoho'ya bağlanılamadı ({type(error).__name__}).") from None
        try:
            messages = fetch_new(imap, config.REPLY_FOLDERS, state, since)
        finally:
            try:
                imap.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
        for key, folder, raw in messages:
            result["yeni"] += 1
            if key in known:
                continue
            message = email.message_from_bytes(raw, policy=policy.default)
            found = classify(message, index, account)
            if not found:
                continue
            try:
                received = parsedate_to_datetime(message.get("Date")).astimezone()
            except (TypeError, ValueError, IndexError):
                received = datetime.now().astimezone()
            entry = {
                "key": key, "folder": folder, "date": received.date().isoformat(),
                "received": received.isoformat(timespec="minutes"), "subject": str(message.get("Subject", "")), **found,
            }
            _append_reply_log(entry)
            known.add(key)
            result[entry["kind"]] += 1
    _save_state(state)
    entries = load_reply_log()
    result.update(sync_trackers(entries, tracker_dir))
    result["ret listesine eklenen"] = add_opt_outs(entries)
    notified = set(state["notified"])
    for entry in entries:
        if entry["kind"] not in NOTIFY_KINDS or entry["key"] in notified or not notify_to:
            continue
        if notify_since and date.fromisoformat(entry["date"]) < notify_since:
            continue
        try:
            outreach.send_one(notice(entry, account=account, to=notify_to), address=account, password=password,
                              smtp_factory=smtp_factory)
        except Exception:  # noqa: BLE001 - the notice is sent again on the next run
            result["bildirim hatası"] += 1
            continue
        notified.add(entry["key"])
        result["bildirim"] += 1
        state["notified"] = sorted(notified)
        _save_state(state)
    return result
