"""A network tripwire for the test run (P1.8, NFR-8; ADR 0024). `tests/conftest.py` installs it
when CE_OFFLINE=1, as CI does, so a test that reaches the network fails instead of passing on
a machine that happens to be online. Name lookups, connects and UDP sends to anything but
loopback raise NetworkBlocked, including asyncio's connects (which skip the lookup for a
literal address and, on Windows, bypass socket.connect). It is a tripwire, not a sandbox: a
child process is not covered."""

from __future__ import annotations

import asyncio.proactor_events
import asyncio.selector_events
import ipaddress
import socket
from collections.abc import Callable


class NetworkBlocked(RuntimeError):
    pass


def allowed(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)  # ::ffff:127.0.0.1
    return ip.is_loopback or (mapped is not None and mapped.is_loopback)


def _host(address: object) -> str | None:
    """The host of an AF_INET/AF_INET6 address tuple; None for AF_UNIX paths."""
    if isinstance(address, tuple) and address:
        return str(address[0])
    return None


def _check_address(address: object) -> None:
    host = _host(address)
    if host is not None and not allowed(host):
        raise NetworkBlocked(f"tests run offline (CE_OFFLINE=1): connect to {host}")


def _check_name(name: object) -> None:
    name = name.decode() if isinstance(name, bytes) else name
    if name is not None and not allowed(str(name)):
        raise NetworkBlocked(f"tests run offline (CE_OFFLINE=1): lookup of {name}")


def install() -> Callable[[], None]:
    """Patch the socket module and asyncio's connects; returns the function that undoes it."""
    patches: list[tuple[object, str, object]] = []

    def patch(owner: object, name: str, make: Callable[[object], object]) -> None:
        original = getattr(owner, name)
        patches.append((owner, name, original))
        setattr(owner, name, make(original))

    def lookup(original):
        def guarded(host, *args, **kwargs):
            _check_name(host)
            return original(host, *args, **kwargs)

        return guarded

    def connect(original):
        def guarded(self, address):
            _check_address(address)
            return original(self, address)

        return guarded

    def sendto(original):
        def guarded(self, data, *args):
            _check_address(args[-1] if args else None)
            return original(self, data, *args)

        return guarded

    def loop_connect(original):
        async def guarded(self, sock, address):
            _check_address(address)
            return await original(self, sock, address)

        return guarded

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        patch(socket, name, lookup)
    patch(socket.socket, "connect", connect)
    patch(socket.socket, "connect_ex", connect)
    patch(socket.socket, "sendto", sendto)
    patch(asyncio.proactor_events.BaseProactorEventLoop, "sock_connect", loop_connect)
    patch(asyncio.selector_events.BaseSelectorEventLoop, "sock_connect", loop_connect)

    def undo() -> None:
        for owner, name, original in reversed(patches):
            setattr(owner, name, original)

    return undo
