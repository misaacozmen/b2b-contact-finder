"""User-scoped secret encryption using Windows DPAPI."""

from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[_DataBlob, object]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _dpapi(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise OSError("DPAPI is only available on Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    input_blob, input_buffer = _blob(data)
    output_blob = _DataBlob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(input_blob), "B2B Contact Finder", None, None, None,
            flags, ctypes.byref(output_blob),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob), None, None, None, None,
            flags, ctypes.byref(output_blob),
        )
    del input_buffer
    if not ok:
        raise OSError(ctypes.get_last_error(), "Windows DPAPI operation failed")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def encode(values: dict[str, str]) -> dict:
    raw = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encrypted = _dpapi(raw, protect=True)
    return {"version": 1, "storage": "windows_dpapi_user", "encrypted": base64.b64encode(encrypted).decode("ascii")}


def decode(payload: dict) -> dict[str, str]:
    if payload.get("storage") != "windows_dpapi_user" or not payload.get("encrypted"):
        return {key: value for key, value in payload.items() if isinstance(key, str) and isinstance(value, str)}
    encrypted = base64.b64decode(payload["encrypted"], validate=True)
    values = json.loads(_dpapi(encrypted, protect=False).decode("utf-8"))
    return {key: value for key, value in values.items() if isinstance(key, str) and isinstance(value, str)}
