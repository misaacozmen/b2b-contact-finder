"""Whether a firm name carries a company form (Talimat 41).

A sole trader (Polish JDG) or a civil partnership (spółka cywilna) is a person
doing business; their name and contacts are personal data. Where the country
profile asks for it, a delivery file holds only firms whose name shows a
company form. A name without any form counts as a sole trader.

Turkish catalogues often list a brand without any form, so a Turkish customer
file keeps such names and leaves out only those that show a person: a person
form (adi ortaklık) or a name ending in a first name and a surname (Talimat 43).

British companies end in Ltd, Limited, PLC, LLP, CIC (Welsh: Cyf, CCC, PAC).
A name ending in "& Co" is a partnership, not a company (Talimat 44).
"""

from __future__ import annotations

import re
import unicodedata

from modules.email_kind import FIRST_NAMES as MAILBOX_FIRST_NAMES


# Company forms written out or abbreviated; matched as whole words anywhere in the name.
COMPANY_FORMS = (
    "spolka z ograniczona", "sp z o o", "sp z oo", "sp zoo", "spzoo", "spolka z o o", "spolka akcyjna",
    "prosta spolka akcyjna", "spolka jawna", "sp j", "spj", "spolka komandytowa", "sp k", "spk",
    "spolka komandytowo akcyjna", "s k a", "spolka partnerska", "sp p", "spoldzielnia", "fundacja",
    "stowarzyszenie", "gmbh", "ltd", "limited", "llc", "inc", "corp", "corporation", "plc", "llp",
    "sarl", "s a r l", "srl", "s r l", "s p a", "sro", "s r o", "d o o", "doo", "uab", "kft", "zrt",
    "eood", "ood", "bvba", "sprl", "pty", "anonim", "sirketi", "ltd sti", "a s", "s a s",
    "zwiazek", "zwiazku", "izba", "uniwersytet", "politechnika", "ltda", "oddzial w polsce",
    "societa per azioni", "mbh", "sdn bhd", "bhd", "cooperative", "consorzio", "konfederacja",
    "automobilklub", "centrum kultury", "osrodek kultury", "gminne", "gminny",
    "kooperatifi", "dernegi", "vakfi", "odasi", "birligi", "universitesi", "belediyesi", "holding",
    "mudurlugu", "baskanligi",
    "public limited company", "limited liability partnership", "community interest company",
    "charitable incorporated organisation", "cyfyngedig", "university",
)
# French forms written before the name (SAS JOUANEL INDUSTRIE).
START_COMPANY_FORMS = ("sas", "sarl", "s a s", "s a r l")
# Short company forms that are ordinary letters elsewhere; matched only as the name's last word(s).
END_COMPANY_FORMS = (
    "s a", "sa", "sp", "ag", "kg", "ohg", "se", "bv", "b v", "nv", "n v", "sas", "spa", "as", "ab",
    "oy", "oyj", "sia", "aps", "sl", "s l", "lda", "ad", "tov", "ou", "s a c", "co", "sti",
    "p l c", "l l p", "cic", "cio", "cyf", "ccc", "pac",
)
# "& Co" or "and Company" ends the name of a British partnership.
PARTNERSHIP_END_RE = re.compile(r"(?:&|\band)\s*co(?:mpany)?\W*$", re.IGNORECASE)
# Forms of a person doing business; they win over any company word.
PERSON_FORMS = ("spolka cywilna", "sp cywilna", "adi ortakligi", "adi ortaklik")
END_PERSON_FORMS = ("s c", "sc", "e k", "ek")
# Turkish first names the mailbox name list lacks.
EXTRA_FIRST_NAMES = frozenset({
    "abdulkadir", "abdurrahman", "akif", "alev", "alp", "altan", "arif", "asli", "atakan", "aydin", "aysenur",
    "bekir", "berat", "beyza", "buse", "cagla", "cagri", "celal", "cihan", "cuneyt", "dogukan", "efe",
    "egemen", "emin", "emrah", "emrullah", "ender", "erol", "ertan", "ertugrul", "esat", "evren", "fahri",
    "feyza", "fuat", "gokce", "gulsah", "gurcan", "harun", "hayati", "hikmet", "idris", "ihsan", "ilyas",
    "irfan", "ismet", "kamil", "koray", "kubilay", "lutfi", "mehtap", "melek", "melih", "mine", "muammer",
    "muhammed", "muhammet", "musa", "nail", "namik", "nazim", "necdet", "nevzat", "nihal", "nilufer",
    "nurettin", "oktay", "onder", "ozcan", "ozkan", "rahmi", "rasim", "riza", "sadik", "sait", "sami",
    "savas", "seckin", "sefa", "selahattin", "selma", "semra", "sevgi", "sevket", "seyma", "sezer",
    "sezgin", "soner", "sukran", "sule", "tahir", "talha", "tayfun", "tevfik", "tufan", "tugrul", "tuncer",
    "turan", "ulas", "unal", "utku", "vedat", "veli", "yalcin", "yasar", "yener", "yesim", "yigit",
    "yucel", "yuksel", "zekeriya", "zuhal",
})
PERSON_FIRST_NAMES = MAILBOX_FIRST_NAMES | EXTRA_FIRST_NAMES
# Trade words that end a firm name; a first name before one of them is a brand, not an owner.
BUSINESS_WORDS = frozenset({
    "sanayi", "sanayii", "san", "ticaret", "tic", "insaat", "yapi", "gida", "tekstil", "mobilya", "makina",
    "makine", "otomotiv", "elektrik", "elektronik", "enerji", "plastik", "metal", "kimya", "tasarim",
    "mimarlik", "dizayn", "design", "reklam", "ajans", "medya", "media", "expo", "fuar", "fuarcilik",
    "organizasyon", "group", "grup", "global", "international", "turkey", "turkiye", "istanbul", "ankara",
    "izmir", "export", "import", "pazarlama", "lojistik", "turizm", "otel", "ambalaj", "kalip", "mutfak",
    "sogutma", "showroom", "studio", "atolye", "atolyesi", "market", "danismanlik", "muhendislik",
    "teknoloji", "technology", "yazilim", "sistem", "sistemleri", "urunleri", "ekipmanlari", "cihazlari",
    "aksesuar", "hirdavat", "boya", "cam", "ahsap", "mermer", "seramik", "kozmetik", "saglik", "medikal",
    "tarim", "yayincilik", "dergi", "dergisi", "matbaa", "baski", "promosyon", "imalat", "imalati", "uretim",
    "stand", "exhibition", "exhibitions", "architecture", "services", "solutions", "sut",
})


def _fold(name: str, *, drop_brackets: bool = False) -> str:
    text = unicodedata.normalize("NFKD", str(name or "").casefold().replace("ł", "l").replace("ı", "i"))
    text = "".join(character for character in text if not unicodedata.combining(character))
    if drop_brackets:
        text = re.sub(r"\([^)]*\)", " ", text)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _has(words: str, forms: tuple[str, ...]) -> bool:
    return any(f" {form} " in f" {words} " for form in forms)


def _ends(words: str, forms: tuple[str, ...]) -> bool:
    return any(words == form or words.endswith(f" {form}") for form in forms)


def is_company(name: str) -> bool:
    words = _fold(name)
    # A bracketed brand after the name hides an end form: "Malion BV (MARKA)".
    outside = _fold(name, drop_brackets=True)
    if not words or _has(words, PERSON_FORMS) or _ends(outside, END_PERSON_FORMS):
        return False
    if PARTNERSHIP_END_RE.search(re.sub(r"\([^)]*\)", " ", str(name)).strip()):
        return False
    return (
        _has(words, COMPANY_FORMS)
        or _ends(outside, END_COMPANY_FORMS)
        or any(outside.startswith(f"{form} ") for form in START_COMPANY_FORMS)
    )


def names_a_person(name: str) -> bool:
    """Whether a firm name shows a person doing business: a person form, or an owner's name at the end."""
    words = _fold(name)
    if not words:
        return False
    if _has(words, PERSON_FORMS) or _ends(_fold(name, drop_brackets=True), END_PERSON_FORMS):
        return True
    if is_company(name):
        return False
    # The owner's name ends the name or the part after a dash: "ORNEK HEDİYELİK - ALİ YILMAZ".
    tail = re.split(r"\s[-–]\s", str(name))[-1]
    tokens = _fold(tail, drop_brackets=True).split()
    if len(tokens) < 2 or tokens[-1] in BUSINESS_WORDS or tokens[-1].isdigit():
        return False
    return tokens[-2] in PERSON_FIRST_NAMES
