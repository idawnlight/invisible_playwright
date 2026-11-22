"""The host network a page sees is the one the session DECLARES, not the one of the machine (red until it is declared).

Design: ``invisible_WebRTC/notes/declared-interfaces-design.md`` and ``declared-interfaces-proofs.md`` (the private study
repository). Behind a
proxy the engine must not read the host's interfaces: the core declares one adapter in ``STEALTHFOX_WEBRTC_NETWORK``,
``v4=<private address>;v6=<0|1>;p6=<the exit's /64 prefix, empty without IPv6>`` (the IPv6 host is that prefix and a
temporary identifier; v6=1 without a prefix is no IPv6), and the engine answers the reads of the host (the interface list,
the default route, the address and port a bound socket reports) from it (nr_stealth_network.h). A page
then sees what retail Firefox on Windows shows on a machine with that adapter, whatever the machine under the browser has:

  * no IPv6: ONE host name (udp + tcp), ranked 127 (the only address);
  * IPv6: TWO host names, the IPv6 one ranked 127 above the IPv4 one at 126 (``nrinterfaceprioritizer.cpp:82-85``);
  * the host ports are those of a Windows machine, chosen by our code: 49152-65535, a consecutive pair per connection
    (IPv4 N, IPv6 N+1), drawn at random by our code for each connection (not by the kernel, not from the seed: two visits
    with the same seed must not share ports), the same distribution on every host;
  * two connections on one page connect to each other in the time retail takes.

Reference shape (retail 159, measured 2026-11-11, ``captures/retail/firefox-159.0-win/direct-dualstack-stun-candidates``):
host udp 2122252543 (IPv6) and 2122187007 (IPv4), host tcp 2105524479 and 2105458943, one name per address.

Red on an engine without the declared network (before webrtc/declared-interfaces b9bdb3c3ad + 6ca7a8c104): it gathered
every filtered address of the OS and let the kernel hand out the ports, so these failed on every machine with more than
one interface or a port range that is not Windows's, the IPv6 case everywhere, and the loopback test was slower than
retail and died with no usable interface. Hermetic: a local TCP-only SOCKS5 proxy and a local page, no external network.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import time

import pytest

from _e2e_verdicts import MDNS_RETRY_MS, opened_at_the_retry_problem
from _evidence_with_files import FailureEvidenceWithFiles, probe_machine, tee_browser_output
from _mdns_listener import MdnsListener
from test_webrtc_realness import _e2e_binary, _launch, candidates, decode_priority, host_candidates, is_mdns  # noqa: F401

pytestmark = [pytest.mark.e2e, pytest.mark.usefixtures("the_udp_line")]
# The two tests that read a consecutive pair of ports (N, N+1) carry `wall_clock`: the e2e run gives them the machine alone, after the parallel tests,
# so that another worker's browser or test server cannot take port N+1 between the engine's draw and the bind (tests/test_the_consecutive_port_tests_run_serially.py).

NETWORK_ENV = "STEALTHFOX_WEBRTC_NETWORK"
# retail 159 on Windows. The interface rank is the position in the prioritizer's sort, starting at 127
# (nrinterfaceprioritizer.cpp:183-189), so an IPv4-only machine ranks its one host 127, and with IPv6 the IPv6 host
# takes 127 and the IPv4 host 126.
UDP_127, UDP_126, TCP_127, TCP_126 = 2122252543, 2122187007, 2105524479, 2105458943
# The two shapes of the declared adapter: without IPv6, and with the exit's /64 prefix.
V4_ONLY = "v4=192.168.31.7;v6=0;p6="
DUAL = "v4=192.168.31.7;v6=1;p6=2a01:e0a:1f2:3450::/64"

_GATHER_JS = r"""async () => {
  const pc = new RTCPeerConnection({iceServers: []});
  pc.createDataChannel('');
  const cands = [];
  pc.addEventListener('icecandidate', e => { if (e.candidate && e.candidate.candidate) cands.push(e.candidate.candidate); });
  await pc.setLocalDescription(await pc.createOffer());
  // Until the gathering says it is over, not for a fixed time: how long a gathering takes is the speed of the machine, whether it ends is the product.
  // The deadline is far beyond any healthy run; a gathering that never completes returns what it has, and the assertions on the candidates fail on it.
  await new Promise(resolve => {
    const timer = setTimeout(resolve, 30000);
    const done = () => { if (pc.iceGatheringState === 'complete') { clearTimeout(timer); resolve(); } };
    pc.addEventListener('icegatheringstatechange', done);
    done();
  });
  try { pc.close(); } catch (e) {}
  return cands;
}"""

# Two connections on one page, connected to each other in script (the loopback test many demos use).
# ``events`` is the page's own timeline for the failure evidence (``at``: ``performance.now()`` since the start of the script, ``origin``:
# ``performance.timeOrigin``, so the wall clock of an event is ``origin + at``): every ``icecandidate`` of both connections with the candidate string,
# every state change, when each ``addIceCandidate`` was called and when it settled. ``steps`` is the time after each of the four descriptions is set.
# ``snapshots`` is what ``getStats`` says of BOTH connections when the wait is over (the failure point of a run that did not open or echo, and the end of
# one that did): every local and remote candidate (type, address, port, protocol) and every candidate pair (state, nominated, the checks sent and
# answered, the two candidate ids and what they are). A remote candidate of type ``host`` on pc1 is a name of pc2 that pc1 resolved, ``prflx`` alone is a
# name that was never resolved, and many requests with no response are STUN checks that were lost, not an mDNS name. It only listens and reads: nothing
# waits on it, and the connections are closed at the end, whatever happened, so that the engine's own timecard of each one can be printed.
_LOOPBACK_JS = r"""async () => {
  const t0 = performance.now();
  const events = [];
  const now = () => Math.round((performance.now() - t0) * 10) / 10;
  const at = (what, extra) => { events.push(Object.assign({at: now(), what}, extra || {})); };
  const steps = {};
  const step = name => { steps[name] = now(); at(name); };
  const pc1 = new RTCPeerConnection(), pc2 = new RTCPeerConnection();
  for (const [name, pc] of [['pc1', pc1], ['pc2', pc2]]) {
    pc.addEventListener('icecandidate', e => at('icecandidate', {pc: name, candidate: e.candidate ? e.candidate.candidate : null}));
    pc.addEventListener('iceconnectionstatechange', () => at('iceconnectionstatechange', {pc: name, state: pc.iceConnectionState}));
    pc.addEventListener('connectionstatechange', () => at('connectionstatechange', {pc: name, state: pc.connectionState}));
    pc.addEventListener('icegatheringstatechange', () => at('icegatheringstatechange', {pc: name, state: pc.iceGatheringState}));
    pc.addEventListener('signalingstatechange', () => at('signalingstatechange', {pc: name, state: pc.signalingState}));
  }
  const add = (target, targetName, candidate) => {
    at('addIceCandidate called', {to: targetName, candidate: candidate.candidate});
    return target.addIceCandidate(candidate).then(
      () => at('addIceCandidate resolved', {to: targetName, candidate: candidate.candidate}),
      err => at('addIceCandidate rejected', {to: targetName, candidate: candidate.candidate, error: String(err)}));
  };
  pc1.onicecandidate = e => { if (e.candidate) add(pc2, 'pc2', e.candidate); };
  pc2.onicecandidate = e => { if (e.candidate) add(pc1, 'pc1', e.candidate); };
  const gathered = {pc1: [], pc2: []};
  const keep = (name, e) => { if (e.candidate && e.candidate.candidate) gathered[name].push(e.candidate.candidate); };
  pc1.addEventListener('icecandidate', e => keep('pc1', e));
  pc2.addEventListener('icecandidate', e => keep('pc2', e));
  const dc = pc1.createDataChannel('x');
  let opened = null, echoed = false, error = null;
  dc.onopen = () => { opened = Math.round(performance.now() - t0); at('datachannel open'); dc.send('ping'); };
  pc2.ondatachannel = ev => { at('datachannel announced on pc2'); ev.channel.onmessage = m => ev.channel.send('pong:' + m.data); };
  dc.onmessage = m => { echoed = (m.data === 'pong:ping'); at('datachannel echo', {echoed}); };
  // What a connection's statistics say now: its candidates and its candidate pairs, each pair with the two candidates it joins.
  const readStats = async pc => {
    const stats = await pc.getStats();
    const byId = {};
    stats.forEach(r => { byId[r.id] = r; });
    const view = r => r ? {id: r.id, candidateType: r.candidateType, address: r.address, port: r.port, protocol: r.protocol, priority: r.priority} : null;
    const out = {local: [], remote: [], pairs: []};
    stats.forEach(r => {
      if (r.type === 'local-candidate') out.local.push(view(r));
      if (r.type === 'remote-candidate') out.remote.push(view(r));
      if (r.type === 'candidate-pair') {
        out.pairs.push({id: r.id, state: r.state, nominated: r.nominated, requestsSent: r.requestsSent, responsesReceived: r.responsesReceived,
                        requestsReceived: r.requestsReceived, responsesSent: r.responsesSent, localCandidateId: r.localCandidateId,
                        remoteCandidateId: r.remoteCandidateId, local: view(byId[r.localCandidateId]), remote: view(byId[r.remoteCandidateId])});
      }
    });
    return out;
  };
  const snapshot = async when => {
    const one = {when, at: now()};
    for (const [name, pc] of [['pc1', pc1], ['pc2', pc2]]) {
      try { one[name] = await readStats(pc); } catch (e) { one[name] = {error: String(e)}; }
    }
    return one;
  };
  let stalled = 0;
  try {
    await pc1.setLocalDescription(await pc1.createOffer());
    step('pc1 local description set');
    await pc2.setRemoteDescription(pc1.localDescription);
    step('pc2 remote description set');
    await pc2.setLocalDescription(await pc2.createAnswer());
    step('pc2 local description set');
    await pc1.setRemoteDescription(pc2.localDescription);
    step('pc1 remote description set');
    // Until the channel has opened and echoed, for 30 s (the deadline of the wait). The loop also keeps the longest time it did not run: a page that
    // was stopped by the machine says so, and the time the connection took is not judged then (`_e2e_verdicts.opened_at_the_retry_problem`).
    const deadline = performance.now() + 30000;
    let last = performance.now();
    while (performance.now() < deadline && !(opened !== null && echoed)) {
      await new Promise(r => setTimeout(r, 5));
      const t = performance.now();
      stalled = Math.max(stalled, t - last);
      last = t;
    }
    at('wait over', {opened, echoed});
  } catch (e) {
    error = String(e);
    at('error', {error});
  }
  // The road the connection took, read from the browser's own statistics: the nominated pair, and the protocol of each end.
  const snapshots = [];
  let pair = null;
  try {
    const final = await snapshot(error ? 'error' : 'wait over');
    snapshots.push(final);
    for (const p of (final.pc1 && final.pc1.pairs) || []) {
      if (p.state === 'succeeded' && (p.nominated || !pair)) {
        const l = p.local || {}, rm = p.remote || {};
        pair = {local: l.protocol, remote: rm.protocol, localType: l.candidateType, remoteType: rm.candidateType, remoteAddress: rm.address || null};
      }
    }
  } catch (e) {}
  // Closed before the browser is: the engine prints a connection's timecard when the connection is destroyed.
  try { pc1.close(); } catch (e) {}
  try { pc2.close(); } catch (e) {}
  return {opened, echoed, pair, gathered, stalled: Math.round(stalled), error, events, steps, snapshots, origin: performance.timeOrigin, started_at: Math.round(t0 * 10) / 10};
}"""

# Retail 159 on Windows, 12 cold launches (``captures/mdns-loopback/retail-159-win-12``): 37-52 ms in ten of twelve, two
# slow ones at about 110, median 41.5. Without mDNS retail takes about 20 ms (``retail-159-win-nomdns``): the other 20 ms are
# the multicast round trip of the responder. That is a MEASURE of a fast machine and is printed, never judged: on a
# GitHub runner the same road takes 67 ms (Windows) and 122 ms (Linux), and a verdict on an absolute time is a verdict on
# the machine. What the test judges is the ROAD, which the machine cannot change (see the test), and a ceiling under the
# first retry of the mDNS responder (3 s), which no machine is slow enough to reach by being slow.
RETAIL_MEDIAN_WINDOW_MS = (30, 55)       # printed beside the measure, for the reader
# What the engine logs for the failure evidence of the loopback test, by its own names (MOZ_LOG; the file is MOZ_LOG_FILE, one per process and
# per launch). ``mdns_service::*`` is the Rust crate of the mDNS responder (dom/media/webrtc/transport/mdns_service/src/lib.rs, crate ``mdns_service``,
# which logs with the ``log`` macros at the crate root, so the module path is ``mdns_service``): a Rust module reaches MOZ_LOG only by a name that
# contains ``::`` (xpcom/base/Logging.cpp, ``LogModule::SetLevel`` calls ``set_rust_log_level`` only then). ``mtransport`` is the module of nICEr's own
# log and of the transport layers (``MOZ_MTLOG_MODULE("mtransport")``, dom/media/webrtc/transport/nricectx.cpp:100, nriceresolver.cpp:67 ...), ML_DEBUG is level 5.
# The engine's ``log`` crate is built with ``release_max_level_info`` (toolkit/library/rust/shared/Cargo.toml:41): a release engine keeps the info, warn and
# error lines of the responder (the ``warn!`` of a failed send, a bad packet, an interface it could not use), not its ``trace!`` lines (questions, answers,
# registrations); level 3 is info, the most such an engine has. ``timestamp`` writes the wall clock (UTC) on each line, ``sync`` writes each line at once.
# ``signaling:1`` is not for its lines (level 1 is error) but because the PeerConnection makes its timecard only while that module is enabled
# (PeerConnectionImpl.cpp:375, ``MOZ_LOG_TEST(logModuleInfo, LogLevel::Error)``, ``logModuleInfo`` is ``signaling``); the engine prints the timecard on the
# stdout of the content process when the connection is destroyed (the same file, :449-453), and the launch's stdout is kept (``tee_browser_output``).
MDNS_MOZ_LOG = "timestamp,sync,mdns_service::*:3,mtransport:5,signaling:1"


def _session(declaration, socks, page_url, monkeypatch, seed, script, engine_log=None):
    """One proxied launch with the declaration in the environment; runs ``script`` on the page and returns the result.
    ``engine_log``: a folder where the engine writes its own log of this launch (failure evidence, ``MDNS_MOZ_LOG``)."""
    binary = _e2e_binary()
    if not binary:
        pytest.skip("no patched binary (set STEALTHFOX_E2E_BINARY)")
    monkeypatch.setenv(NETWORK_ENV, declaration)
    if engine_log is not None:
        os.makedirs(engine_log, exist_ok=True)
        monkeypatch.setenv("MOZ_LOG", MDNS_MOZ_LOG)
        monkeypatch.setenv("MOZ_LOG_FILE", os.path.join(engine_log, "engine-%PID.log"))
        # The browser's stdout (the timecards of the content process, ``MDNS_MOZ_LOG``), next to its log files: ``engine-stdout.log``.
        tee_browser_output(monkeypatch, os.path.join(engine_log, "engine-stdout.log"))
    prefs = {"network.proxy.allow_hijacking_localhost": True, "network.proxy.no_proxies_on": ""}
    proxy = {"server": "socks5://127.0.0.1:%d" % socks.port}
    with _launch(binary_path=binary, proxy=proxy, extra_prefs=prefs, seed=seed) as browser:
        page = browser.new_context().new_page()
        page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        return script(page)


def _host_shape(declaration, socks, page_url, monkeypatch, seed=42):
    return host_candidates(candidates(
        _session(declaration, socks, page_url, monkeypatch, seed, lambda page: page.evaluate(_GATHER_JS))))


def _by_protocol(hosts):
    return {proto: sorted((c["priority"] for c in hosts if c["proto"] == proto), reverse=True)
            for proto in ("UDP", "TCP")}


def _udp_ports_by_rank(hosts):
    return {decode_priority(c["priority"])["iface_pref"]: c["port"] for c in hosts if c["proto"] == "UDP"}


def test_without_a_declared_ipv6_the_page_sees_one_host_name(local_https_page, socks5_tcp_only, monkeypatch):
    """One adapter, no IPv6: a udp and a tcp host candidate on ONE mDNS name, ranked 127 as the only address.

    The shape is a function of the declaration alone, so this must hold on every machine: run it on a host with three
    IPv4 adapters (the engine gathers all three today), in a container with two interfaces (``docker network connect``
    before ``docker start``), and in a container with one."""
    hosts = _host_shape(V4_ONLY, socks5_tcp_only, local_https_page, monkeypatch)
    assert all(is_mdns(c["address"]) for c in hosts), [c["address"] for c in hosts]
    assert len({c["address"] for c in hosts}) == 1, "host names: %r" % sorted({c["address"] for c in hosts})
    assert _by_protocol(hosts) == {"UDP": [UDP_127], "TCP": [TCP_127]}, _by_protocol(hosts)


def test_a_declared_ipv6_gives_two_host_names_ranked_127_and_126(local_https_page, socks5_tcp_only, monkeypatch):
    """One adapter with IPv6: two names; the IPv6 host ranks one above the IPv4 one, as on the retail capture."""
    hosts = _host_shape(DUAL, socks5_tcp_only, local_https_page, monkeypatch)
    assert all(is_mdns(c["address"]) for c in hosts), [c["address"] for c in hosts]
    assert len({c["address"] for c in hosts}) == 2, "host names: %r" % sorted({c["address"] for c in hosts})
    assert _by_protocol(hosts) == {"UDP": [UDP_127, UDP_126], "TCP": [TCP_127, TCP_126]}, _by_protocol(hosts)
    assert [decode_priority(p)["iface_pref"] for p in _by_protocol(hosts)["UDP"]] == [127, 126]


def test_ipv6_without_the_exit_prefix_is_no_ipv6(local_https_page, socks5_tcp_only, monkeypatch):
    """v6=1 with an empty prefix: the exit gave the core no /64, so the machine has no IPv6 address, and no fallback
    invents one. One host name, as without IPv6."""
    hosts = _host_shape("v4=192.168.31.7;v6=1;p6=", socks5_tcp_only, local_https_page, monkeypatch)
    assert _by_protocol(hosts) == {"UDP": [UDP_127], "TCP": [TCP_127]}, hosts


@pytest.mark.wall_clock
def test_the_host_ports_are_those_of_a_windows_machine(local_https_page, socks5_tcp_only, monkeypatch):
    """Retail on Windows (6 of 6 runs, ``captures/retail/firefox-159.0-win/direct-dualstack-srflx-ports``): the UDP host
    ports are in the dynamic range 49152-65535 and, on one connection, the IPv4 port and the IPv6 port are consecutive
    (N, N+1). A page reads them. Linux hands out 32768-60999 at random, so on a Linux host this is red today."""
    hosts = _host_shape(DUAL, socks5_tcp_only, local_https_page, monkeypatch)
    by_rank = _udp_ports_by_rank(hosts)
    assert sorted(by_rank) == [126, 127], by_rank
    assert all(49152 <= port <= 65535 for port in by_rank.values()), by_rank
    assert by_rank[127] == by_rank[126] + 1, "IPv4 port %d, IPv6 port %d" % (by_rank[126], by_rank[127])


def test_the_host_port_is_in_the_windows_range_without_ipv6_too(local_https_page, socks5_tcp_only, monkeypatch):
    """The one-address case of the range check. On a Linux host it can pass by luck (a kernel port in 49152-60999 falls
    in the range); the same-seed test above is the one that cannot."""
    hosts = _host_shape(V4_ONLY, socks5_tcp_only, local_https_page, monkeypatch)
    by_rank = _udp_ports_by_rank(hosts)
    assert list(by_rank) == [127] and 49152 <= by_rank[127] <= 65535, by_rank


def _ports_of_two_connections(declaration, socks, page_url, monkeypatch, seed):
    def script(page):
        first = host_candidates(candidates(page.evaluate(_GATHER_JS)))
        second = host_candidates(candidates(page.evaluate(_GATHER_JS)))
        return first, second

    first, second = _session(declaration, socks, page_url, monkeypatch, seed, script)
    return (sorted(c["port"] for c in first if c["proto"] == "UDP"),
            sorted(c["port"] for c in second if c["proto"] == "UDP"))


@pytest.mark.wall_clock
def test_two_connections_on_one_page_get_their_own_port_pair(local_https_page, socks5_tcp_only, monkeypatch):
    """Retail (``captures/retail/firefox-159.0-win/two-pc-port-sequence``, 6 of 6): each connection gets its own pair,
    consecutive (N, N+1) in the range, and the second pair is unrelated to the first (not increasing)."""
    first, second = _ports_of_two_connections(DUAL, socks5_tcp_only, local_https_page, monkeypatch, 42)
    for pair in (first, second):
        assert len(pair) == 2 and pair[1] == pair[0] + 1 and 49152 <= pair[0] and pair[1] <= 65535, (first, second)
    assert first != second, (first, second)


def test_two_connections_on_one_page_connect_in_the_time_of_retail(local_https_page, socks5_tcp_only, monkeypatch, capsys):
    """The names the browser registered for itself must resolve although the declared address is not the host's, and
    the connection must take the road retail takes. Five cold launches; EVERY one is judged on the road, none on the clock:

      * the data channel opens and echoes (a responder that cannot hear itself, or an OS list that is empty, kills it);
      * each connection gathered a UDP host candidate named by mDNS (a run without UDP is the reserved-port failure: the
        socket could not bind and the connection fell back to TCP, about 600 ms on average);
      * the nominated pair is direct and local over UDP at both ends: host or peer-reflexive, never relay or server-reflexive, never
        TCP (a peer-reflexive end is legitimate: the other connection's mDNS name was not resolved yet and its check came first);
      * it opens in less than the responder's first retry (3 s) unless the page itself was stopped for a second or more in the meantime: a name that does
        not come back opens only at the retry, and a machine that stops for seconds opens late on any road.

    The time itself is printed with the retail window beside it: a fast machine shows 30-55 ms, a runner shows more, and
    neither is a verdict (an absolute time failed on both GitHub runners with a healthy engine)."""
    runs = []
    launches = []
    # Failure evidence (tests/_evidence_with_files.py): per launch, the engine's own log (the mDNS responder and mtransport, one file per process) and the
    # browser's stdout (``engine-stdout.log``, with the timecards), kept only for the launch that fails; ``machine-run-N.txt``, who holds UDP 5353 when a
    # run is not right; and ``launches.json``, the timings and the page's timeline (events, the time after each description, the statistics of both
    # connections) of EVERY launch up to it (the loop stops at the first red).
    with FailureEvidenceWithFiles("declared-host-two-connections-open-in-time") as evidence:
        def loopback(n):
            # The page's measure, and, while the browser is still up, who holds UDP 5353 on the machine when the run is not right
            # (``machine-run-N.txt``; the verdicts below are what judge it, this only decides when to look).
            def run(page):
                res = page.evaluate(_LOOPBACK_JS)
                if (not res["echoed"] or res["opened"] is None or not res["pair"]
                        or opened_at_the_retry_problem(res["opened"], res["stalled"], MDNS_RETRY_MS) is not None):
                    probe_machine(evidence.files / ("machine-run-%d.txt" % n))
                return res
            return run

        try:
            for n in range(5):
                log_dir = evidence.files / ("engine-log-run-%d" % n)
                launch = {"run": n, "seed": 100 + n, "moz_log": MDNS_MOZ_LOG, "started_m": round(time.perf_counter(), 6),
                          "started_t": round(time.time(), 4)}
                launches.append(launch)
                # The lab's own ear on the mDNS group (tests/_mdns_listener.py), started BEFORE the browser so that the engine's socket is the later one:
                # it only listens, and what it heard of this launch is ``mdns-wire-run-N.jsonl`` (``m`` is the scale of ``started_m`` above).
                listener = MdnsListener().start()
                try:
                    res = _session(V4_ONLY, socks5_tcp_only, local_https_page, monkeypatch, 100 + n,
                                   loopback(n), engine_log=str(log_dir))
                finally:
                    launch.update(returned_m=round(time.perf_counter(), 6), returned_t=round(time.time(), 4))
                    listener.stop()
                    listener.write(evidence.files / ("mdns-wire-run-%d.jsonl" % n))
                    launch["mdns_wire"] = listener.summary()
                page_timeline = {key: res.pop(key, None) for key in ("events", "steps", "snapshots", "error", "origin", "started_at")}
                launch.update(opened=res["opened"], echoed=res["echoed"], stalled=res["stalled"], pair=res["pair"], gathered=res["gathered"],
                              page=page_timeline)
                assert res["echoed"] and res["opened"] is not None, "run %d: the data channel did not open: %r" % (n, res)
                runs.append(res)
                for side in ("pc1", "pc2"):
                    hosts = [c for c in res["gathered"][side] if " typ host" in c]
                    udp = [c for c in hosts if " UDP " in c.upper() or " udp " in c]
                    assert udp, "run %d: %s gathered no UDP host candidate (the road without UDP): %r" % (n, side, res["gathered"][side])
                    assert any(c.split()[4].endswith(".local") for c in udp), (
                        "run %d: %s has no mDNS name among its UDP host candidates: %r" % (n, side, udp))
                pair = res["pair"]
                assert pair, "run %d: the browser reports no nominated pair: %r" % (n, res)
                assert pair["local"] == "udp" and pair["remote"] == "udp", (
                    "run %d: the connection did not take UDP at both ends (the road through TCP): %r" % (n, pair))
                # DIRECT and local: each end is a host candidate, or a peer-reflexive one. The remote end is prflx when the other connection's mDNS
                # name was not resolved yet and its connectivity check arrived first: the address the check came from is learned as prflx, and
                # that is the same road. What it must never be is a relay or a server-reflexive candidate (a road through the proxy or the
                # network), and never TCP (checked above).
                assert pair["localType"] in ("host", "prflx") and pair["remoteType"] in ("host", "prflx"), (
                    "run %d: the pair is not direct and local (a relay or a server-reflexive road): %r" % (n, pair))
                problem = opened_at_the_retry_problem(res["opened"], res["stalled"], MDNS_RETRY_MS)
                assert problem is None, "run %d: %s" % (n, problem)
                # This launch is right: its engine log is no evidence of anything.
                shutil.rmtree(log_dir, ignore_errors=True)
        finally:
            (evidence.files / "launches.json").write_text(json.dumps(launches, indent=1, default=str), encoding="utf-8")
    times = sorted(r["opened"] for r in runs)
    low, high = RETAIL_MEDIAN_WINDOW_MS
    with capsys.disabled():
        print("\n[measure] two connections on one page open in %r ms, median %s (retail on a fast machine %d-%d ms; not a verdict)"
              % (times, statistics.median(times), low, high))
