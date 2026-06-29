import re

import phonenumbers

import config


def normalize_phone(raw: str, default_country: str | None = None) -> str:
    country = default_country or config.PHONE_DEFAULT_COUNTRY
    value = raw.strip()
    if not value:
        return ""

    candidates = [value]
    digits = re.sub(r"\D", "", value)
    if digits.startswith("00"):
        candidates.append(f"+{digits[2:]}")
    if country == "TR" and digits.startswith("0"):
        candidates.append(f"+90{digits[1:]}")

    for candidate in candidates:
        try:
            parsed = phonenumbers.parse(candidate, country)
        except phonenumbers.NumberParseException:
            continue
        if not phonenumbers.is_possible_number(parsed):
            continue
        region = phonenumbers.region_code_for_number(parsed)
        if config.PHONE_ALLOWED_COUNTRIES and region not in config.PHONE_ALLOWED_COUNTRIES:
            continue
        if config.PHONE_OUTPUT_FORMAT == "e164":
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        national_digits = re.sub(
            r"\D",
            "",
            phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.NATIONAL),
        )
        if country == "TR" and not national_digits.startswith("0"):
            national_digits = f"0{national_digits}"
        return national_digits

    return ""
