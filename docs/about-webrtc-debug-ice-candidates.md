---
title: "about:webrtc: read your real ICE candidates"
description: "Read your live ICE candidates in Firefox's built-in about:webrtc: tell a real LAN-IP leak from a masked .local host and a proxy-egress srflx line."
parent: "Network, Proxy and WebRTC"
grand_parent: "Guides"
nav_order: 17
---


# about:webrtc: read your real ICE candidates

A public leak site gives you a verdict and a couple of IP strings. Firefox ships a
page that gives you the raw material behind that verdict: every
[ICE candidate](https://datatracker.ietf.org/doc/html/rfc8445) the
browser gathered, its type, its address, its priority, and the timeline of when each
one arrived. It is `about:webrtc`, it is built in, it needs no extension, and it is
the first place to look after any change to a WebRTC or proxy setting.

This page is how to read it, what the three interesting candidate states look like
line by line, how to pair it with `about:config`, and how to reproduce the same read
from a Playwright script so a bad candidate fails a test instead of a real session.

## Why a leak site is not enough

A leak site aggregates. It runs the gathering for you, picks the addresses it thinks
matter, and prints something like "Local IP" and "Public IP" with a green or red
badge. That summary hides exactly the distinctions you need while debugging, because
three very different underlying states can round to the same badge:

- A host candidate carrying a raw `192.168.x.x` private address. A stock browser hides
  that address by default, so this is *worse* than stock. (In this product it is the
  home network the session declares, never an address read from the machine.)
- A host candidate carrying an obfuscated `<uuid>.local` name. This is the correct
  state, and it is what a stock Firefox produces.
- A server-reflexive (`srflx`) candidate whose address is your proxy egress, with a
  plausible NAT port and a priority in the range a real candidate uses.

A "No Leak" badge can be produced by the second and third states working correctly,
and it can also be produced by WebRTC being suppressed to the point that nothing is
gathered at all, which is [its own detectable signal](webrtc-leak-proxy.md). The
badge does not tell you which. The candidate list does.

## Opening about:webrtc and reading a session

Type `about:webrtc` into the address bar. The page is empty until a peer connection
exists, so open a page that creates one first, then come back to it. Each connection
gets a collapsible block; expand it and the two things worth reading are:

- **ICE Stats / the candidate table.** Every gathered candidate with its type
  (`host`, `srflx`, `relay`), its address, its port, its protocol and its priority.
- **The ICE gathering log / timeline.** Timestamped lines showing when gathering
  started, when each candidate was added, and when it finished. A candidate that
  arrives suspiciously early (time zero, no round trip) is as much a tell as a wrong
  address, and the timeline is the only view that shows arrival time.

You are reading for three things: that a host candidate exists and is masked, that a
server-reflexive candidate points at the exit you expect (or is absent for a reason you
can name, see [the four proxy cases](#reading-a-proxied-session-the-four-cases)), and that the
timeline looks like a browser talking to a
[STUN](https://datatracker.ietf.org/doc/html/rfc8489) server rather than fabricating an
answer instantly.

## The three states a leak site collapses into one

Three candidate states, plus a suppressed section that gathers nothing, all round to
the same one-word leak-site badge. The table below is how each reads in the candidate
list; the lines underneath show the exact shape of each one.

| State | Candidate line | Leak-site badge | Verdict |
|---|---|---|---|
| Leak | `typ host` with a raw `192.168.x.x` private address | "Local IP" / red warning | Visible, worse than stock |
| Correct host | `typ host` with a `<uuid>.local` name | "No Leak", local IP shown as "-" | Shipped, correct |
| Correct srflx | `typ srflx` with your proxy egress address | contributes to "No Leak" | Correct exit |
| Suppressed | nothing gathered at all | "No Leak" | Detectable tell, a FAIL |

Here is what each state looks like as a candidate line, so you can recognise it on
sight in the table.

**Leak (worst case).** The host candidate carries a raw private address:

```text
candidate:0 1 UDP 2122252543 192.168.1.24 51894 typ host
```

If you see a private `192.168.x.x`, `10.x.x.x` or `172.16-31.x.x` address on a `typ
host` line, WebRTC is handing out a private network address. A stock browser does
not do this; it masks it. Seeing it means a masking setting is off (for example
`media.peerconnection.ice.obfuscate_host_addresses` set to `false`), and you are more
identifiable than a default install. The address is the home network the session
declares, not one read from the machine.

**Correct host.** The same candidate with the address replaced by a per-session
[mDNS](https://datatracker.ietf.org/doc/html/rfc6762) name:

```text
candidate:0 1 UDP 2122252543 3f1c9a7e-....local 51894 typ host
```

The `.local` name is what a stock Firefox emits by default, and a leak site reads it
as "No Leak" with the local IP shown as "-". This is the shipped state here: the
patched engine keeps host-address obfuscation on, so the host line is a `.local` name
and never a raw LAN IP.

**Correct srflx.** Behind a SOCKS5 proxy whose UDP relay works, the server-reflexive
candidate carries the proxy exit and a NAT port:

```text
candidate:1 1 UDP 1686052863 203.0.113.45 49731 typ srflx raddr 0.0.0.0 rport 0
```

Behind a proxy, that address must be the proxy egress, not your real public IP and not
a private address. The candidate comes out of Firefox's own ICE code from a real STUN
reply, so the [port, priority and foundation, not just the address](webrtc-ice-candidate-spoofing.md),
are the ones a real `srflx` carries. If the proxy cannot carry the STUN request there
is no `srflx` at all; see the four cases below.

## Pairing it with about:config

`about:webrtc` shows you the result; `about:config` shows you the switches that
produced it. Open a second tab on `about:config` and filter on `media.peerconnection`.

- **`media.peerconnection.*`** are the standard Firefox WebRTC prefs. The one that
  governs the host state above is `media.peerconnection.ice.obfuscate_host_addresses`:
  `true` gives you the `.local` name, `false` gives you the raw private address. If
  `about:webrtc` shows a `192.168.x.x` host, this is the first pref to check.

There is no pref that holds the proxy exit or an IPv6 switch to read here. What the
session did with the proxy shows in the candidate list and in the `icecandidateerror`
events, which the next section teaches you to read.

The workflow is: change one pref, reload the page that builds the connection, refresh
`about:webrtc`, and read the candidate that changed. One switch at a time keeps cause
and effect legible, which is the same discipline that makes a bisect a bisect.

## Reading a proxied session: the four cases

With a proxy set, no datagram ever leaves directly from the machine, and there is no
fallback to a direct send. What you see depends on whether the proxy can carry the
STUN request. Candidates always come from Firefox's own ICE code and from real STUN
replies; none is invented and no address is rewritten.

| Session | The STUN datagram goes | `srflx` | `host` | `icecandidateerror` |
|---|---|---|---|---|
| SOCKS5 proxy whose UDP relay works and exits from the same address as its TCP | through the proxy's UDP relay | present, equal to the proxy exit | mDNS names, UDP and TCP | none |
| SOCKS5 proxy that cannot carry UDP, or SOCKS4 | nowhere | absent | mDNS names, UDP and TCP | one `701` for the STUN request, after about eleven seconds, when gathering ends |
| HTTP or HTTPS proxy | nowhere (CONNECT carries no UDP) | absent | mDNS names, UDP and TCP | same as the row above |
| No proxy | directly from the machine, as in any browser | present when the STUN server answers: the line's real public address | mDNS names | none |

How to recognise each one from `about:webrtc` or from the `icecandidate` and
`icecandidateerror` events of your own `RTCPeerConnection`:

- **The relay carried it.** A `typ srflx` line whose address equals the address the
  proxy exits from, and no error event. Behind a SOCKS5 proxy this is the only shape
  in which a `srflx` appears.
- **The proxy does not carry coherent UDP.** Host candidates only, then a single `701`
  `icecandidateerror` for the STUN request about eleven seconds after the offer, at the
  moment gathering finishes, and no `srflx`. This covers a SOCKS5 proxy that refuses
  UDP ASSOCIATE, a relay that never answers, a relay whose UDP leaves from an address
  other than the TCP exit, and SOCKS4. An error that arrives at once instead of after
  about eleven seconds points at a STUN server name that did not resolve, not at the proxy.
- **An HTTP or HTTPS proxy.** The same picture as the previous case: CONNECT carries
  no UDP, so the STUN request goes nowhere and ends in one `701`.
- **No proxy.** A `typ srflx` line, present when the STUN server answers: the line's
  real public address, as in any browser.

This is not the "suppressed" state from the table above. The host candidates are
there; only the `srflx` is missing, and the `701` tells you why.

Three things hold in every case:

- Host candidates are mDNS names (`<uuid>.local`), never an address of the machine.
  The browser does not read the machine's network interfaces; the session declares one
  plausible home network.
- No IPv6 candidate is produced, with or without a proxy.
- The names of STUN and TURN servers in `iceServers` are resolved by the browser over
  DNS-over-HTTPS through the proxy of the context, not by the machine's resolver.
  Without a proxy the system resolver is used, as in retail Firefox.

The decision is made by the library, not by you. Before launch, and before a context
that brings its own SOCKS5 proxy, it opens a SOCKS5 UDP ASSOCIATE, sends a STUN Binding
request through the relay and compares the address the STUN server saw with the proxy's
TCP exit. Only if a reply comes back and the two addresses are equal does the browser
send WebRTC's UDP through the relay; anything else is treated as a router that drops
UDP. No option forces UDP on. The measurement uses two public STUN servers by default;
to choose them, pass IPv4 literals, for example `udp_stun_servers=["192.0.2.10:3478"]`,
to `InvisiblePlaywright`. Each context uses its own proxy for WebRTC, so a context with
its own SOCKS5 proxy gets its own `srflx`.

## Driving the same session from Playwright

The manual read is for exploring. Once you know which candidate line matters, you want
it as an assertion that runs every session, and `InvisiblePlaywright` returns a real
Playwright `Browser`, so the same code that opens `about:webrtc` can also gather
candidates programmatically.

First, opening the built-in page under a seeded, proxied session so you can eyeball it:

```python
from invisible_playwright import InvisiblePlaywright

PROXY = {"server": "socks5://gate.example.com:1080", "username": "u", "password": "p"}

with InvisiblePlaywright(seed=42, proxy=PROXY) as browser:
    page = browser.new_page()
    # Gathering only starts after setLocalDescription, not at construction,
    # so about:webrtc has nothing to show until the offer is set.
    page.goto("https://example.com")
    page.evaluate("""
        async () => {
            const pc = new RTCPeerConnection({
                // Use a STUN server you can reach: with an unreachable one a 701 is
                // expected in every case, and the cases cannot be told apart.
                iceServers: [{ urls: "stun:stun.example.org:3478" }],
            });
            pc.createDataChannel('x');
            await pc.setLocalDescription(await pc.createOffer());
        }
    """)

    inspector = browser.new_page()
    inspector.goto("about:webrtc")
    print("Expand the session to read candidates + the gathering timeline")
    inspector.wait_for_timeout(3000)
```

Then the same read as data, so a wrong candidate fails a check instead of a session.
This gathers the candidate strings and the `icecandidateerror` codes the page itself
would list, waits for gathering to finish (about eleven seconds when the proxy cannot
carry the STUN request), and prints them. Replace the STUN server with one you control:

```python
from invisible_playwright import InvisiblePlaywright

PROXY = {"server": "socks5://gate.example.com:1080", "username": "u", "password": "p"}

GATHER = """
async () => {
  const pc = new RTCPeerConnection({
    // Use a STUN server you can reach. With an unreachable one a 701 is expected in
    // every case, and the cases cannot be told apart.
    iceServers: [{ urls: "stun:stun.example.org:3478" }],
  });
  pc.createDataChannel("probe");
  const lines = [];
  const errors = [];
  pc.onicecandidate = (e) => { if (e.candidate) lines.push(e.candidate.candidate); };
  pc.onicecandidateerror = (e) => { errors.push(e.errorCode); };
  await pc.setLocalDescription(await pc.createOffer());
  await new Promise((r) => {
    const cap = setTimeout(r, 25000);
    const done = () => {
      if (pc.iceGatheringState === "complete") { clearTimeout(cap); r(); }
    };
    pc.onicegatheringstatechange = done;
    done();
  });
  pc.close();
  return { lines, errors };
}
"""

with InvisiblePlaywright(seed=42, proxy=PROXY) as browser:
    page = browser.new_page()
    page.goto("https://example.com")
    result = page.evaluate(GATHER)
    candidates, errors = result["lines"], result["errors"]

    host = [c for c in candidates if "typ host" in c]
    srflx = [c for c in candidates if "typ srflx" in c]

    for c in candidates:
        print(c)
    print("icecandidateerror codes:", errors)

    # Assert the positive shape, not the absence of a leak.
    assert host and all(".local" in c for c in host), "host must be an mDNS .local name"
    # A proxied session has exactly two shapes: a srflx from the relay (no error), or
    # no srflx and a single 701 for the STUN request.
    assert srflx or errors == [701], "no srflx must come with exactly one 701"
    assert not any(":" in c.split()[4] for c in candidates), "no IPv6 candidate"
```

Because the identity is seed-derived, `seed=42` gives the same session every run, so a
candidate that fails this on one run fails it on the next, and you can [replay the
exact browser that produced it](quickstart.md) instead of chasing a random draw.

## A before-and-after you can measure

The distinction is not theoretical. With host obfuscation off, a leak page reports the
host candidate as a raw private address and shows the red "WebRTC exposes your Local IP"
warning; the same page on the shipped baseline reports "No Leak" with the local IP as
"-", because the host line is a `<uuid>.local` name and the only routable address
on the page, when the proxy's UDP relay carries the STUN request, is the proxy egress
on the `srflx` line. Same session, one pref, and the
difference is the whole point of reading candidates instead of badges.

Assert the presence of the right candidate, not the absence of a wrong one. A WebRTC
section that comes back empty passes every "no leak" test and is itself a signal, which
is the same false pass that a [verdict-based test hides and a field comparison
catches](how-to-test-bot-detection.md).

## Conclusion

`about:webrtc` turns WebRTC from a badge into a list you can read. It shows the host
candidate so you can confirm it is a `.local` name and not a raw private address, the
server-reflexive candidate so you can confirm it is your proxy egress when the proxy
carries UDP (and its absence, with a single `701` after about eleven seconds, when it does
not), and the gathering timeline so you can confirm the answers arrived
the way a browser produces them rather than instantly. Pair it with `about:config` to
see the switches behind each line, drive the same read from Playwright to make it a
test, and change one pref at a time so every difference has a cause.

## Short answers to the questions that lead here

**What is about:webrtc for?** It is Firefox's built-in inspector for live peer
connections: the full ICE candidate list, each candidate's type and address and
priority, and the gathering timeline. It shows the detail a leak site summarises away.

**Why is my host candidate a 192.168 address?** Host-address obfuscation is off. A
stock Firefox emits a `<uuid>.local` name instead, and a raw private address is
visible where stock hides it. Check `media.peerconnection.ice.obfuscate_host_addresses` in `about:config`.

**What should the srflx candidate show behind a proxy?** When the proxy's UDP relay
carries the STUN request, your proxy egress address, with the port and priority of a
real `srflx`. Not your real public IP and not a private address. When the proxy cannot
carry UDP there is no `srflx`: you see host candidates and a single `701`
`icecandidateerror` after about eleven seconds.

**A leak site says "No Leak" but I still get flagged. Why?** "No Leak" can mean the
candidates are correct, or it can mean WebRTC was suppressed and gathered nothing,
which is itself detectable. The candidate list tells you which; the badge does not.

**Can I read about:webrtc from Playwright?** You can navigate to it with `page.goto`,
and you can gather the same candidate strings programmatically with `page.evaluate`
over a real [`RTCPeerConnection`](https://developer.mozilla.org/en-US/docs/Web/API/RTCPeerConnection),
which turns the manual read into an assertion.

**Why does an empty WebRTC section count as a failure?** Because a real browser always
gathers host candidates, and behind NAT a server-reflexive one as soon as its STUN
request is answered. Emitting nothing is not neutral; it is a shape no ordinary browser
has, so assert presence, not absence.

## Sources

- Mozilla's Firefox Source Docs, [Debugging WebRTC Calls](https://firefox-source-docs.mozilla.org/contributing/debugging/debugging_webrtc_calls.html),
  retrieved 2026-08-28, for `about:webrtc` itself, read directly rather than through a
  third-party summary.
- Mozilla Support, [Configuration Editor for Firefox](https://support.mozilla.org/en-US/kb/about-config-editor-firefox),
  retrieved 2026-08-28, for `about:config`, read the same way.

**See also:** [what actually leaks through a proxy](webrtc-leak-proxy.md),
[the srflx fields beyond the address](webrtc-ice-candidate-spoofing.md), and
[everything that has to agree with your exit IP](timezone-proxy-mismatch.md).

---

*Written while maintaining [invisible_playwright](https://github.com/oss-invisible/playwright),
a Firefox patched at the C++ level driven by stock Playwright. Every candidate line in
this page is one I have read off about:webrtc while chasing a leak that a badge missed.*
