"""The lab's HTTP/3 server finishes the QUIC handshake of a client before it finishes the TLS of a TCP connection of the same client (no browser, loopback).

A real network gives QUIC the lead: QUIC is ready after 1 round trip and TCP+TLS after 2, and a browser that holds an Alt-Svc mapping opens both and takes the FIRST that is
ready. The lab server in Python completed the QUIC handshake in 49-73 ms against 6-22 ms for TCP+TLS, so under load the TCP connection won and the visits that had to read h3
read h2 (``test_a_page_with_no_proxy_reads_h2_then_h3_then_h3``, 2 red copies in 200; the engine's own log said ``HandleConnectionResult id=1 isUDP=0`` and closed the QUIC
session). The server restores the order of a real network by an event, never by a timer: for a peer address that has a QUIC handshake in progress toward it, the TLS of a new
TCP connection from that address waits until that handshake has ended (completed, failed, or the connection terminated).

The known-bad input is the interleaving that lost: a QUIC handshake of a peer is in progress (the test holds the packets that finish it) and a TCP connection of the same peer arrives.
The test judges the ORDER the client sees, not a duration: the QUIC connection is ready for the client before the TCP+TLS connection is.
"""
from __future__ import annotations

import socket
import ssl
import threading
import time

import pytest

from _h3_server import H3Server
from _waits import wait_until

pytestmark = pytest.mark.unit

#: The bound of every wait of this file (a wait for something that must happen: it ends the test when the server hangs, it judges no speed).
DEADLINE = 30


class _QuicClient:
    """A QUIC client by hand, over a UDP socket the test owns, so that the test decides when each packet of the server is read: the handshake finishes when the test lets it."""

    def __init__(self, server, source="127.0.0.1"):
        from aioquic.h3.connection import H3_ALPN
        from aioquic.quic.configuration import QuicConfiguration
        from aioquic.quic.connection import QuicConnection

        self.server_addr = (server.address, server.port)
        self.quic = QuicConnection(configuration=QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN, verify_mode=ssl.CERT_NONE))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((source, 0))
        self.sock.settimeout(DEADLINE)
        #: The packets of the server that arrived and that the client has not read yet.
        self.unread = []
        self.ready = False

    def send_first_flight(self):
        self.quic.connect(self.server_addr, now=time.monotonic())
        self.flush()

    def flush(self):
        for data, addr in self.quic.datagrams_to_send(now=time.monotonic()):
            self.sock.sendto(data, addr)

    def server_has_answered(self):
        """Blocks (with the deadline) until a packet of the server arrived: the server has seen the first flight, so a handshake of this peer is in progress there."""
        self.unread.append(self.sock.recv(65536))

    def read_until_ready(self, order):
        """The client reads what the server sent, and answers, until its QUIC handshake is complete: the moment a browser takes this connection. It writes ``quic_ready`` in ``order`` then,
        before its last packet (the one that lets the server finish) leaves."""
        from aioquic.quic.events import HandshakeCompleted

        while not self.ready:
            data = self.unread.pop(0) if self.unread else self.sock.recv(65536)
            self.quic.receive_datagram(data, self.server_addr, now=time.monotonic())
            while True:
                event = self.quic.next_event()
                if event is None:
                    break
                if isinstance(event, HandshakeCompleted):
                    self.ready = True
                    order.append("quic_ready")
            self.flush()

    def close(self):
        self.sock.close()


def _tcp_tls(server, order, done, source="127.0.0.1"):
    """A TCP+TLS client on its own thread: when its handshake is complete it writes ``tcp_tls`` in ``order`` and sets ``done``."""
    def run():
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.set_alpn_protocols(["h2", "http/1.1"])
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.bind((source, 0))
        raw.settimeout(DEADLINE)
        try:
            raw.connect((server.address, server.port))
            tls = context.wrap_socket(raw, server_hostname=server.address)
            order.append("tcp_tls")
            tls.close()
        except OSError as error:
            order.append("tcp_tls failed: %r" % (error,))
        finally:
            raw.close()
            done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _held_or_done(server, done):
    """The TCP connection is either held by the server or finished: a fact of the server's own record, never a pause."""
    held = lambda: getattr(server, "tcp_held", [])  # noqa: E731 - a server that holds nothing has no record
    assert wait_until(lambda: held() or done.is_set(), DEADLINE, lambda: done.wait(0.01)), "the TCP connection was neither held nor served"


def test_the_quic_connection_is_ready_before_the_tcp_one_when_the_quic_handshake_is_in_progress():
    """Known-bad: the old server finished the TLS of the TCP connection while the QUIC handshake of the same client was still in progress, so the TCP connection was ready first."""
    with H3Server("127.0.0.1") as server:
        client = _QuicClient(server)
        order, done = [], threading.Event()
        try:
            client.send_first_flight()
            client.server_has_answered()
            _tcp_tls(server, order, done)
            _held_or_done(server, done)
            client.read_until_ready(order)
            assert done.wait(DEADLINE), "the TCP connection was never served after the QUIC handshake"
        finally:
            client.close()
    assert order == ["quic_ready", "tcp_tls"], (
        "the TCP+TLS connection was ready before the QUIC connection of the same client: %s (a browser takes the first that is ready and reads h2)" % order)


def test_a_tcp_connection_with_no_quic_handshake_in_progress_is_not_held():
    with H3Server("127.0.0.1") as server:
        order, done = [], threading.Event()
        _tcp_tls(server, order, done)
        assert done.wait(DEADLINE), "the TCP connection was held with no QUIC handshake in progress"
        assert order == ["tcp_tls"], order
        assert getattr(server, "tcp_held", []) == [], "the server held a TCP connection with no QUIC handshake in progress"


def test_a_quic_handshake_that_ends_without_completing_lets_the_tcp_connection_go_on():
    """The QUIC side ends by the peer closing it before the handshake is done: nothing is waited for after that."""
    with H3Server("127.0.0.1") as server:
        client = _QuicClient(server)
        order, done = [], threading.Event()
        try:
            client.send_first_flight()
            client.server_has_answered()
            _tcp_tls(server, order, done)
            _held_or_done(server, done)
            client.quic.close()
            client.flush()
            assert done.wait(DEADLINE), "the TCP connection stayed held after the QUIC handshake was closed by the peer"
        finally:
            client.close()
    assert order == ["tcp_tls"], order


def test_a_quic_connection_the_server_terminates_lets_the_tcp_connection_go_on():
    """The event aioquic gives the server at the end of a connection that never completed (the idle timeout is one of them) releases the TCP connection too."""
    from aioquic.quic.events import ConnectionTerminated

    with H3Server("127.0.0.1") as server:
        client = _QuicClient(server)
        order, done = [], threading.Event()
        try:
            client.send_first_flight()
            client.server_has_answered()
            _tcp_tls(server, order, done)
            _held_or_done(server, done)

            def terminate():
                for protocol in set(server._quic._protocols.values()):
                    protocol.quic_event_received(ConnectionTerminated(error_code=0, frame_type=None, reason_phrase="idle timeout"))

            server._loop.call_soon_threadsafe(terminate)
            assert done.wait(DEADLINE), "the TCP connection stayed held after the server's QUIC connection terminated"
        finally:
            client.close()
    assert order == ["tcp_tls"], order


def test_a_quic_handshake_of_another_address_holds_nothing():
    """The peer addresses of TCP and QUIC differ behind some proxies: then nothing is held, and nothing can hang."""
    with H3Server("127.0.0.1") as server:
        client = _QuicClient(server)
        order, done = [], threading.Event()
        try:
            client.send_first_flight()
            client.server_has_answered()
            _tcp_tls(server, order, done, source="127.0.0.2")
            assert done.wait(DEADLINE), "a TCP connection of another address was held by a QUIC handshake of this one"
            assert getattr(server, "tcp_held", []) == []
        finally:
            client.close()
    assert order == ["tcp_tls"], order


def test_a_client_that_starts_a_quic_handshake_and_vanishes_holds_the_tcp_connection_until_the_quic_idle_timeout():
    """The residual case of the order (``_QuicFirst``): the client sends its first QUIC flight and then nothing (no CONNECTION_CLOSE). A TCP+TLS connection from the same IP is
    held, and it goes on only when the server's QUIC idle timeout ends that QUIC connection. The order is asserted, not a duration: the server's QUIC idle timeout is set to about 1 s
    only to keep the test short (the default is 60 s)."""
    with H3Server("127.0.0.1", quic_idle_timeout=1.0) as server:
        order = []
        ended = server._quic_first.ended

        def ended_after_noting(peer):
            # Written before the TCP connection is released, so the order of the two lines is the order of the facts.
            order.append("quic_terminated")
            ended(peer)

        server._quic_first.ended = ended_after_noting
        client = _QuicClient(server)
        done = threading.Event()
        try:
            client.send_first_flight()
            client.server_has_answered()
            _tcp_tls(server, order, done)
            assert wait_until(lambda: server.tcp_held, DEADLINE, lambda: done.wait(0.01)), "the TCP connection was not held by the QUIC handshake in progress"
            assert done.wait(DEADLINE), "the TCP connection was never served after the QUIC idle timeout"
        finally:
            client.close()
        closed = [line for line in server.record if line["kind"] == "quic_close"]
    assert order == ["quic_terminated", "tcp_tls"], (
        "the TCP+TLS connection went on before the QUIC connection of the vanished client was terminated, or the QUIC one was never terminated: %s" % order)
    assert closed and "idle" in str(closed[0]["reason"]).lower(), "the QUIC connection did not end by the idle timeout: %s" % closed
