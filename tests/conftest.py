"""Pytest configuration and network isolation audit hook."""

from __future__ import annotations

import os
import sys

if os.environ.get("B2B_TEST_OFFLINE") == "1":
    def _network_audit_hook(event: str, args: tuple) -> None:
        if event in {
            "socket.__new__",
            "socket.bind",
            "socket.connect",
            "socket.connect_ex",
            "socket.sendto",
            "socket.sendmsg",
            "socket.getaddrinfo",
            "socket.gethostbyname",
            "socket.gethostbyname_ex",
            "socket.gethostbyaddr",
            "socket.getnameinfo",
            "urllib.Request",
        }:
            raise RuntimeError(f"Network access disabled during tests: {event}")

    sys.addaudithook(_network_audit_hook)
