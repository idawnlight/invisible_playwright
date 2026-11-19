"""The e2e tests of the first audit fix judge the browser's own time and what happened, not the speed of the runner.

Each test below (no browser, no sleep that decides anything) runs the judgement of an e2e on the case it must still call
red and the case it must call green:

* the first STUN datagram of a PeerConnection is judged by WHICH association carried it, and the gateway says which;
* a handshake the test holds (`Gateway.gate` for all of them, `Gateway.hold` for the ones it chooses) answers nothing until the test
  lets it go;
* the road of a TURN server written as a name is judged by an ORDER (the Allocate was answered while the later handshakes were held)
  and by a COUNT (the exchanges the gateway, the DoH and the TURN server recorded), never by a duration;
* the page of the key-delay test and of the typing-pause test stamp each event with `event.timeStamp` (the browser's own
  creation time), not with `performance.now()` read when the handler happens to run;
* the pages of the `networkidle` cases of `test_a_page_that_moves_on_e2e.py` have no timer: they move from inside `load`
  or from the first bytes of a response the server holds open;
* the rate of a screencast is the rate of the frames that came, and a cap that is not honoured is above its ceiling;
* the screenshot of a frame of another site is taken again until it is painted, and says what it saw if it never is.
"""
from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from _e2e_verdicts import cold_association_problem
from _screencast_evidence import Stream
from _socks_servers import STUN_NUMBER, STUN_PORT, coherent
from _waits import wait_until

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# the first datagram goes out on an association that was ready before the birth
# ---------------------------------------------------------------------------

BORN = 10.0
#: [start, end] of each association granted: #0 is the core's probe, #1 and #2 are the pool of the engine, #3 is made later
ASSOCIATIONS = [[0.1, 0.3], [9.0, 9.4], [9.1, 9.5], [10.05, 10.5]]


def test_a_first_datagram_on_a_ready_association_is_green_at_any_speed():
    for at in (10.01, 10.4, 25.0, 300.0):      # the same identity whether the machine is quick or three minutes late
        assert cold_association_problem(ASSOCIATIONS, [(STUN_NUMBER, STUN_PORT, 1, at)], STUN_NUMBER, BORN) is None
        assert cold_association_problem(ASSOCIATIONS, [(STUN_NUMBER, STUN_PORT, 2, at)], STUN_NUMBER, BORN) is None


def test_a_first_datagram_on_an_association_made_after_the_birth_is_red_even_when_it_is_fast():
    problem = cold_association_problem(ASSOCIATIONS, [(STUN_NUMBER, STUN_PORT, 3, 10.06)], STUN_NUMBER, BORN)
    assert problem and "association #3" in problem and "a cold association" in problem


def test_an_engine_that_asked_for_nothing_or_whose_pool_was_late_or_that_sent_nothing_is_red():
    assert "never asked" in cold_association_problem([[0.1, 0.3]], [], STUN_NUMBER, BORN)
    assert "no association was completed before" in cold_association_problem(
        [[0.1, 0.3], [10.2, 10.6]], [(STUN_NUMBER, STUN_PORT, 1, 10.7)], STUN_NUMBER, BORN)
    assert "no STUN datagram" in cold_association_problem(ASSOCIATIONS, [("203.0.113.9", 9, 1, 10.1)], STUN_NUMBER, BORN)


def test_the_first_datagram_is_the_earliest_of_the_log_whatever_the_order_it_was_written_in():
    log = [(STUN_NUMBER, STUN_PORT, 1, 10.9), (STUN_NUMBER, STUN_PORT, 3, 10.06)]
    assert "association #3" in cold_association_problem(ASSOCIATIONS, log, STUN_NUMBER, BORN)


def _hello(port):
    t = socket.create_connection(("127.0.0.1", port), timeout=10)
    t.sendall(b"\x05\x01\x00")
    return t


def test_the_gateway_says_which_association_carried_each_datagram():
    with coherent() as gateway:
        clients = []
        try:
            for _ in range(2):
                t = _hello(gateway.port)
                assert t.recv(2) == b"\x05\x00"
                t.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
                reply = t.recv(10)
                assert reply[1] == 0
                clients.append((t, (socket.inet_ntoa(reply[4:8]), struct.unpack("!H", reply[8:10])[0])))
            for _t, relay in clients:
                udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                udp.sendto(b"\x00\x00\x00\x01" + socket.inet_aton(STUN_NUMBER) + struct.pack("!H", STUN_PORT) + b"x", relay)
                udp.close()
            assert wait_until(lambda: len(gateway.datagram_via) == 2, 10, lambda: time.sleep(0.01))
        finally:
            for t, _relay in clients:
                t.close()
        assert sorted((index, host) for host, _port, index, _at in gateway.datagram_via) == [(0, STUN_NUMBER), (1, STUN_NUMBER)]


# ---------------------------------------------------------------------------
# a handshake the test holds
# ---------------------------------------------------------------------------

def test_a_gateway_with_its_gate_closed_answers_nothing_until_the_gate_opens():
    with coherent() as gateway:
        gateway.gate = threading.Event()
        t = _hello(gateway.port)
        t.settimeout(0.4)
        with pytest.raises(socket.timeout):      # a window of observation: it can only hide a failure, never make one
            t.recv(2)
        gateway.gate.set()
        t.settimeout(10)
        assert t.recv(2) == b"\x05\x00"
        t.close()


def test_a_gateway_with_no_gate_holds_nothing():
    with coherent() as gateway:
        assert gateway.gate is None
        t = _hello(gateway.port)
        t.settimeout(10)
        assert t.recv(2) == b"\x05\x00"
        t.close()


def _associate(port):
    t = _hello(port)
    assert t.recv(2) == b"\x05\x00"
    t.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
    return t


def test_a_gateway_holds_one_handshake_by_its_connection_and_what_it_asked_for_and_the_others_run():
    from _socks_servers import WEB_ADDRESS
    with coherent() as gateway:
        held = threading.Event()
        gateway.hold = lambda index, kind: held if kind == "associate" and index == 0 else None
        first = _associate(gateway.port)                    # connection 0: its greeting is answered, its ASSOCIATE is held
        assert wait_until(lambda: any(r[2] == "associate" for r in gateway.requests), 10, lambda: time.sleep(0.01))
        first.settimeout(0.4)
        with pytest.raises(socket.timeout):                 # a window of observation: it can only hide a failure, never make one
            first.recv(10)
        # the others are not held, with the first one still held: a CONNECT, and another ASSOCIATE (what a road that does not wait for
        # a handshake does), each read with a long timeout that decides nothing
        second = _hello(gateway.port)
        second.settimeout(10)
        assert second.recv(2) == b"\x05\x00"
        second.sendall(b"\x05\x01\x00\x01" + socket.inet_aton(WEB_ADDRESS) + struct.pack("!H", 80))
        assert second.recv(10)[1] == 0
        third = _associate(gateway.port)
        third.settimeout(10)
        assert third.recv(10)[1] == 0
        assert gateway.associations[0][1] is None            # the held one is still not answered
        held.set()
        first.settimeout(10)
        assert first.recv(10)[1] == 0
        assert [r[1:3] for r in gateway.requests if r[2] != "greeting"] == [(0, "associate"), (1, "connect"), (2, "associate")]
        assert [r[2] for r in gateway.requests].count("greeting") == 3
        for t in (first, second, third):
            t.close()


def test_a_gateway_holds_the_greeting_of_one_connection_when_it_is_asked_to_and_not_the_others():
    with coherent() as gateway:
        held = threading.Event()
        gateway.hold = lambda index, kind: held if index == 1 and kind is None else None
        a = _hello(gateway.port)
        assert wait_until(lambda: len(gateway.accepted) == 1, 10, lambda: time.sleep(0.01))
        b = _hello(gateway.port)                            # connection 1: its greeting is held
        a.settimeout(10)
        assert a.recv(2) == b"\x05\x00"
        b.settimeout(0.4)
        with pytest.raises(socket.timeout):
            b.recv(2)
        held.set()
        b.settimeout(10)
        assert b.recv(2) == b"\x05\x00"
        a.close()
        b.close()


# ---------------------------------------------------------------------------
# the pages stamp events with the browser's time
# ---------------------------------------------------------------------------

def test_the_keys_of_the_delay_test_are_stamped_when_the_browser_made_them():
    import test_key_delay_reaches_the_keyboard as module
    assert b"e.timeStamp" in module.PAGE and b"performance.now" not in module.PAGE


def test_the_focus_and_the_first_key_of_the_typing_pause_test_are_stamped_when_the_browser_made_them():
    import test_a_field_is_typed_after_a_pause as module
    page = module.STORE_PAGE.decode()
    script = page[page.index("window.__events"):page.index("const answer")]
    assert "e.timeStamp" in script and "performance.now" not in script


# ---------------------------------------------------------------------------
# a page that moves with no timer
# ---------------------------------------------------------------------------

def test_the_networkidle_pages_of_a_page_that_moves_have_no_timer_in_them():
    import test_a_page_that_moves_on_e2e as module
    for name in ["/ir0", "/ir50", "/ix0", "/ix50"]:
        page = module.PAGES[name].decode()
        assert "setTimeout" not in page and "setInterval" not in page, name
        assert "location.replace" in page, name
    # 0: inside the load handler itself; 50: when the first bytes of a response the server holds open come in
    assert "/hold" not in module.PAGES["/ir0"].decode() and "/hold" in module.PAGES["/ir50"].decode()
    assert "127.0.0.1','localhost'" in module.PAGES["/ix0"].decode()
    # the pages of the read-at-once test keep their timers: they are the stimulus there, and each read accepts either page
    assert "setTimeout" in module.PAGES["/r50"].decode()


# ---------------------------------------------------------------------------
# the rate of the frames that came
# ---------------------------------------------------------------------------

def test_the_rate_of_a_screencast_is_that_of_its_frames_from_the_first_to_the_last():
    stream = Stream("x")
    assert stream.rate() is None
    stream.stamps = [5.0]
    assert stream.rate() is None
    stream.stamps = [100.0 + 0.5 * i for i in range(6)]            # a cap of 2 fps honoured: 5 gaps of 0.5 s
    assert stream.rate() == pytest.approx(2.0)
    stream.stamps = [100.0 + 0.04 * i for i in range(6)]           # a cap that is not honoured: 25 fps
    assert stream.rate() == pytest.approx(25.0) and stream.rate() > 2 * 2
    # a machine that took ten seconds to deliver the first frame does not change the rate of the ones that came
    stream.stamps = [110.0 + 0.5 * i for i in range(6)]
    assert stream.rate() == pytest.approx(2.0)


def test_the_rate_is_read_on_the_engines_stamps_not_on_when_a_stalled_client_heard_the_frames():
    """The known-bad input: a cap of 2 fps honoured by the engine, heard by a client that stalled for 2 s and then
    received four frames at once. On the arrival times that reads 5 fps, above the ceiling of 4 (red with no defect);
    on the engine's stamps it is 2."""
    stream = Stream("x")
    for i, heard in enumerate([102.0, 102.0, 102.0, 102.0, 102.5, 103.0]):
        stream.on_frame({"data": b"", "timestamp": 100.0 + 0.5 * i})
        stream.arrivals[-1] = heard
    assert stream.rate(stream.arrivals) == pytest.approx(5.0)         # what the old rate read
    assert stream.rate() == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# the screenshot taken again until the frame is painted
# ---------------------------------------------------------------------------

class _Page:
    def wait_for_timeout(self, ms):
        time.sleep(ms / 1000.0)


def _looks(colours):
    """A fake decode: the colour at every point, from the list of looks the page will give (the last one repeats)."""
    looks = iter(colours)
    last = [next(looks)]

    def decode(_data):
        last[0] = next(looks, last[0])
        shade = last[0]
        return (1, 1, lambda x, y: shade)

    return decode


def test_a_frame_that_is_painted_late_is_waited_for_and_one_never_painted_is_red_with_what_was_seen(monkeypatch):
    import test_fission_frames_tree_input_and_pixels_e2e as module
    pink = module.PINK
    monkeypatch.setattr(module, "decode_png", _looks([(255, 255, 255)] * 3 + [pink]))
    taken = []
    found = module.painted_shot(_Page(), lambda: taken.append(1) or b"png", [((5, 5), pink)], seconds=5.0)
    assert found[2](0, 0) == pink and len(taken) == 3   # two looks too early, the third painted
    monkeypatch.setattr(module, "decode_png", _looks([(255, 255, 255)]))
    with pytest.raises(AssertionError) as red:
        module.painted_shot(_Page(), lambda: b"png", [((5, 5), pink)], seconds=0.3)
    assert "never painted" in str(red.value) and "(255, 255, 255)" in str(red.value)


# ---------------------------------------------------------------------------
# the road of a TURN server written as a name: an order and a count, never a duration
# ---------------------------------------------------------------------------

from _e2e_verdicts import (held_handshake_problem, turn_chain_problem, turn_exchanges, turn_exchanges_most,  # noqa: E402
                           turn_exchanges_problem, turn_road_connections)

TURN_BORN = 10.0
HOST = "198.51.100.7"
#: #0 the core's probe, #1 and #2 the pool of the engine (granted before the birth), #3 begun after it (held by the test)
TURN_ASSOCIATIONS = [[0.1, 0.3], [9.0, 9.26], [9.1, 9.36], [10.1, None]]
#: what the gateway read: the pool's two ASSOCIATEs before the birth, then the CONNECT to the DoH that carries the question
REQUESTS = [(9.0, 1, "associate", "association #1"), (9.1, 2, "associate", "association #2"),
            (10.02, 3, "connect", "192.0.2.53:443"), (10.1, 4, "associate", "association #3")]
QUESTIONS = [("turn.relay-only.test", 1, 3)]       # asked on the control connection #3, the CONNECT to the DoH
#: the Allocate without credentials (answered 401), then the one with them: two transactions
ALLOCATES = [(10.5, b"\x01" * 12, 3, False), (10.8, b"\x02" * 12, 3, True)]
VIA = [(HOST, 3478, 1, 10.5), (HOST, 3478, 1, 10.8)]


def _scaled(k, requests, allocates, associations, via):
    """The same road on a machine k times slower: every stamp is k times later. An order and a count do not notice."""
    return ([[None if t is None else t * k for t in a] for a in associations],
            [(r[0] * k,) + tuple(r[1:]) for r in requests],
            [(a[0] * k,) + tuple(a[1:]) for a in allocates],
            [v[:3] + (v[3] * k,) for v in via])


def _road(k=1.0, allocated=10.8, released=11.5, held=(10.1,), requests=REQUESTS, questions=QUESTIONS, allocates=ALLOCATES,
          via=VIA, auth=False):
    associations, requests, allocates, via = _scaled(k, requests, allocates, TURN_ASSOCIATIONS, via)
    return turn_chain_problem(TURN_BORN * k, None if allocated is None else allocated * k, released * k, [h * k for h in held], requests,
                              questions, allocates, associations, via, HOST, auth)


def test_a_road_that_costs_what_the_protocols_cost_is_green_at_the_speed_of_any_machine():
    for k in (1.0, 0.01, 40.0, 5000.0):      # a quick one, and a machine so slow that every stamp is five thousand times later
        assert _road(k) is None, k
    assert turn_exchanges(TURN_BORN, 10.8, REQUESTS, QUESTIONS, ALLOCATES) == (turn_exchanges_most(), [])      # exactly the protocols' five
    # the connection that loaded the page still carries the question: no CONNECT after the birth, three exchanges
    kept = [r for r in REQUESTS if r[2] != "connect"]
    assert _road(requests=kept) is None and turn_exchanges(TURN_BORN, 10.8, kept, QUESTIONS, ALLOCATES)[0] == 3
    # nothing was held (no association was begun after the pool was ready): nothing to wait for
    assert _road(held=()) is None


def test_the_most_the_road_may_cost_is_five_exchanges_and_six_with_a_user_and_a_password():
    assert turn_exchanges_most() == 5 and turn_exchanges_most(auth=True) == 6      # 2 + 1 + 2, and the sub-negotiation of RFC 1929
    assert turn_exchanges(TURN_BORN, 10.8, REQUESTS, QUESTIONS, ALLOCATES, auth=True)[0] == 6
    assert _road(auth=True) is None


def test_handshakes_that_wait_for_each_other_are_red_even_when_the_machine_is_quick():
    # the Allocate was answered only after the test let the held handshakes go: it was waiting for one
    problem = _road(allocated=11.6, released=11.5, held=(10.1, 10.4, 10.7))
    assert problem and "after the 3 held association handshake(s)" in problem and "one handshake waits for another" in problem
    for k in (0.001, 1.0, 5000.0):
        assert _road(k, allocated=11.6, released=11.5), k
    # and one that never came at all
    assert "never answered an Allocate" in _road(allocated=None)


def test_the_held_verdict_is_an_order_of_two_stamps_not_a_distance_between_them():
    assert held_handshake_problem(11.499, 11.5, [10.1]) is None
    assert held_handshake_problem(11.5, 11.5, [10.1]) is None        # at the same instant: not after
    assert held_handshake_problem(11.501, 11.5, [10.1])
    assert held_handshake_problem(1e-6, 1e6, [10.1]) is None         # however late the let-go


def test_one_exchange_more_than_the_protocols_ask_for_is_red_and_at_the_most_is_not():
    # a second question for the name (an AAAA nobody needs)
    problem = _road(questions=QUESTIONS + [("turn.relay-only.test", 28, 3)])
    assert problem and "6 exchanges" in problem and "5 at most" in problem and "2 DoH questions" in problem
    # a second CONNECT on the road: the question asked again on a connection made anew after the first (control connection #5)
    again = REQUESTS + [(10.05, 5, "connect", "192.0.2.53:443")]
    problem = _road(requests=again, questions=QUESTIONS + [("turn.relay-only.test", 1, 5)])
    assert problem and "8 exchanges" in problem and "2 SOCKS5 control connections made after the birth carried" in problem
    # the same question asked again on the same connection is an exchange more, not a connection more
    problem = _road(questions=QUESTIONS * 2)
    assert problem and "6 exchanges" in problem and "2 DoH questions" in problem and "SOCKS5 control connections" not in problem
    # a third Allocate transaction (a transaction id that is not a retransmission)
    problem = _road(allocates=ALLOCATES + [(10.7, b"\x03" * 12, 3, True)])
    assert problem and "6 exchanges" in problem and "3 Allocate transactions" in problem
    # none of those is a matter of speed
    for k in (0.01, 5000.0):
        assert _road(k, questions=QUESTIONS * 2), k


def test_the_exchanges_are_the_ones_between_the_birth_and_the_allocation_and_an_association_is_none_of_them():
    # before the birth: the DoH connection of the page; after the allocation: the POST of the page
    around = [(9.5, 0, "connect", "192.0.2.53:443"), (11.9, 9, "connect", "127.0.0.1:8000")] + REQUESTS
    assert turn_exchanges_problem(TURN_BORN, 10.8, around, QUESTIONS, ALLOCATES) is None
    # and what the browser does on other connections inside that window (the DoH for names of its own, opened in parallel, and the
    # server of the page) is not the road: four of them, and the verdict is the same
    parallel = REQUESTS + [(10.03, 6, "connect", "192.0.2.53:443"), (10.04, 7, "connect", "192.0.2.53:443"),
                           (10.05, 8, "connect", "192.0.2.53:443"), (10.2, 9, "connect", "127.0.0.1:43167")]
    assert turn_exchanges_problem(TURN_BORN, 10.8, parallel, QUESTIONS, ALLOCATES) is None
    assert turn_exchanges(TURN_BORN, 10.8, parallel, QUESTIONS, ALLOCATES) == (turn_exchanges_most(), [])
    assert _road(requests=parallel) is None
    # when the road is red they are listed in the message as evidence, and are not what made it red
    problem = _road(requests=parallel, questions=QUESTIONS * 2)
    assert problem and "6 exchanges" in problem and "127.0.0.1:43167" in problem and "not counted" in problem
    # a later Allocate is not on the road, and another type of transaction (a Refresh) is not an Allocate
    late = ALLOCATES + [(12.0, b"\x04" * 12, 3, True), (10.7, b"\x05" * 12, 4, True)]
    assert turn_exchanges_problem(TURN_BORN, 10.8, REQUESTS, QUESTIONS, [a for a in late if a[2] == 3]) is None
    # five associations begun after the birth cost the road nothing it counts (the held test and the cold check judge them)
    pool = REQUESTS + [(10.2 + i / 10, 20 + i, "associate", "association #%d" % (4 + i)) for i in range(5)]
    assert turn_exchanges_problem(TURN_BORN, 10.8, pool, QUESTIONS, ALLOCATES) is None


def test_an_allocation_on_an_association_made_after_the_birth_is_red_too():
    problem = _road(via=[(HOST, 3478, 3, 10.5), (HOST, 3478, 3, 10.8)])
    assert problem and "a cold association" in problem and "association #3" in problem


def test_the_turn_server_counts_transactions_and_a_retransmission_is_not_another():
    import hashlib
    import _turn_chain as chain_mod
    server = chain_mod.TurnServer()
    try:
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.settimeout(5)
        first = struct.pack("!HHI", 0x0003, 8, chain_mod.MAGIC) + b"\x07" * 12 + struct.pack("!HH", 0x0019, 4) + b"\x11\x00\x00\x00"
        for _ in range(3):                                     # the request, and the retransmissions ICE makes of it
            udp.sendto(first, server.address)
            udp.recvfrom(65536)
        key = hashlib.md5(b"u:" + chain_mod.REALM.encode() + b":" + chain_mod.PASSWORD.encode(), usedforsecurity=False).digest()
        body = (chain_mod._attr(0x0019, b"\x11\x00\x00\x00") + chain_mod._attr(0x0006, b"u") +
                chain_mod._attr(0x0014, chain_mod.REALM.encode()) + chain_mod._attr(0x0015, chain_mod.NONCE.encode()))
        udp.sendto(chain_mod._message(0x0003, b"\x08" * 12, body, key), server.address)
        udp.recvfrom(65536)
        udp.close()
        assert len(server.seen) == 4
        assert [(t[1], t[2], t[3]) for t in server.transactions()] == [(b"\x07" * 12, 0x0003, False), (b"\x08" * 12, 0x0003, True)]
    finally:
        server.close()


def test_a_client_that_walks_the_road_by_the_protocols_spends_exactly_the_exchanges_the_verdict_allows():
    """The number is the protocols', not a slack: a plain client (no browser) that takes an association, resolves the name through the
    DoH and asks for the Allocate twice (the first request three times: ICE retransmits) is judged green and spends all of it."""
    import hashlib
    import _turn_chain as chain_mod
    from _doh_servers import QTYPE_A
    from test_the_udp_chain_helper import _resolve_through_doh, _socks_associate, _via
    with chain_mod.Chain(rtt_ms=0, relay_host="127.0.0.1") as chain:
        t, relay = _socks_associate(chain)                    # the pool: before the birth
        born = time.monotonic() - chain.t0
        _resolve_through_doh(chain, chain_mod.TURN_NAME)
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.settimeout(5)
        alloc = struct.pack("!HHI", 0x0003, 8, chain_mod.MAGIC) + b"\x01" * 12 + struct.pack("!HH", 0x0019, 4) + b"\x11\x00\x00\x00"
        for _ in range(3):
            _via(u, relay, chain_mod.TURN_IP, chain_mod.TURN_PORT, alloc)
        key = hashlib.md5(b"u:" + chain_mod.REALM.encode() + b":" + chain_mod.PASSWORD.encode(), usedforsecurity=False).digest()
        body = (chain_mod._attr(0x0019, b"\x11\x00\x00\x00") + chain_mod._attr(0x0006, b"u") +
                chain_mod._attr(0x0014, chain_mod.REALM.encode()) + chain_mod._attr(0x0015, chain_mod.NONCE.encode()))
        answer = _via(u, relay, chain_mod.TURN_IP, chain_mod.TURN_PORT, chain_mod._message(0x0003, b"\x02" * 12, body, key))
        assert struct.unpack("!H", answer[:2])[0] == 0x0103
        u.close()
        t.close()
        allocated_at = chain.turn.allocations[0][3] - chain.t0
        questions = [q for q in chain.doh.question_connections if q[0] == chain_mod.TURN_NAME]
        allocates = [(a[0] - chain.t0,) + tuple(a[1:]) for a in chain.turn.transactions()]
        assert [q[:2] for q in questions] == [(chain_mod.TURN_NAME, QTYPE_A)] and len(chain.turn.seen) == 4 and len(allocates) == 2
        requests = list(chain.requests)
        assert [r[2] for r in requests] == ["greeting", "associate", "greeting", "connect"]
        assert requests[3][3].startswith("192.0.2.53:")
        assert questions[0][2] == requests[3][1]            # the DoH names the connection that carried the question: the gateway's own number
        assert turn_exchanges(born, allocated_at, requests + [(allocated_at - 0.001, 77, "connect", "127.0.0.1:1")], questions,
                              allocates) == (turn_exchanges_most(), [])       # a stranger's CONNECT in the window is not on the road
        assert turn_exchanges(born, allocated_at, requests, questions, allocates) == (turn_exchanges_most(), [])
        assert turn_exchanges_problem(born, allocated_at, requests, questions, allocates) is None


def test_the_doh_says_which_control_connection_of_the_gateway_carried_each_question():
    """With a round trip (the tunnel goes through a socket pair) and with none (the socket itself): two connections to the DoH, each with its
    own question, and a third that asks nothing. The number the DoH gives is the one the gateway logged for that CONNECT."""
    import _turn_chain as chain_mod
    from _doh_servers import DOH_BOOTSTRAP
    from test_the_udp_chain_helper import _resolve_through_doh, _socks_connect
    for rtt in (0, 10):
        with chain_mod.Chain(rtt_ms=rtt, relay_host="127.0.0.1") as chain:
            _resolve_through_doh(chain, chain_mod.PAGE_NAME)
            stranger, code = _socks_connect(chain, "127.0.0.1", chain.page.port)           # a CONNECT that asks the DoH nothing
            assert code == 0
            _resolve_through_doh(chain, chain_mod.TURN_NAME)
            stranger.close()
            connects = {r[3]: r[1] for r in chain.requests if r[2] == "connect"}
            doh_connections = [r[1] for r in chain.requests if r[2] == "connect" and r[3].startswith(DOH_BOOTSTRAP)]
            assert [q[2] for q in chain.doh.question_connections] == doh_connections and len(set(doh_connections)) == 2, rtt
            assert connects["127.0.0.1:%d" % chain.page.port] not in doh_connections
            asked = [q for q in chain.doh.question_connections if q[0] == chain_mod.TURN_NAME]
            road, others = turn_road_connections(0.0, 1e9, chain.requests, asked)
            assert [r[1] for r in road] == [asked[0][2]] and len(others) == 2, rtt


def test_the_gateway_logs_the_greeting_the_connect_and_the_associate_of_each_connection_and_only_those():
    import _turn_chain as chain_mod
    from _doh_servers import DOH_BOOTSTRAP
    from test_the_udp_chain_helper import _socks_connect
    with chain_mod.Chain(rtt_ms=0, relay_host="127.0.0.1") as chain:
        assert chain.requests == []
        s, code = _socks_connect(chain, "192.0.2.77", 9)             # refused: not the DoH, and still a CONNECT the gateway read
        s.close()
        assert code == 2
        s, code = _socks_connect(chain, DOH_BOOTSTRAP, 443)
        s.close()
        assert code == 0
        kinds = [(r[1], r[2], r[3]) for r in chain.requests]
        assert kinds == [(0, "greeting", ""), (0, "connect", "192.0.2.77:9"), (1, "greeting", ""), (1, "connect", DOH_BOOTSTRAP + ":443")]
        assert all(0 <= r[0] < 30 for r in chain.requests)


def test_a_connection_that_opened_after_the_retry_is_red_on_a_page_that_ran_and_not_on_one_the_machine_stopped():
    from _e2e_verdicts import opened_at_the_retry_problem
    assert opened_at_the_retry_problem(60, 12) is None
    assert opened_at_the_retry_problem(2999, 5) is None
    assert opened_at_the_retry_problem(3100, 40) is not None, "the slow road: the page ran all along and the connection opened at the retry"
    assert "3100" in opened_at_the_retry_problem(3100, 40)
    assert opened_at_the_retry_problem(3100, 2400) is None, "the page's own loop stopped for 2.4 s: the machine, not the road"
    assert opened_at_the_retry_problem(9000, 999) is not None
    assert opened_at_the_retry_problem(9000, 1000) is None


def test_the_turn_page_reports_its_birth_reads_a_srflx_after_its_tick_and_has_an_empty_twin():
    import _turn_chain as chain_mod
    page = chain_mod.PAGE_HTML
    assert "window.__result.born = Date.now()" in page[:page.index("new RTCPeerConnection")]
    handler = page[page.index("pc.onicecandidate"):]
    guard = handler[:handler.index("clearInterval(tick)")]
    assert "status !== 'PENDING' ||" not in guard and "window.__result.late = status !== 'PENDING'" in guard
    assert "window.__result.posted = true" in handler
    assert "await" not in handler[handler.index("window.__result.posted = true"):], "the status is final in the step that says `posted`: a read made at that moment sees DONE"
    assert handler.index("(await r.json()).ok") < handler.index("window.__result.posted = true")
    with chain_mod.Chain(rtt_ms=0, relay_host="127.0.0.1") as chain:
        assert chain.page.warm_url.endswith(chain_mod.WARM_PATH)
        import urllib.request
        body = urllib.request.urlopen("http://127.0.0.1:%d%s" % (chain.page.port, chain_mod.WARM_PATH), timeout=10).read()
        assert b"RTCPeerConnection" not in body and b"warm" in body
