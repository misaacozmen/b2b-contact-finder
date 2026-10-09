"""Only generic company mailboxes reach a delivery file (Talimat 40).

A delivery file names no person. An address that looks like a person's
mailbox (a first name, first.last, initial.last) or a data protection
mailbox (RODO, KVKK, privacy) is left out; a generic address of the same
firm takes its place when the site showed one. An initial glued to a
surname (jkowalski@) cannot be told from a brand and stays.
"""

from __future__ import annotations

import re
import unicodedata

from modules import scorer


DATA_PROTECTION_WORDS = frozenset({
    "rodo", "iod", "dpo", "gdpr", "kvkk", "privacy", "datenschutz", "dsgvo",
})
DATA_PROTECTION_PHRASES = (
    "ochronydanych", "ochronadanych", "inspektorochrony", "dataprotection", "dataprivacy", "kisiselveri",
)
ROLE_WORDS = frozenset({
    "info", "iletisim", "contact", "contacts", "kontakt", "satis", "sales", "sprzedaz", "handel", "handlowy",
    "handlowe", "bilgi", "office", "biuro", "export", "eksport", "ihracat", "import", "ithalat", "sekretariat",
    "marketing", "destek", "support", "serwis", "service", "servis", "teknik", "technik", "techniczne",
    "order", "orders", "siparis", "zamowienia", "zapytania", "shop", "sklep", "store", "magazyn", "faktury",
    "faktura", "invoice", "muhasebe", "accounting", "finans", "finance", "kadry", "hr", "ik", "praca",
    "kariera", "careers", "jobs", "media", "press", "pr", "reklam", "mail", "poczta", "admin", "web",
    "www", "online", "hello", "team", "firma", "company", "general", "enquiries", "inquiries", "recepcja",
    "logistyka", "logistics", "lojistik", "zakupy", "satinalma", "purchasing", "produkcja", "uretim",
    "kalite", "quality", "jakosc", "projekt", "projects", "proje", "design", "commerce", "customer",
    "obsluga", "dzial", "pl", "tr", "en", "de", "biuro1", "info1",
})
FIRST_NAMES = frozenset({
    # Polish
    "adam", "adrian", "agata", "agnieszka", "aleksander", "aleksandra", "alicja", "andrzej", "aneta",
    "anna", "ania", "antoni", "arkadiusz", "artur", "barbara", "bartek", "bartlomiej", "bartosz", "beata",
    "bogdan", "bogumila", "bozena", "cezary", "czeslaw", "damian", "danuta", "dariusz", "darek", "dawid",
    "dominik", "dominika", "dorota", "edyta", "elzbieta", "emilia", "ewa", "ewelina", "filip",
    "franciszek", "genowefa", "grazyna", "grzegorz", "halina", "henryk", "hubert", "iwona", "izabela",
    "jacek", "jadwiga", "jakub", "jan", "janek", "janina", "janusz", "jaroslaw", "jarek", "jerzy",
    "joanna", "jolanta", "jozef", "justyna", "kacper", "kamila", "karol", "karolina", "katarzyna",
    "kasia", "kazimierz", "kinga", "klaudia", "konrad", "krystyna", "krzysztof", "krzysiek", "leszek",
    "lucyna", "lukasz", "maciej", "maciek", "magdalena", "magda", "malgorzata", "gosia", "marcin",
    "marek", "marian", "mariusz", "marta", "marzena", "mateusz", "michal", "mikolaj", "miroslaw",
    "monika", "natalia", "norbert", "oliwia", "patryk", "patrycja", "pawel", "piotr", "piotrek",
    "przemyslaw", "przemek", "radoslaw", "radek", "rafal", "renata", "ryszard", "sebastian", "slawomir",
    "slawek", "stanislaw", "sylwia", "szymon", "tadeusz", "teresa", "tomasz", "tomek", "urszula",
    "wanda", "weronika", "wieslaw", "wiktor", "wiktoria", "witold", "wojciech", "wojtek", "zbigniew",
    "zbyszek", "zdzislaw", "zofia", "zuzanna", "zenon",
    # Turkish
    "abdullah", "adem", "ahmet", "ali", "alper", "arda", "ayhan", "aykut", "aylin", "ayse", "aysegul",
    "aysel", "aysun", "ayten", "aynur", "bahar", "banu", "baris", "batuhan", "bayram", "berk", "berkay",
    "berna", "betul", "bilal", "bilge", "birol", "bulent", "burak", "burcu", "busra", "can", "caner",
    "cansu", "cem", "cemal", "cengiz", "ceren", "cetin", "cigdem", "damla", "deniz", "derya", "dilara",
    "duygu", "ebru", "ece", "eda", "ekrem", "elif", "emel", "emine", "emre", "enes", "engin", "erdal",
    "erdem", "erdogan", "eren", "erhan", "erkan", "ersin", "esra", "ezgi", "fadime", "faruk", "fatih",
    "fatma", "ferhat", "fikret", "filiz", "furkan", "gamze", "gizem", "gokhan", "gonul", "gulay",
    "gulsen", "gulten", "gurkan", "hakan", "halil", "hamza", "hande", "hasan", "hatice", "havva",
    "hilal", "hulya", "huseyin", "ibrahim", "ilhan", "ilker", "ilknur", "irem", "ismail", "kaan",
    "kadir", "kemal", "kenan", "kerem", "kubra", "levent", "mahmut", "mehmet", "melike", "meltem",
    "merve", "mert", "mesut", "metin", "murat", "mustafa", "nazli", "necati", "nermin", "nesrin",
    "nihat", "nuri", "oguz", "oguzhan", "okan", "omer", "onur", "orhan", "osman", "ozan", "ozge",
    "ozgur", "ozlem", "pinar", "ramazan", "recep", "sabri", "salih", "seda", "sedat", "selcuk", "selim",
    "selin", "sema", "semih", "sena", "serap", "serdar", "serkan", "serpil", "sevda", "sevim", "sibel",
    "sinan", "sinem", "songul", "suat", "sukru", "suleyman", "taner", "tamer", "tarik", "tolga", "tuba",
    "tugba", "tulay", "tuncay", "turgay", "ufuk", "ugur", "umit", "volkan", "yakup", "yasemin", "yasin",
    "yavuz", "yeliz", "yunus", "yusuf", "zafer", "zehra", "zeki", "zeynep",
    # Other common first names in European fair lists
    "alessandro", "alex", "alexander", "alexandra", "andrea", "andreas", "andrei", "anton", "antonio",
    "carlos", "christian", "christoph", "daniel", "david", "dmitry", "elena", "eric", "eva", "francesco",
    "frank", "george", "giuseppe", "hans", "igor", "irina", "ivan", "james", "jens", "john", "jose",
    "juan", "julia", "julien", "klaus", "lars", "laura", "luca", "lucas", "marco", "maria", "mario",
    "markus", "martin", "matteo", "michael", "michel", "nicolas", "olga", "oliver", "olivier", "paolo",
    "pascal", "paul", "peter", "philippe", "pierre", "robert", "roman", "sandra", "sara", "sarah",
    "sergey", "sergio", "stefan", "stefano", "stephan", "svetlana", "tatiana", "thomas", "uwe",
    "viktor", "vladimir", "wolfgang",
})


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.casefold().replace("ł", "l").replace("ı", "i"))
    return "".join(character for character in text if not unicodedata.combining(character))


def _local_parts(email: str) -> list[str]:
    local = _fold(str(email or "").strip().split("@", 1)[0])
    return [part for part in re.split(r"[._+-]+", local) if part]


def is_data_protection(email: str) -> bool:
    parts = _local_parts(email)
    compact = "".join(parts)
    return any(part in DATA_PROTECTION_WORDS for part in parts) or any(
        phrase in compact for phrase in DATA_PROTECTION_PHRASES
    )


def is_personal(email: str) -> bool:
    parts = _local_parts(email)
    if not parts or len(parts) > 3 or not all(part.isalpha() for part in parts):
        return False
    if len(parts) == 1:
        return parts[0] in FIRST_NAMES
    if any(part in ROLE_WORDS for part in parts):
        return False
    initial_then_surname = len(parts[0]) == 1 and len(parts[-1]) >= 3
    return initial_then_surname or any(part in FIRST_NAMES for part in parts)


def is_excluded(email: str) -> bool:
    return bool(str(email or "").strip()) and (is_personal(email) or is_data_protection(email))


def _alternatives(row: dict) -> list[str]:
    values = row.get("alternative_emails") or []
    if isinstance(values, str):
        values = re.split(r"[;,\s]+", values)
    return [str(value).strip() for value in values if "@" in str(value)]


def keep_generic_email(row: dict) -> dict:
    """Swap a person's or data protection mailbox for a generic one of the same firm, or leave the e-mail empty."""
    for key in ("stage_a", "stage_ab"):
        stage = row.get(key)
        if isinstance(stage, dict) and is_excluded(stage.get("email", "")):
            row[key] = {**stage, "email": "", "email_confidence": ""}
    email = str(row.get("email") or "").strip()
    if not is_excluded(email):
        return row
    domain = scorer.registrable_domain(email.rsplit("@", 1)[-1])
    for alternative in _alternatives(row):
        if not is_excluded(alternative) and scorer.registrable_domain(alternative.rsplit("@", 1)[-1]) == domain:
            row["email"] = alternative
            return row
    row["email"] = ""
    row["email_source_tier"] = ""
    row["email_source"] = ""
    return row
