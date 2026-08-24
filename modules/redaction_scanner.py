"""Pure normalization and fixed-point shadow decoder for credential detection."""

from __future__ import annotations

import html
import re


MAX_NORMALIZATION_ROUNDS = 64

_PERCENT_CHAIN = re.compile(r"%(?:25)*([0-9a-fA-F]{2})")
_ENTITY_CHAIN = re.compile(r"&(?:amp;)+([a-zA-Z0-9#x]+);")
_UNICODE_HEX = re.compile(r"\\u([0-9a-fA-F]{4})")


def _decode_percent(m: re.Match) -> str:
    return chr(int(m.group(1), 16))


def _decode_unicode(m: re.Match) -> str:
    try:
        return chr(int(m.group(1), 16))
    except (ValueError, OverflowError):
        return m.group(0)


def _step(s: str) -> str:
    s = _PERCENT_CHAIN.sub(_decode_percent, s)
    s = _ENTITY_CHAIN.sub(r"&\1;", s)
    s = html.unescape(s)
    return _UNICODE_HEX.sub(_decode_unicode, s)


def normalize_shadow(text: str) -> tuple[str, bool]:
    """Return normalized text for credential detection and whether encoding remains unresolved."""
    s = str(text)
    for _ in range(MAX_NORMALIZATION_ROUNDS):
        prev = s
        s = _step(s)
        if s == prev:
            return s, False
    check = _step(s)
    return s, (check != s)
