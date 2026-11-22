"""The record a failed far-away gather leaves (``_Watch`` of tests/test_webrtc_realness.py), on a real gateway and plain sockets (no browser).

The watch wraps the gateway of a test and its datagram sockets to say, when the test fails, where the page's STUN request went. It is
bookkeeping: it must NEVER change what the gateway does. A recording that raised once (a keyword that two records both passed) ended the
gateway's own thread and no datagram was ever relayed, so the test failed for the watch and not for the product.

* one SOCKS5 UDP ASSOCIATE and one datagram through the relay, with and without credentials: the datagram is relayed and answered, and the
  records exist (the control connection, the ASSOCIATE with the BND address, the datagram with its source address, the route);
* the same with every recording made to raise (the journal, the clock, the source classifier): the datagram is still relayed and the
  failures are said as records, never raised;
* the peer of an IPv6 datagram is a 4-tuple and is recorded.
"""
from __future__ import annotations

import socket
import struct
import time

import pytest

import _socks_servers as servers
import test_webrtc_realness as realness
from _socks_servers import STUN_NUMBER, STUN_PORT, coherent

pytestmark = pytest.mark.unit

_STUN_REQUEST = b"\x00\x01\x00\x00" + b"\x21\x12\xa4\x42" + bytes(range(12))


class _Evidence:
    """What ``FailureEvidence`` gives the watch: the cells, the records it keeps by name, and ``arm``."""

    def __init__(self):
        self.cells = None
        self.records = {}

    def note_cells(self, cells):
        self.cells = cells

    def note_records(self, name, state):
        self.records[name] = state

    def arm(self, moz_log=""):
        pass


def _read(c, n):
    data = b""
    while len(data) < n:
        chunk = c.recv(n - len(data))
        assert chunk, "the gateway closed the control connection"
        data += chunk
    return data


def _associate_and_relay(gateway, credentials):
    """One control connection that asks for a UDP ASSOCIATE, and one datagram to the fake STUN server through the relay. Returns the answer
    the client gets back (the SOCKS UDP header and the STUN response) and the BND address it was told."""
    control = socket.create_connection(("127.0.0.1", gateway.port), timeout=30)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if credentials is None:
            control.sendall(b"\x05\x01\x00")
            assert _read(control, 2) == b"\x05\x00"
        else:
            user, password = (part.encode() for part in credentials)
            control.sendall(b"\x05\x01\x02")
            assert _read(control, 2) == b"\x05\x02"
            control.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
            assert _read(control, 2) == b"\x01\x00"
        control.sendall(b"\x05\x03\x00\x01" + bytes(4) + bytes(2))
        reply = _read(control, 10)
        assert reply[:2] == b"\x05\x00", reply
        bnd = (socket.inet_ntoa(reply[4:8]), struct.unpack("!H", reply[8:10])[0])
        udp.bind(("127.0.0.1", 0))
        udp.settimeout(30)
        udp.sendto(b"\x00\x00\x00\x01" + socket.inet_aton(STUN_NUMBER) + struct.pack("!H", STUN_PORT) + _STUN_REQUEST, bnd)
        answer, _ = udp.recvfrom(2048)
        return answer, bnd
    finally:
        udp.close()
        control.close()


def _watched(monkeypatch, credentials):
    user, password = credentials or (None, None)
    gateway = coherent(user=user, password=password)
    evidence = _Evidence()
    evidence.note_cells({})
    watch = realness._Watch(gateway, monkeypatch, evidence, "127.0.0.1")
    return gateway, watch


def _events(journal):
    with journal.lock:
        return list(journal.records)


def _wait_for(journal, event):
    """The control connection ends in the gateway's own thread, a moment after the client closed it: wait for its record (a deadline, not a pause)."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if any(r.get("event") == event for r in _events(journal)):
            return
        time.sleep(0.01)


def _the_datagram_was_relayed(gateway, answer):
    assert answer[:4] == b"\x00\x00\x00\x01", answer
    assert answer[10:12] == b"\x01\x01", "no STUN success response came back through the relay: %r" % (answer,)
    assert [(host, port) for host, port, _ in gateway.datagrams] == [(STUN_NUMBER, STUN_PORT)]
    assert gateway.stun.requests == 1


@pytest.mark.parametrize("credentials", [None, ("alice", "s3cret")], ids=["no_auth", "user_and_password"])
def test_a_watched_gateway_relays_the_datagram_and_the_records_exist(monkeypatch, credentials):
    gateway, watch = _watched(monkeypatch, credentials)
    try:
        answer, bnd = _associate_and_relay(gateway, credentials)
        _the_datagram_was_relayed(gateway, answer)
    finally:
        gateway.close()
    _wait_for(watch.control, "control connection closed")
    control, udp = _events(watch.control), _events(watch.udp)
    by_event = {}
    for record in control:
        by_event.setdefault(record["event"], []).append(record)
    assert not [r for r in control + udp if r.get("event") == "a record could not be made"], "a recording failed: %r" % (control + udp,)
    greeting = by_event["greeting and authentication done"][0]
    assert greeting["ok"] is True and greeting["gateway_wants_credentials"] is (credentials is not None)
    if credentials is not None:
        assert greeting["user_sent"] == "alice" and greeting["password_length_sent"] == len("s3cret")
    assert by_event["request"][0]["command"] == "UDP ASSOCIATE"
    assert by_event["UDP ASSOCIATE handled"][0]["address_declared_by_the_client"] == "0.0.0.0:0"
    replies = [r for r in by_event["answer sent"] if r.get("bnd")]
    assert replies and replies[0]["bnd"] == "%s:%d" % bnd and replies[0]["rep"] == 0
    assert "control connection accepted" in by_event and "control connection closed" in by_event
    received = [r for r in udp if r["event"] == "datagram received" and r["on"].startswith("relay")]
    assert received and received[0]["source"].startswith("127.0.0.1:") and received[0]["origin"] == "socket"
    assert received[0]["skipped_by_the_gateway"] is False and received[0]["source_refused_as_foreign"] is False
    assert [r for r in udp if r["event"] == "datagram routed" and r["destination_in_the_header"] == "%s:%d" % (STUN_NUMBER, STUN_PORT)]
    assert [r for r in udp if r["event"] == "datagram sent" and r["on"].startswith("exit")]
    assert [r for r in udp if r["event"] == "datagram received" and r["on"].startswith("exit")]
    assert watch.bnd_ports == {bnd[1]}


def _journal_raises(monkeypatch, watch):
    def add(self, **fields):
        raise RuntimeError("the journal is broken")

    monkeypatch.setattr(realness._Journal, "add", add)


def _clock_raises(monkeypatch, watch):
    def broken():
        raise RuntimeError("the clock is broken")

    monkeypatch.setattr(watch, "_gw", broken)


def _classifier_raises(monkeypatch, watch):
    def broken(ip):
        raise RuntimeError("the classifier is broken")

    monkeypatch.setattr(watch, "_kind_of", broken)


def _connection_index_raises(monkeypatch, watch):
    def broken():
        raise RuntimeError("the connection index is broken")

    monkeypatch.setattr(watch, "_conn", broken)


@pytest.mark.parametrize("sabotage", [_journal_raises, _clock_raises, _classifier_raises, _connection_index_raises],
                         ids=["the_journal", "the_clock", "the_source_classifier", "the_connection_index"])
def test_a_recording_that_raises_never_stops_the_gateway(monkeypatch, sabotage):
    gateway, watch = _watched(monkeypatch, ("alice", "s3cret"))
    sabotage(monkeypatch, watch)
    try:
        answer, _bnd = _associate_and_relay(gateway, ("alice", "s3cret"))
        _the_datagram_was_relayed(gateway, answer)
        assert gateway.associates == 1
    finally:
        gateway.close()
    if sabotage is not _journal_raises:     # a journal that raises keeps nothing, and says nothing either
        said = [r for r in _events(watch.control) if r.get("event") == "a record could not be made"]
        assert said and all(r["origin"] == "watch" and r["error"] for r in said), _events(watch.control)


def test_the_peer_of_an_ipv6_datagram_is_a_four_tuple_and_is_recorded(monkeypatch):
    gateway, watch = _watched(monkeypatch, None)
    try:
        listening = servers.socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            listening._note_received(b"\x00" * 12, ("::1", 5000, 0, 0))
            listening._note_sent(b"\x00" * 12, (("2001:db8::1", 19302, 0, 0),))
        finally:
            listening.close()
    finally:
        gateway.close()
    udp = _events(watch.udp)
    assert [r["source"] for r in udp if r["event"] == "datagram received"] == ["::1:5000"]
    assert [r["destination"] for r in udp if r["event"] == "datagram sent"] == ["2001:db8::1:19302"]
