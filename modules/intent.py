"""What a reply to a cold e-mail asks for, decided by TypeSafe's Jev model (Talimat 51).

Jev answers typed questions (one of the given choices, or a yes/no
probability) with a confidence; it writes no text.  Before a reply leaves the
computer its greeting and signature lines are dropped and e-mail addresses,
phone numbers and web addresses are replaced by markers, so no personal data
goes to the service.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import urllib.error
import urllib.request

import config


INTENTS = {
    "ilgileniyor": "Shows interest or asks a general question about the lists or the service (format, columns, "
                   "which fairs, delivery time, a call), without asking for a price, a sample or placing an order.",
    "fiyat": "Asks for a price, cost, fee or quote. Turkish examples: 'fiyat', 'ücret', 'ne kadar', 'kaça', "
             "'kaç para', 'ücretli mi', 'teklif'.",
    "ornek": "Asks for a sample list to check the quality.",
    "siparis": "Wants to buy or has decided or paid: asks how to proceed, about payment, invoice or delivery, "
               "or makes a price offer or bargains.",
    "soru_adres": "Asks how their own address was found or why we wrote to them.",
    "soru_kvkk": "Asks whether the lists themselves comply with KVKK or data protection law, or where the data "
                 "in the lists comes from.",
    "simdi_degil": "Not now: no need or budget at the moment, will evaluate or get back later; the door is left open.",
    "olumsuz": "Not interested and closes the conversation, without asking to be removed.",
    "istemiyor": "Asks to stop the e-mails or to be removed from the list, or complains about unsolicited e-mail.",
    "yanlis_kisi": "Wrong recipient or address: redirects to another department or address, the address is not "
                   "used, or the firm closed.",
}
FAQ = {
    "bicim": "Asks about the file format (Excel, CSV).",
    "sutunlar": "Asks which columns or fields the list contains (website, e-mail, phone, stand).",
    "kisi_bilgisi": "Asks whether the list contains person names, personal e-mails or mobile phones.",
    "teslim": "Asks how long the delivery takes.",
    "fark": "Asks how the list differs from the list the fair itself publishes.",
    "yok": "None of these questions.",
}
INSTRUCTIONS = {
    "niyet": "This is a reply to our cold e-mail offering exhibitor lists of trade fairs in Turkey (company, "
             "stand, website, generic e-mail, phone). What does the sender want?",
    "fiyat_istiyor": "The sender asks for a price, cost, fee or quote. Turkish examples: 'fiyat', 'ücret', "
                     "'ne kadar', 'kaça', 'ücretli mi', 'teklif'.",
    "ornek_istiyor": "The sender asks for a sample list or wants to see part of a list first.",
    "fuar": "Which trade fair does the sender mean? If several are mentioned, choose the first one.",
    "sss": "Which of these questions about the lists does the sender ask?",
}
CLOSING_RE = re.compile(
    r"^(saygılarımla|saygılar|iyi çalışmalar|teşekkürler|teşekkür ederim|kolay gelsin|selamlar|sevgiler|"
    r"best regards|kind regards|regards|thanks|thank you|cheers)[\s.,!]*$",
    re.IGNORECASE,
)
GREETING_RE = re.compile(r"^(merhaba|sayın|selam|iyi günler|günaydın|dear|hello|hi)\b.{0,40}$", re.IGNORECASE)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
URL_RE = re.compile(r"(https?://\S+|www\.\S+)", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d ()./-]{6,}\d")


class IntentError(Exception):
    """Jev could not be asked; the reply goes to the user without a decision."""


@dataclass
class Decision:
    niyet: str
    guven: float
    fiyat_istiyor: bool
    ornek_istiyor: bool
    fuar: str
    fuar_guven: float
    sss: str


def anonymize(text: str) -> str:
    """The reply without greeting and signature, with addresses and numbers replaced by markers."""
    lines = [line.strip() for line in str(text or "").replace("\r\n", "\n").split("\n")]
    while lines and (not lines[0] or GREETING_RE.match(lines[0])):
        lines.pop(0)
    kept = []
    for line in lines:
        if CLOSING_RE.match(line):
            break
        kept.append(line)
    body = "\n".join(kept).strip()
    body = EMAIL_RE.sub("[e-posta]", body)
    body = URL_RE.sub("[web adresi]", body)
    return PHONE_RE.sub("[telefon]", body)


def api_key() -> str:
    from modules import api_configuration

    return api_configuration.load_saved_api_keys().get("typesafe", "") or os.environ.get("TYPESAFE_API_KEY", "")


def _fair_choices(fairs: list[dict]) -> dict[str, str]:
    choices = {}
    for fair in fairs:
        names = "; ".join(part for part in [fair["ad"], fair.get("diger_adlar", "")] if part)
        choices[f"f{fair['id']}"] = f"The trade fair {names}"
    choices["katalog_disi"] = "A specific trade fair is mentioned but it is none of the fairs above"
    choices["yok"] = "No specific trade fair is mentioned"
    return choices


def request_body(text: str, fairs: list[dict]) -> dict:
    return {
        "state": text,
        "model": config.TYPESAFE_MODEL,
        "questions": {
            "niyet": {"type": "choice", "instructions": INSTRUCTIONS["niyet"], "criteria": INTENTS},
            "fiyat_istiyor": {"type": "noul", "instructions": INSTRUCTIONS["fiyat_istiyor"]},
            "ornek_istiyor": {"type": "noul", "instructions": INSTRUCTIONS["ornek_istiyor"]},
            "fuar": {"type": "choice", "instructions": INSTRUCTIONS["fuar"], "criteria": _fair_choices(fairs)},
            "sss": {"type": "choice", "instructions": INSTRUCTIONS["sss"], "criteria": FAQ},
        },
    }


def decide(text: str, fairs: list[dict], *, key: str, opener=None) -> Decision:
    """Ask Jev about one reply; ``text`` must already be anonymized."""
    if not key:
        raise IntentError("Jev anahtarı kayıtlı değil")
    request = urllib.request.Request(
        config.TYPESAFE_API_URL, data=json.dumps(request_body(text, fairs)).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with (opener or urllib.request.urlopen)(request, timeout=config.TYPESAFE_TIMEOUT_SEC) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise IntentError(f"Jev HTTP {error.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise IntentError(f"Jev'e ulaşılamadı ({type(error).__name__})") from None
    answers = data.get("answers") or {}
    try:
        niyet, fuar, sss = answers["niyet"], answers["fuar"], answers["sss"]
        return Decision(
            niyet=str(niyet["choice"]), guven=float(niyet.get("confidence") or 0.0),
            fiyat_istiyor=float(answers["fiyat_istiyor"]["noul"]) >= 0.5,
            ornek_istiyor=float(answers["ornek_istiyor"]["noul"]) >= 0.5,
            fuar=str(fuar["choice"]), fuar_guven=float(fuar.get("confidence") or 0.0), sss=str(sss["choice"]),
        )
    except (KeyError, TypeError, ValueError):
        raise IntentError("Jev yanıtı beklenen biçimde değil") from None
