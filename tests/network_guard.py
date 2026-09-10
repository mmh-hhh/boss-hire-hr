from __future__ import annotations

import socket
from typing import Any


BLOCKED_BOSS_SUFFIXES = ("zhipin.com",)
_INSTALLED = False
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_CREATE_CONNECTION = socket.create_connection


class BossNetworkBlocked(ConnectionError):
    pass


def is_boss_host(host: Any) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    normalized = str(host or "").strip().rstrip(".").lower()
    return any(normalized == suffix or normalized.endswith(f".{suffix}") for suffix in BLOCKED_BOSS_SUFFIXES)


def install_boss_network_guard() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if is_boss_host(host):
            raise BossNetworkBlocked(f"test process blocked BOSS DNS resolution: {host}")
        return _ORIGINAL_GETADDRINFO(host, *args, **kwargs)

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        host = address[0] if isinstance(address, tuple) and address else address
        if is_boss_host(host):
            raise BossNetworkBlocked(f"test process blocked BOSS connection: {host}")
        return _ORIGINAL_CREATE_CONNECTION(address, *args, **kwargs)

    socket.getaddrinfo = guarded_getaddrinfo
    socket.create_connection = guarded_create_connection
    _INSTALLED = True
