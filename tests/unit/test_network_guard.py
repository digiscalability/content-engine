"""tests/network_guard.py (P1.8, NFR-8): with CE_OFFLINE=1 the test run cannot reach the
network: lookups, connects (asyncio's too) and UDP sends to anything but loopback fail.
Loopback stays open (asyncio's self-pipe and local test servers use it)."""

import asyncio
import socket

import pytest
from network_guard import NetworkBlocked, allowed, install

LOOPBACK = ["127.0.0.1", "::1", "localhost", "127.5.6.7", "::ffff:127.0.0.1"]


@pytest.mark.parametrize("host", LOOPBACK)
def test_loopback_is_allowed(host):
    assert allowed(host)


@pytest.mark.parametrize("host", ["example.com", "192.0.2.1", "2001:db8::1", ""])
def test_anything_else_is_not(host):
    assert not allowed(host)


@pytest.fixture
def guarded():
    original = socket.getaddrinfo
    undo = install()
    yield
    undo()
    assert socket.getaddrinfo is original


def test_a_name_lookup_is_blocked(guarded):
    with pytest.raises(NetworkBlocked, match="example.invalid"):
        socket.getaddrinfo("example.invalid", 443)


def test_a_connect_to_a_literal_address_is_blocked(guarded):
    with socket.socket() as sock, pytest.raises(NetworkBlocked, match="connect to 192.0.2.1"):
        sock.connect(("192.0.2.1", 9))
    with socket.socket() as sock, pytest.raises(NetworkBlocked, match="connect to 192.0.2.1"):
        sock.connect_ex(("192.0.2.1", 9))


def test_an_asyncio_connect_to_a_literal_address_is_blocked(guarded):
    """asyncio skips the lookup for a literal address, and on Windows its Proactor loop
    connects without socket.connect."""

    async def reach():
        await asyncio.wait_for(asyncio.open_connection("192.0.2.1", 9), timeout=2)

    with pytest.raises(NetworkBlocked, match="192.0.2.1"):
        asyncio.run(reach())


def test_the_other_lookups_and_udp_are_blocked(guarded):
    for lookup in (socket.gethostbyname, socket.gethostbyname_ex):
        with pytest.raises(NetworkBlocked, match="example.invalid"):
            lookup("example.invalid")
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with udp, pytest.raises(NetworkBlocked, match="192.0.2.1"):
        udp.sendto(b"x", ("192.0.2.1", 9))


def test_loopback_still_connects(guarded):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        with socket.create_connection(server.getsockname(), timeout=2):
            pass


def test_loopback_still_resolves(guarded):
    assert socket.getaddrinfo("localhost", 0)


def test_undo_restores_the_socket_module():
    connect = socket.socket.connect
    install()()
    assert socket.socket.connect is connect
