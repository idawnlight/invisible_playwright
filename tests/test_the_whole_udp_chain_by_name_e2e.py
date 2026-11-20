"""The whole chain a leak test walks, by NAME, behind a SOCKS5 gateway that carries UDP, all on loopback (hermetic).

A page asks for a TURN allocation at a server written as a NAME (`turn:turn.relay-only.test:3478`), as the real leak-test pages
do: no STUN, so a server-reflexive candidate can only be born from a successful allocation. Its logic is the logic of such a page:
a tick of 2 seconds from the start of the page that marks TIMEOUT, cleared the moment the srflx arrives (before the POST made for
it); the server compares the address of that srflx with the source the TURN server saw for the allocation. The tick is the model of
the page and decides nothing here: the verdict is read off the stamps of the gateway, the DoH and the TURN server (see below).

The only road to anything is a gateway at 130 ms of round trip (every step of its control connection, every datagram, every
chunk of a tunnel): the TURN server is at an unroutable documentation address that only the gateway maps to it; the names (the
TURN's and the page's) are known only to a DoH server (a `FakeDoh`, with a certificate for `mozilla.cloudflare-dns.com`) that
only the gateway reaches, by a CONNECT; and the gateway's datagrams leave from 127.0.0.2 so that a TURN server hearing 127.0.0.1
proves the browser went around the gateway. The gateway resolves nothing: it answers no DNS query and no name sent as SOCKS5
address type 3, on the relay or in a CONNECT.

What the product must do, to be what a retail Firefox is with no proxy at all (the same candidates, in the same time):
  * the name of the ICE server is resolved by the DoH of Firefox (`network.trr.mode` 3, found with no DNS at 162.159.61.4),
    whose request leaves THROUGH the proxy of the context: a SOCKS5 CONNECT to 162.159.61.4:443; no local resolution;
  * the UDP to the address the DoH gave leaves through the gateway's relay (address type 1);
  * and nothing of it waits for a handshake it does not need, nor costs an exchange more than the protocols ask for. Neither is a
    number of milliseconds (`_e2e_verdicts.turn_chain_problem`): (1) from the instant the pool of associations is ready the test HOLDS
    the answer of every association handshake begun after it (`Gateway.hold`), and the TURN server has to answer the Allocate with
    credentials all the same, BEFORE the test lets them go: a handshake that waits for another leaves the road waiting for the test,
    on any machine, and a quick or a slow one makes no difference; (2) from the birth of the PeerConnection to that Allocate the
    gateway, the DoH and the TURN server recorded at most one SOCKS5 control connection with its CONNECT to the DoH (greeting and
    CONNECT, RFC 1928; the one that carried the question of the TURN name, told by the number of the connection the gateway gave
    the DoH: the page's own server and the DoH connections of other names, made in parallel, are shown and decide nothing), one DoH question (the A record, RFC 8484), and the two transactions of an Allocate (the first answered 401, the
    second with the long-term credential, RFC 8656 and RFC 8489): five exchanges, and one more is red; (3) the Allocate leaves on an
    association that was ready before the birth. The page is waited for until its srflx is posted (30 s), not until its tick.

It fails on an engine that does not keep this (the one of the wrong candidate), and says WHY: the explanation lists what the
DoH, the gateway, the TURN server and the page each saw. The engine refuses to resolve a name that is not a literal behind a
proxy, so the DoH is never asked (no CONNECT to it reaches the gateway) and the browser never reaches the TURN server; then
the handshakes of its associations are serial (each costs round trips of the gateway before a socket is usable).

The browser accepts the certificate of the FakeDoh the way `test_doh_through_the_proxy_e2e.py` does: the page is opened in a
context made with `ignore_https_errors=True` (applied per user context to the DoH request of the lookup of that context), and
the provider is the FakeDoh's own name (`doh.gateway.test`, declared through `extra_prefs`): the DoH of Mozilla is HSTS-preloaded
and Firefox never accepts an override for it.
"""
import threading
import time

import pytest

import _turn_chain as chain_mod
from _doh_servers import DOH_PROVIDER_PREFS
from _e2e_verdicts import turn_chain_problem
from _udp_line import declared_udp, egress_by_proxy, measure_servers
from _waits import wait_until

pytestmark = pytest.mark.e2e

#: Loopback goes through the proxy too (as in test_doh_through_the_proxy_e2e.py), so the road of the page is observable.
PREFS = {"network.proxy.allow_hijacking_localhost": True, "network.proxy.no_proxies_on": ""}

#: ⛔ The variant with the lab's exit on loopback (127.0.0.2). The TURN server tells the browser, in the XOR-MAPPED-ADDRESS of its
#: answer, the address it heard the Allocate from: here a loopback one, and a retail Firefox throws that answer away as "bogus"
#: (`nr_stun_transport_addr_check` in stun_client_ctx.cpp) unless the ICE context allows loopback, which this pref does. It is the lab's
#: address, not a behaviour of the product: on the real network the mapped address is the exit's, and the variant above (an exit that
#: is not loopback, no pref) proves that on Linux. This one runs on Windows too: the exit above is an address added to an interface
#: (administrator and `ip`), which is Linux only until the runner of Windows does the same (work of the release after firefox-63).
PREFS_LOOPBACK_EXIT = {**PREFS, "media.peerconnection.ice.loopback": True}


def _the_core_is_told_the_exit(monkeypatch, chain):
    # The probe of the core that asks for the TCP exit goes nowhere (the world has no network but the gateway): it is told
    # the exit of the chain, the address its datagrams leave from. The core's UDP measurement that follows is NOT replaced:
    # it asks the chain's relay for a STUN Binding for real and compares what comes back with that exit, so the road
    # is declared ``coherent`` by measurement and the engine sends the page's UDP to the relay.
    try:
        from invisible_core import _geo
        monkeypatch.setattr(_geo, "discover_egress_ip", egress_by_proxy(chain), raising=False)
    except Exception:   # noqa: BLE001
        pass
    try:
        from invisible_playwright import InvisiblePlaywright
        monkeypatch.setattr(InvisiblePlaywright, "_assert_uscita_invariata", lambda self: None, raising=False)
    except Exception:   # noqa: BLE001
        pass


@pytest.fixture
def gateway(monkeypatch):
    # ⛔ THE EXIT OF THE LAB IS NOT A LOOPBACK ADDRESS. The TURN server tells the browser, in the XOR-MAPPED-ADDRESS of its answer, the
    # address it heard the Allocate from: the gateway's exit. nICEr throws away an answer whose mapped address is loopback as "bogus"
    # (stun_client_ctx.cpp nr_stun_transport_addr_check, unless the pref `media.peerconnection.ice.loopback` is on, which it is not in a
    # product), so the exit is a documentation address held on `lo` (root, Linux: the suite of this test is listed for Linux only).
    with chain_mod.lab_exit_address() as exit_ip, chain_mod.Chain(rtt_ms=130, exit_host=exit_ip) as chain:
        _the_core_is_told_the_exit(monkeypatch, chain)
        yield chain


@pytest.fixture
def gateway_on_loopback(monkeypatch):
    with chain_mod.Chain(rtt_ms=130) as chain:     # the exit is chain_mod.EXIT_HOST, 127.0.0.2
        _the_core_is_told_the_exit(monkeypatch, chain)
        yield chain


def test_a_turn_srflx_by_name_arrives_through_the_relay_before_the_tick(gateway, firefox_binary, engine_at_least):
    """The exit of the lab is an address that is not loopback, and no pref: the product's own setting (Linux, root)."""
    _walk(gateway, firefox_binary, engine_at_least, PREFS)


def test_a_turn_srflx_by_name_arrives_through_the_relay_when_the_exit_of_the_lab_is_loopback(gateway_on_loopback, firefox_binary,
                                                                                          engine_at_least):
    """The exit of the lab is 127.0.0.2 and the ICE context allows loopback (the pref): the same walk on every system, Windows too."""
    _walk(gateway_on_loopback, firefox_binary, engine_at_least, PREFS_LOOPBACK_EXIT)


def _walk(gateway, firefox_binary, engine_at_least, prefs):
    import h2  # noqa: F401 - the dev extra installs it: a missing one is a failure, never a skip
    engine_at_least("firefox-63")
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(headless=True, binary_path=firefox_binary, timezone="America/New_York", locale="en-US",
                             proxy=gateway.proxy, udp_stun_servers=measure_servers(gateway),
                             extra_prefs={**prefs, **DOH_PROVIDER_PREFS}) as browser:
        page = browser.new_context(ignore_https_errors=True).new_page()
        # An empty page first, through the proxy: Firefox learns the proxy there and its pool makes the associations. The
        # PeerConnection is born once the gateway has granted one (the first granted is the core's own probe, no association of the
        # engine), so "ready before the birth" is a fact of the road and not a race with the load of the machine.
        page.goto(gateway.page.warm_url, wait_until="domcontentloaded", timeout=60000)
        wait_until(lambda: len([a for a in gateway.associations if a[1] is not None]) >= 2, 30, lambda: page.wait_for_timeout(50))
        # From here on every association handshake the engine begins is HELD (the gateway answers nothing on it): the pool is ready and
        # the road of the name needs none, so the Allocate must be answered while they are held. The page is waited for until it has
        # posted its srflx (30 s); its tick of 2 s is its model and decides nothing.
        first_held = len(gateway.accepted)
        held = threading.Event()
        held_from = time.monotonic() - gateway.t0
        gateway.hold = lambda index, kind: held if kind == "associate" and index >= first_held else None
        loaded = "the page loaded"
        try:
            try:
                page.goto(gateway.page.url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_function("window.__result.posted === true", timeout=30000)
            except Exception as error:   # noqa: BLE001 - the verdict below says what the road did; this says where the page stopped
                loaded = "the page stopped: %s" % str(error).splitlines()[0][:200]
        finally:
            released_at = time.monotonic() - gateway.t0
            gateway.hold = None
            held.set()
        try:
            page.wait_for_function("window.__result.candidates.some(c => c.indexOf(' typ relay ') >= 0)", timeout=30000)
        except Exception:   # noqa: BLE001
            pass
        result = page.evaluate("window.__result")
        # what the road looked like, read before anything else asks the gateway for an association (the declared_udp below does)
        t0, wall0 = gateway.t0, gateway.wall0
        born = (result.get("born") or wall0 * 1000.0) / 1000.0 - wall0     # the asserts below name a page that never ran
        associations = [list(a) for a in gateway.associations]
        via = list(gateway.datagram_via)
        requests = list(gateway.requests)
        doh_questions = [q for q in gateway.doh.question_connections if q[0] == chain_mod.TURN_NAME]
        allocates = [(t - t0, tid, mtype, credentials) for t, tid, mtype, credentials in gateway.turn.transactions()]
        allocated_at = gateway.turn.allocations[0][3] - t0 if gateway.turn.allocations else None
        held_starts = [a[0] for a in associations if a[0] >= held_from]
        auth = gateway.user is not None
    declared = declared_udp(gateway.proxy)
    assert declared == "coherent", "the core did not measure the chain's UDP coherent (%s): the engine would not use the relay" % declared + gateway.explain(result)
    ok = result.get("status") == "DONE" and gateway.page.verdicts and gateway.page.verdicts[-1]["ok"]
    assert ok, "the page did not get a server-reflexive candidate that the TURN server saw (the page was waited for 30 s):" + gateway.explain(result)
    problem = turn_chain_problem(born, allocated_at, released_at, held_starts, requests, doh_questions, allocates, associations, via,
                                 chain_mod.TURN_IP, auth)
    seen = ("%s; the page's tick of 2 s %s; associations %s (held from %.3f s, let go at %.3f s); datagrams to the TURN server %s; requests of "
            "the gateway %s; DoH questions for the TURN name %s; Allocate transactions %s; the PeerConnection born at %.3f s; the "
            "allocation at %s s" % (
                loaded, "had passed (the srflx was late)" if result.get("late") else "had not passed", associations, held_from, released_at,
                [d for d in via if d[0] == chain_mod.TURN_IP], [(round(r[0], 3),) + tuple(r[1:]) for r in requests], doh_questions,
                [(round(a[0], 3), a[2], "credentials" if a[3] else "no credentials") for a in allocates], born,
                None if allocated_at is None else round(allocated_at, 3)))
    assert problem is None, "%s; %s%s" % (problem, seen, gateway.explain(result))
    # what the PAGE reads: a server-reflexive candidate that is the gateway's exit, and the relay of the TURN server it asked
    srflx = (result.get("srflx") or "").split()
    assert len(srflx) > 5 and srflx[4] == gateway.exit_ip, "the srflx of the page is not the exit of the gateway %s: %r" % (gateway.exit_ip, result.get("srflx"))
    relays = [c.split() for c in (result.get("candidates") or []) if " typ relay " in c]
    assert relays and (relays[0][4], int(relays[0][5])) == chain_mod.RELAYED, "the page has no relay candidate of the TURN server: %r" % (relays,) + gateway.explain(result)
