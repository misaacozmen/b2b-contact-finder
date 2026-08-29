"""Canonical contact keys used by collision and publication gates."""

from __future__ import annotations

from modules import phone, scorer


def canonical_website(value: object) -> str:
    return scorer.normalize_domain(str(value or ""))


def canonical_email(value: object) -> str:
    return str(value or "").strip().casefold()


def canonical_phone(value: object) -> str:
    return phone.normalize_phone(str(value or ""), default_country="TR")


def contact_key(field: str, value: object) -> str:
    normalizer = {"website": canonical_website, "email": canonical_email, "phone": canonical_phone}[field]
    return normalizer(value)
