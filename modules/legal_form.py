"""Whether a firm name carries a company form (Talimat 41).

A sole trader (Polish JDG) or a civil partnership (spółka cywilna) is a person
doing business; their name and contacts are personal data. Where the country
profile asks for it, a delivery file holds only firms whose name shows a
company form. A name without any form counts as a sole trader.
"""

from __future__ import annotations

import re
import unicodedata


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
)
# French forms written before the name (SAS JOUANEL INDUSTRIE).
START_COMPANY_FORMS = ("sas", "sarl", "s a s", "s a r l")
# Short company forms that are ordinary letters elsewhere; matched only as the name's last word(s).
END_COMPANY_FORMS = (
    "s a", "sa", "sp", "ag", "kg", "ohg", "se", "bv", "b v", "nv", "n v", "sas", "spa", "as", "ab",
    "oy", "oyj", "sia", "aps", "sl", "s l", "lda", "ad", "tov", "ou", "s a c", "co",
)
# Forms of a person doing business; they win over any company word.
PERSON_FORMS = ("spolka cywilna", "sp cywilna")
END_PERSON_FORMS = ("s c", "sc", "e k", "ek")


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
    return (
        _has(words, COMPANY_FORMS)
        or _ends(outside, END_COMPANY_FORMS)
        or any(outside.startswith(f"{form} ") for form in START_COMPANY_FORMS)
    )
