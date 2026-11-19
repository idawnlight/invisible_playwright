"""The UDP bench (``_socks_servers``) does not die of what a loaded Windows machine does to its sockets, and the verdict of a
failed UDP test says what the bench saw.

Three things made the red of ``test_a_gateway_that_is_far_away_still_gives_the_page_a_srflx_born_through_its_relay`` unreadable:

* the fake STUN server read with a socket that still reported ICMP "port unreachable" (``SIO_UDP_CONNRESET``): the next ``recvfrom``
  raised ``ConnectionResetError`` and the ``except OSError: return`` ended the thread that answers, in silence, for every client after;
* the relay's ``inbound`` thread asked the exit socket for its port at every datagram, and at the teardown that socket was already
  closed (``OSError`` WinError 10038, an unhandled exception of a thread);
* the verdict of the test said nothing of the gateway's send errors, CONNECTs and associations, nor of what the core had declared
  for the proxy: a measurement of the core that ended past its budget looked like a page that never took the relay.

No browser, loopback only. The ICMP of Windows is simulated by a socket that raises the same error, so the tests fail on the old code
on any system.
"""
from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from _socks_servers import MEASURE_NUMBER, STUN_PORT, FakeStun, coherent
from _udp_line import bench_report, declared_udp_or_absent, udp_measure_sentence

pytestmark = pytest.mark.unit

_REAL_SOCKET = socket.socket

_BINDING = b"\x00\x01\x00\x00" + b"\x21\x12\xa4\x42" + b"0123456789ab"


def _flaky_socket(fail_recv: int = 0, fail_send: int = 0):
    """A socket class whose first ``fail_recv`` recvfrom raise ConnectionResetError (the ICMP of Windows) and whose first
    ``fail_send`` sendto raise ConnectionResetError, then behave as real ones."""
    state = {"recv": fail_recv, "send": fail_send}

    class Flaky(_REAL_SOCKET):
        def recvfrom(self, *args):
            if state["recv"] > 0:
                state["recv"] -= 1
                raise ConnectionResetError(10054, "an existing connection was forcibly closed")
            return super().recvfrom(*args)

        def sendto(self, *args):
            if state["send"] > 0:
                state["send"] -= 1
                raise ConnectionResetError(10054, "an existing connection was forcibly closed")
            return super().sendto(*args)

    return Flaky


def _fake_stun(monkeypatch, **flaky) -> FakeStun:
    with monkeypatch.context() as patch:
        patch.setattr(socket, "socket", _flaky_socket(**flaky))
        return FakeStun("203.0.113.7")


def _ask(stun: FakeStun) -> bytes:
    client = _REAL_SOCKET(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        client.settimeout(2)
        client.sendto(_BINDING, stun.address)
        return client.recvfrom(2048)[0]
    finally:
        client.close()


def test_the_fake_stun_answers_after_a_recvfrom_that_raised_connection_reset(monkeypatch):
    stun = _fake_stun(monkeypatch, fail_recv=1)
    try:
        assert _ask(stun)[:2] == b"\x01\x01", "the server ended its thread at the reset and answered nobody after it"
        assert stun.requests == 1
    finally:
        stun.close()


def test_the_fake_stun_answers_the_next_client_after_a_sendto_that_failed(monkeypatch):
    stun = _fake_stun(monkeypatch, fail_send=1)
    try:
        client = _REAL_SOCKET(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(0.3)
        client.sendto(_BINDING, stun.address)       # its answer fails to be sent: nobody is told
        with pytest.raises(socket.timeout):
            client.recvfrom(2048)
        client.close()
        assert _ask(stun)[:2] == b"\x01\x01", "a failed send ended the server for every client after it"
        assert stun.requests == 2
    finally:
        stun.close()


def test_a_closed_fake_stun_ends_its_thread(monkeypatch):
    stun = _fake_stun(monkeypatch)
    stun.close()
    stun._thread.join(timeout=2)
    assert not stun._thread.is_alive()


def _associate(gateway):
    """The SOCKS5 handshake and a UDP ASSOCIATE; the control connection and the address of the relay."""
    control = _REAL_SOCKET(socket.AF_INET, socket.SOCK_STREAM)
    control.settimeout(3)
    control.connect(("127.0.0.1", gateway.port))
    control.sendall(b"\x05\x01\x00")
    assert control.recv(2) == b"\x05\x00"
    control.sendall(b"\x05\x03\x00\x01" + b"\x00" * 4 + b"\x00\x00")
    reply = b""
    while len(reply) < 10:
        reply += control.recv(10 - len(reply))
    assert reply[1] == 0, reply
    return control, (socket.inet_ntoa(reply[4:8]), struct.unpack("!H", reply[8:10])[0])


def _wait(condition, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_the_inbound_thread_of_an_association_ends_clean_when_its_exit_socket_is_closed(monkeypatch):
    created = []

    class Recording(_REAL_SOCKET):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    raised = []
    monkeypatch.setattr(threading, "excepthook", lambda args: raised.append(args))
    datagram = b"\x00\x00\x00\x01" + socket.inet_aton("198.51.100.77") + struct.pack("!H", 9) + b"payload"
    with coherent() as gateway:
        gateway.forward_only_to = set()                 # nothing leaves the machine
        monkeypatch.setattr(socket, "socket", Recording)
        control, relay = _associate(gateway)
        monkeypatch.setattr(socket, "socket", _REAL_SOCKET)
        client = _REAL_SOCKET(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            client.sendto(datagram, relay)
            assert _wait(lambda: len(gateway.datagram_log) == 1), "the relay never carried the first datagram"
            out = [s for s in created if s.type == socket.SOCK_DGRAM and s.fileno() != -1
                   and s.getsockname()[1] == gateway.exit_ports[0]]
            assert len(out) == 1
            out[0].close()                              # the teardown closes the exit socket before the thread has seen it
            client.sendto(datagram, relay)
            assert _wait(lambda: len(gateway.datagram_log) == 2), "the relay stopped at the closed exit socket"
            time.sleep(0.3)                             # the time a thread needs to raise what it raises
        finally:
            client.close()
            control.close()
    assert not raised, "the inbound thread raised: %s" % [repr(args.exc_value) for args in raised]


class _Gateway:
    """What a report reads of a gateway, set by hand."""
    datagram_log = [(MEASURE_NUMBER, STUN_PORT, 1, 0.5), ("198.51.100.5", STUN_PORT, 1, 1.25)]
    send_errors = ["the datagram to 198.51.100.4:5: OSError [WinError 10051] unreachable"]
    connects = [("api.ipify.org", 80)]
    accepted = [0.1, 0.2, 0.3]
    associations = [[0.4, 1.0], [2.0, None]]


def test_the_report_of_a_bench_says_what_the_gateway_saw():
    text = bench_report(_Gateway, 900, "coherent")
    for piece in ("WinError 10051", "api.ipify.org", "TCP connections it accepted: 3", "[0.4, 1.0]", "[2.0, None]",
                  "%s:%d atyp=1 t=0.500s" % (MEASURE_NUMBER, STUN_PORT), "198.51.100.5:%d atyp=1 t=1.250s" % STUN_PORT,
                  "declared for this proxy: coherent"):
        assert piece in text, (piece, text)
    assert "did not finish" not in text


@pytest.mark.parametrize("declared", [None, "unmeasured", "no"])
def test_a_core_measure_that_is_not_coherent_is_said_outright_with_the_round_trip(declared):
    sentence = udp_measure_sentence(declared, 900)
    assert "did not finish" in sentence and "rtt 900 ms" in sentence, sentence
    assert "did not finish" in bench_report(_Gateway, 900, declared)
    if declared:
        assert declared in sentence


def test_what_the_core_declared_is_absent_not_an_error_when_it_measured_nothing():
    assert declared_udp_or_absent({"server": "socks5://127.0.0.1:1"}, ["198.51.100.6:19302"]) is None
