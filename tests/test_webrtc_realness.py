"""WebRTC realness regression tests.

Two layers, both runnable on GitHub CI:

* **unit** (`@pytest.mark.unit`) - pure SDP/candidate assertions against golden
  samples. No browser, no proxy, no network. These lock in every rule we found
  on 2026-06-06: host must be mDNS ``.local``; an srflx must carry a
  GENUINE nICEr priority (never ``local_pref == 0xFFFF``) and a
  stable, distinct foundation; CreepJS's resolver must return the egress, and a
  host-only SDP must read as "blocked". They run in the standard ``tests.yml``.

* **e2e** (`@pytest.mark.e2e`) - launch the patched binary and verify the live
  ICE gather. "Being behind a proxy" is faked WITHOUT any external proxy provider:
    - the "behind a TCP-only SOCKS proxy" condition is reproduced by a tiny
      in-process SOCKS5 server that relays TCP CONNECT but refuses UDP ASSOCIATE
      (exactly a residential TCP-only proxy → WebRTC's default-route UDP probe
      fails → exercises the Fix C fallback). No credentials, no external proxy.
  Excluded from the default run; a binary is located via ``STEALTHFOX_E2E_BINARY``
  (or the locally-built tree), else the test skips.
"""
from __future__ import annotations

import os
import re
import select
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler, HTTPServer

import pytest

from _evidence_on_failure import FailureEvidence
from _far_away_probes import FarAwayProbes
from _host_address import this_host_address
from _socks_servers import MEASURE_NUMBER, STUN_NUMBER, STUN_PORT, Socks5TcpOnly
from _udp_line import bench_report, declared_udp, declared_udp_or_absent, egress_by_proxy, measure_servers

# ──────────────────────────────────────────────────────────────────────────
#  Pure SDP / ICE-candidate helpers (no I/O) - the heart of the sentinels.
# ──────────────────────────────────────────────────────────────────────────
_CAND = re.compile(
    r"candidate:(?P<foundation>\S+)\s+(?P<component>\d+)\s+(?P<proto>UDP|TCP|udp|tcp)\s+"
    r"(?P<priority>\d+)\s+(?P<address>\S+)\s+(?P<port>\d+)\s+typ\s+(?P<typ>\w+)"
    r"(?:.*?raddr\s+(?P<raddr>\S+)\s+rport\s+(?P<rport>\d+))?"
)


def parse_candidate(line):
    """Parse one ``a=candidate:`` / ``candidate:`` line into a dict (or None)."""
    m = _CAND.search(line)
    if not m:
        return None
    d = m.groupdict()
    d["component"] = int(d["component"])
    d["priority"] = int(d["priority"])
    d["port"] = int(d["port"])
    d["proto"] = d["proto"].upper()
    if d["rport"] is not None:
        d["rport"] = int(d["rport"])
    return d


def decode_priority(prio):
    """Split a candidate priority into nICEr's fields (RFC 5245 layout that
    nICEr emits: type<<24 | iface<<16 | dir<<13 | stun<<8 | (256-component))."""
    return {
        "type_pref": (prio >> 24) & 0xFF,
        "iface_pref": (prio >> 16) & 0xFF,
        "local_pref": (prio >> 8) & 0xFFFF,
        "direction": (prio >> 13) & 0x7,
        "stun_priority": (prio >> 8) & 0x1F,
        "component": 256 - (prio & 0xFF),
    }


def is_mdns(addr):
    return bool(addr) and str(addr).endswith(".local")


def candidates(sdp_or_lines):
    if isinstance(sdp_or_lines, str):
        lines = re.findall(r"(?:a=)?candidate:[^\r\n]*", sdp_or_lines)
    else:
        lines = list(sdp_or_lines)
    return [c for c in (parse_candidate(l) for l in lines) if c]


def host_candidates(cands):
    return [c for c in cands if c["typ"] == "host"]


def srflx_candidates(cands):
    return [c for c in cands if c["typ"] == "srflx"]


def host_is_mdns(cands):
    """Every host candidate must be a ``<uuid>.local`` mDNS name, never a raw
    LAN IP (the §9.4 leak form that fails BrowserLeaks)."""
    hosts = host_candidates(cands)
    return bool(hosts) and all(is_mdns(c["address"]) for c in hosts)


def srflx_realness(cand, expected_ip=None):
    """Return (ok, reasons) for whether ``cand`` looks like a GENUINE nICEr UDP
    server-reflexive candidate. Encodes the 2026-06-06 findings."""
    reasons = []
    if cand["typ"] != "srflx":
        reasons.append("not a srflx candidate")
        return False, reasons
    if expected_ip is not None and cand["address"] != expected_ip:
        reasons.append(f"address {cand['address']} != expected {expected_ip}")
    p = decode_priority(cand["priority"])
    if p["type_pref"] != 100:
        reasons.append(f"type_pref {p['type_pref']} != 100 (SRV_RFLX)")
    if p["local_pref"] == 0xFFFF:
        reasons.append("local_pref == 0xFFFF - impossible nICEr value (the old hardcoded tell)")
    elif not (0x7000 <= p["local_pref"] < 0x8000):
        reasons.append(f"local_pref {p['local_pref']} outside the genuine ~0x7E00-0x7FFF band")
    if not (16 <= p["stun_priority"] <= 31):
        reasons.append(f"stun_priority {p['stun_priority']} implausible (expect 31-server_id)")
    if cand.get("raddr") not in (None, "0.0.0.0"):
        reasons.append(f"raddr {cand['raddr']} not redacted to 0.0.0.0")
    return (not reasons), reasons


def creep_get_ipaddress(sdp):
    """Faithful port of CreepJS's getIPAddress(sdp): connection line first, then
    the first candidate IP; '0.0.0.0' counts as blocked. Returns None if blocked
    - i.e. exactly what makes CreepJS render 'stun connection: blocked'."""
    blocked = "0.0.0.0"
    conn = (re.findall(r"c=IN\s.+\s", sdp) or [""])[0].strip().split(" ")
    conn_ip = conn[2] if len(conn) > 2 else ""
    if conn_ip and conn_ip != blocked:
        return conn_ip
    m = re.search(r"(udp|tcp)\s\w+\s([\w.:]+)(?=\s)", sdp, re.I)
    ip = m.group(2) if m else None
    return ip if (ip and ip != blocked) else None


# ──────────────────────────────────────────────────────────────────────────
#  Golden samples - real priority/foundation values, TEST-NET IPs (RFC 5737)
#  so no real address is ever committed (feedback_pre_push_privacy_check).
# ──────────────────────────────────────────────────────────────────────────
HOST_MDNS = "candidate:0 1 UDP 2122252543 1460e928-16b3-4c66-80ad-04abcdef0000.local 54551 typ host"
HOST_RAW_IP = "candidate:0 1 UDP 2122252543 192.168.1.20 54551 typ host"  # §9.4 leak form
VANILLA_SRFLX = "candidate:1 1 UDP 1685987327 203.0.113.50 3755 typ srflx raddr 0.0.0.0 rport 0"
OURS_SRFLX = "candidate:1 1 UDP 1686052863 203.0.113.7 58555 typ srflx raddr 0.0.0.0 rport 0"
# Pre-fix injection: local_pref hardcoded to 0xFFFF (priority 1694498815). The tell.
OLD_BAD_SRFLX = "candidate:2 1 UDP 1694498815 203.0.113.7 58555 typ srflx raddr 0.0.0.0 rport 0"

SDP_GOOD = (
    "v=0\r\nc=IN IP4 0.0.0.0\r\n"
    f"a={HOST_MDNS}\r\na={OURS_SRFLX}\r\n"
)
SDP_BLOCKED = "v=0\r\nc=IN IP4 0.0.0.0\r\n" f"a={HOST_MDNS}\r\n"  # host-only, no srflx


# ──────────────────────────────────────────────────────────────────────────
#  UNIT sentinels (run on GitHub CI)
# ──────────────────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_parse_and_decode_basics():
    c = parse_candidate(OURS_SRFLX)
    assert c["typ"] == "srflx" and c["proto"] == "UDP"
    assert c["address"] == "203.0.113.7" and c["raddr"] == "0.0.0.0" and c["rport"] == 0
    p = decode_priority(c["priority"])
    assert p["type_pref"] == 100 and p["stun_priority"] == 31 and p["component"] == 1


@pytest.mark.unit
def test_genuine_srflx_passes():
    for line in (VANILLA_SRFLX, OURS_SRFLX):
        ok, reasons = srflx_realness(parse_candidate(line), expected_ip=parse_candidate(line)["address"])
        assert ok, reasons


@pytest.mark.unit
def test_old_0xffff_srflx_is_rejected():
    """Fix A sentinel: local_pref == 0xFFFF must be flagged as fake."""
    ok, reasons = srflx_realness(parse_candidate(OLD_BAD_SRFLX))
    assert not ok
    assert any("0xFFFF" in r for r in reasons), reasons


@pytest.mark.unit
def test_host_must_be_mdns_not_raw_ip():
    """§9.4 sentinel: raw-IP host candidate is a leak; .local is required."""
    assert host_is_mdns(candidates([HOST_MDNS])) is True
    assert host_is_mdns(candidates([HOST_RAW_IP])) is False


@pytest.mark.unit
def test_srflx_foundation_distinct_from_host():
    """Fix B sentinel: srflx foundation must differ from the host foundations."""
    cands = candidates([HOST_MDNS, OURS_SRFLX])
    host_fnds = {c["foundation"] for c in host_candidates(cands)}
    srflx_fnds = {c["foundation"] for c in srflx_candidates(cands)}
    assert srflx_fnds and srflx_fnds.isdisjoint(host_fnds)


@pytest.mark.unit
def test_creep_resolver_returns_egress_when_srflx_present():
    assert creep_get_ipaddress(SDP_GOOD) == "203.0.113.7"


@pytest.mark.unit
def test_creep_resolver_reports_blocked_for_host_only():
    """The exact false-green we shipped: host-only (.local) SDP → no public IP
    → CreepJS shows 'blocked'. The resolver must return None here."""
    assert creep_get_ipaddress(SDP_BLOCKED) is None


@pytest.mark.unit
def test_mdns_host_is_invisible_to_creep_resolver():
    """A .local host must NOT be mis-read as an IP (the hyphen in the UUID is
    what makes CreepJS skip it and fall through to the srflx)."""
    assert creep_get_ipaddress("v=0\r\nc=IN IP4 0.0.0.0\r\n" f"a={HOST_MDNS}\r\n") is None


# ──────────────────────────────────────────────────────────────────────────
#  SHIPPED-BASELINE guard - the cheap unit test that would have caught the
#  2026-06-10 gap (baseline obfuscate=False, dead disableIPv6, orphan prefs).
#  These lock the shipped wrapper config to the manually-validated one so a
#  future edit / merge can't silently un-ship it. Run in tests.yml.
# ──────────────────────────────────────────────────────────────────────────
from invisible_core._fpforge import generate_profile  # noqa: E402
from invisible_core.prefs import translate_profile_to_prefs  # noqa: E402


@pytest.mark.unit
def test_shipped_webrtc_baseline_is_the_validated_config():
    prefs = translate_profile_to_prefs(generate_profile(seed=42), timezone="America/New_York")
    # host candidate must be mDNS .local like vanilla Firefox (manually
    # validated on BrowserLeaks/CreepJS through a residential proxy) - not a
    # raw LAN IP.
    assert prefs["media.peerconnection.ice.obfuscate_host_addresses"] is True
    # IPv6 is removed from ICE gathering, but NOT from a pref: since 2026-08-25
    # the native bridge reads a single source, the environment. The same-named
    # pref is no longer emitted - the bridge no longer read it and it would
    # have become an orphan, which is exactly what test_no_orphan_prefs_in_baseline
    # below forbids. Before there were two sources, and which one decided
    # depended on the presence of the proxy: with a proxy the environment won,
    # without a proxy the pref did.
    assert "zoom.stealth.webrtc.disable_ipv6" not in prefs
    assert "media.peerconnection.ice.disableIPv6" not in prefs
    # The single source is the environment (STEALTHFOX_WEBRTC_NETWORK, drawn from
    # the seed), declared with or without a proxy.
    from invisible_core import network_to_declare
    from invisible_core.launch import build_launch_env
    from invisible_playwright._session import build_env
    for build_fn in (lambda **k: build_launch_env({}, **k), build_env):
        env = build_fn(network_declared=network_to_declare(42), base_env={})
        assert env["STEALTHFOX_WEBRTC_NETWORK"] == network_to_declare(42)
    # peerconnection stays ON (a disabled WebRTC is itself a tell).
    assert prefs["media.peerconnection.enabled"] is True


@pytest.mark.unit
def test_no_orphan_prefs_in_baseline():
    """zoom.stealth.timezone / zoom.stealth.seed are read by NO C++ - they must
    not be written (juggler.timezone.override + zoom.stealth.fpp.hw_seed are the
    real ones). Guards against re-introducing a pref the binary ignores."""
    prefs = translate_profile_to_prefs(generate_profile(seed=42), timezone="America/Chicago")
    assert "zoom.stealth.timezone" not in prefs
    assert "zoom.stealth.seed" not in prefs
    assert prefs["juggler.timezone.override"] == "America/Chicago"
    assert "zoom.stealth.fpp.hw_seed" in prefs


# ──────────────────────────────────────────────────────────────────────────
#  Fake-proxy infrastructure for e2e: a tiny TCP-only SOCKS5 server (Socks5TcpOnly, in _socks_servers.py).
# ──────────────────────────────────────────────────────────────────────────
# The per-event probe a fingerprinting page runs (kept tiny; raw string = one escape level). Its STUN server is an argument: a test names one of its own
# (a server on this host, or a number nothing answers), never a public one.
_PROBE_JS = r"""async (stun) => {
  const pc = new RTCPeerConnection({iceCandidatePoolSize:1, iceServers:[{urls:[stun]}]});
  pc.createDataChannel('');
  const cands = [];
  pc.addEventListener('icecandidate', e => { if (e.candidate && e.candidate.candidate) cands.push(e.candidate.candidate); });
  await pc.setLocalDescription(await pc.createOffer({offerToReceiveAudio:1, offerToReceiveVideo:1}));
  // until the gather says it is complete (what the page reads is the whole of it), or 30 s: the deadline of the wait, not a
  // window the verdict depends on
  await new Promise(resolve => {
    const timer = setTimeout(resolve, 30000);
    const done = () => { if (pc.iceGatheringState === 'complete') { clearTimeout(timer); resolve(); } };
    pc.addEventListener('icegatheringstatechange', done);
    done();
  });
  const sdp = (pc.localDescription && pc.localDescription.sdp) || '';
  try { pc.close(); } catch(e) {}
  return { candidates: cands, sdp };
}"""

_FAKE_EGRESS = "203.0.113.7"  # RFC 5737 TEST-NET-3


def _e2e_binary():
    # Honor both env vars so the whole e2e suite targets one binary from a single
    # setting (INVPW_BINARY_PATH is what conftest's firefox_binary uses).
    cand = os.environ.get("STEALTHFOX_E2E_BINARY") or os.environ.get("INVPW_BINARY_PATH")
    if cand and os.path.exists(cand):
        return cand
    built = r"C:\ff\source\obj-x86_64-pc-windows-msvc\dist\bin\firefox.exe"
    if os.path.exists(built):
        return built
    return None


def _launch(**extra):
    from invisible_playwright import InvisiblePlaywright

    kw = {"headless": True,
          # Fixed zone so the wrapper does NOT run timezone="auto" egress
          # discovery through the (fake) proxy - irrelevant here, we inject the
          # egress IP directly and want the launch deterministic/offline.
          "timezone": "America/New_York",
          # Where the core measures the UDP exit of a SOCKS5 proxy from (an explicit launch parameter, never the public
          # defaults in a hermetic test): a documentation address only the relay of a test gateway answers.
          "udp_stun_servers": ["%s:%d" % (MEASURE_NUMBER, STUN_PORT)],
          "extra_prefs": {"media.peerconnection.ice.obfuscate_host_addresses": True}}
    kw.update(extra)
    return InvisiblePlaywright(**kw)


def _launch_behind(gateway, **extra):
    """The launch behind a test gateway: its proxy, and the STUN server its relay answers for the measurement of its UDP
    exit (``udp_stun_servers``). ONE place for every e2e that needs the relay."""
    return _launch(proxy=gateway.proxy, udp_stun_servers=measure_servers(gateway), **extra)


@pytest.mark.e2e
@pytest.mark.usefixtures("the_udp_line")
def test_the_gather_is_real_without_a_proxy(local_https_page):
    """No proxy: the engine keeps the real network. The gather completes, the
    host is mDNS, and an srflx, when one is born, is genuine (Fix A/B) and
    resolvable by the resolver of a studied system (not blocked)."""
    binary = _e2e_binary()
    if not binary:
        pytest.skip("no patched binary (set STEALTHFOX_E2E_BINARY)")
    from _socks_servers import FakeStun

    # The STUN server is the test's own, on the address of this host (a real one is a third party and the runner's network): the engine's UDP is
    # real, the server answers with the address it is reached at, and a srflx born from it is judged as before.
    try:
        host = this_host_address()
    except pytest.skip.Exception:       # no default route: the same gather against a server on loopback (no srflx is born from it)
        host = "127.0.0.1"
    stun = FakeStun(host, bind_host=host)
    try:
        with _launch(binary_path=binary) as browser:
            page = browser.new_context().new_page()
            page.goto(local_https_page, wait_until="domcontentloaded", timeout=60000)
            res = page.evaluate(_PROBE_JS, "stun:%s:%d" % stun.address)
    finally:
        stun.close()
    cands = candidates(res["candidates"])
    assert cands, "ICE produced ZERO candidates (blocked)"
    assert host_is_mdns(cands), [c["address"] for c in host_candidates(cands)]
    srflx = srflx_candidates(cands)
    for c in srflx:
        ok, reasons = srflx_realness(c, expected_ip=c["address"])
        assert ok, reasons
    # Two srflx for the same base must share ONE stable foundation (Fix B).
    assert len({c["foundation"] for c in srflx}) <= 1


@pytest.mark.e2e
def test_not_blocked_behind_tcp_only_socks(socks5_tcp_only, local_https_page):
    """Behind a TCP-only SOCKS proxy on a remote origin, ICE must still complete
    (host .local candidates), not return zero candidates: a fully blocked
    WebRTC is itself a tell. The session declares its network from the seed
    (STEALTHFOX_WEBRTC_NETWORK); that variable is read by the new engine only."""
    binary = _e2e_binary()
    if not binary:
        pytest.skip("no patched binary (set STEALTHFOX_E2E_BINARY)")
    proxy = {"server": f"socks5://127.0.0.1:{socks5_tcp_only.port}"}
    # The page is the test's own, carried THROUGH the proxy (the local address is let through the proxy by the two prefs, as the declared-network tests
    # do): a real site is a third party, and a proxy or network that fails is a failure here, not a skip. The STUN server is a number nothing answers;
    # behind a TCP-only proxy no UDP leaves at all.
    prefs = {"network.proxy.allow_hijacking_localhost": True, "network.proxy.no_proxies_on": ""}
    with _launch(binary_path=binary, proxy=proxy, extra_prefs=prefs) as browser:
        page = browser.new_context().new_page()
        page.goto(local_https_page, wait_until="domcontentloaded", timeout=70000)
        res = page.evaluate(_PROBE_JS, "stun:%s:%d" % (STUN_NUMBER, STUN_PORT))
    cands = candidates(res["candidates"])
    assert cands, "behind SOCKS the gather returned ZERO candidates (blocked)"
    assert host_is_mdns(cands)


# The gather the next tests read: one STUN server, by number, and everything the engine says about it.
_RELAY_PROBE_JS = r"""async (stun) => {
  const t0 = performance.now();
  // What the page saw, with its time (``ms`` from the offer, ``wall`` the epoch clock the test's own record shares): the evidence of a
  // failed gather. It is also left on ``window`` so that a test whose evaluation did not return can still read it. It changes nothing.
  const events = window.__relayEvents = [];
  const note = (kind, data) => {
    try { events.push(Object.assign({kind, ms: Math.round((performance.now() - t0) * 10) / 10, wall: Date.now()}, data || {})); } catch (e) {}
  };
  const pc = new RTCPeerConnection({iceServers: [{urls: [stun]}]});
  pc.createDataChannel('');
  const cands = [], errors = [], at = {};
  note('start', {state: pc.iceGatheringState});
  pc.addEventListener('icegatheringstatechange', () => note('icegatheringstatechange', {state: pc.iceGatheringState}));
  pc.addEventListener('icecandidate', e => {
    try {
      const c = e.candidate;
      note('icecandidate', c ? {candidate: c.candidate, type: c.type, protocol: c.protocol, address: c.address, port: c.port,
                                relatedAddress: c.relatedAddress, relatedPort: c.relatedPort, url: e.url} : {end: true});
    } catch (x) {}
    if (e.candidate && e.candidate.candidate) {
      cands.push(e.candidate.candidate);
      const typ = (e.candidate.candidate.match(/ typ (\w+)/) || [])[1];
      if (typ && !(typ in at)) at[typ] = Math.round(performance.now() - t0);
    }
  });
  pc.addEventListener('icecandidateerror', e => {
    note('icecandidateerror', {errorCode: e.errorCode, errorText: e.errorText, url: e.url, address: e.address, port: e.port});
    errors.push({code: e.errorCode, text: e.errorText, url: e.url}); at.error = Math.round(performance.now() - t0);
  });
  await pc.setLocalDescription(await pc.createOffer());
  note('setLocalDescription returned', {state: pc.iceGatheringState});
  await new Promise(resolve => {
    const timer = setTimeout(() => { note('the 30 s wait for the end of the gathering ran out'); resolve(); }, 30000);
    pc.addEventListener('icegatheringstatechange', () => {
      if (pc.iceGatheringState === 'complete') { at.complete = Math.round(performance.now() - t0); clearTimeout(timer); resolve(); }
    });
  });
  const sdp = (pc.localDescription && pc.localDescription.sdp) || '';
  note('closing', {state: pc.iceGatheringState});
  try { pc.close(); } catch (e) {}
  return {candidates: cands, errors, sdp, at_ms: at, events};
}"""


# The two prefs that decide where ICE runs are LOCKED to the retail value by the engine (toolkit/xre/nsAppRunner.cpp, after the user
# prefs are loaded): a caller that asks for the ICE in the process of the page, or for no socket process, gets the socket process all the
# same, and the srflx is born through the relay. Both rows below are such callers; the first is the default.
_ICE_PREFS = [
    {},                                                          # the engine's default: ICE in the socket process
    {"media.peerconnection.mtransport_process": False},          # a caller asks for the ICE in the process of the page: locked
    {"network.process.enabled": False},                          # a caller asks for no socket process: locked
]
_ICE_IDS = ["ice_in_the_socket_process", "the_caller_asks_for_ice_in_the_content_process_and_the_lock_wins",
            "the_caller_asks_for_no_socket_process_and_the_lock_wins"]


# What a failed gather leaves for a reader (tests/_evidence_on_failure.py): where the page's STUN request went. Nothing here changes an
# assertion, a wait or a deadline: the gateway of the test and its sockets are listened to, never steered.
class _Journal:
    """A record the evidence saves as ``<name>.jsonl`` (``lock``, ``records``). ``t`` is the wall clock (seconds since the epoch), ``m`` the
    ``perf_counter`` of this process; a record that carries its own time says ``m=None``."""

    def __init__(self):
        self.lock = threading.Lock()
        self.records = []

    def add(self, **fields):
        """Never raises: it is called from the gateway's own threads, and a failure there would be the test's, not the product's.
        Who wrote a record is ``origin`` (gateway, socket, test, page), never ``source``: that is a field of a datagram."""
        try:
            fields.setdefault("t", round(time.time(), 4))
            fields.setdefault("m", round(time.perf_counter(), 6))
            with self.lock:
                self.records.append(fields)
        except Exception as problem:  # noqa: BLE001 - recorded, never raised
            with self.lock:
                self.records.append({"origin": "journal", "event": "a record could not be kept", "error": repr(problem)})


def _interfaces_of_this_runner(relay_host):
    """Every address this runner has, as the several ways to ask give it (a failure of one is said, not raised): what the hostname
    resolves to, the interfaces of ``psutil`` when it is installed, and the system's own listing (``ipconfig /all`` or ``ip addr``)."""
    found = {"relay_host_this_host_address": relay_host}
    try:
        name = socket.gethostname()
        found["hostname"] = name
        found["getaddrinfo_of_the_hostname"] = sorted({"%s %s" % (info[0].name, info[4][0]) for info in socket.getaddrinfo(name, None)})
    except Exception as problem:
        found["getaddrinfo_error"] = repr(problem)
    try:
        import psutil
        found["psutil_net_if_addrs"] = {nic: [{"family": a.family.name, "address": a.address, "netmask": a.netmask} for a in addrs]
                                       for nic, addrs in psutil.net_if_addrs().items()}
    except Exception as problem:
        found["psutil"] = "not read: %r" % (problem,)
    command = ["ipconfig", "/all"] if sys.platform == "win32" else ["ip", "-o", "addr"]
    try:
        done = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=30)
        found["listing_command"] = " ".join(command)
        found["listing"] = done.stdout[:30000]
    except Exception as problem:
        found["listing_error"] = "%s: %r" % (" ".join(command), problem)
    return found


class _Watch:
    """The record of one gather behind a gateway of the test, kept only when ``evidence`` (a ``FailureEvidence``) is given; with none every
    method is the plain call. It wraps the OBJECT of the gateway and the ``socket`` name of ``tests/_socks_servers.py`` (not the file), so that
    the failed gather says, with times, for each control connection what the client sent and what the gateway answered (the greeting, the
    authentication, the ASSOCIATE with the address the client declared, the BND address and port), and for each datagram on the relay and on
    the exit sockets where it came from, where it went and whether the gateway skipped, dropped or carried it. The Gateway has no source
    filter: it takes the datagram of any address and answers the last one it heard (the cells say so)."""

    def __init__(self, gateway, monkeypatch, evidence, relay_host):
        self.gateway = gateway
        self.evidence = evidence
        self.active = evidence is not None
        self.relay_host = relay_host
        if not self.active:
            return
        self.cells = evidence.cells if evidence.cells is not None else {}
        evidence.note_cells(self.cells)
        self.control, self.udp, self.page = _Journal(), _Journal(), _Journal()
        evidence.note_records("gateway-control-connections", self.control)
        evidence.note_records("gateway-udp-datagrams", self.udp)
        evidence.note_records("page-view", self.page)
        # The recorders of the machine and of the core's own measurement (tests/_far_away_probes.py): started with the watch, stopped with it.
        self.probes = FarAwayProbes(gateway.t0)
        evidence.note_records("stall-witness", self.probes.stall_records)
        evidence.note_records("runner-load", self.probes.load_records)
        evidence.note_records("core-udp-exit-log", self.probes.core_records)
        self.bnd_ports = set()
        self._reads = threading.local()
        self.cells["relay_host"] = relay_host
        self._wrap_the_gateway(monkeypatch)
        self._wrap_the_sockets(monkeypatch)

    def __enter__(self):
        if self.active:
            self.safely(self.probes.start)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.active:
            self.safely(self.probes.stop)
            try:
                self.snapshot()
            except Exception as problem:      # the evidence never hides the failure it is about
                sys.stderr.write("gateway evidence: could not be taken: %r\n" % (problem,))
            if exc is not None:               # a failure that is not an assertion of the test (a wait, a launch) says it too
                self.safely(lambda: exc.add_note(self.summary()))
        return False

    def mark(self, what):
        """A moment of the test on the scale of the core's records (the launch that runs the core's UDP measurement starts, returns)."""
        if self.active:
            self.safely(lambda: self.probes.mark(what))

    def summary(self):
        """The lines a failed gather adds to its message: the control connections' times, the core's UDP measurement, the stalls of the
        process and the runner's load (the evidence has them whole). Empty when no evidence is kept. Never raises."""
        if not self.active:
            return ""
        try:
            with self.control.lock:
                records = list(self.control.records)
            return self.probes.lines(records)
        except Exception as problem:  # noqa: BLE001
            return "far-away probes: not read (%r)" % (problem,)

    # ---- the clocks ---------------------------------------------------------------------------------------------------------
    def _gw(self):
        """Seconds from the start of the gateway (``Gateway.t0``): the scale of its ``requests``, ``accepted``, ``associations`` and ``datagram_log``."""
        return round(time.monotonic() - self.gateway.t0, 4)

    def _conn(self):
        return getattr(self.gateway._local, "index", -1)

    def _kind_of(self, ip):
        if ip.startswith("127."):
            return "loopback"
        if ip == self.relay_host:
            return "the host address of the runner (this_host_address(), where the relay listens)"
        return "another address"

    # ---- the control connections --------------------------------------------------------------------------------------------------
    def safely(self, record):
        """Runs ``record``, a recording (it builds a record and adds it): it NEVER raises, whatever it reads or formats, and a failure of it
        is itself a record (``origin: watch``). Every recording of every wrapper goes through here, and the call of the original (the
        gateway's own method, the real socket) is made outside it, with its own arguments, so that what the gateway returns or raises is
        what it did before and a datagram is never lost to the test's own bookkeeping."""
        try:
            record()
        except Exception as problem:  # noqa: BLE001 - the recording failed: say so, and go on
            try:
                self.control.add(origin="watch", event="a record could not be made", error=repr(problem))
            except Exception:  # noqa: BLE001 - not even that could be kept: nothing is raised into the gateway's thread
                pass

    def _wrap_the_gateway(self, monkeypatch):
        gateway, control, reads, watch = self.gateway, self.control, self._reads, self
        import _socks_servers as servers

        original_recv_exact = servers._recv_exact

        def keep_the_read(data):
            sink = getattr(reads, "sink", None)
            if sink is not None:
                sink.append(data)

        def recv_exact(s, n):
            data = original_recv_exact(s, n)
            watch.safely(lambda: keep_the_read(data))
            return data

        monkeypatch.setattr(servers, "_recv_exact", recv_exact)

        original_handle, original_greet = gateway._handle, gateway._greet
        original_read_address = type(gateway)._read_address
        original_send, original_connect = gateway._send, gateway._connect
        original_associate, original_route, original_deliver = gateway._associate, gateway._route, gateway._deliver

        def note_accepted(c):
            try:
                peer, local = c.getpeername(), c.getsockname()
            except OSError as problem:
                peer = local = repr(problem)
            control.add(origin="gateway", event="control connection accepted", conn=watch._conn(), gw=watch._gw(),
                        client=str(peer), gateway_end=str(local))

        def note_closed(started):
            control.add(origin="gateway", event="control connection closed", conn=watch._conn(), gw=watch._gw(),
                        lasted_s=round(time.monotonic() - started, 4), bytes_read_from_the_client_hex=[r.hex() for r in reads.sink])

        def handle(c):
            reads.sink, reads.address, reads.cmd = [], None, None
            started = time.monotonic()
            watch.safely(lambda: note_accepted(c))
            try:
                return original_handle(c)
            finally:
                watch.safely(lambda: note_closed(started))

        def note_greeting(ok):
            sink = reads.sink
            record = {"origin": "gateway", "event": "greeting and authentication done", "ok": ok, "conn": watch._conn(), "gw": watch._gw(),
                      "gateway_wants_credentials": gateway.user is not None,
                      "methods_offered_by_the_client": list(sink[1])}
            if gateway.user is not None and len(sink) >= 7:
                record["user_sent"] = sink[4].decode("utf-8", "replace")
                record["password_length_sent"] = len(sink[6])
            control.add(**record)

        def greet(c):
            ok = original_greet(c)
            watch.safely(lambda: note_greeting(ok))
            return ok

        def note_request(host, port):
            header = next((r for r in reversed(reads.sink) if len(r) == 3 and r[0] == 5), b"")
            reads.cmd = {1: "CONNECT", 3: "UDP ASSOCIATE"}.get(header[1], "command %d" % header[1]) if len(header) == 3 else "unknown"
            reads.address = "%s:%d" % (host, port)
            control.add(origin="gateway", event="request", command=reads.cmd, address_declared_by_the_client=reads.address,
                        conn=watch._conn(), gw=watch._gw())

        def read_address(c):
            host, port = original_read_address(c)
            watch.safely(lambda: note_request(host, port))
            return host, port

        def connect(c, host, port):
            watch.safely(lambda: control.add(origin="gateway", event="CONNECT handled", target="%s:%d" % (host, port),
                                             conn=watch._conn(), gw=watch._gw()))
            return original_connect(c, host, port)

        def associate(c):
            watch.safely(lambda: control.add(origin="gateway", event="UDP ASSOCIATE handled",
                                             address_declared_by_the_client=getattr(reads, "address", None),
                                             associations_so_far=len(gateway.associations), conn=watch._conn(), gw=watch._gw()))
            return original_associate(c)

        def note_answer(data, asked, kind, problem):
            record = {"origin": "gateway", "event": "answer sent" if problem is None else "answer could not be sent", "kind": kind,
                      "hex": data.hex(), "conn": watch._conn(), "asked_gw": round(asked - gateway.t0, 4), "gw": watch._gw()}
            if problem is not None:
                record["error"] = repr(problem)
            elif len(data) == 2:
                record["what"] = ("method selection (0x00 none, 0x02 user/password, 0xff none acceptable)" if data[0] == 5 else
                                  "authentication status (0x00 accepted)")
                record["value"] = data[1]
            elif len(data) == 10 and data[0] == 5 and data[3] == 1:
                host, port = socket.inet_ntoa(data[4:8]), struct.unpack("!H", data[8:10])[0]
                record.update(what="reply (REP 0 = succeeded)", rep=data[1], bnd="%s:%d" % (host, port))
                if kind == "associate" and data[1] == 0:
                    watch.bnd_ports.add(port)
            control.add(**record)

        def send(c, data):
            asked = time.monotonic()
            kind = getattr(gateway._local, "kind", None)
            try:
                original_send(c, data)
            except BaseException as problem:
                watch.safely(lambda problem=problem: note_answer(data, asked, kind, problem))
                raise
            watch.safely(lambda: note_answer(data, asked, kind, None))

        def note_route(host, dport, atyp, payload, out_port, routed):
            record = {"origin": "gateway", "event": "datagram dropped by the route (nothing is sent on)" if routed is None else "datagram routed",
                      "gw": watch._gw(), "destination_in_the_header": "%s:%d" % (host, dport), "atyp": atyp, "payload_len": len(payload),
                      "exit_socket_port": out_port}
            if routed is not None:
                record["sent_on_to"] = "%s:%d" % tuple(routed[0][:2])
                record["answer_will_be_reported_as"] = None if routed[1] is None else "%s:%d" % tuple(routed[1][:2])
            watch.udp.add(**record)

        def route(host, dport, atyp, payload, out_port, answer):
            routed = original_route(host, dport, atyp, payload, out_port, answer)
            watch.safely(lambda: note_route(host, dport, atyp, payload, out_port, routed))
            return routed

        def deliver(relay, to, host, port, payload):
            watch.safely(lambda: watch.udp.add(origin="gateway", event="answer handed to the relay socket for the client", gw=watch._gw(),
                                               to=str(to), reported_as_coming_from="%s:%d" % (host, port), payload_len=len(payload),
                                               payload_head_hex=payload[:2].hex()))
            return original_deliver(relay, to, host, port, payload)

        for name, function in (("_handle", handle), ("_greet", greet), ("_read_address", read_address), ("_send", send),
                               ("_connect", connect), ("_associate", associate), ("_route", route), ("_deliver", deliver)):
            setattr(gateway, name, function)

    # ---- the datagrams ----------------------------------------------------------------------------------------------------------
    def _wrap_the_sockets(self, monkeypatch):
        """The datagram sockets the gateway opens from now on (the relay of each association and its exit socket) report every ``recvfrom``
        and ``sendto``: the ``socket`` name of ``tests/_socks_servers.py`` stands for a module whose ``socket`` class does."""
        watch, gateway, real = self, self.gateway, socket
        import _socks_servers as servers

        def address_of(pair):
            return "%s:%d" % tuple(pair[:2])           # an IPv6 address is a 4-tuple

        class Listening(real.socket):
            def _role(self):
                try:
                    local = self.getsockname()
                except OSError:
                    return None, "closed"
                if local[1] in gateway.exit_ports:
                    return local, "exit socket (leaves for the world)"
                if local[1] in watch.bnd_ports:
                    return local, "relay socket (the BND address the client was told)"
                return local, "datagram socket"

            def _note_reset(self, problem):
                if self.type == real.SOCK_DGRAM:
                    watch.udp.add(origin="socket", event="recvfrom raised ConnectionResetError", gw=watch._gw(), error=repr(problem),
                                  local=str(self._role()[0]))

            def _note_received(self, data, peer):
                if self.type != real.SOCK_DGRAM or not data:
                    return
                local, role = self._role()
                record = {"origin": "socket", "event": "datagram received", "gw": watch._gw(), "on": role,
                          "local": address_of(local) if local else None, "source": address_of(peer), "source_kind": watch._kind_of(peer[0]),
                          "len": len(data), "head_hex": data[:24].hex()}
                if role.startswith("relay"):
                    skipped = len(data) < 10 or data[2] != 0
                    record["skipped_by_the_gateway"] = skipped
                    record["why"] = ("shorter than 10 bytes" if len(data) < 10 else "FRAG is not 0") if skipped else None
                    record["source_refused_as_foreign"] = False     # the Gateway has no source filter: see the cells
                watch.udp.add(**record)

            def _note_send_failed(self, data, args, problem):
                if self.type == real.SOCK_DGRAM and data:
                    watch.udp.add(origin="socket", event="sendto failed", gw=watch._gw(), error=repr(problem), destination=str(args[-1]),
                                  len=len(data), local=str(self._role()[0]))

            def _note_sent(self, data, args):
                if self.type != real.SOCK_DGRAM or not data:
                    return
                local, role = self._role()
                watch.udp.add(origin="socket", event="datagram sent", gw=watch._gw(), on=role, local=address_of(local) if local else None,
                              destination=address_of(args[-1]), len=len(data), head_hex=data[:24].hex())

            def recvfrom(self, *args):
                try:
                    data, peer = super().recvfrom(*args)
                except ConnectionResetError as problem:
                    watch.safely(lambda problem=problem: self._note_reset(problem))
                    raise
                watch.safely(lambda: self._note_received(data, peer))
                return data, peer

            def sendto(self, data, *args):
                try:
                    sent = super().sendto(data, *args)
                except OSError as problem:
                    watch.safely(lambda problem=problem: self._note_send_failed(data, args, problem))
                    raise
                watch.safely(lambda: self._note_sent(data, args))
                return sent

        class Module:
            socket = Listening

            def __getattr__(self, name):
                return getattr(real, name)

        monkeypatch.setattr(servers, "socket", Module())

    # ---- the launch and the page ------------------------------------------------------------------------------------------------
    def arm(self):
        """Before the browser is launched: the engine's trace and the Juggler wire."""
        if self.active:
            self.evidence.arm()

    def launched(self, session):
        """What the core declared for this session, read from the object the launch made."""
        if not self.active:
            return
        seed = getattr(session, "seed", None)
        self.cells["seed"] = seed
        try:
            from invisible_core import network_to_declare
            self.cells["network_the_core_declares_to_the_engine (STEALTHFOX_WEBRTC_NETWORK)"] = network_to_declare(seed)
        except Exception as problem:
            self.cells["network_the_core_declares_to_the_engine"] = "not read: %r" % (problem,)

    def _note_page_events(self, events):
        for event in events:
            self.page.add(**dict(event, origin="page", t=event.get("wall", 0) / 1000.0, m=None))

    def evaluate(self, page, script, argument):
        """``page.evaluate`` and, when the evidence is kept, the page's own view of the gather as ``page-view.jsonl``."""
        if not self.active:
            return page.evaluate(script, argument)
        self.safely(lambda: self.page.add(origin="test", event="page.evaluate of the gather starts", stun=argument,
                                          wall_ms=round(time.time() * 1000), gw=self._gw()))
        try:
            res = page.evaluate(script, argument)
        except BaseException as problem:
            self.safely(lambda problem=problem: self.page.add(origin="test", event="page.evaluate raised", error=repr(problem), gw=self._gw()))
            try:
                events = page.evaluate("() => window.__relayEvents || []")
                self.safely(lambda: self._note_page_events(events))
            except Exception as again:
                self.safely(lambda again=again: self.page.add(origin="test", event="the page's events could not be read back", error=repr(again)))
            raise
        self.safely(lambda: self.page.add(origin="test", event="page.evaluate of the gather returned", wall_ms=round(time.time() * 1000),
                                          gw=self._gw()))
        self.safely(lambda: self._note_page_events(res.get("events", [])))
        self.safely(lambda: self.cells.__setitem__("page_result", {key: value for key, value in res.items() if key != "events"}))
        return res

    def snapshot(self):
        """The gateway's own lists, taken before it closes (times in seconds from its start; ``wall0`` is that instant on the wall clock)."""
        gateway = self.gateway
        with self.control.lock:
            control = list(self.control.records)
        self.safely(lambda: self.cells.update(self.probes.cells(control)))
        self.cells["gateway"] = {
            "wall0_epoch_seconds_of_the_gateway_start": gateway.wall0,
            "udp": gateway.udp, "wants_credentials": gateway.user is not None, "rtt_s": gateway.rtt,
            "relay_host (BND.ADDR it answers)": gateway.relay_host, "exit_host (source of the datagrams it sends on)": gateway.exit_host,
            "fake_stun_address": list(gateway.stun.address), "fake_stun_requests_answered": gateway.stun.requests,
            "tcp_exit": gateway.tcp_exit,
            "accepted_control_connections_s": list(gateway.accepted),
            "requests (s, connection, kind, detail)": [list(r) for r in gateway.requests],
            "connects": [list(c) for c in gateway.connects],
            "associates": gateway.associates,
            "associations (start, end in s)": [list(a) for a in gateway.associations],
            "exit_ports": list(gateway.exit_ports),
            "bnd_ports_told_to_the_client": sorted(self.bnd_ports),
            "datagram_log (host, port, atyp, s)": [list(d) for d in gateway.datagram_log],
            "datagram_via (host, port, association, s)": [list(d) for d in gateway.datagram_via],
            "datagrams_carried (host, port, payload length, head hex)": [[h, p, len(d), d[:24].hex()] for h, p, d in gateway.datagrams],
            "send_errors": list(gateway.send_errors),
            "source_filter": ("none: Gateway._associate keeps the source address of the last well-formed datagram as the client and answers "
                              "it; no datagram is refused as foreign by its source address. What it does not carry: a datagram shorter than "
                              "10 bytes or with FRAG != 0 (skipped), one the route drops (stun_answers False, forward_only_to); both are in "
                              "gateway-udp-datagrams.jsonl."),
        }
        try:
            self.cells["declared_udp_of_the_core_for_this_proxy"] = declared_udp_or_absent(gateway.proxy)
        except Exception as problem:
            self.cells["declared_udp_of_the_core_for_this_proxy"] = "not read: %r" % (problem,)


def _gather_behind_a_coherent_gateway(monkeypatch, credentials, ice_prefs, rtt_ms, evidence=None):
    """Launch the default engine behind a local gateway with coherent UDP, `rtt_ms` away, gather with a STUN server only
    its relay reaches, and say whether the page's request took the relay and what the gather looked like. With `evidence` (a
    `FailureEvidence`) the gateway, its sockets, the page and the launch are also recorded for it (`_Watch`); nothing else differs."""
    binary = _e2e_binary()
    if not binary:
        pytest.skip("no patched binary (set STEALTHFOX_E2E_BINARY)")
    from invisible_core import _geo

    from _socks_servers import STUN_NUMBER, STUN_PORT, coherent
    stun = f"stun:{STUN_NUMBER}:{STUN_PORT}"
    user, password = credentials or (None, None)
    # The relay sits on the address of this host, as a real gateway's does: the engine's ICE sockets are bound to an
    # interface address, and on Windows such a socket cannot send to a relay on loopback.
    relay_host = this_host_address()
    with coherent(user=user, password=password, relay_host=relay_host, rtt_ms=rtt_ms) as gateway,             _Watch(gateway, monkeypatch, evidence, relay_host) as watch:
        exit_ip = gateway.tcp_exit
        # The session asks the gateway for its TCP exit over HTTPS, which no local gateway can answer as itself: the
        # answer is the gateway's. The UDP probe that follows is NOT replaced: it runs through the relay, for real.
        monkeypatch.setattr(_geo, "discover_egress_ip", egress_by_proxy(gateway))
        extra_prefs = {"media.peerconnection.ice.obfuscate_host_addresses": True, **ice_prefs}
        watch.arm()
        launched = _launch_behind(gateway, binary_path=binary, extra_prefs=extra_prefs)
        watch.launched(launched)
        watch.mark("launch starts")
        with launched as browser:
            watch.mark("launch returned")
            page = browser.new_context().new_page()
            page.goto("about:blank")
            res = watch.evaluate(page, _RELAY_PROBE_JS, stun)
        carried = [(host, port) for host, port, _payload in gateway.datagrams]
        stun_requests = gateway.stun.requests
        associates = gateway.associates
        # What the bench saw, taken before the gateway closes: its send errors, CONNECTs, accepted connections, associations and the
        # datagrams with their times, and what the core declared for this proxy (absent when its measure never finished).
        bench = bench_report(gateway, rtt_ms, declared_udp_or_absent(gateway.proxy))
    srflx = [c for c in srflx_candidates(candidates(res["candidates"])) if c["address"] == exit_ip]
    verdict = (f"srflx with the gateway's exit {exit_ip}: {srflx}; candidates: {res['candidates']}; "
               f"ICE errors: {res['errors']}; gather timings (ms from the offer): {res['at_ms']}; "
               f"datagrams the relay carried: {carried}; STUN requests the server answered: {stun_requests}; "
               f"UDP ASSOCIATEs the gateway saw: {associates}; round trip to the gateway: {rtt_ms} ms; {bench}")
    probes = watch.summary()
    if probes:
        verdict += "\n" + probes
    return bool(srflx) and (STUN_NUMBER, STUN_PORT) in carried, verdict, res["at_ms"]


@pytest.mark.e2e
@pytest.mark.parametrize("credentials", [None, ("alice", "s3cret"), ("user~tcp-windows-65535", "not-a-real-password")],
                         ids=["no_auth", "user_and_password", "user_with_a_tilde"])
@pytest.mark.parametrize("ice_prefs", _ICE_PREFS, ids=_ICE_IDS)
@pytest.mark.usefixtures("the_udp_line")
def test_a_gateway_with_coherent_udp_gives_the_page_a_srflx_born_through_its_relay(monkeypatch, credentials, ice_prefs):
    """The default engine, a SOCKS5 gateway that relays UDP and whose UDP exit is its TCP exit: the page's STUN request
    goes THROUGH the relay and the srflx it gets is the gateway's exit.

    ICE runs in the socket process (the sockets are NrSocket's, where the layer is attached) whatever the caller asks:
    `media.peerconnection.mtransport_process` and `network.process.enabled` are locked to their retail value by the engine,
    so a caller who sets either to false (rows 2 and 3) gets the same srflx through the relay as the default. Each with a
    gateway that wants no credentials and with one that does. A request that does not take the relay ends in an ICE
    error 701 and no srflx; the failure message says what the gateway saw.

    The gateway is local (tests/_socks_servers.py): the TCP exit and the UDP exit it reports are documentation addresses,
    and the STUN server the page is given is a documentation address too, so the only road to it is the relay. The page
    reaches the proxy by the session the Juggler holds, as every page does; nothing here sets a pref of the engine."""
    took_the_relay, verdict, _timings = _gather_behind_a_coherent_gateway(monkeypatch, credentials, ice_prefs, rtt_ms=0)
    assert took_the_relay, "the page's STUN request did not take the relay. " + verdict


@pytest.mark.e2e
@pytest.mark.usefixtures("the_udp_line")
def test_a_host_with_ipv6_addresses_gathers_through_an_ipv4_relay(monkeypatch):
    """The ICE sockets are opened with the family of the host candidate they carry (NrSocket::create, PR_OpenUDPSocket
    with the address's family): an IPv6 host gets AF_INET6 sockets next to its AF_INET ones, while the relay of the
    gateway is IPv4. With the host addresses in the clear, the IPv4 STUN server must still be answered through the relay."""
    took_the_relay, verdict, _timings = _gather_behind_a_coherent_gateway(
        monkeypatch, ("alice", "s3cret"), {"media.peerconnection.ice.obfuscate_host_addresses": False}, 0)
    print("[families] " + verdict)
    assert took_the_relay, "the page's STUN request did not take the relay. " + verdict


@pytest.mark.e2e
@pytest.mark.parametrize("rtt_ms", [80, 150, 250])
@pytest.mark.parametrize("ice_prefs", _ICE_PREFS, ids=_ICE_IDS)
@pytest.mark.usefixtures("the_udp_line")
def test_a_gateway_that_is_far_away_still_gives_the_page_a_srflx_born_through_its_relay(monkeypatch, ice_prefs, rtt_ms):
    """The same gather with a gateway that is not on loopback: every answer on the control connection waits `rtt_ms`
    and the relay adds half of it each way. The layer opens one association per ICE socket, one after the other, each
    with a connect, a greeting, an authentication and the ASSOCIATE; a gather must not be over before they are done."""
    ice_id = _ICE_IDS[_ICE_PREFS.index(ice_prefs)]
    # When it fails it saves, under `invpw-evidence/webrtc-far-away-<ice row>-<rtt>/`, where the page's STUN request went: `cells.json` (the
    # gateway's own lists, the page's result, `timings_ms_from_the_offer`, the verdict, the addresses of the runner and the network the core
    # declares), `gateway-control-connections.jsonl`, `gateway-udp-datagrams.jsonl`, `page-view.jsonl`, `juggler-wire.jsonl` and
    # `juggler-engine-trace/`. The assertion below is the one it always was.
    with FailureEvidence("webrtc-far-away-%s-%d" % (ice_id, rtt_ms)) as evidence:
        cells = {"test": "far-away gateway", "rtt_ms": rtt_ms, "ice_prefs": ice_prefs, "ice_row": ice_id}
        evidence.note_cells(cells)
        try:
            took_the_relay, verdict, _timings = _gather_behind_a_coherent_gateway(
                monkeypatch, ("alice", "s3cret"), ice_prefs, rtt_ms, evidence=evidence)
            cells["timings_ms_from_the_offer"] = _timings
            cells["verdict"] = verdict
            print(f"[gather] rtt {rtt_ms} ms, {ice_id}: {_timings}")
            assert took_the_relay, "the page's STUN request did not take the relay. " + verdict
        except BaseException:
            cells["interfaces_of_the_runner"] = _interfaces_of_this_runner(cells.get("relay_host"))
            raise


_START_A_GATHER_JS = r"""async (stun) => {
  const g = window.__gather = {host: null, srflx: null, complete: false, candidates: [], errors: [], born: Date.now()};
  const t0 = performance.now();
  const pc = window.__pc = new RTCPeerConnection({iceServers: [{urls: [stun]}]});
  pc.createDataChannel('');
  pc.addEventListener('icecandidate', e => {
    if (!e.candidate || !e.candidate.candidate) return;
    g.candidates.push(e.candidate.candidate);
    const typ = (e.candidate.candidate.match(/ typ (\w+)/) || [])[1];
    if (typ === 'host' && g.host === null) g.host = Math.round(performance.now() - t0);
    if (typ === 'srflx' && g.srflx === null) g.srflx = Math.round(performance.now() - t0);
  });
  pc.addEventListener('icecandidateerror', e => g.errors.push({code: e.errorCode, text: e.errorText, url: e.url}));
  pc.addEventListener('icegatheringstatechange', () => { if (pc.iceGatheringState === 'complete') g.complete = true; });
  await pc.setLocalDescription(await pc.createOffer());
  return true;
}"""


@pytest.mark.e2e
@pytest.mark.parametrize("ice_prefs", _ICE_PREFS, ids=_ICE_IDS)
@pytest.mark.usefixtures("the_udp_line")
def test_the_first_candidate_of_a_gather_does_not_wait_for_a_gateway_that_is_far_away(monkeypatch, ice_prefs):
    """⛔ THE KNOWN-BAD INPUT: a gateway 250 ms away. The layer made its associations one after the other on the thread of
    the ICE sockets, a connect, a greeting, an authentication and the ASSOCIATE each, so the first candidate of the page
    came out after about twelve round trips (host candidate at 2.3 s on Linux and 3.0 s on Windows, against well under a
    second next to the gateway), and a page that times `icecandidate` reads it. A candidate does not wait for the proxy.

    The gateway here is not far, it is HELD: from the moment the page asks for the gather, every answer of the gateway on a
    control connection waits for the test (`Gateway.gate`). The host candidate has to come while the gateway has answered
    nothing, which no speed of the machine changes (a host candidate that waits for the gateway never comes, and the test
    is red at its deadline); then the gateway is let go and the srflx has to be born through its relay."""
    binary = _e2e_binary()
    if not binary:
        pytest.skip("no patched binary (set STEALTHFOX_E2E_BINARY)")
    from invisible_core import _geo

    from _socks_servers import STUN_NUMBER, coherent
    from _waits import watch
    stun = f"stun:{STUN_NUMBER}:{STUN_PORT}"
    with coherent(user="alice", password="s3cret", relay_host=this_host_address()) as gateway:
        monkeypatch.setattr(_geo, "discover_egress_ip", egress_by_proxy(gateway))
        extra_prefs = {"media.peerconnection.ice.obfuscate_host_addresses": True, **ice_prefs}
        with _launch_behind(gateway, binary_path=binary, extra_prefs=extra_prefs) as browser:
            page = browser.new_context().new_page()
            page.goto("about:blank")
            granted_before = len([a for a in gateway.associations if a[1] is not None])
            gateway.gate = threading.Event()           # held: nothing the gateway answers reaches the engine from here on
            try:
                page.evaluate(_START_A_GATHER_JS, stun)
                host = watch(lambda: page.evaluate("window.__gather.host"), lambda at: at is not None, 30,
                             lambda: page.wait_for_timeout(50))
                granted_while_held = len([a for a in gateway.associations if a[1] is not None]) - granted_before
            finally:
                gateway.gate.set()
            srflx = watch(lambda: page.evaluate("window.__gather.srflx"), lambda at: at is not None, 30,
                          lambda: page.wait_for_timeout(50))
            res = page.evaluate("window.__gather")
        carried = [(host_, port) for host_, port, _payload in gateway.datagrams]
        seen = (f"candidates: {res['candidates']}; ICE errors: {res['errors']}; associations granted while the gateway was held: "
                f"{granted_while_held}; datagrams the relay carried: {carried}; host candidate looks: {host.render()}; "
                f"srflx looks: {srflx.render()}")
    assert host.ok, "the first candidate waited for the gateway: no host candidate while the gateway answered nothing. " + seen
    assert srflx.ok and (STUN_NUMBER, STUN_PORT) in carried, (
        "the srflx was not born through the relay once the gateway was let go. " + seen)


@pytest.mark.e2e
@pytest.mark.usefixtures("the_udp_line")
def test_a_gather_after_the_page_loaded_through_the_gateway_costs_only_the_road_to_the_server(monkeypatch):
    """⛔ THE KNOWN-BAD INPUT: a gateway 250 ms away, a page loaded through it, and a while later the page asks for WebRTC.
    The connect, the greeting, the authentication and the ASSOCIATE of each socket (about 0.56 s against the real driver) are made
    before the page asks, when the proxy of the context is first used, not after: the STUN request of the page leaves on an
    association the gateway had granted before the PeerConnection was born, and the host candidate does not wait for the srflx.
    Both are facts the gateway and the page record, whatever the speed of the machine (a comparison with the same gather next to a
    gateway was a comparison of two launches of a browser): the gateway says which association carried the datagram, and the
    page orders its candidates. The page waits until the gateway has granted an association of the engine (30 s), not 2.5 s."""
    binary = _e2e_binary()
    if not binary:
        pytest.skip("no patched binary (set STEALTHFOX_E2E_BINARY)")
    from invisible_core import _geo

    from _e2e_verdicts import cold_association_problem
    from _socks_servers import STUN_NUMBER, STUN_PORT, WEB_ADDRESS, coherent
    from _waits import wait_until, watch
    stun = f"stun:{STUN_NUMBER}:{STUN_PORT}"
    # When it fails it saves, under `invpw-evidence/webrtc-gather-after-load-250/`, what the far-away test saves (cells.json, the control
    # connections and datagrams of the gateway with their times, the stall witness, the runner's load and the core's own log of its UDP
    # measurement); the lines of the same record are in the message. No assertion, wait or deadline below differs from before.
    relay_host = this_host_address()
    with FailureEvidence("webrtc-gather-after-load-250") as evidence:
        cells = {"test": "gather after the page loaded", "rtt_ms": 250}
        evidence.note_cells(cells)
        with coherent(user="alice", password="s3cret", relay_host=relay_host, rtt_ms=250) as gateway, \
                _Watch(gateway, monkeypatch, evidence, relay_host) as record:
            monkeypatch.setattr(_geo, "discover_egress_ip", egress_by_proxy(gateway))
            extra_prefs = {"media.peerconnection.ice.obfuscate_host_addresses": True}
            launched = _launch_behind(gateway, binary_path=binary, extra_prefs=extra_prefs)
            record.mark("launch starts")
            with launched as browser:
                record.mark("launch returned")
                page = browser.new_context().new_page()
                page.goto("http://%s/" % WEB_ADDRESS)       # by address: the browser resolves names itself
                # the pool makes its associations: wait until the gateway has granted one of the engine's (the first granted is the
                # core's probe); the verdict names what it saw if none came
                wait_until(lambda: len([a for a in gateway.associations if a[1] is not None]) >= 2, 30,
                           lambda: page.wait_for_timeout(50))
                page.evaluate(_START_A_GATHER_JS, stun)
                srflx = watch(lambda: page.evaluate("window.__gather.srflx"), lambda at: at is not None, 30,
                              lambda: page.wait_for_timeout(50))
                gather = page.evaluate("window.__gather")
            associations = [list(a) for a in gateway.associations]
            via = list(gateway.datagram_via)
            born = gather["born"] / 1000.0 - gateway.wall0
        seen = (f"candidates: {gather['candidates']}; ICE errors: {gather['errors']}; associations (start, end) seen by the gateway: "
                f"{associations}; datagrams and the association each left on: {via}; srflx looks: {srflx.render()}")
        cells["seen"] = seen
        probes = record.summary()
        if probes:
            seen += "\n" + probes
        assert srflx.ok, "no srflx within 30 s behind a gateway 250 ms away. " + seen
        problem = cold_association_problem(associations, via, STUN_NUMBER, born)
        assert problem is None, f"{problem}. {seen}"
        assert gather["host"] is not None and gather["host"] < gather["srflx"], (
            f"the host candidate (at {gather['host']} ms) did not come before the srflx ({gather['srflx']} ms): it waited for the "
            f"road to the server. {seen}")


@pytest.mark.e2e
@pytest.mark.usefixtures("the_udp_line")
def test_two_contexts_with_two_proxies_each_gets_the_srflx_of_its_own(monkeypatch):
    """⛔ THE KNOWN-BAD INPUT: a session whose proxy is one gateway and a context whose proxy is another. The proxy a
    page's traffic takes is the one of its context, UDP included: the srflx of each context is the exit of its own
    gateway. (The engine took one proxy for the whole session and handed over nothing when two differed.)"""
    binary = _e2e_binary()
    if not binary:
        pytest.skip("no patched binary (set STEALTHFOX_E2E_BINARY)")
    from invisible_core import _geo

    from _socks_servers import STUN_NUMBER, STUN_PORT, coherent
    stun = f"stun:{STUN_NUMBER}:{STUN_PORT}"
    host = this_host_address()
    with coherent(exit_ip="203.0.113.7", user="alice", password="s3cret", relay_host=host) as first,             coherent(exit_ip="198.51.100.77", user="bob", password="hunter2", relay_host=host) as second:
        # Each gateway reports the TCP exit of its own: the context proxy is measured against ITS exit, not the session's.
        monkeypatch.setattr(_geo, "discover_egress_ip", egress_by_proxy(first, second))
        extra_prefs = {"media.peerconnection.ice.obfuscate_host_addresses": True}
        with _launch_behind(first, binary_path=binary, extra_prefs=extra_prefs) as browser:
            of_the_session = browser.new_context().new_page()
            of_its_own = browser.new_context(proxy=second.proxy).new_page()
            res_session = of_the_session.evaluate(_RELAY_PROBE_JS, stun)
            res_own = of_its_own.evaluate(_RELAY_PROBE_JS, stun)

    def exits(res):
        return sorted(c["address"] for c in srflx_candidates(candidates(res["candidates"])))

    assert exits(res_session) == [first.tcp_exit], f"the context of the session: {res_session}"
    assert exits(res_own) == [second.tcp_exit], f"the context with a proxy of its own: {res_own}"


@pytest.mark.e2e
@pytest.mark.usefixtures("the_udp_line")
def test_the_turn_over_tcp_of_a_context_goes_through_the_proxy_of_that_context(monkeypatch):
    """⛔ THE KNOWN-BAD INPUT: the proxy lookup of the TCP socket of a TURN server was made with a channel of the system
    principal, so the filter of the session answered for context 0 and a SOCKS5 proxy of the context was not the one the
    TURN connection took. The page below sits in a context with a proxy of its own and asks for a TURN server over TCP:
    the CONNECT must reach the gateway of that context and not the one of the session."""
    binary = _e2e_binary()
    if not binary:
        pytest.skip("no patched binary (set STEALTHFOX_E2E_BINARY)")
    from invisible_core import _geo

    from _socks_servers import coherent
    from _waits import wait_until
    host = this_host_address()
    turn_host, turn_port = "198.51.100.9", 3478
    with coherent(exit_ip="203.0.113.7", user="alice", password="s3cret", relay_host=host) as session,             coherent(exit_ip="198.51.100.77", user="bob", password="hunter2", relay_host=host) as context:
        monkeypatch.setattr(_geo, "discover_egress_ip", egress_by_proxy(session, context))
        extra_prefs = {"media.peerconnection.ice.obfuscate_host_addresses": True}
        with _launch_behind(session, binary_path=binary, extra_prefs=extra_prefs) as browser:
            page = browser.new_context(proxy=context.proxy).new_page()
            page.goto("about:blank")
            # the page starts the ICE agent and leaves it alive; it is closed AFTER the gateway of the context has been handed the TURN address (a
            # condition with a deadline, not 6 s of a timer of the page: an agent closed first would never have tried the server on a slow machine)
            page.evaluate("""async (turn) => {
              const pc = window.__pc = new RTCPeerConnection({iceServers: [{urls: [turn], username: 'u', credential: 'p'}]});
              pc.createDataChannel('');
              await pc.setLocalDescription(await pc.createOffer());
            }""", f"turn:{turn_host}:{turn_port}?transport=tcp")
            wait_until(lambda: (turn_host, turn_port) in context.connects, 30, lambda: page.wait_for_timeout(50))
            page.evaluate("() => window.__pc.close()")
        in_context = [c for c in context.connects if c == (turn_host, turn_port)]
        in_session = [c for c in session.connects if c == (turn_host, turn_port)]
    assert in_context and not in_session, (
        f"the TURN connection of the context: by the gateway of the context {context.connects}, "
        f"by the gateway of the session {session.connects}")


def _gather_where_the_udp_is_dropped(gateway, monkeypatch, measured):
    """Launch behind ``gateway`` (a SOCKS5 proxy whose UDP is not coherent), gather with a STUN server only its relay could
    reach, and say what the target must look like: a retail Windows at home on a network that blocks UDP.

    The core measured the proxy before the launch declared it, and it is ``measured`` (``no`` or ``unmeasured``): the engine
    sends the page's UDP nowhere, not to the relay and not direct, so the STUN request retransmits and ends in 701 in nICEr's
    own time. The page gets host candidates and no srflx: the session does not invent one (a candidate with the exit of TCP and
    no allocation behind it, raddr 0.0.0.0, is a value no browser makes)."""
    binary = _e2e_binary()
    if not binary:
        pytest.skip("no patched binary (set STEALTHFOX_E2E_BINARY)")
    from invisible_core import _geo

    from _socks_servers import STUN_NUMBER
    stun = f"stun:{STUN_NUMBER}:{STUN_PORT}"
    monkeypatch.setattr(_geo, "discover_egress_ip", egress_by_proxy(gateway))
    extra_prefs = {"media.peerconnection.ice.obfuscate_host_addresses": True}
    with _launch_behind(gateway, binary_path=binary, extra_prefs=extra_prefs) as browser:
        page = browser.new_context().new_page()
        page.goto("about:blank")
        res = page.evaluate(_RELAY_PROBE_JS, stun)
    carried = [(host, port) for host, port, _payload in gateway.datagrams]
    declared = declared_udp(gateway.proxy)
    assert declared == measured, f"the core declared udp={declared} for this proxy, the measurement is {measured}"
    assert (STUN_NUMBER, STUN_PORT) not in carried, (
        f"the engine sent the page's UDP to a relay that was not measured coherent: {carried}")
    typ = [c["typ"] for c in candidates(res["candidates"])]
    assert "srflx" not in typ and "relay" not in typ, f"a candidate that nothing allocated: {res['candidates']}"
    assert "host" in typ, f"no host candidate at all: {res['candidates']}"
    assert [e["code"] for e in res["errors"]] == [701], f"a network that blocks UDP ends the STUN request in 701: {res['errors']}"
    # The shape of a retail Firefox on a network that drops UDP (notes/retail-udp-blocked-reference.md): udp and tcp host
    # candidates, and the 701 of the UDP request at nICEr's own time, 11.1-11.3 s, together with the end of the gather: never
    # an immediate error, never a candidate that nothing allocated.
    protocols = sorted({c["proto"].upper() for c in candidates(res["candidates"]) if c["typ"] == "host"})
    assert protocols == ["TCP", "UDP"], f"host candidates of a retail on a network that drops UDP: {res['candidates']}"
    # The floor only (an error that comes at once is a request that was never sent); a slow machine can only stretch nICEr's
    # 11.1-11.3 s, and the page waits 30 s for the end of the gather.
    assert res["at_ms"]["error"] >= 10_000, (
        f"the 701 of the UDP request came at {res['at_ms']['error']} ms; nICEr's time on a network that drops UDP is 11.1-11.3 s")


@pytest.mark.e2e
@pytest.mark.usefixtures("the_udp_line")
def test_a_proxy_that_does_not_relay_udp_gives_no_srflx_at_all(monkeypatch):
    """⛔ THE KNOWN-BAD INPUT: a gateway that refuses UDP ASSOCIATE (reply 7). The core measures it ``no``: the target is a
    retail Windows at home on a network that blocks UDP (see :func:`_gather_where_the_udp_is_dropped`)."""
    from _socks_servers import tcp_only
    with tcp_only(user="alice", password="s3cret") as gateway:
        _gather_where_the_udp_is_dropped(gateway, monkeypatch, "no")


@pytest.mark.e2e
@pytest.mark.usefixtures("the_udp_line")
def test_a_proxy_whose_udp_leaves_from_another_address_gives_no_srflx_at_all(monkeypatch):
    """⛔ THE KNOWN-BAD INPUT: a gateway that grants UDP ASSOCIATE and answers STUN, but with an address that is not its TCP
    exit: the classic leak (a srflx that is not the address of the page's HTTP). The core measures it ``no`` and the engine
    treats it as a router that drops UDP: the relay is granted but the page's datagrams never go to it."""
    from _socks_servers import mismatched
    with mismatched(user="alice", password="s3cret", relay_host=this_host_address()) as gateway:
        _gather_where_the_udp_is_dropped(gateway, monkeypatch, "no")


@pytest.mark.e2e
@pytest.mark.usefixtures("the_udp_line")
def test_a_proxy_whose_relay_never_answers_gives_no_srflx_at_all(monkeypatch):
    """⛔ THE KNOWN-BAD INPUT: a gateway that grants UDP ASSOCIATE and carries datagrams, but no STUN server ever answers (a
    router that drops UDP behind a working handshake). The measurement ends within its budget in ``unmeasured``: not a
    refusal, but the engine acts on ``coherent`` only, so the page is where it is on a network that blocks UDP."""
    from _socks_servers import silent_udp
    with silent_udp(user="alice", password="s3cret", relay_host=this_host_address()) as gateway:
        _gather_where_the_udp_is_dropped(gateway, monkeypatch, "unmeasured")




# ──────────────────────────────────────────────────────────────────────────
#  The egress IP must not change MID-SESSION.
#
#  Measured on 2026-08-25 on browserleaks, behind a residential proxy: the
#  page exited from 203.0.113.30 while a second reading of the exit said
#  216.131.76.64, and the site says so plainly ("WebRTC IP doesn't match your
#  Remote IP"). It was not a regression in the binary - interleaved A/B between
#  the release binary and the new one: 4 runs out of 4 consistent on both.
#
#  The cause is that the egress behind the timezone and the locale is photographed
#  ONCE, at launch, and residential proxies do not promise to keep it: the docs of the
#  two providers tried declare TIME-based sticky sessions (60 minutes max for
#  one; a sliding timeout for the other, which also expires early if the peer
#  disconnects). On a long session the drift is a certainty, not a risk - which
#  is why short probes never saw it.
#
#  Owner's decision, 2026-08-25: it is not updated on the fly. A timezone that
#  changes in front of the site's own eyes is as unnatural as the mismatch
#  itself. A proxy that does not hold the session is not fit for purpose, and
#  the product must SAY SO instead of carrying on.
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_an_unchanged_egress_triggers_nothing(monkeypatch):
    """The check must STAY SILENT when the proxy behaves well."""
    from invisible_playwright import _session
    monkeypatch.setattr("invisible_core._geo.discover_egress_ip",
                        lambda *a, **k: "203.0.113.5")
    outcome, current = _session.egress_ancora_valido(
        {"server": "http://p:1"}, "203.0.113.5")
    assert outcome == _session.USCITA_REGGE
    assert current == "203.0.113.5"


@pytest.mark.unit
def test_a_changed_egress_is_detected(monkeypatch):
    """The KNOWN-BAD INPUT. Without this, the check above proves nothing."""
    from invisible_playwright import _session
    monkeypatch.setattr("invisible_core._geo.discover_egress_ip",
                        lambda *a, **k: "198.51.100.9")
    outcome, current = _session.egress_ancora_valido(
        {"server": "http://p:1"}, "203.0.113.5")
    assert outcome == _session.USCITA_DERIVATA
    assert current == "198.51.100.9"


@pytest.mark.unit
def test_a_failed_discovery_is_not_a_drift(monkeypatch):
    """A network problem does not turn into an accusation against the proxy.

    Raising `ProxyEgressDrifted` here would mean killing a healthy session
    every time an echo endpoint is unreachable for an instant.

    ⛔ BUT THIS TEST'S NAME WAS RIGHT AND ITS ASSERTION WAS NOT. Until
    2026-08-25 it expected `(True, None)`, i.e. it asked the function to
    answer **holds** after a measurement that had never happened. "It is not
    a drift" is true; "therefore it is parity" does not follow, and that was
    the second half-step nobody had written. The test now asserts the two
    things separately: it is not a drift, AND it is not a confirmation.
    """
    from invisible_playwright import _session

    def explode(*a, **k):
        raise RuntimeError("network is down")

    monkeypatch.setattr("invisible_core._geo.discover_egress_ip", explode)
    outcome, current = _session.egress_ancora_valido(
        {"server": "http://p:1"}, "203.0.113.5")
    assert outcome != _session.USCITA_DERIVATA, "a timeout is not a drift"
    assert outcome != _session.USCITA_REGGE, (
        "and it is not a confirmation either: no measurement took place")
    assert outcome == _session.USCITA_NON_MISURABILE
    assert current is None


@pytest.mark.unit
def test_both_classes_are_checked(monkeypatch):
    """The defect that `_session.py` exists to not repeat.

    Three real bugs were born from a fix that reached only one of the two
    classes. This test watches that the check is wired into BOTH.

    ⛔ AND IT READS THE SYNTAX TREE, not the source text. The first draft did
    `sorgente.count("_assert_uscita_invariata") == 2`, and that count has two
    defects: it expects an EXACT number, so it goes red the moment a legitimate
    checkpoint is added, and it also counts mentions in COMMENTS, so the
    number is not even the number of calls. Once surveillance on
    `context.new_page` was added the count became 4: three calls and one
    mention in a comment.

    The checkpoints are THREE, and the third is the one that was missing:
    `browser.new_context`, `browser.new_page`, and **`context.new_page`**,
    which is the NORMAL way to open a tab. Without the third, a session that
    opens one context and then N pages ran ONE check only, at the very first
    instant. Measured on 2026-08-25: at launch the egress was one address,
    nine tabs later another, and the two showed up together on the same
    detector page.
    """
    import ast
    import inspect
    import textwrap
    from invisible_playwright import async_api, launcher

    def _calls(fn):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        return [n for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and getattr(n.func, "attr", getattr(n.func, "id", None))
                == "_assert_uscita_invariata"]

    for module in (launcher, async_api):
        fn = module.InvisiblePlaywright._patch_new_context_defaults
        calls = _calls(fn)
        assert len(calls) == 3, (
            f"{module.__name__}: the egress check must be wired into THREE "
            f"places - browser.new_context, browser.new_page and "
            f"context.new_page - and I counted {len(calls)}. "
            f"context.new_page is the normal way to open a tab: without it, "
            f"a long session stops watching after the very first instant")


# ---------------------------------------------------------------------------
# The egress check has THREE outcomes, and the third is the one that was
# missing.
# ---------------------------------------------------------------------------

def _with_probe(monkeypatch, behavior):
    """Replaces the network probe with `behavior`, which may also raise."""
    from invisible_core import _geo
    monkeypatch.setattr(_geo, "discover_egress_ip", behavior)


def test_a_dropped_probe_is_not_a_confirmation(monkeypatch):
    """⛔ THE DEFECT THIS TEST EXISTS TO KEEP CLOSED.

    Until 2026-08-25 `egress_ancora_valido` returned `(True, None)` when the
    probe FAILED. The comment beside it argued the correct half - a failed
    discovery is not a drift, and that is true - but the returned value said
    `holds`, i.e. it asserted parity on the basis of a measurement that had
    never happened. It is the same class as `fppro_consistency.py`, which
    printed CONSISTENCY PASS when `visitor_id` was silent on both runs.
    """
    from invisible_playwright import _session

    def explode(*a, **k):
        raise OSError("network is down")

    _with_probe(monkeypatch, explode)
    outcome, ip = _session.egress_ancora_valido({"server": "socks5://x"}, "203.0.113.5")
    assert outcome == _session.USCITA_NON_MISURABILE, (
        "a dropped probe cannot answer 'holds': it measured nothing")
    assert outcome != _session.USCITA_REGGE
    assert ip is None


def test_the_three_outcomes_are_distinct_and_all_reachable(monkeypatch):
    from invisible_playwright import _session

    _with_probe(monkeypatch, lambda *a, **k: "203.0.113.5")
    assert _session.egress_ancora_valido({"server": "s"}, "203.0.113.5")[0] == _session.USCITA_REGGE

    _with_probe(monkeypatch, lambda *a, **k: "198.51.100.9")
    drifted = _session.egress_ancora_valido({"server": "s"}, "203.0.113.5")
    assert drifted[0] == _session.USCITA_DERIVATA
    assert drifted[1] == "198.51.100.9", "the current IP feeds the rejection message"

    def explode(*a, **k):
        raise RuntimeError("boom")
    _with_probe(monkeypatch, explode)
    assert (_session.egress_ancora_valido({"server": "s"}, "203.0.113.5")[0]
            == _session.USCITA_NON_MISURABILE)

    assert len({_session.USCITA_REGGE, _session.USCITA_DERIVATA,
                _session.USCITA_NON_MISURABILE}) == 3


def test_without_a_proxy_there_is_nothing_to_betray():
    """Unlike the other two: here it is not a measurement that is missing, it
    is an obligation."""
    from invisible_playwright import _session
    assert _session.egress_ancora_valido(None, None)[0] == _session.USCITA_REGGE
    assert _session.egress_ancora_valido({"server": "s"}, None)[0] == _session.USCITA_REGGE


def test_a_burst_of_silent_probes_rejects_the_session():
    """One drop: network. Three in a row: blindness, and the session is no
    longer defensible."""
    from invisible_playwright import _session
    from invisible_playwright.launcher import InvisiblePlaywright

    assert InvisiblePlaywright._MAX_USCITE_NON_MISURABILI >= 2, (
        "rejecting on the FIRST silent probe turns every timeout into an error")
    assert issubclass(_session.ProxyEgressNonVerificabile, RuntimeError)
    assert _session.ProxyEgressNonVerificabile is not _session.ProxyEgressDrifted, (
        "unmeasurable and drifted are two different diagnoses and must be "
        "distinguished by type too, or a catch cannot react differently")
    # what goes unconfirmed is the timezone and the locale decided from the egress: the engine declares no egress address
    doc = " ".join(_session.ProxyEgressNonVerificabile.__doc__.split())
    assert "timezone" in doc and "locale" in doc and "an address" not in doc, doc
