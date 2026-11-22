---
title: "No public ICE candidate behind a proxy"
description: "Why the WebRTC ICE candidate list loses its server reflexive candidate behind a proxy that cannot carry UDP, and what a page sees in each case: a real srflx through a working SOCKS5 UDP relay, host candidates only and one error otherwise."
parent: "Network, Proxy and WebRTC"
grand_parent: "Guides"
nav_order: 14
---


# No public ICE candidate behind a proxy

With this library behind a proxy that cannot carry UDP, the page reads a list of host
candidates only (mDNS names), no public candidate, and one ICE error after about eleven
seconds: not an empty list.

The short version: STUN needs UDP, and most residential proxies do not carry it. SOCKS5
can (its UDP ASSOCIATE command), but most residential exits do not implement it or relay
from an address other than their TCP exit. invisible measures the proxy before launch.
If the SOCKS5 UDP relay works and exits from the same address as the proxy's TCP, STUN
goes through the relay and the page gets a real `srflx` carrying the proxy's exit
address. Otherwise (SOCKS5 without a working relay, SOCKS4, HTTP or HTTPS) no datagram
leaves at all: there is no `srflx`, the host candidates are still there as mDNS names,
and one `icecandidateerror` 701 arrives after about eleven seconds. The list is not
empty, but in that case it has no public candidate.

You route the browser through a residential proxy, open a detection page, and the
WebRTC section has no server reflexive candidate. It feels like a win, because nothing
leaked. Whether it is one depends on what else the list carries and on what the proxy
can do, and this page is about why the `srflx` goes missing and what a page sees in
each case.

## A missing srflx is not the same as a clean result

Most WebRTC advice is written around the opposite failure: the browser exposing your
real local address next to the proxy's. That is a real problem and it has [its own
page](webrtc-leak-proxy.md). But what you see behind a proxy that cannot carry UDP is
the mirror image, and it can look like success.

A browser that has gathered [ICE candidates](https://www.rfc-editor.org/rfc/rfc8445)
emits at least two kinds. A host candidate,
which is the machine's own address (or a masked `.local` form of it), and a server
reflexive candidate, the `srflx`, which is the public address a STUN server saw the
traffic arrive from. A typical consumer browser behind a home NAT, with a STUN server
that answers, shows a `srflx`, because that is the point of STUN: to discover the public
side of the NAT.

So a list without any public candidate has a different shape from the one an unproxied
consumer browser usually shows. This is the general rule that shows up across every
surface: [a suppressed signal is itself a signal](how-to-test-bot-detection.md), and
asserting the absence of a leak is not the same as asserting the presence of a real
value.

## Why a residential proxy makes STUN return nothing

STUN, the [Session Traversal Utilities for NAT protocol](https://www.rfc-editor.org/rfc/rfc8489),
works over UDP. It sends a small datagram to a STUN server and reads back the
public address and port the server observed. That round trip is how the srflx candidate
gets its value.

SOCKS5 is not a TCP-only protocol. It defines a UDP mode, the [UDP ASSOCIATE
command](https://www.rfc-editor.org/rfc/rfc1928), precisely so that a proxy can relay
datagrams. In practice a SOCKS5 residential proxy, of the kind sold as a pool of home IP
addresses, mostly does not implement it, or relays UDP from an address different from
its TCP exit. HTTP and HTTPS proxies (CONNECT) and SOCKS4 carry no UDP at all. So when
the STUN datagram has no usable relay, there is nowhere for it to go. The STUN
transaction gets no answer. No public address comes back, so no srflx candidate is formed.

A stock browser does not even try the relay: its proxy setting covers TCP traffic, and
its ICE stack sends UDP from the machine's own interface, whatever the proxy supports.
That is why a proxy alone does not stop the leak in a stock browser. invisible does not
send that datagram from the machine when a proxy is set: it either goes through the
proxy's UDP relay or it is not sent.

Whether the proxy can carry UDP coherently is a property of the transport you chose, and
no JavaScript patch can change it, because the packet genuinely cannot traverse a tunnel
that does not relay it. If you want to understand why UDP and TCP behave so differently
through these tunnels, [SOCKS5 versus HTTP proxying](socks5-vs-http-proxy-browser.md)
covers what each one actually forwards.

The consequence for stealth is direct. With a stock browser the proxy does not protect
the UDP side of WebRTC. With invisible, the proxy's UDP capability decides whether a
public candidate can exist at all: with a working relay it does, and without one the
public candidate a typical home line shows is absent, while the host candidates remain.

## In a stock Firefox: the default-route probe

A stock Firefox behind a SOCKS proxy has a second way to reach zero candidates, and it
fires even before STUN is attempted. It is worth knowing because it explains an empty
list on pages that never prompted for any device. It describes a stock Firefox, not this
library.

A page that has not been granted camera or microphone permission does not get the full
candidate set. To limit what an untrusted page can learn about your interfaces, the
browser switches into a reduced mode, one of the [WebRTC IP address handling
modes](https://www.rfc-editor.org/rfc/rfc8828), in which it only offers the address of the
default route: the single interface your traffic would leave by. Deciding which interface that
is requires a quick internal probe. The browser opens a UDP socket and "connects" it to
the remote address of the document, not to send anything, but to ask the operating
system which local address that route would use.

Behind a SOCKS proxy, that remote is not reachable directly. The probe is UDP, it is
aimed at an address the proxy has interposed itself in front of, and it can fail. In the
reduced mode, a failed default-route probe is fatal to gathering: it returns a failure,
and the whole candidate list comes back empty. So on an ordinary page, one with no media
prompt at all, the very step that is supposed to pick a safe address to advertise can
collapse into advertising nothing.

In this library, the host candidates do not come from the machine's network interfaces,
which the browser does not read. The session declares one plausible home network (one
adapter, one private IPv4 drawn from the seed), and the host candidates are mDNS names
(`<uuid>.local`), never an address of the machine. A page therefore gets host candidates
behind any proxy.

## What a typical Firefox shows, and what a page sees in invisible

The reference is a stock Firefox behind a home router, unproxied. Open a WebRTC probe in
one, on a normal connection, and you typically get a host candidate and, when the STUN
server answers, a server reflexive candidate whose address is your public IP. That is the
usual shape of a consumer browser.

The cases a page can observe, side by side:

| WebRTC state | Candidate count | Server reflexive (srflx) | What the page reads |
|---|---|---|---|
| Stock Firefox behind a home NAT (no proxy) | Non-empty | Typically present when the STUN server answers, carries the public IP | The usual shape |
| invisible, no proxy | Host (mDNS) plus srflx | Present when the STUN server answers: the line's real public address | Same as a browser on that line |
| invisible via a SOCKS5 proxy whose UDP relay works and exits from the same address as its TCP | Host (mDNS) plus srflx | Present, carries the proxy exit address | A real srflx, no error |
| invisible via a proxy that cannot carry UDP (SOCKS5 without a working relay, SOCKS4, HTTP, HTTPS) | Host candidates only, mDNS, UDP and TCP | Absent | No public candidate, and one `icecandidateerror` 701 after about eleven seconds |

What happens in each case:

- **SOCKS5 with a working UDP relay.** Before launch the library opens a UDP ASSOCIATE
  against the proxy, sends a STUN Binding request through the relay, and compares the
  address the STUN server saw with the proxy's TCP exit. Only if a reply comes back and
  the two addresses are equal does the browser send WebRTC's UDP through the relay. The
  STUN datagram goes through the proxy's relay and the `srflx` carries the proxy's exit
  address. The measurement fits in about three seconds and runs in parallel with the
  exit discovery.
- **A proxy that cannot carry UDP.** Anything else (ASSOCIATE refused, no reply, a
  different address, an IPv6 TCP exit, SOCKS4, HTTP or HTTPS) makes the browser treat
  the proxy as a router that drops UDP. No datagram leaves, neither through the proxy
  nor from the machine. The `srflx` is absent. The host candidates are present, as mDNS
  names, for UDP and TCP, and one `icecandidateerror` 701 for the STUN request arrives
  when gathering ends, after about eleven seconds. The list has no public candidate
  because the proxy cannot give it one.
- **No proxy.** The STUN datagram leaves directly from the machine, as in any browser,
  and when the STUN server answers the `srflx` is the line's real public address.

What is true in every case:

- Candidates are produced by Firefox's own ICE code from real STUN or TURN replies. No
  candidate is invented and none is edited after the fact.
- Host candidates are mDNS names, never an address of the machine.
- With a proxy set, a datagram never leaves directly from the machine. There is no
  fallback to direct.
- No IPv6 candidate is produced, with or without a proxy.
- With a proxy, the names of STUN and TURN servers in `iceServers` are resolved by the
  browser over DNS-over-HTTPS through the proxy of that context, never by the machine's
  resolver. Without a proxy the system resolver is used, as in retail Firefox.
- Each context uses its own proxy for WebRTC: a context created with its own SOCKS5
  proxy gets its own `srflx`.

The result cannot be overridden by the caller: there is no option that forces UDP on,
declares a public IP, edits a candidate or disables WebRTC. The STUN servers used for
the measurement can be chosen with `udp_stun_servers=["192.0.2.10:3478"]` (IPv4 literals
only; two public ones are used by default); they are used by the Python process only.
None of this changes the behaviour without a proxy, where the real gather runs normally.

## Reproduce it and read the candidates yourself

The only way to trust any of this is to read the candidate list on the path you deploy
on, through the proxy you deploy with, not on localhost. Here is the probe. It creates a
peer connection, waits for gathering to complete, and returns the raw candidate lines and
any ICE error codes so you can count them and inspect the addresses.

```python
from invisible_playwright import InvisiblePlaywright

GATHER_ICE = """
() => new Promise((resolve) => {
    // Firefox has no default STUN server: without one there is no srflx and no 701.
    // Use a STUN server you can reach (this is a generic example).
    const pc = new RTCPeerConnection({
        iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
    });
    const lines = [];
    const errors = [];
    const done = () => resolve({ lines, errors });
    pc.onicecandidateerror = (e) => errors.push(e.errorCode);
    pc.onicecandidate = (e) => {
        if (e.candidate) {
            lines.push(e.candidate.candidate);
        } else {
            setTimeout(done, 500);   // null candidate marks gathering complete; the 701 comes just before
        }
    };
    pc.createDataChannel("probe");
    pc.createOffer().then((offer) => pc.setLocalDescription(offer));
    // Safety net only: a proxy that cannot carry UDP ends gathering, and delivers the 701,
    // at about eleven seconds, so the normal exit is the null candidate above.
    setTimeout(done, 25000);
});
"""

proxy = {
    "server": "socks5://gate.example.com:1080",
    "username": "user",
    "password": "pass",
}

with InvisiblePlaywright(seed=42, proxy=proxy) as browser:
    page = browser.new_page()
    page.goto("https://example.com")
    result = page.evaluate(GATHER_ICE)
    candidates, errors = result["lines"], result["errors"]

    print("candidate count:", len(candidates))
    print("ice errors:", errors)
    srflx = [c for c in candidates if " typ srflx " in c]
    print("has server reflexive:", bool(srflx))
    for line in candidates:
        print(" ", line)

    # What is guaranteed behind any proxy: a non-empty list whose host
    # candidates are mDNS names, never a plain address of the machine.
    assert candidates, "empty candidate list"
    hosts = [c for c in candidates if " typ host " in c]
    assert hosts, "no host candidate"
    assert all(c.split()[4].endswith(".local") for c in hosts), "a host candidate is not an mDNS name"

    # Then tell the two cases apart.
    if srflx:
        # The proxy's UDP relay works: the srflx is the proxy's exit address.
        EXIT_IP = "203.0.113.7"   # your proxy's TCP exit, from your provider
        assert srflx[0].split()[4] == EXIT_IP, "srflx is not the proxy exit"
        assert not errors, f"unexpected ICE errors: {errors}"
    else:
        # The proxy cannot carry UDP: no datagram left, so no srflx, and
        # one icecandidateerror 701 for the STUN request.
        assert errors == [701], f"expected a single 701, got {errors}"
```

Note that the assertions check what is actually guaranteed, in the positive form. They
fail on an empty list and on a host candidate that is not an mDNS name, and they check
the case-specific outcome: either a `srflx` equal to the proxy exit address and no
error, or no `srflx` and exactly one 701. A srflx is not guaranteed behind a proxy
that cannot carry UDP, so the script does not require one there. A test that only
checked "my real IP did not appear" would pass on a completely empty list.

Run it more than once, and compare against the same probe in a stock Firefox on the
same machine, since the interesting differences are the ones between the two reports
rather than the raw numbers in either. If you need to confirm the exit address the
proxy is actually giving you before trusting the srflx value, and to get authentication
right for a SOCKS5 pool, see [SOCKS5 proxy
authentication](playwright-socks5-proxy-authentication.md).

A minimal install to run the snippet:

```bash
uv add invisible-playwright
```

## Conclusion

A candidate list without a public candidate is not the same thing as a clean result. It
differs from what a typical unproxied consumer browser shows. The root cause is the
transport: STUN needs UDP, and most residential proxies do not carry it, so the public
candidate a home line normally shows does not form. In a stock Firefox the
default-route probe can fail the same way before STUN is even tried.

invisible does not hide WebRTC and does not invent a candidate. It measures the proxy
before launch. Behind a SOCKS5 proxy whose UDP relay works and exits from the same
address as its TCP, STUN goes through the relay and the page gets a real `srflx` with the
proxy's exit address. Behind a proxy that cannot carry UDP, no datagram leaves, there is no
`srflx`, the host candidates stay as mDNS names, and one `icecandidateerror` 701 arrives
after about eleven seconds. Which case you are in is decided by the proxy, not by an
option. And whichever tool you use, read the candidate list on the deployed path instead
of trusting that no leak means all is well.

## Short answers to the questions that lead here

**Why does WebRTC return no ICE candidates behind my proxy?** Because STUN needs UDP to
discover your public address and most residential proxies do not carry it: SOCKS5
defines UDP ASSOCIATE, but most exits do not implement it or relay from an address other
than their TCP exit, and HTTP, HTTPS and SOCKS4 proxies carry no UDP at all. Behind such
a proxy invisible sends no datagram, so there is no server reflexive candidate. The host
candidates are still there as mDNS names, and one `icecandidateerror` 701 arrives after
about eleven seconds.

**Is a candidate list without a srflx safe because nothing leaked?** Nothing leaked, but
it is not the shape a typical unproxied consumer browser shows, which has a server
reflexive candidate when the STUN server answers. invisible never returns a bare empty
list behind a proxy: the host candidates are always there. Behind a proxy that cannot
carry UDP the list has no `srflx` and one error 701.

**Why can the list be empty in a stock Firefox even on a page that never asked for my
camera?** In a stock Firefox, pages without media permission use a reduced mode that
only advertises the default-route address, and the probe that finds that route is itself
UDP. Behind SOCKS it can fail and gathering can return empty. In this library the host
candidates come from the declared network as mDNS names, so they are present behind any
proxy.

**Can I fix this from JavaScript?** No. Whether a datagram can traverse the tunnel is a
property of the proxy, and a page cannot change it. Nothing in the library lets you force
UDP on, declare a public IP, edit a candidate or disable WebRTC: the library measures
the proxy before launch and the engine follows the result.

**What should the candidates look like through a working proxy?** If it is a SOCKS5
proxy whose UDP relay works and exits from the same address as its TCP, host candidates
as mDNS names plus a real `srflx` whose address is the proxy's exit address, and no
error. If the proxy cannot carry UDP, host candidates as mDNS names, no `srflx`, and
one `icecandidateerror` 701 after about eleven seconds.

**How do I test it without fooling myself?** Assert the list is non-empty and that its
host candidates are mDNS names, then check the case you expect: a `srflx` equal to the
proxy exit, or no `srflx` and exactly one 701. Run it through the real proxy rather than
localhost, repeat it, and diff against a stock Firefox on the same machine.

## Sources

- [RFC 8489, Session Traversal Utilities for NAT (STUN)](https://www.rfc-editor.org/rfc/rfc8489)
  - STUN runs over UDP to discover the public address behind a NAT.
- [RFC 1928, SOCKS Protocol Version 5](https://www.rfc-editor.org/rfc/rfc1928) - defines the
  UDP ASSOCIATE command that a residential exit typically leaves unimplemented.
- [RFC 8445, Interactive Connectivity Establishment (ICE)](https://www.rfc-editor.org/rfc/rfc8445)
  - the candidate types, including the server reflexive candidate a NAT produces.
- [RFC 8828, WebRTC IP Address Handling Requirements](https://www.rfc-editor.org/rfc/rfc8828)
  - the reduced default-route modes a page without media permission runs in.

**See also:** [WebRTC leaks through a proxy](webrtc-leak-proxy.md) for the opposite
failure of exposing your real address, [ICE candidate spoofing](webrtc-ice-candidate-spoofing.md)
for the priority and foundation of the candidates a gather produces, and [testing whether your browser
is detected](how-to-test-bot-detection.md) for the compare-against-stock method used
throughout.

---

*Written while maintaining [invisible_playwright](https://github.com/oss-invisible/playwright),
a Firefox patched at the C++ level driven by stock Playwright.*
