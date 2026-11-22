---
title: "WebRTC ICE candidate spoofing: the fields that give it away"
description: "Spoofing a WebRTC ICE candidate is more than the address: priority, foundation and arrival time are all checkable, so invisible does not spoof candidates; it changes the road the datagrams take."
parent: "Network, Proxy and WebRTC"
grand_parent: "Guides"
nav_order: 2
---


# WebRTC ICE candidate spoofing: the fields that give it away

Every guide about hiding your address in WebRTC stops at the address. Rewrite the
server-reflexive candidate to say the proxy's IP and you are done.

**You are not done: the candidate that betrays you is a structured record with half a
dozen fields, and this page covers what a spoofed candidate has to replace, the ways to do
it, the three fields that give away a fabricated candidate - when it arrives, what priority
it claims, and what foundation it shares - and the other road, the one invisible takes:
no candidate is spoofed at all.**

A candidate arrives at a particular moment, and it sits in a set alongside other
candidates that have to be consistent with it. Getting the address right is the easy part;
three other things are where a fabricated candidate gives itself away.

## What a spoofed candidate has to replace

**Only the server-reflexive (srflx) candidate has to be replaced; the host candidate
should be left alone.** The srflx carries your public address and is the one that betrays
the real interface behind a proxy. A normal browser behind NAT produces two candidates
that matter:

```
candidate:... typ host   <uuid>.local          the LAN address, masked
candidate:... typ srflx  203.0.113.7  raddr ...  the public address, from STUN
```

The **srflx** is the problem. A stock browser does not ask the proxy to relay WebRTC's UDP,
even when the proxy could: its proxy setting covers TCP traffic, and the ICE stack sends
UDP from the machine's own interface. So the browser asks a STUN server directly from the
real interface, and the server truthfully answers with your real address. Your HTTP
traffic exits one way and your WebRTC exits another, and the two disagree. The goal is the
opposite: a [WebRTC address that matches the proxy exit](webrtc-ip-match-proxy-exit.md).

The **host** candidate is not the problem, and this is where people overcorrect. Stock
browsers generally conceal it behind an mDNS `<uuid>.local` name, the
[host-candidate concealment mechanism drafted at the IETF](https://datatracker.ietf.org/doc/html/draft-ietf-rtcweb-mdns-ice-candidates)
for exactly this privacy problem. Replacing it with a fake LAN IP makes you stand out:
a browser that shows a LAN address is different from one that shows only the name. The
correct move is to leave the host candidate alone.

[The wider version of this argument](webrtc-leak-proxy.md) is that suppression and
overcorrection are both louder than the leak.

## Three ways to spoof the address, and why each is detectable

There are three ways to put the proxy's address in front of a page, and each leaves
something to find.

**Rewrite after the STUN reply.** Let the real STUN transaction complete, then replace the
address in the result before the candidate is emitted. The advantage is that everything
else about the candidate is genuine: the timing, the port, the priority. The disadvantage
is that it needs the STUN transaction to succeed, which behind a proxy that does not carry
UDP it never does, and that the rewritten address is one no STUN server ever
reported for that socket.

**Fabricate.** Build a candidate that never came from a STUN server. This works when there
is no reachable STUN at all, and it is where every field you do not think about becomes a
thing you have to get right: the three tells below are all fields of a fabricated
candidate.

**Patch the page.** Wrap `RTCPeerConnection` in JavaScript, or filter the SDP and the
`icecandidate` events before the page sees them. This lives in the page, so it is the
easiest to write and the easiest to find: the wrapper changes how the constructor and its
events look, and the values the browser's own code produced are still readable through
other routes, so a patched event and an untouched record disagree.

All three have one thing in common: they edit the answer after the browser has already
computed it, and the edit has to stay consistent with everything the browser did not edit.

## Tell one: arrival time

**A real server-reflexive candidate cannot arrive instantly; it appears one network round
trip after gathering starts, so a candidate that materialises with zero delay describes a
STUN server in the same room as you.** A fabricated candidate that is emitted immediately
has this problem.

The candidate exists only because a request travelled to a STUN server and a response came
back. Skipping that trip is the tell: no round trip, no real candidate.

This is not a theoretical concern. Published detection advice for this exact technique
names round-trip timing as the way to spot a spoofed address, in both directions: a
candidate that arrives too fast never made the trip, and one that arrives too slow made
the trip from somewhere other than where it claims.

A timer that delays the candidate does not fix it, because a timer is a number somebody
chose, and the real value is whatever the round trip took.

## Tell two: priority

**A fabricated candidate is exposed when its priority falls outside the narrow band the
real ICE stack produces, because priority is computed, not chosen.** It is a single number
in the SDP, and it can be checked by anyone.

ICE priority is not arbitrary. It is computed by a documented formula (defined in
[RFC 8445](https://www.rfc-editor.org/rfc/rfc8445), the ICE specification) from the
candidate type, an interface preference and the component id:

```
priority = (type_preference << 24) | (local_preference << 16) | (256 - component_id)
```

For a server-reflexive UDP candidate the type preference is fixed by the specification.
The **local preference** is where implementations differ: the ICE stack in Firefox
produces values in a narrow band.

A fabricated candidate that hardcodes the local preference to `0xFFFF`, which is 65535,
has a priority that no real candidate on that stack ever has, because the code that
generates them cannot produce it. One integer, visible in the SDP, and it says the
candidate did not come from the browser.

**The general lesson**, and it is worth more than the specific number: when you fabricate
a record, every field with a computed value is a field somebody can recompute.

## Tell three: foundation

The foundation is an identifier shared by candidates of the same type from the same base
address, used to group them for the connectivity checks.

A fabricated candidate can get it wrong in two ways: by taking a different value on each
launch, and by colliding with the foundation of the TCP host candidate, which is a
grouping no real browser produces.

Two separate problems in one field: a value that moves when it should be stable, and a
value that groups things that do not belong together.

### The general shape of the fixes

Notice what repairing these looks like: not inventing plausible values, but **finding the
function the browser already uses and calling it with the right inputs**. A fabricated
record is credible exactly to the degree that it was produced by the same code as a real
one. Taken to its end, that argument leaves nothing to fabricate.

## What invisible does instead

**invisible does not spoof, synthesise or rewrite ICE candidates.** Candidates are
produced by Firefox's own ICE code from real STUN and TURN replies, so priority,
foundation and arrival time are the browser's own, because the browser computed them. What
changes is the **road** the datagrams take, and the network the browser believes it is on.

- **The road.** With a proxy, WebRTC's UDP goes through the proxy's SOCKS5 UDP relay when
  the relay works and exits from the same address as the proxy's TCP traffic. Otherwise it
  goes nowhere. It never leaves directly from the machine: with a proxy set there is no
  fallback to direct.
- **The network.** The browser does not read the machine's network interfaces. Every
  session declares one plausible home network (one adapter, one private IPv4 drawn from
  the seed), and the host candidates are mDNS `<uuid>.local` names, never an address of
  the machine.

### The four cases

What a page can observe depends on the session:

| Session | Where the STUN datagram goes | `srflx` candidate | `host` candidates | ICE error |
|---|---|---|---|---|
| SOCKS5 proxy whose UDP relay works and exits from the same address as its TCP | through the proxy's UDP relay (SOCKS5 UDP ASSOCIATE) | present, and it is the proxy's exit address | mDNS names (`<uuid>.local`), UDP and TCP | none |
| SOCKS5 proxy that cannot carry UDP (ASSOCIATE refused, relay silent, UDP exits from another address), or SOCKS4 | nowhere: no datagram leaves, neither through the proxy nor from the machine | absent | mDNS names, UDP and TCP | one `icecandidateerror` 701 for the STUN request, after about eleven seconds, when gathering ends |
| HTTP or HTTPS proxy (CONNECT) | nowhere, same as above: CONNECT carries no UDP | absent | mDNS names, UDP and TCP | same as above |
| No proxy | directly from the machine, as in any browser | present when the STUN server answers: the line's real public address | mDNS names | none |

In the last row there is nothing to hide: without a proxy the browser behaves like a
retail Firefox on that machine. In the middle rows the browser treats the proxy as a
router that drops UDP: no `srflx` and one `701`, and nothing sent from the machine. In the first row the `srflx` is a real one, obtained through
the relay, so it carries the fields of a real `srflx` and it equals the exit address.

### How the road is chosen

Before launch, and before a context that brings its own SOCKS5 proxy, the library measures
the proxy: it opens a SOCKS5 UDP ASSOCIATE, sends a STUN Binding request through the
relay, and compares the address the STUN server saw with the proxy's TCP exit. Only if a
reply comes back and the two addresses are equal does the browser send WebRTC's UDP
through the relay. The whole measurement fits in about three seconds and runs in parallel
with the discovery of the exit.

Anything else (refused, no reply, a different address, an IPv6 TCP exit, a proxy that is
not SOCKS5) means the browser treats the proxy as a router that drops UDP.

There is no option that forces UDP on, declares a public IP, rewrites a candidate or
disables WebRTC; the result of the measurement cannot be overridden. The only knob is
which STUN servers the measurement uses, as IPv4 literals (two public ones by default):

```python
from invisible_playwright import InvisiblePlaywright

proxy = {"server": "socks5://203.0.113.5:1080", "username": "user", "password": "pass"}

# 192.0.2.10 is a placeholder: put a STUN server you can reach, or leave
# udp_stun_servers out to use the defaults. An unreachable one means no UDP.
with InvisiblePlaywright(proxy=proxy, udp_stun_servers=["192.0.2.10:3478"]) as browser:
    page = browser.new_page()
    page.goto("https://example.com")
```

Those servers are contacted by the Python process only. Each context uses its own proxy
for WebRTC: a context created with `browser.new_context(proxy=...)` that carries its own
SOCKS5 proxy gets its own `srflx`.

### What holds in every session

- No candidate is synthesised and no address is rewritten.
- Host candidates are mDNS names, never an address of the machine.
- No IPv6 candidate is produced, with or without a proxy, and the browser does no AAAA
  lookups.
- With a proxy, the names of STUN and TURN servers in `iceServers` are resolved by the
  browser over DNS-over-HTTPS through the proxy of that context, never by the machine's
  resolver. Without a proxy the system resolver is used, as in retail Firefox.
- TURN over TCP goes through the context's proxy. Whether a TURN relay candidate is
  obtained through a proxy that cannot carry UDP is not something this page claims.
- HTTP/3 follows the same road: over the relay when the proxy carries coherent UDP,
  otherwise the browser falls back to HTTP/2.

### SOCKS5 and UDP in general

SOCKS5 does define a UDP mode, the UDP ASSOCIATE command
([RFC 1928](https://www.rfc-editor.org/rfc/rfc1928)), so "SOCKS5 is TCP only" is wrong. In
practice most residential exits do not implement it, or relay UDP from an address
different from the TCP exit. That is why the measurement above exists, and why a proxy
alone does not stop the leak in a stock browser, whatever the proxy supports.

## An empty or inconsistent list is a tell

**The most expensive failure in this area is not a wrong value. It is no values at all:
gathering that produces no ICE candidates at all behind a proxy (what a page should read
instead is in [no public ICE candidate behind a proxy](webrtc-no-candidates-behind-proxy.md)).**

A browser that is silent is [a stronger signal than the leak](webrtc-leak-proxy.md), and a
check that only asserts "nothing leaked" passes while it happens. A list that is
inconsistent with itself is the same kind of tell: a `srflx` with no matching host
candidate, a `srflx` that arrives with zero delay, a priority that no stack produces.

Behind a proxy invisible always produces the host candidates (mDNS names, UDP and TCP).
Whether a `srflx` follows depends only on whether the proxy can carry the UDP, as in the
table above, and when it cannot the page sees one `701` error, never an empty silence and never an
address of the machine.

## Conclusion

Faking a WebRTC address is a small change with a long tail. The address is one field.
The priority is computed, the foundation is shared, the timing is observable, and the
whole set has to hold together with the host candidate and with each other.

The approach that holds is to stop fabricating: take the values from the code that
produces the real ones, and change only the road the datagrams travel and the network the
browser believes it is on.

And check the positive case. A candidate with perfect fields is worth nothing if
gathering silently produced nothing at all.

## Short answers to the questions that lead here

**Why does WebRTC leak my real IP behind a SOCKS proxy?** Because a stock browser's proxy
setting covers TCP traffic and the ICE stack sends UDP from the machine's own interface,
so the STUN request leaves from your real interface whatever the proxy supports. SOCKS5
does define a UDP mode, but a stock browser does not ask the proxy to use it for WebRTC.

**Should I replace the host candidate too?** No. Stock browsers generally mask it as an
mDNS `.local` name, and replacing that with a fake LAN address makes you stand out.

**What gives away a fabricated candidate?** Its priority if the value is outside the range
the real code produces, its foundation if it drifts or collides, and its arrival time if
it appears with no round trip.

**How is ICE priority calculated?** From the candidate type, a local preference and the
component id, combined by a documented formula. The local preference is
implementation-specific and lives in a narrow band.

**Does invisible spoof the ICE candidate?** No. The candidates are produced by Firefox's
own ICE code from real STUN and TURN replies. What the library decides is the road: through
the proxy's SOCKS5 UDP relay when the relay works and exits from the same address as the
TCP, nowhere otherwise.

**What do I see behind a proxy that cannot carry UDP?** Host candidates as mDNS names, no
`srflx`, and one `icecandidateerror` 701 for the STUN request after about eleven seconds,
when gathering ends. No datagram leaves directly from the machine. A SOCKS4 proxy and an
HTTP or HTTPS proxy behave the same way.

**Can I force the UDP through the proxy, or declare a public IP?** No. The library
measures the proxy and decides; the result cannot be overridden.

**Can I just disable WebRTC?** A browser with no ICE candidates at all is a more specific
thing to be than one with an unusual address, and invisible has no option that disables
it.

**How do I test this properly?** Assert the positive shape for a proxied session: the
host candidates are `.local` names, and either there is a `srflx` (with a SOCKS5 proxy
whose relay works, its address is your proxy's exit) or there is no `srflx` and exactly
one 701 (a proxy that cannot carry UDP). Read the live candidates in
[about:webrtc](about-webrtc-debug-ice-candidates.md), and see
[the wider method](how-to-test-bot-detection.md).

```python
from invisible_playwright import InvisiblePlaywright

proxy = {"server": "socks5://203.0.113.5:1080", "username": "user", "password": "pass"}

# Use a STUN server you can reach in place of 192.0.2.10: with an unreachable one a 701
# is expected in every case, and the cases cannot be told apart.
with InvisiblePlaywright(proxy=proxy) as browser:
    page = browser.new_page()
    page.goto("https://example.com")
    seen = page.evaluate("""async () => {
        const pc = new RTCPeerConnection({iceServers: [{urls: "stun:192.0.2.10:3478"}]});
        pc.createDataChannel("probe");
        const out = {candidates: [], errors: []};
        pc.onicecandidate = e => { if (e.candidate) out.candidates.push(e.candidate.candidate); };
        pc.onicecandidateerror = e => out.errors.push(e.errorCode);
        await pc.setLocalDescription(await pc.createOffer());
        await new Promise(resolve => {
            pc.onicegatheringstatechange = () => {
                if (pc.iceGatheringState === "complete") resolve();
            };
            setTimeout(resolve, 25000);
        });
        pc.close();
        return out;
    }""")
    candidates, errors = seen["candidates"], seen["errors"]
    host = [c for c in candidates if "typ host" in c]
    srflx = [c for c in candidates if "typ srflx" in c]

    # Assert the positive shape, not the absence of a leak.
    assert host and all(".local" in c for c in host), "host must be an mDNS .local name"
    # A proxied session has exactly two shapes: a srflx from the relay (no error), or
    # no srflx and a single 701 for the STUN request.
    assert srflx or errors == [701], "no srflx must come with exactly one 701"
```

**See also:** [WebRTC leak with a proxy](webrtc-leak-proxy.md) for the problem this
solves, the [WebRTC IPv6 leak a proxy does not stop](webrtc-ipv6-leak-proxy.md),
[when the timezone does not match the proxy](timezone-proxy-mismatch.md) for the
other half of making a proxied session agree with itself, and
[how to test whether your browser is detected](how-to-test-bot-detection.md).

## Sources

- [RFC 8445 (Interactive Connectivity Establishment)](https://www.rfc-editor.org/rfc/rfc8445),
  which defines the candidate priority formula.
- [RFC 1928 (SOCKS Protocol Version 5)](https://www.rfc-editor.org/rfc/rfc1928), which
  defines the UDP ASSOCIATE command.
- [The IETF draft on mDNS ICE candidates](https://datatracker.ietf.org/doc/html/draft-ietf-rtcweb-mdns-ice-candidates),
  which documents concealing host-candidate IP addresses behind mDNS names.
- Published detection guidance for this technique, which names round-trip timing and
  candidate ordering as the ways to spot a fabricated address.

---

*From the notes of [invisible_playwright](https://github.com/oss-invisible/playwright),
a Firefox patched at the C++ level. The method that finds this class of problem is
comparing the output against a stock Firefox, field by field.*
