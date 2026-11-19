"""The UDP association of the proxy is ready BEFORE the PeerConnection is born: its first datagram does not wait for a handshake.

⛔ THE KNOWN-BAD INPUT. The sockets of a PeerConnection are made in the SOCKET process; the pool of UDP associations was warmed only
where Necko opens a SOCKS5 connection (the parent, where HTTP lives). So the first ICE socket found the pool empty and made its
association from scratch: TCP, greeting, auth, ASSOCIATE, four round trips before the first STUN datagram left (605 ms of 1083 on a
real proxy 150 ms away; measured in the logs of the giro 2 binary: "layer pushed" at +34 ms from the birth of the PeerConnection,
"ASSOCIATE OK" at +639 ms). A retail Firefox at home has no such wait.

What is asserted, with a gateway that answers every step of the handshake after a round trip (130 ms):
  1. an association the gateway granted was completed before the PeerConnection was created (the page loaded through the same
     proxy first, which is when Firefox learns the proxy); alone this does not tell the processes apart (the parent's pool is
     warmed by the page load), the second line does;
  2. the first STUN datagram of the PeerConnection leaves on one of those associations (the gateway says which association carried
     each datagram), not on one made after the birth: a cold association needs three answered steps, 3 x 130 ms, before it can
     send, so a datagram that leaves on one is the defect. It is an identity, not a number of milliseconds, so it does not depend
     on how loaded the machine is (a bound of 250 ms was crossed by 419 and 425 ms on a loaded runner, three reds).
Hermetic: a local SOCKS5 gateway with a relay and a fake STUN server, a local page reached through the proxy, no external network.
Red on an engine that warms the pool only in the process of the HTTP connections: measured on the giro 2 binary, the first datagram left
396 ms after the birth of the PeerConnection (3 x 130 ms) instead of at once, on an association made after the birth.
"""
from __future__ import annotations

import pytest

from _e2e_verdicts import cold_association_problem
from _socks_servers import STUN_NUMBER, STUN_PORT, coherent
from _udp_line import egress_by_proxy
from _waits import wait_until
from test_webrtc_realness import _e2e_binary, _launch  # noqa: F401

# Bound to the engine that carries the UDP line: red before the UDP line (firefox-63): skips on the published engine; runs on the candidate.
pytestmark = [pytest.mark.e2e, pytest.mark.usefixtures("the_udp_line")]

RTT_MS = 130

_PC_JS = """async (stun) => {
  const t_pc = Date.now();
  const pc = new RTCPeerConnection({iceServers: [{urls: ['stun:' + stun]}]});
  pc.createDataChannel('x');
  let t_srflx = null;
  pc.onicecandidate = e => { if (e.candidate && e.candidate.type === 'srflx' && t_srflx === null) t_srflx = Date.now(); };
  await pc.setLocalDescription(await pc.createOffer());
  const deadline = Date.now() + 30000;
  while (t_srflx === null && Date.now() < deadline) await new Promise(r => setTimeout(r, 5));
  pc.close();
  return {t_pc, t_srflx};
}"""


@pytest.mark.wall_clock
def test_the_first_datagram_does_not_wait_for_a_handshake(local_https_page, monkeypatch):
    binary = _e2e_binary()
    if not binary:
        pytest.skip("no patched binary (set STEALTHFOX_E2E_BINARY)")
    from invisible_core import _geo
    with coherent(user="alice", password="s3cret", rtt_ms=RTT_MS) as gateway:
        # ⛔ THE SESSION'S UDP HAS TO BE DECLARED COHERENT, or the engine treats the proxy as a router that drops UDP (no association is
        # asked, no datagram leaves) - the red of the giro 3 gate. The core declares `coherent` when the UDP probe's mapped address IS
        # the TCP exit it discovered; a hermetic test gives it the gateway's TCP exit as every e2e of the line does (the probe that
        # follows still runs through the relay, for real). The hermetic world has no network but the gateway, so the core's own discovery of the
        # exit (third-party echo services) fails and the verdict would be `unmeasured`.
        monkeypatch.setattr(_geo, "discover_egress_ip", egress_by_proxy(gateway))
        prefs = {"network.proxy.allow_hijacking_localhost": True, "network.proxy.no_proxies_on": ""}
        with _launch(binary_path=binary, proxy=gateway.proxy, extra_prefs=prefs, seed=7) as browser:
            page = browser.new_context().new_page()
            page.goto(local_https_page, wait_until="load", timeout=60000)   # through the proxy: Firefox learns it here
            # the pool makes its associations: wait until the gateway has granted one of the engine's (the first granted is the
            # core's probe), with a deadline; the verdict below names what it saw if none came
            wait_until(lambda: len([a for a in gateway.associations if a[1] is not None]) >= 2, 30,
                       lambda: page.wait_for_timeout(50))
            result = page.evaluate(_PC_JS, "%s:%d" % (STUN_NUMBER, STUN_PORT))
        t_pc = result["t_pc"] / 1000.0
        associations = [list(a) for a in gateway.associations]
        via = list(gateway.datagram_via)
        wall0 = gateway.wall0
    problem = cold_association_problem(associations, via, STUN_NUMBER, t_pc - wall0)
    assert problem is None, "%s; the gateway saw associations %s and datagrams %s" % (problem, associations, via)
    assert result["t_srflx"] is not None, "no srflx candidate came back through the relay"
