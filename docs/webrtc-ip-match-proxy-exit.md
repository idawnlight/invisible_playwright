---
title: "WebRTC IP that matches the proxy exit, by design"
description: "WebRTC and the proxy exit: the proxy's SOCKS5 UDP relay is measured before launch; when it exits from the same address, the server-reflexive candidate is the exit IP, otherwise there is no srflx and nothing leaks."
parent: "Network, Proxy and WebRTC"
grand_parent: "Guides"
nav_order: 15
---


# WebRTC IP that matches the proxy exit, by design

WebRTC is kept matching the proxy exit by measuring, before launch, whether the proxy's
SOCKS5 UDP relay can carry WebRTC's UDP from the same address as its TCP exit. When it
can, the browser sends the STUN request through the relay, so the
[server-reflexive](https://developer.mozilla.org/en-US/docs/Web/API/RTCIceCandidate/type) (`srflx`) candidate is a genuine one whose address is
the exit, because that is where the datagram really left from. When it cannot, no
datagram leaves at all and there is no `srflx`, so there is nothing to mismatch.

A browser behind a proxy has two ways to say where it is. The HTTP request exits
through the proxy, so the visible address is the proxy's. WebRTC asks a separate
question over a separate path, and if nobody has arranged otherwise it answers with
the machine's own public address. When those two disagree, the session is telling two
stories, and the disagreement is cheaper to detect than any single fingerprint field.

This page is about the arrangement that keeps them agreeing: how the proxy's UDP relay
is measured before launch, how that decides where WebRTC's UDP goes, and what the page
sees in each case.

## What "matching" actually means

Matching means that, whenever a server-reflexive (`srflx`) candidate exists, it carries the
same public address as the HTTP exit, paired with the port the STUN server actually reported.
That candidate is what a real browser behind a home router produces: the router rewrites the
source address of an outbound packet to the household's public address and to some port the
router chose, and a [STUN](https://www.rfc-editor.org/rfc/rfc8489) server on the far
side reports that rewritten pair back. The pair is the NAT mapping, and a real `srflx` is
always a public address with a plausible port next to it.

So matching is not "show the proxy's address". It is: let the real STUN exchange leave
from the exit. A STUN server on the far side then reports a real mapping, and the
candidate built from that reply is a `srflx` like any other. Nothing is rewritten and no
candidate is synthesised. The goal is a `srflx` whose address equals the HTTP exit
because the datagram really left from there, or no `srflx` at all. For the wider list of
what a leak check reads out of that candidate, see
[WebRTC through a proxy](webrtc-leak-proxy.md).

## One lookup, shared with the timezone and the locale

The exit address is not guessed and not read from the machine. At every launch, whatever
you pass for `timezone` and `locale`, the library discovers the proxy's exit address once,
by making a round trip out through the same proxy the session uses and reading
back the address the far side saw. That is the address the rest of the world will see
for this session, because it is the path the session's own traffic takes.

By default (`timezone` unset and `locale="auto"`) the timezone and the locale of the
session are derived from that same address; an explicit `timezone` replaces only the zone,
and the discovery is made all the same. The
measurement of the proxy's UDP relay runs in parallel with the discovery and compares
the address the STUN server saw with that same exit, so when a `srflx` exists it is that
same address. If you have read [why the timezone has to match the
proxy](timezone-proxy-mismatch.md), this is the other half of the same lookup: the
timezone and the WebRTC measurement both rest on the one exit address found at launch.

With the defaults you do not call any of this yourself. Passing a proxy is enough, and
everything derived from the exit is set up before the browser starts:

```python
from invisible_playwright import InvisiblePlaywright

proxy = {"server": "socks5://gate.example.com:1080", "username": "u", "password": "p"}

with InvisiblePlaywright(seed=42, proxy=proxy) as browser:
    page = browser.new_page()
    page.goto("https://example.com/")
    # The proxy's UDP relay was measured before launch. If it answers from the
    # exit address, the srflx candidate is the exit; otherwise there is no srflx.
```

## Measure the relay, then send the datagram through it

Before the browser starts, the library measures the proxy's UDP path. It opens a SOCKS5
UDP ASSOCIATE with the proxy, sends a STUN Binding request through the relay it is
given, and compares the address the STUN server saw with the proxy's TCP exit. The
measurement runs in parallel with the exit discovery and fits in about three seconds.

Only if a reply comes back and the two addresses are equal does the browser send
WebRTC's UDP through the relay. The `srflx` candidate is then a genuine server-reflexive
candidate, built by Firefox's own ICE code from a real STUN reply, and its address is
the proxy's exit because that is where the datagram really left from. Nothing is
rewritten and no candidate is synthesised, so the address and the port in the candidate
are whatever the STUN server actually reported.

SOCKS5 does define a UDP mode, the UDP ASSOCIATE command of RFC 1928, so "SOCKS5 is TCP
only" is wrong. In practice, though, most residential exits do not implement it, or
relay UDP from an address different from their TCP exit. A stock browser does not ask
the proxy to relay WebRTC's UDP even when the proxy could: its proxy setting covers TCP
traffic, and the ICE stack sends UDP from the machine's own interface. That is why a
proxy alone does not stop the leak in a stock browser, whatever the proxy supports.

## When there is no srflx

If the proxy refuses the UDP ASSOCIATE, the relay stays silent, UDP exits from another
address than the TCP exit, or the TCP exit is an IPv6 address, the browser treats the
proxy as a router that drops UDP. The same holds for a proxy that is not SOCKS5. No
datagram leaves, neither through the proxy nor from the machine, so there is no `srflx`
to disagree with the HTTP exit and nothing to leak. There is no fallback to a direct
send: with a proxy set, a datagram never leaves directly from the machine.

| Session | Where the STUN datagram goes | `srflx` candidate | `host` candidates | ICE error |
|---|---|---|---|---|
| SOCKS5 proxy whose UDP relay works and exits from the same address as its TCP | Through the proxy's UDP relay | Present, and it is the proxy's exit address | mDNS names (`<uuid>.local`), UDP and TCP | None |
| SOCKS5 proxy that cannot carry UDP, or SOCKS4 | Nowhere | Absent | mDNS names, UDP and TCP | One `icecandidateerror` 701 for the STUN request, after about eleven seconds, when gathering ends |
| HTTP or HTTPS proxy | Nowhere: CONNECT carries no UDP | Absent | mDNS names, UDP and TCP | Same as above |
| No proxy | Directly from the machine, as in any browser | Present when the STUN server answers: the line's real public address | mDNS names | None |

The host candidates are never an address of the machine. The browser does not read the
machine's network interfaces: the session declares one plausible home network, and the
host candidates are `.local` names. No IPv6 candidate is produced, with or without a
proxy.

## One srflx per context, and the options that exist

Each context uses its own proxy for WebRTC. A context created with its own SOCKS5 proxy
(`browser.new_context(proxy=...)`) has that proxy measured before it is created, and
gets its own `srflx`, whose address is the exit of that proxy.

There is one option related to the measurement: `udp_stun_servers=["192.0.2.10:3478"]`,
on the constructor and on `launch_server`. It chooses the STUN servers used for the
measurement (IPv4 literals in `ip:port` form; by default two public ones are used), and
they are used by the Python process only. No option forces UDP on, declares a public
address, rewrites a candidate or disables WebRTC, and the result of the measurement
cannot be overridden.

## Measuring it: read the srflx candidate through the proxy

The only test worth trusting here is the one that reads the candidate list the page
actually receives, through the proxy the job uses. Whether a `srflx` should exist depends
on the proxy: with a SOCKS5 relay that exits from the same address as the TCP exit, its
address must equal the exit; with any other proxy the list holds no `srflx` at all.
Assert the presence of the right candidates rather than the absence of a leak, because
an empty gather passes every negative check while proving nothing. That principle is the
subject of
[how to test whether your browser is detected](how-to-test-bot-detection.md), and WebRTC
is where it bites hardest.

```python
import json
from invisible_playwright import InvisiblePlaywright

proxy = {"server": "socks5://gate.example.com:1080", "username": "u", "password": "p"}

GATHER = """
() => new Promise((resolve) => {
  // Firefox has no default STUN server: without one there is never a srflx.
  // Use a STUN server you can reach (this is a generic example).
  const pc = new RTCPeerConnection({
    iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
  });
  const cands = [];
  pc.onicecandidate = (e) => {
    if (e.candidate) { cands.push(e.candidate.candidate); }
    else { resolve(cands); }        // null candidate == gathering complete
  };
  pc.createDataChannel("probe");
  pc.createOffer().then((o) => pc.setLocalDescription(o));
  // Safety net only: a proxy that cannot carry UDP ends gathering at about eleven seconds.
  setTimeout(() => resolve(cands), 25000);
})
"""

with InvisiblePlaywright(seed=42, proxy=proxy) as browser:
    page = browser.new_page()

    # The exit address, read back through the same proxy the browser uses.
    # (your own IP-echo endpoint; example.com is a stand-in here)
    exit_ip = page.goto("https://example.com/ip?format=json") and \
        json.loads(page.locator("pre, body").inner_text())["ip"]

    page.goto("https://example.com/")
    candidates = page.evaluate(GATHER)

    srflx = [c for c in candidates if "typ srflx" in c]
    host = [c for c in candidates if "typ host" in c]
    assert host, "no host candidate: an empty gather is a FAIL, not a pass"

    if srflx:
        # The proxy's UDP relay carried the datagram: the srflx is the exit.
        for line in srflx:
            addr = line.split()[4]            # candidate ... <addr> <port> typ srflx
            assert addr == exit_ip, f"srflx {addr} does not match exit {exit_ip}"
            print("srflx", addr, "matches the exit")
    else:
        # The relay cannot carry UDP from the exit address: no datagram left, so
        # there is no srflx to mismatch. That is the intended result, not a leak.
        print("no srflx: nothing left the machine")
```

Run it at least ten times and from the machine that runs production, not from your
laptop. Behind a SOCKS5 proxy whose UDP relay answers from the exit address it reports
a `srflx` whose address equals the exit. Behind any other proxy it reports no `srflx`,
and gathering ends after about eleven seconds with one `icecandidateerror` 701 for the
STUN request. In both cases the host candidates are `.local`
names rather than LAN addresses, which is what a current browser emits and what a
[reader of the candidate list](playwright-detected-as-bot.md) expects to see.

## Conclusion

Keeping WebRTC aligned with the proxy is not a matter of hiding or editing a value. It is
a matter of choosing the road the datagram takes. The exit is measured once, on the path
the session actually uses, and the proxy's UDP relay is measured against it. If the
relay answers from the exit address, WebRTC's UDP goes through it and the `srflx` is the
exit because the datagram really left from there. In every other case no datagram
leaves, there is no `srflx`, and nothing can mismatch or leak.

The test that confirms all of this is the same in shape as every other good stealth
test: read the positive signal the page receives, through production inputs, more than
once. Where a `srflx` exists, it matching the exit is the pass. Where none exists, the
host candidates still being there is the pass. Nothing arriving at all is the failure
that a leak-only check would have called clean.

## Short answers to the questions that lead here

**Why does WebRTC show a different IP than my proxy?** Because a stock browser's proxy
setting covers TCP traffic, while its ICE stack sends UDP from the machine's own
interface, so WebRTC discovers the machine's own public address over its own path. Here,
with a proxy set, WebRTC's UDP goes through the proxy's UDP relay when that relay exits
from the same address as the TCP exit, and otherwise goes nowhere. It never leaves
directly from the machine, and WebRTC itself is not disabled.

**Is any WebRTC candidate faked?** No. Candidates are produced by Firefox's own ICE code
from real STUN and TURN replies. No candidate is synthesised and no address is
rewritten. A `srflx` exists only when a datagram really went out through the relay, and
its address is then the proxy's exit.

**Why is there no srflx behind my proxy?** Because the proxy cannot carry WebRTC's UDP
from its exit address: the UDP ASSOCIATE was refused, the relay did not answer, UDP
leaves from another address than the TCP exit, or the proxy is SOCKS4, HTTP or HTTPS.
No datagram leaves, so there is no `srflx` and nothing to mismatch. The page sees mDNS
host candidates and, when gathering ends after about eleven seconds, one
`icecandidateerror` 701 for the STUN request.

**How is the exit address found?** Once at every launch, with or without an explicit
`timezone` or `locale`, by a round trip out through the same proxy the session uses,
reading back the address the far side saw. By default (`timezone` unset and
`locale="auto"`) the timezone and the locale are derived from that same address; an
explicit `timezone` replaces only the zone. The UDP measurement runs in parallel and
compares the address the STUN server saw with that exit, whatever you pass for them.

**Do I have to configure any of this?** With the defaults, no. Pass a proxy and the
exit and its UDP relay are measured before the browser launches. No option forces UDP on or declares an
address; the only option related to the measurement is `udp_stun_servers`. With no proxy
nothing is measured and WebRTC goes out directly from the machine, as in any browser.

**Does a context with its own proxy get its own srflx?** Yes, when that proxy is SOCKS5
and its UDP relay exits from the same address as its TCP. Each context uses its own
proxy for WebRTC.

**How do I confirm it is working?** Read the ICE candidates through the proxy. If a
server-reflexive candidate is present, its address must equal the exit; if none is
present, the host candidates must still be there. Assert presence, not the absence of a
leak, and repeat the run.

**Why can an empty WebRTC result still fail a check?** Because a leak test that only
looks for a wrong address is satisfied by no address at all. A blank or blocked gather
passes the negative check while telling you nothing about what the page can read.

**See also:** [reading a WebRTC leak behind a proxy](webrtc-leak-proxy.md),
[how ICE candidates get spoofed](webrtc-ice-candidate-spoofing.md), and
[keeping the timezone matched to the exit](timezone-proxy-mismatch.md).

## Sources

- This project's WebRTC handling in the patched engine and its wrapper: the single
  exit-address lookup shared with timezone resolution, the measurement of the proxy's
  SOCKS5 UDP relay against that exit, and the ICE sockets that send through the relay
  or send nothing.
- The project's end-to-end tests for WebRTC behind a proxy, which assert the candidate
  list each kind of proxy produces: a `srflx` equal to the exit through a SOCKS5 relay
  that works, and no `srflx` and no datagram leaving directly in every other case.
- [RFC 8489, Session Traversal Utilities for NAT (STUN)](https://www.rfc-editor.org/rfc/rfc8489),
  retrieved 2026-08-28, which defines the address-and-port pair a NAT allocates and how a
  STUN server reports it back to the agent that sent the original packet.
- [MDN, `RTCIceCandidate.type`](https://developer.mozilla.org/en-US/docs/Web/API/RTCIceCandidate/type),
  retrieved 2026-08-28, documenting the `srflx` value as a binding a NAT allocated, learned
  through a STUN or TURN server.

---

*Written while maintaining [invisible_playwright](https://github.com/oss-invisible/playwright),
a Firefox patched at the C++ level driven by stock Playwright. The exit is measured once,
on the path the traffic takes, and everything else is kept honest around it.*
