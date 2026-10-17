"""Commands the user sends by e-mail, and drafts sent from the Zoho Drafts folder (Talimat 53).

Every notice and the morning summary give each reply a code: T12 for a reply
with a draft, S12 for one left to the user.  The user answers from their own
address with lines such as "T12 gönder", "T12 sil" or "S12 kapat"; the buttons
in the e-mails open such an answer ready to send.  A command counts only when

- it comes from the user's address with Zoho's own DKIM and DMARC pass on top,
- and the e-mail carries a command code of the last days (it is in every notice
  and summary, so a plain "Reply" quotes it).

A draft is sent as it is in Zoho at that moment, so changes the user made there
go out; afterwards it is moved to the Trash folder.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import email
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid
import html
import imaplib
import re
from urllib.parse import quote

import config
from modules import legal_form, outreach, reply_drafts, sales_db


CODE_RE = re.compile(r"\b([TS])\s?-?(\d{1,6})\b", re.IGNORECASE)
SUBJECT_CODE_RE = re.compile(r"\bB2B komut [A-Z0-9]{8}:?", re.IGNORECASE)
TAG_RE = re.compile(r"\[([TS]\d{1,6})\]")
VERB_LABELS = {"gonder": "gönder", "sil": "sil", "kapat": "kapat"}
BUTTONS = {"T": ("gonder", "sil"), "S": ("kapat",)}


# Only orders count; "göndereceğim" or "gönderdim" is not a command.
VERB_WORDS = {
    "gonder": {"gonder", "gonderin", "gonderiniz", "gonderebilirsin", "gonderebilirsiniz", "gonderelim", "gonderilsin",
               "yolla", "yollayin"},
    "sil": {"sil", "silin", "siliniz", "silebilirsin", "silebilirsiniz", "silelim", "silinsin", "iptal", "gonderme",
            "gondermeyin", "gondermeyelim"},
    "kapat": {"kapat", "kapatin", "kapatiniz", "kapatabilirsin", "kapatilsin", "hallettim", "yanitladim", "cevapladim"},
}


def verbs(text: str) -> list[str]:
    return [verb for word in legal_form._fold(text).split() for verb, words in VERB_WORDS.items() if word in words]


def from_owner(message, owner: str) -> bool:
    """The message comes from the user's address and Zoho, on receiving it, found DKIM and DMARC passing."""
    sender = next((address.casefold() for _name, address in getaddresses([str(message.get("From", ""))])
                   if "@" in address), "")
    if not owner or sender != owner.strip().casefold():
        return False
    results = message.get_all("Authentication-Results") or []
    top = " ".join(str(results[0]).split()).casefold() if results else ""
    return top.startswith(config.REPLY_AUTH_SERVER) and "dkim=pass" in top and "dmarc=pass" in top


def parse(subject: str, new_text: str, quoted_text: str) -> tuple[list[tuple[str, int, str]], list[str]]:
    """Commands as (letter, number, verb) and the lines that could not be read."""
    commands: dict[int, tuple[str, int, str]] = {}
    unclear = []
    loose_verbs = []
    for line in [SUBJECT_CODE_RE.sub(" ", subject), *new_text.split("\n")]:
        codes = [(letter.upper(), int(number)) for letter, number in CODE_RE.findall(line)]
        found = verbs(line)
        if not codes:
            loose_verbs += found
            continue
        if len(found) == 1:
            pairs = [(code, found[0]) for code in codes]
        elif found and len(found) == len(codes):
            pairs = list(zip(codes, found))
        else:
            unclear.append(line.strip())
            continue
        for (letter, number), verb in pairs:
            commands.setdefault(number, (letter, number, verb))
    if not commands and loose_verbs and not unclear:
        # "gönder" written over a quoted notice: the notice's single code is meant.
        quoted = {(letter.upper(), int(number)) for letter, number in CODE_RE.findall(quoted_text)}
        if len(quoted) == 1 and len(set(loose_verbs)) == 1:
            letter, number = next(iter(quoted))
            commands[number] = (letter, number, loose_verbs[0])
        else:
            unclear.append(" ".join(new_text.split())[:80])
    return list(commands.values()), unclear


def has_valid_code(text: str, codes: set[str]) -> bool:
    return any(code in text for code in codes)


def mailto(account: str, command_code: str, item: str, verb: str) -> str:
    label = VERB_LABELS[verb]
    subject = f"B2B komut {command_code}: {item} {label}"
    return f"mailto:{account}?subject={quote(subject)}&body={quote(f'{item} {label}')}"


def html_version(text: str, *, account: str, command_code: str) -> str:
    """The plain text as HTML, with command buttons after every line that shows a [T12] or [S12] code."""
    out = []
    for line in text.split("\n"):
        out.append(html.escape(line) or "&nbsp;")
        match = TAG_RE.search(line)
        if match and command_code:
            item = match.group(1)
            links = " ".join(
                f'<a href="{html.escape(mailto(account, command_code, item, verb))}" style="display:inline-block;'
                f'margin:2px 6px 6px 0;padding:4px 10px;border:1px solid #1a73e8;border-radius:4px;color:#1a73e8;'
                f'text-decoration:none">{item} {VERB_LABELS[verb]}</a>'
                for verb in BUTTONS[item[0]]
            )
            out.append(f"&nbsp;&nbsp;&nbsp;{links}")
    body = "<br>\n".join(out)
    return f'<html><body><div style="font-family:Arial,sans-serif;font-size:14px">{body}</div></body></html>'


# --- drafts in Zoho -------------------------------------------------------------------------------

def find_draft(imap, row: dict) -> int | None:
    """The UID of the reply's draft in the Drafts folder (by its Message-ID, else by the reply it answers)."""
    status, _data = imap.select(f'"{config.REPLY_DRAFTS_FOLDER}"')
    if status != "OK":
        return None
    for header, value in (("Message-ID", row.get("taslak_id")), ("In-Reply-To", row.get("message_id"))):
        if not value:
            continue
        status, data = imap.uid("SEARCH", None, "HEADER", header, f'"{value}"')
        uids = sorted(int(uid) for uid in (data[0] or b"").split()) if status == "OK" and data else []
        if uids:
            return uids[-1]
    return None


def _to_trash(imap, uid: int) -> None:
    """Move one message of the selected folder to the Trash (Zoho supports IMAP MOVE); nothing else is touched."""
    status, _data = imap.uid("MOVE", str(uid), f'"{config.REPLY_TRASH_FOLDER}"')
    if status != "OK":
        raise imaplib.IMAP4.error("MOVE refused")


def send_draft(imap, row: dict, *, account: str, password: str, smtp_factory=None) -> str:
    """Send the draft as it is in Zoho now and move it to the Trash; the problem as text, or ""."""
    try:
        uid = find_draft(imap, row)
        if uid is None:
            return "taslak Zoho'da bulunamadı (silinmiş ya da Zoho'dan gönderilmiş olabilir)"
        status, parts = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
        raw = next((part[1] for part in parts or [] if isinstance(part, tuple)), None)
        if status != "OK" or not raw:
            return "taslak Zoho'dan okunamadı"
    except (imaplib.IMAP4.error, OSError) as error:
        return f"Zoho'ya ulaşılamadı ({type(error).__name__})"
    message = email.message_from_bytes(raw, policy=policy.default)
    del message["Date"]
    message["Date"] = formatdate(localtime=True)
    try:
        outreach.send_one(message, address=account, password=password, smtp_factory=smtp_factory)
    except Exception as error:  # noqa: BLE001 - reported to the user; the draft stays in Zoho
        return f"gönderilemedi ({type(error).__name__})"
    try:
        _to_trash(imap, uid)
    except (imaplib.IMAP4.error, OSError):
        pass  # sent; the draft left in Zoho does no harm
    return ""


def delete_draft(imap, row: dict) -> None:
    try:
        uid = find_draft(imap, row)
        if uid is not None:
            _to_trash(imap, uid)
    except (imaplib.IMAP4.error, OSError):
        pass


# --- running commands -----------------------------------------------------------------------------

def run(commands: list[tuple[str, int, str]], *, connection, imap, account: str, password: str, today: date,
        smtp_factory=None) -> list[str]:
    """Carry out the commands; one result line each."""
    lines = []
    for letter, number, verb in commands:
        item = f"{letter}{number}"
        row = sales_db.by_code(connection, number)
        if row is None:
            lines.append(f"{item}: böyle bir kod yok")
            continue
        who = row["firma"] or row["eposta"]
        if row["yanitlandi"] or row["sonuc"] == sales_db.CLOSED:
            lines.append(f"{item} ({who}): zaten gönderilmiş ya da kapatılmış")
            continue
        if verb == "gonder":
            if row["sonuc"] != sales_db.DRAFT:
                lines.append(f"{item} ({who}): taslak yok; yanıtı Zoho'dan siz verin")
                continue
            problem = send_draft(imap, row, account=account, password=password, smtp_factory=smtp_factory)
            if problem:
                lines.append(f"{item} ({who}): {problem}")
                continue
            sales_db.mark_sent(connection, row["anahtar"], day=today, how=sales_db.BY_COMMAND)
            lines.append(f"{item} ({who}): gönderildi")
        elif verb == "sil":
            if row["sonuc"] == sales_db.DRAFT:
                delete_draft(imap, row)
            sales_db.close_reply(connection, row["anahtar"], neden="taslak silindi (komut)")
            lines.append(f"{item} ({who}): taslak silindi, yanıt kapatıldı")
        else:
            sales_db.close_reply(connection, row["anahtar"], neden="kapatıldı (komut)")
            lines.append(f"{item} ({who}): kapatıldı")
    return lines


def send_due(connection, imap, *, now: datetime, account: str, password: str, smtp_factory=None) -> tuple[int, list[str]]:
    """Send the drafts allowed to go by themselves: inside office hours, within the daily limit."""
    if not outreach.office_hours(now, "TR"):
        return 0, []
    room = config.AUTO_REPLY_DAILY_LIMIT - len(sales_db.sent_by(connection, day=now.date(), how=sales_db.BY_ITSELF))
    sent, problems = 0, []
    before = now - timedelta(minutes=config.AUTO_REPLY_DELAY_MIN)
    for row in sales_db.auto_due(connection, before=before):
        if room <= 0:
            break
        problem = send_draft(imap, row, account=account, password=password, smtp_factory=smtp_factory)
        if problem:
            # It stays a draft for the user; the problem shows in the morning summary.
            sales_db.stop_auto(connection, row["anahtar"])
            problems.append(f"{reply_drafts.item_code(row)} ({row['firma'] or row['eposta']}) kendiliğinden gitmedi: {problem}")
            continue
        sales_db.mark_sent(connection, row["anahtar"], day=now.date(), how=sales_db.BY_ITSELF)
        sent += 1
        room -= 1
    return sent, problems


def self_test(*, account: str, password: str, owner: str, imap_factory=None, smtp_factory=None) -> str:
    """Put a test draft to the user's own address in Zoho, send it the way a command does and move it to the Trash."""
    message = EmailMessage()
    message["From"] = formataddr(("B2B deneme", account))
    message["To"] = owner
    message["Subject"] = "[DENEME] B2B komut denemesi"
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=account.rsplit("@", 1)[-1])
    message.set_content("Bu e-posta, taslağın Zoho'dan komutla gönderilmesini denemek için gönderildi.\n")
    factory = imap_factory or (lambda: imaplib.IMAP4_SSL(config.OUTREACH_IMAP_HOST, config.OUTREACH_IMAP_PORT))
    imap = factory()
    try:
        imap.login(account, password)
        status, _data = imap.append(f'"{config.REPLY_DRAFTS_FOLDER}"', r"(\Draft \Seen)",
                                    imaplib.Time2Internaldate(datetime.now().timestamp()), message.as_bytes())
        if status != "OK":
            return "deneme taslağı Zoho'ya yazılamadı"
        return send_draft(imap, {"taslak_id": str(message["Message-ID"]), "message_id": ""}, account=account,
                          password=password, smtp_factory=smtp_factory)
    finally:
        try:
            imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def result_message(lines: list[str], *, account: str, to: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr(("Komut sonucu", account))
    message["To"] = to
    message["Subject"] = "Komut sonucu: " + (lines[0] if len(lines) == 1 else f"{len(lines)} komut")
    message.set_content("\n".join(lines) + "\n\nBu e-posta otomatik bildirimdir.\n")
    return message
