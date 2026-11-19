"""A port number of the harness is held on TCP and on UDP from the first look at it to its use, shown on real loopback sockets.

The picker used to prove that a number binds on UDP, keep the TCP server and CLOSE the UDP probe ("whoever needs the UDP socket binds it next":
the HTTP/3 server did, after it had read its certificate and started its loop). In that window the number was held on TCP only: another picker of
the harness could not take it (every picker binds TCP first), but any other bind of that UDP number could, the system's own pick of an ephemeral
UDP port for another process first among them, and the caller's bind was refused (EADDRINUSE / WinError 10048) in a test that did nothing wrong.
``bind_tcp_and_hold_udp`` returns the UDP socket too; the caller owns it. No browser.
"""
from __future__ import annotations

import socket
import socketserver

import pytest

import _ports

pytestmark = pytest.mark.unit

ADDRESS = "127.0.0.1"


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        pass


def _tcp(address, number):
    return socketserver.TCPServer((address, number), _Handler)


def _udp_bind(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((ADDRESS, port))
    except OSError:
        sock.close()
        raise
    return sock


def test_the_hold_variant_keeps_the_number_on_udp_and_tcp_until_the_caller_uses_it():
    tcp, port, udp = _ports.bind_tcp_and_hold_udp(ADDRESS, _tcp)
    try:
        assert udp.getsockname() == (ADDRESS, port) and tcp.server_address[1] == port
        with pytest.raises(OSError):
            _udp_bind(port)                     # nobody else can take the UDP number...
        with pytest.raises(OSError):
            _tcp(ADDRESS, port)                 # ...nor the TCP one
        udp.sendto(b"x", (ADDRESS, port))       # and the socket the caller got is a working one, bound to that number
        udp.settimeout(30)
        assert udp.recvfrom(10)[0] == b"x"
    finally:
        udp.close()
        tcp.server_close()


class _Probe:
    closed = False

    def bind(self, pair):
        raise OSError(10013, "forbidden by its access permissions")

    def close(self):
        self.closed = True


class _Refusing:
    AF_INET, SOCK_DGRAM = 2, 2

    def __init__(self):
        self.opened = []

    def socket(self, family, kind):
        self.opened.append(_Probe())
        return self.opened[-1]


def test_the_hold_variant_closes_what_it_refused_and_says_why_when_it_finds_none(monkeypatch):
    layer = _Refusing()
    monkeypatch.setattr(_ports, "who_holds", lambda port: "; held by: nothing listed")
    with pytest.raises(RuntimeError, match="no port free for both TCP and UDP"):
        _ports.bind_tcp_and_hold_udp(ADDRESS, _tcp, tries=3, socket_module=layer)
    assert len(layer.opened) == 3 and all(probe.closed for probe in layer.opened), "a probe that was refused is closed, not kept"
