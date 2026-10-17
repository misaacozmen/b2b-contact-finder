"""Draft answers to the replies, from the user's templates, and the morning summary (Talimat 51).

A draft is only put in the Zoho Drafts folder; the user reads it, may change
it, and sends it.  Orders, bargaining, unknown fairs and unclear replies get
no draft: they go to the user.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
import random
import re
import tempfile
from pathlib import Path

import config
from modules import customer_file, email_kind, intent, legal_form, outreach


SECTION_RE = re.compile(r"^\[([a-z_]+)\]\s*$")
FIELD_RE = re.compile(r"\{([a-z_]+)\}")
REQUIRED = (
    "imza", "ilgileniyor", "sss_bicim", "sss_sutunlar", "sss_kisi_bilgisi", "sss_teslim", "sss_fark",
    "fiyat_fuarsiz", "teklif", "teklif_ornek", "liste_yok", "ornek_fuarsiz", "soru_adres", "soru_kvkk",
    "simdi_degil", "istemiyor", "yanlis_kisi",
)
FIELDS = {"sss", "fuar", "turk_firma", "en_cok_tutar"}
REPLY_PREFIX_RE = re.compile(r"^\s*(re|ynt|yanıt|aw|sv)\s*:", re.IGNORECASE)
# Intents that end in a draft built only from their own template.
PLAIN = {"soru_adres": "soru_adres", "soru_kvkk": "soru_kvkk", "simdi_degil": "simdi_degil", "istemiyor": "istemiyor"}
INTENT_LABELS = {
    "ilgileniyor": "ilgileniyor", "fiyat": "fiyat", "ornek": "örnek", "siparis": "sipariş",
    "soru_adres": "adres sorusu", "soru_kvkk": "KVKK sorusu", "simdi_degil": "şimdi değil", "olumsuz": "olumsuz",
    "istemiyor": "istemiyor", "yanlis_kisi": "yanlış kişi",
}
LATER_DAYS = 90


class ReplyDraftError(Exception):
    """The reply templates or settings are not usable."""


@dataclass
class Drafting:
    """Everything a check needs to write drafts; made from the saved settings."""

    templates: dict[str, str]
    sender_name: str
    since: date
    db_path: Path
    jev_key: str
    cold_template: dict | None = None
    fallback_sample: Path | None = None
    opener: object = None
    attachments_dir: Path | None = None


def load_templates(path: Path) -> dict[str, str]:
    """Read the reply templates: "[name]" lines start a section; {fields} are filled by the tool."""
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n").split("\n")
    except FileNotFoundError:
        raise ReplyDraftError(f"Yanıt şablonu bulunamadı: {path}") from None
    sections: dict[str, list[str]] = {}
    current = None
    for line in lines:
        match = SECTION_RE.match(line)
        if match:
            current = match.group(1)
            sections[current] = []
        elif current:
            sections[current].append(line)
    templates = {name: "\n".join(body).strip() for name, body in sections.items()}
    missing = [name for name in REQUIRED if not templates.get(name)]
    if missing:
        raise ReplyDraftError(f"Yanıt şablonunda eksik bölüm: {', '.join(missing)}")
    unknown = sorted({name for text in templates.values() for name in FIELD_RE.findall(text)} - FIELDS)
    if unknown:
        raise ReplyDraftError(f"Yanıt şablonunda bilinmeyen alan: {', '.join(unknown)}")
    return templates


def render(text: str, values: dict[str, str]) -> str:
    filled = FIELD_RE.sub(lambda match: str(values.get(match.group(1), "")), text)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in filled.split("\n"))).strip()


# --- price and sample ------------------------------------------------------------------------------

def rate_for(count: int) -> float:
    for limit, rate in config.PRICE_TIERS:
        if limit is None or count <= limit:
            return rate
    return config.PRICE_TIERS[-1][1]


def money(amount: float) -> str:
    text = f"{amount:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
    return text[:-3] if text.endswith(",00") else text


def quote(fair: dict) -> dict:
    """The highest price for a fair: its Turkish exhibitors times the rate of that count."""
    count = int(fair.get("turk_firma") or 0)
    rate = rate_for(count)
    return {"turk_firma": count, "oran": rate, "en_cok": count * rate}


def sample_file(fair: dict, *, seed: str, folder: Path, size: int = config.SAMPLE_SIZE) -> Path | None:
    """A workbook of random firms of the fair that have an e-mail or a phone, contacts shown in full."""
    source = Path(fair.get("iletisim_dosyasi") or "")
    if not source.is_file():
        return None
    try:
        rows = outreach.read_customer_list(source)
    except outreach.OutreachError:
        return None
    found = [row for row in rows if row.get("E-posta") or row.get("Telefon")]
    if not found:
        return None
    chosen = random.Random(seed).sample(found, min(size, len(found)))
    table = [{key: row.get(key, "") for key in customer_file.TABLE_KEYS} | {"Fuar": row.get("Fuar") or fair["ad"]}
             for row in chosen]
    slug = re.sub(r"[^a-z0-9]+", "_", legal_form._fold(fair["ad"])).strip("_") or "fuar"
    path = Path(folder) / f"{slug}_ornek_{len(table)}_firma.xlsx"
    customer_file.write_customer_file(path, table, lang="tr")
    return path


# --- one draft ---------------------------------------------------------------------------------------

def _subject(original: str) -> str:
    original = " ".join(str(original or "").split())
    return original if REPLY_PREFIX_RE.match(original) else f"Re: {original}".strip()


def _quoted(entry: dict) -> str:
    try:
        received = datetime.fromisoformat(entry["received"]).strftime("%d.%m.%Y %H:%M")
    except (KeyError, ValueError):
        received = entry["date"]
    lines = [f"> {line}".rstrip() for line in (entry.get("text") or "").split("\n")]
    return f"{received} tarihinde {entry['from']} şunu yazdı:\n" + "\n".join(lines)


def _new_address(entry: dict, account: str) -> str:
    """A generic company address the reply points to, other than the replying and the written address."""
    for match in intent.EMAIL_RE.finditer(entry.get("text") or ""):
        address = match.group(0).casefold().strip(".")
        if address in {entry["from"], entry["email"], account} or email_kind.is_excluded(address):
            continue
        return address
    return ""


def plan(entry: dict, decision: intent.Decision | None, fairs: list[dict]) -> tuple[str, str]:
    """Which template answers the reply, or why the reply goes to the user: ("template", "") or ("", reason)."""
    if entry.get("kind") == "istemiyor":
        return "istemiyor", ""
    if decision is None:
        return "", "niyet belirlenemedi"
    if decision.guven < config.INTENT_MIN_CONFIDENCE:
        return "", "niyet belirsiz"
    if decision.niyet == "siparis":
        return "", "sipariş, ödeme ya da pazarlık"
    if decision.niyet == "olumsuz":
        return "", "olumsuz yanıt; firma kapatıldı"
    if decision.niyet in PLAIN:
        return PLAIN[decision.niyet], ""
    if decision.niyet == "yanlis_kisi":
        return "yanlis_kisi", ""
    by_key = {f"f{fair['id']}": fair for fair in fairs}
    if decision.fuar == "katalog_disi":
        return "", "katalogda olmayan fuar"
    if decision.fuar in by_key:
        if decision.fuar_guven < config.INTENT_MIN_CONFIDENCE:
            return "", "fuar belirsiz"
        fair = by_key[decision.fuar]
        return ("teklif" if fair["durum"] == "hazir" and int(fair["turk_firma"] or 0) > 0 else "liste_yok"), ""
    if decision.niyet == "ornek" or decision.ornek_istiyor:
        return "ornek_fuarsiz", ""
    if decision.niyet == "fiyat" or decision.fiyat_istiyor:
        return "fiyat_fuarsiz", ""
    return "ilgileniyor", ""


def build(entry: dict, decision: intent.Decision | None, fairs: list[dict], *, drafting: Drafting,
          account: str) -> tuple[EmailMessage | None, str, str]:
    """The draft message for a reply, the template used, and the reason when there is no draft."""
    template, reason = plan(entry, decision, fairs)
    if not template:
        return None, "", reason
    texts = drafting.templates
    to, subject, threaded = entry["from"], _subject(entry.get("subject", "")), True
    attachments: list[Path] = []
    fair = next((item for item in fairs if decision and f"f{item['id']}" == decision.fuar), None)
    values = {"fuar": fair["ad"] if fair else ""}
    if template == "yanlis_kisi":
        to = _new_address(entry, account)
        if not to or not drafting.cold_template:
            return None, "", "yeni adres yok ya da kişiye ait"
        hitap = outreach.salutation(entry.get("company") or "", "TR")
        cold_subject, cold_body = outreach.render(drafting.cold_template, {"Hitap adı": hitap, "Firma": entry.get("company")})
        greeting, _, rest = cold_body.partition("\n")
        body = f"{greeting}\n\n{texts['yanlis_kisi']}\n{rest}".replace("\n\n\n", "\n\n")
        subject, threaded = cold_subject, False
    else:
        if template == "teklif":
            price = quote(fair)
            values |= {"turk_firma": str(price["turk_firma"]), "en_cok_tutar": money(price["en_cok"])}
        if template == "ilgileniyor" and decision and decision.sss in intent.FAQ and decision.sss != "yok":
            values["sss"] = texts[f"sss_{decision.sss}"]
        body = render(texts[template], values)
        wants_sample = bool(decision and (decision.ornek_istiyor or decision.niyet == "ornek"))
        if template == "teklif" and wants_sample:
            path = sample_file(fair, seed=entry["key"], folder=drafting.attachments_dir or Path(tempfile.gettempdir()))
            if path:
                body = f"{body}\n\n{render(texts['teklif_ornek'], values)}"
                attachments.append(path)
        if template == "ornek_fuarsiz" and drafting.fallback_sample and Path(drafting.fallback_sample).is_file():
            attachments.append(Path(drafting.fallback_sample))
        body = f"{body}\n\n{texts['imza']}\n\n{_quoted(entry)}"
    message = EmailMessage()
    message["From"] = formataddr((drafting.sender_name, account))
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=account.rsplit("@", 1)[-1])
    if threaded and entry.get("message_id"):
        message["In-Reply-To"] = entry["message_id"]
        message["References"] = " ".join(part for part in [entry.get("references", ""), entry["message_id"]] if part)
    message.set_content(body + "\n")
    for path in attachments:
        message.add_attachment(
            path.read_bytes(), maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=path.name,
        )
    return message, template, ""


def next_contact(decision: intent.Decision | None, today: date) -> date | None:
    return today + timedelta(days=LATER_DAYS) if decision and decision.niyet == "simdi_degil" else None


# --- morning summary --------------------------------------------------------------------------------

def summary(*, today: date, open_rows: list[dict], sent_yesterday: dict[str, int], replies_yesterday: dict[str, int],
            answered_yesterday: int, ready: dict[str, int], room: int, problems: list[str]) -> tuple[str, str]:
    """The subject and text of the morning summary e-mail."""
    drafts = [row for row in open_rows if row["sonuc"] == "taslak hazır"]
    to_user = [row for row in open_rows if row["sonuc"] == "size"]

    def line(row: dict) -> str:
        label = INTENT_LABELS.get(row["niyet"], row["niyet"] or "niyet yok")
        extra = f" — {row['neden']}" if row["neden"] else ""
        return f"  • {row['firma'] or row['eposta']} — {label}{extra} ({row['alindi'][:16].replace('T', ' ')})"

    out = [f"Günlük özet — {today:%d.%m.%Y}", "", "SİZİ BEKLEYENLER"]
    out.append(f"- Zoho'da gönderilmeyi bekleyen taslak: {len(drafts)}")
    out += [line(row) for row in drafts]
    out.append(f"- Taslak yazılmayan, sizin yanıtlayacağınız: {len(to_user)}")
    out += [line(row) for row in to_user]
    out += ["", "DÜN"]
    total_sent = sum(sent_yesterday.values())
    out.append(f"- Gönderilen soğuk e-posta: {total_sent}"
               + (f" ({', '.join(f'{name} {count}' for name, count in sorted(sent_yesterday.items()))})" if total_sent else ""))
    out.append("- Gelen: " + (", ".join(f"{name} {count}" for name, count in sorted(replies_yesterday.items()))
                              if replies_yesterday else "yok"))
    out.append(f"- Gönderdiğiniz yanıt: {answered_yesterday}")
    out += ["", "BUGÜN", f"- Günlük gönderim hakkı: {room}"]
    out += [f"- {name}: gönderilebilecek {count} firma" for name, count in sorted(ready.items())]
    out += ["", "SORUNLAR"] + ([f"- {problem}" for problem in problems] or ["- yok"])
    return f"Günlük özet — {today:%d.%m.%Y}", "\n".join(out) + "\n"
