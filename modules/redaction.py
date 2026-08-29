"""Recursive credential redaction for logs, cache, and portable artifacts."""

from __future__ import annotations

import html
import re
import urllib.parse
from typing import Any

from modules import redaction_scanner


_MAX_ENCODING_DEPTH = 8
MAX_SANITIZE_DEPTH = 64

_SENSITIVE_KEYS = {
    "api_key", "apikey", "access_token", "accesstoken", "authorization",
    "client_secret", "clientsecret", "client_id", "clientid", "credential",
    "cookie", "password", "proxy_authorization", "private_key", "refresh_token",
    "refreshtoken", "secret", "secret_key", "sig", "signature", "token",
    "x_amz_credential", "x_amz_security_token", "x_amz_signature",
    "x_goog_credential", "x_goog_signature", "x_api_key", "x_auth_token",
}
_SENSITIVE_SUFFIXES = (
    "_api_key", "_apikey", "_access_token", "_password", "_secret", "_token",
    "_client_secret", "_client_id", "_credential", "_cookie",
    "_proxy_authorization", "_private_key",
)
_PARAM_NAMES = (
    r"api[_-]?key|access[_-]?token|authorization|client[_-]?secret|client[_-]?id|"
    r"credential|cookie|proxy[_-]?authorization|private[_-]?key|password|"
    r"refresh[_-]?token|secret|secret[_-]?key|sig|signature|token|"
    r"x-amz-credential|x-amz-security-token|x-amz-signature|"
    r"x-goog-credential|x-goog-signature|x-api-key|x-auth-token"
)
_EMBEDDED_PROPS = (
    r"api[_-]?key|apikey|access[_-]?token|accesstoken|client[_-]?secret|clientsecret|"
    r"client[_-]?id|clientid|credential|cookie|password|proxy[_-]?authorization|"
    r"private[_-]?key|refresh[_-]?token|refreshtoken|secret|secret[_-]?key|"
    r"sig|signature|token|authorization"
)

_CONTAINER_PROP = re.compile(rf'(?i)[\"\']?\b(?:{_EMBEDDED_PROPS})\b[\"\']?\s*:\s*(?:\[(?!REDACTED\])|\{{)')
_NARROW_HEADER = re.compile(
    r"(?im)^[ \t]*(?:"
    r"((?:Authorization|Proxy-Authorization)\s*:\s*(?:Basic|Bearer|Digest|Negotiate|AWS4[A-Za-z0-9\-_]*)\b[^\r\n]*)|"
    r"((?:Cookie|Set-Cookie)\s*:\s*[a-zA-Z0-9_\-]+=+[^\r\n]*)|"
    r"((?:X-Api-Key|Api-Key|X-Auth-Token|X-Amz-Security-Token)\s*:\s*[^\r\n]+))"
)
_LOG_HEADER_REGEX = re.compile(
    r"(?im)(?:^|[ \t]+)(Authorization|Proxy-Authorization|Cookie|Set-Cookie|X-Api-Key|Api-Key|X-Auth-Token|X-Amz-Security-Token)\s*:\s*[^\r\n]+"
)
_SEP_PREFIX = r"(?:[?&#;]|&(?:amp;)*(?:amp|#38|#x26);|%(?:25){0,7}(?:3f|26|23|3b))"
_SEP_EQUALS = r"(?:=|&(?:amp;)*(?:#61|#x3d);|%(?:25){0,7}3d)"
_VALUE_STOP = r'(?:[&#\s"\'<>`\\]|%(?:25){0,7}(?:26|3f|23|3b|20)|&(?:amp;)*(?:amp|#38|#x26);)'
_QUERY_SECRET = re.compile(rf"({_SEP_PREFIX}(?:data[-_])?(?:{_PARAM_NAMES}){_SEP_EQUALS})(?:(?!{_VALUE_STOP}).)+", re.IGNORECASE)
_BEARER_SECRET = re.compile(r"\bBearer\s+[^\s&#\"\'<>`{}\[\]\\|(),;]+", re.IGNORECASE)
_URL_USERINFO = re.compile(r"(https?://)[^/@\s\"\'<>`{}\[\]\\|(),;]+@", re.IGNORECASE)

_JSON_JS_QUOTED = re.compile(rf'(?i)(["\']?\b(?:{_EMBEDDED_PROPS})\b["\']?\s*:\s*)(["\'`])(?:\\.|(?!\2)[^\\])*\2')
_JSON_JS_UNQUOTED = re.compile(
    rf'(?i)(["\']?\b(?:{_EMBEDDED_PROPS})\b["\']?\s*:\s*)(?!(?:["\'`]|\[REDACTED\](?![a-zA-Z0-9_\-\.\$])|true\b|false\b|null\b))([a-zA-Z0-9_\-\.\$]+(?:\[REDACTED\][a-zA-Z0-9_\-\.\$]*)*)(?=[,\s\}};])',
)
_HTML_ATTR_QUOTED = re.compile(rf'(?i)(\b(?:data-)?(?:{_EMBEDDED_PROPS})\b\s*=\s*)(["\'])(?:\\.|(?!\2)[^\\])*\2')
_HTML_ATTR_UNQUOTED = re.compile(rf'(?i)(\b(?:data-)?(?:{_EMBEDDED_PROPS})\b\s*=\s*)(?!(?:["\']|\[REDACTED\](?![^\s>\"\'\]])))([^\s>\"\'\]]+)(?=[\s>])')
_STANDALONE_ASSIGN = re.compile(rf'(?i)((?:\A|[\s{{,])\b(?:{_EMBEDDED_PROPS})\b\s*[:=]\s*)(["\'`])(?:\\.|(?!\2)[^\\])*\2')
_STANDALONE_UNQUOTED = re.compile(rf'(?i)((?:\A|[\s{{,])\b(?:{_EMBEDDED_PROPS})\b\s*[:=]\s*)(?!(?:["\'`]|\[REDACTED\](?![^\s,}}])))([^\s,}}]+)')


def normalize_unicode_scalars(value: Any, *, _depth: int = 0, _seen: set[int] | None = None) -> Any:
    """Replace lone UTF-16 surrogate code points throughout a value tree."""
    if _depth > MAX_SANITIZE_DEPTH:
        return "[REDACTED]"
    if isinstance(value, str):
        return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    if _seen is None:
        _seen = set()
    if isinstance(value, (dict, list, tuple, set)):
        value_id = id(value)
        if value_id in _seen:
            return "[REDACTED]"
        _seen.add(value_id)
    if isinstance(value, dict):
        result = {
            normalize_unicode_scalars(str(key), _depth=_depth + 1, _seen=_seen): normalize_unicode_scalars(item, _depth=_depth + 1, _seen=_seen)
            for key, item in value.items()
        }
        _seen.remove(value_id)
        return result
    if isinstance(value, list):
        result = [normalize_unicode_scalars(item, _depth=_depth + 1, _seen=_seen) for item in value]
        _seen.remove(value_id)
        return result
    if isinstance(value, tuple):
        result = tuple(normalize_unicode_scalars(item, _depth=_depth + 1, _seen=_seen) for item in value)
        _seen.remove(value_id)
        return result
    if isinstance(value, set):
        result = {normalize_unicode_scalars(item, _depth=_depth + 1, _seen=_seen) for item in value}
        _seen.remove(value_id)
        return result
    return value


def _sensitive_key(value: object) -> bool:
    raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value).strip())
    key = re.sub(r"[-.\s]+", "_", raw.casefold())
    if key in {"independence_key", "cache_key"}:
        return False
    return key in _SENSITIVE_KEYS or key.startswith(("_secret", "secret_")) or key.endswith(_SENSITIVE_SUFFIXES)


def _redact_plain_text(text: str) -> tuple[str, bool]:
    orig = str(text)
    res = _NARROW_HEADER.sub(lambda m: m.group(0).split(":", 1)[0] + ": [REDACTED]", orig)
    res = _QUERY_SECRET.sub(r"\1[REDACTED]", res)
    res = _BEARER_SECRET.sub("Bearer [REDACTED]", res)
    res = _URL_USERINFO.sub(r"\1[REDACTED]@", res)
    res = _JSON_JS_QUOTED.sub(r"\1\2[REDACTED]\2", res)
    res = _JSON_JS_UNQUOTED.sub(r"\1[REDACTED]", res)
    res = _HTML_ATTR_QUOTED.sub(r"\1\2[REDACTED]\2", res)
    res = _HTML_ATTR_UNQUOTED.sub(r"\1[REDACTED]", res)
    res = _STANDALONE_ASSIGN.sub(r"\1\2[REDACTED]\2", res)
    res = _STANDALONE_UNQUOTED.sub(r"\1[REDACTED]", res)
    return res, (res != orig)


def redact_text(value: str) -> str:
    raw_str = normalize_unicode_scalars(str(value))
    if _CONTAINER_PROP.search(raw_str):
        return "[REDACTED]"
    p_text, p_mod = _redact_plain_text(raw_str)
    shadow, unresolved = redaction_scanner.normalize_shadow(p_text)
    if unresolved or _CONTAINER_PROP.search(shadow):
        return "[REDACTED]"
    s_text, s_mod = _redact_plain_text(shadow)
    return "[REDACTED]" if s_mod else p_text


def redact_known_values(text: str) -> str:
    if not text:
        return text
    result = redact_text(str(text))
    if result == "[REDACTED]":
        return result
    import config

    known_secrets: set[str] = set()
    for attr in dir(config):
        if any(attr.endswith(s) for s in ("_API_KEY", "_CLIENT_ID", "_TOKEN", "_SECRET", "_PASSWORD")):
            val = getattr(config, attr, None)
            if isinstance(val, str) and val.strip():
                known_secrets.add(val.strip())

    candidates: set[str] = set()
    for s in known_secrets:
        candidates.update({s, f"[REDACTED]{s}", urllib.parse.quote(s, safe=""), urllib.parse.quote_plus(s)})
        curr_p, curr_h = s, s
        for _ in range(_MAX_ENCODING_DEPTH):
            curr_p = urllib.parse.quote(curr_p, safe="")
            curr_h = html.escape(curr_h)
            candidates.update({curr_p, curr_h})

    for secret in sorted(candidates, key=len, reverse=True):
        if secret and secret != "[REDACTED]":
            result = result.replace(secret, "[REDACTED]")

    shadow, unresolved = redaction_scanner.normalize_shadow(result)
    if unresolved or any(s and s in shadow for s in known_secrets if result != "[REDACTED]"):
        return "[REDACTED]"
    return result


def redact_log_text(text: str) -> str:
    if not text:
        return text
    res = redact_known_values(str(text))
    res = _LOG_HEADER_REGEX.sub(r"\1: [REDACTED]", res)
    return redact_known_values(res)


def _sanitize_depth(value: Any, depth: int, seen: set[int]) -> Any:
    if depth > MAX_SANITIZE_DEPTH:
        return "[REDACTED]"
    value = normalize_unicode_scalars(value, _depth=depth)
    val_id = id(value)
    if isinstance(value, dict):
        if val_id in seen:
            return "[REDACTED]"
        seen.add(val_id)
        try:
            return {str(k): _sanitize_depth(v, depth + 1, seen) for k, v in value.items() if not _sensitive_key(k)}
        finally:
            seen.remove(val_id)
    if isinstance(value, (list, tuple)):
        if val_id in seen:
            return "[REDACTED]"
        seen.add(val_id)
        try:
            return [_sanitize_depth(item, depth + 1, seen) for item in value]
        finally:
            seen.remove(val_id)
    if isinstance(value, set):
        if val_id in seen:
            return "[REDACTED]"
        seen.add(val_id)
        try:
            return sorted((_sanitize_depth(item, depth + 1, seen) for item in value), key=repr)
        finally:
            seen.remove(val_id)
    if isinstance(value, str):
        return redact_known_values(value)
    return value


def sanitize(value: Any) -> Any:
    return _sanitize_depth(value, 0, set())
