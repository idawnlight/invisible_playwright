---
title: "WebRTC IPv6 leak: why a proxy does not stop it"
description: "A stock browser sends WebRTC's UDP from the machine, not through the proxy, so an IPv6 candidate can still show the machine, and the disableIPv6 pref is not a reliable way to remove it. invisible produces no IPv6 candidate."
parent: "Network, Proxy and WebRTC"
grand_parent: "Guides"
nav_order: 11
---


# WebRTC IPv6 leak: why a proxy does not stop it

You routed everything through a proxy, the page shows the proxy's IPv4, and you
still got recognised across sessions. The address that gave you away was never on
the surface you were watching. In a stock browser it can be an IPv6 candidate
in WebRTC's list, on a transport the browser never sends through your SOCKS proxy,
and the pref everyone reaches for to turn it off is not a reliable fix.

This page is why that happens in a stock browser, how to see it in your own setup,
and what invisible_playwright does instead.

## The two transports, and which one your proxy carries

A [SOCKS5](https://datatracker.ietf.org/doc/html/rfc1928) proxy tunnels TCP through
its CONNECT command. Your HTTP requests, your TLS, your WebSocket traffic all ride
TCP and all exit at the proxy, which is why the page reads the proxy's address.

SOCKS5 also defines a UDP mode, the UDP ASSOCIATE command, but in practice most
residential exits do not implement it, or relay UDP from an address different from
the TCP exit. And a stock browser does not ask the proxy to relay WebRTC's UDP even
when the proxy could: its proxy setting covers TCP traffic, and the ICE stack sends
UDP from the machine's own interface. That is why a proxy alone does not stop the
leak in a stock browser, whatever the proxy supports.

WebRTC does not only report the address a STUN server sees. Before any of that, a
stock browser enumerates the host's own network interfaces and emits a
[**host candidate**](https://datatracker.ietf.org/doc/html/rfc8445) for each usable
local address. That enumeration is local. It does not travel through your proxy,
because there is nothing to travel: the browser is reading its own interface list
off the operating system.

On an IPv4-only host behind a proxy this is harmless. The host candidate is a
private LAN address, and a real browser masks it as an `<uuid>.local` mDNS name
anyway, which is [the shape a genuine Firefox produces](webrtc-leak-proxy.md). But
a host with working IPv6 has a **global** IPv6 address on that interface, routable
from anywhere, unique to the machine. Depending on the browser and its permissions,
an IPv6 host or server-reflexive candidate may appear in the candidate list, as a raw
address or behind an mDNS name. When it appears as a raw address, the page can read a
stable global address that your proxy never touched.

That is the leak. In a stock browser, the server reflexive candidate (the one
derived from a STUN round trip) is sent from the machine's own interface, outside
the proxy, so it carries the line's real public address rather than the proxy's
egress; an IPv6 candidate, when one is present as a raw address, shows the machine.
Both are in the same candidate list, and a detector that reads the list sees both.

| ICE candidate, stock browser | Where the address comes from | Does the proxy cover it? | What it reveals |
|---|---|---|---|
| `typ srflx` (server reflexive) | A STUN round trip over UDP, sent from the machine's own interface | No | The line's real public address |
| `typ host`, IPv4 | Local interface enumeration | No | A private LAN address, masked as `<uuid>.local` |
| `typ host`, IPv6 | Local interface enumeration | No | A global IPv6 of the machine, when it is not masked |

None of the three rows travels a road the proxy carries. The two `typ host` rows
come from the operating system's interface list, which is why the proxy never sees
them and an unmasked IPv6 one can expose a stable global address.

## Why the standard "disable IPv6" pref is not a reliable fix

The advice you will find everywhere is to toggle
`media.peerconnection.ice.disableIPv6` in about:config, or to flip the matching
Playwright/Selenium preference. Do not rely on it: the pref is not a reliable way to
remove IPv6 candidates from the list a page reads. Whether it changes anything in
your browser is something to check, not to assume, by reading the candidate list
with the pref set and without it.

This is the same class of trap covered in
[testing for presence rather than absence](how-to-test-bot-detection.md): a setting
that looks like it is doing something is not evidence that the signal moved. Read
the candidate list, and look for an IPv6 host or server-reflexive candidate, as a
raw address or behind an mDNS name.

## What the leak looks like in a report

You do not need a detector to see it. Ask WebRTC for its own candidates and read
them. Here is a page that gathers candidates in the proxied browser and prints
them, using stock Playwright methods on the real `Browser` object:

```python
from invisible_playwright import InvisiblePlaywright

PROXY = {"server": "socks5://gate.example.com:1080", "username": "u", "password": "p"}

GATHER = """
() => new Promise((resolve) => {
    const pc = new RTCPeerConnection({
        iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
    });
    const cands = [];
    pc.onicecandidate = (e) => {
        if (e.candidate === null) { resolve(cands); return; }
        cands.push(e.candidate.candidate);
    };
    pc.createDataChannel("x");
    pc.createOffer().then((o) => pc.setLocalDescription(o));
    setTimeout(() => resolve(cands), 4000);
})
"""

with InvisiblePlaywright(seed=42, proxy=PROXY) as browser:
    page = browser.new_page()
    page.goto("https://example.com")
    for line in page.evaluate(GATHER):
        print(line)
```

Each printed line is one ICE candidate. In a stock browser, the `typ host` lines
are the local interface addresses and the `typ srflx` lines are what STUN
reported. On a leaking stock setup you may see a `typ host` or `typ srflx` line
carrying a colon-bearing IPv6 address, and a srflx line that carries the machine's
public address instead of the proxy's. A raw global IPv6 string is the machine,
printed to the page.

Read the same list from a stock browser on the same host and compare the two, which
is the [only reliable way to tell a real value from a spoofed one](how-to-test-bot-detection.md).
A stock browser behind the same proxy sends WebRTC's UDP from the machine, so its
list can carry the machine's addresses. The list from invisible_playwright never
carries an IPv6 line. One known difference: with no proxy, on a machine that has
both IPv4 and IPv6, a retail browser also shows IPv6 candidates, while
invisible_playwright shows IPv4 only.

The snippet gives up after four seconds. When the proxy cannot carry UDP, gathering
ends after about eleven seconds, so a run of this snippet returns the host
candidates only.

## What actually stops it

Because the pref is not a reliable way to remove the candidate, invisible_playwright
does not rely on a pref. The browser does not read
the machine's network interfaces: each session declares one plausible home network
(one adapter, one private IPv4 drawn from the seed), and the host candidates are
`<uuid>.local` mDNS names for that declared network, never an address of the
machine. No IPv6 candidate, host or srflx, is produced in any session, with or
without a proxy, and the browser does no AAAA lookups. The global IPv6 address of
the machine cannot appear in the candidate list.

Candidates are produced by Firefox's own ICE code from real STUN and TURN replies.
No candidate is synthesised and no address is rewritten. What the `typ srflx` line
shows depends on the proxy. Before launch, and before a context that brings its own
SOCKS5 proxy, the library measures the proxy: it opens a SOCKS5 UDP ASSOCIATE, sends
a STUN Binding request through the relay, and compares the address the STUN server
saw with the proxy's TCP exit. The measurement fits in about three seconds and runs
in parallel with the exit discovery.

- **A SOCKS5 proxy whose UDP relay works and exits from the same address as its
  TCP.** The STUN datagram goes through the proxy's UDP relay, and the `typ srflx`
  line is the proxy's exit address.
- **A SOCKS5 proxy that cannot carry UDP** (ASSOCIATE refused, relay silent, UDP
  exits from another address, or a TCP exit that is IPv6), **a SOCKS4 proxy, or an
  HTTP or HTTPS proxy.** No datagram leaves, neither through the proxy nor from the
  machine. There is no `typ srflx` line, the host candidates are still there, and
  gathering ends with one `icecandidateerror` 701 for the STUN request after
  about eleven seconds.
- **No proxy.** The STUN datagram goes out directly from the machine, as in any
  browser, and when the STUN server answers the `typ srflx` line is the line's real
  public address.

With a proxy set, a datagram never leaves directly from the machine: there is no
fallback to direct. The caller cannot override the measurement: there is no option
that forces UDP on, declares a public IP, rewrites a candidate or disables WebRTC.

Run the code block above with a proxy and the IPv6 `typ host` line is not there,
whichever of the cases applies. What is left is host candidates with mDNS names,
plus a srflx line only when the proxy's UDP relay works.

One case is not supported: a machine that has only IPv6 and no proxy. The launch
fails with an error.

## Keep it true when you rotate the exit

The absence of IPv6 candidates holds across every session and every seed. The thing
that does not hold for free is agreement between the proxy and the rest of the
identity. When you [rotate to a new exit](how-to-rotate-proxies-playwright.md),
each proxy is measured on its own, and a `typ srflx` line appears only if the new
proxy's UDP relay works, with that proxy's exit address. The timezone, locale
and geolocation only follow if you let them, and a browser whose WebRTC says one
country while its clock says another is its own signal. That coupling is covered in
[when the timezone does not match the proxy](timezone-proxy-mismatch.md), and it is
worth checking on every rotation rather than once at setup.

## Conclusion

The IPv6 WebRTC leak survives a proxy in a stock browser for a simple reason: the
browser does not send WebRTC's UDP through the proxy, the leak is on interface
enumeration that never touches the proxy, and the pref named to stop it,
`media.peerconnection.ice.disableIPv6`, is not a reliable way to remove IPv6
candidates. In invisible_playwright the browser never reads the machine's interfaces and
produces no IPv6 candidate in any session. Read your own candidate list, compare it
against a stock browser, and confirm there is no IPv6 candidate and that no
datagram leaves from the machine when a proxy is set.

## Short answers to the questions that lead here

**Does a SOCKS5 proxy stop a WebRTC IPv6 leak?** Not in a stock browser. Its proxy
setting covers TCP traffic, and an IPv6 candidate comes from local interface
enumeration that never touches the proxy, so the machine's global IPv6 can still
appear. In invisible_playwright no IPv6 candidate is produced, with or without a
proxy.

**Does media.peerconnection.ice.disableIPv6 work?** Do not rely on it. It is not a
reliable way to remove IPv6 candidates, so read the candidate list: an IPv6 host or
server-reflexive candidate may appear, as a raw address or behind an mDNS name,
depending on the browser and its permissions.

**Why does my IPv4 show the proxy but I still got tracked?** Because in a stock
browser the page's HTTP traffic exits at the proxy, while WebRTC's STUN round trip
leaves from the machine's own interface and an IPv6 candidate can carry your real
machine address. One is disguised, the others are not.

**How do I see the leak myself?** Open an `RTCPeerConnection`, collect
`onicecandidate` events, and print them. A line with a colon-bearing global IPv6
address is the leak. The code block above does exactly this.

**Is an empty WebRTC result the safe outcome?** No. A completely blocked or empty
candidate list is a different shape from the one a typical consumer browser behind NAT
shows: a masked host candidate and, when the STUN server answers, a server reflexive
one. In invisible_playwright the host candidates (`<uuid>.local` names) are present in
every session; a server reflexive candidate is present when there is no proxy and the
STUN server answers, or when the proxy's UDP relay works.

**Does this come back when I rotate proxies?** The absence of IPv6 candidates holds
across rotations, but timezone and locale agreement with the new exit does not
follow automatically, so recheck those each time you change the exit.

## Sources

- [RFC 1928, SOCKS Protocol Version 5](https://datatracker.ietf.org/doc/html/rfc1928),
  retrieved 2026-08-29, which defines the CONNECT command a SOCKS5 proxy uses to
  tunnel TCP and the UDP ASSOCIATE command it can use to relay UDP, and says nothing
  about the interface enumeration a host candidate comes from.
- [RFC 8445, Interactive Connectivity Establishment (ICE)](https://datatracker.ietf.org/doc/html/rfc8445),
  retrieved 2026-08-29, which defines the host candidate as a binding to a local
  interface address, gathered independently of any STUN round trip.
- [RFC 8828, WebRTC IP Address Handling Requirements](https://www.rfc-editor.org/rfc/rfc8828),
  retrieved 2026-08-29, which sets the default mode for exposing a host's private
  IPv4 and IPv6 addresses as ICE candidates.
- [The IETF mDNS ICE candidates draft](https://datatracker.ietf.org/doc/html/draft-ietf-mmusic-mdns-ice-candidates-03),
  retrieved 2026-08-29, which documents the `<uuid>.local` name masking applied to the
  IPv4 host candidate referenced above.

**See also:** [what a WebRTC leak through a proxy looks like end to end](webrtc-leak-proxy.md),
[which ICE candidate fields a detector can check](webrtc-ice-candidate-spoofing.md),
and [testing for the presence of the right signal instead of the absence of a wrong one](how-to-test-bot-detection.md).

---

*Written while maintaining [invisible_playwright](https://github.com/oss-invisible/playwright),
a Firefox patched at the C++ level driven by stock Playwright. This page describes
how a stock browser can leak a real global address past a proxy that carries everything
else, and what the candidate list of invisible_playwright contains instead.*
