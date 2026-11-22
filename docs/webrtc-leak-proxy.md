---
title: "WebRTC leak with a proxy in Playwright and Selenium"
description: "A SOCKS5 proxy does not stop a WebRTC leak, and disabling WebRTC just trades the leak for a detectable signature. What actually leaks, and how to check."
parent: "Network, Proxy and WebRTC"
grand_parent: "Guides"
nav_order: 1
---


# WebRTC leak with a proxy in Playwright and Selenium

In a stock browser a proxy does not stop a WebRTC leak: the browser's proxy
setting covers TCP traffic, but WebRTC's STUN step is UDP sent from the
machine's own interface, so the browser asks a STUN server directly and the real
IP comes back. SOCKS5 does define a UDP mode (UDP ASSOCIATE), and a stock browser
still does not use it for WebRTC, even when the proxy could carry it. Disabling
WebRTC avoids that leak, but a browser with zero ICE candidates is its own
signature.

"My browser leaks my real IP while using a proxy." "Playwright leaks IP with proxy."
"How do I prevent WebRTC leaks in Selenium?" Every answer to those questions ends the
same way: disable WebRTC, or set
`--force-webrtc-ip-handling-policy=disable_non_proxied_udp`.

That advice passes every WebRTC leak test and fails at the only thing that matters.

This page covers what actually leaks, why the standard fix creates a bigger problem
than it solves, and how to test WebRTC so that a check cannot print green over a
browser whose WebRTC is completely dead.

## What a real Firefox behind NAT emits

A real Firefox behind NAT emits exactly two ICE candidates: one host
candidate carrying a random mDNS name instead of the LAN address, and one
srflx candidate carrying the public IP learned from a STUN server. Start
here, because almost nobody states this baseline explicitly.

Open a `RTCPeerConnection` on an ordinary consumer machine and you get, at minimum:

- one **host** candidate, whose address is not your LAN IP but a random
  `<uuid>.local` name
- one **srflx** (server reflexive) candidate, whose address is your **public** IP,
  learned from a STUN server

Two candidates. One meaningless, one your public address. That is the shape.

The `.local` part surprises people. Firefox sets
`media.peerconnection.ice.obfuscate_host_addresses` to `true` by default, so the LAN
address is replaced with an mDNS name. It has been the default for years.

## The three failure states, in increasing order of how bad they are

**1. You leak the LAN IP.** The host candidate is `192.168.x.x`. This is what leak
tests are built to catch, and it is the least interesting failure.

**2. You leak the real public IP while claiming another one.** You are on a proxy,
your HTTP traffic exits from one address, and the srflx candidate names a different
one. STUN runs over UDP, and a stock browser's proxy setting covers TCP. So the
browser asks a STUN server directly, from the real interface, and the STUN server
truthfully answers with the real address. Two addresses for one session, and the
contradiction is the signal, not either address on its own.

**3. You emit nothing at all.** No candidates, or the connection never completes.
This is the one people create on purpose, and it is the worst of the three.

Almost nobody browses with WebRTC broken. It is on by default and it stays on,
because video calls are normal. A session with zero ICE candidates has told the page
something much more specific than an IP address: it has said *this browser has been
modified*. An unusual IP is a weak signal. A missing capability is a strong one.

## SOCKS5 has a UDP mode. Does that close the leak?

SOCKS5 defines a UDP command, UDP ASSOCIATE
([RFC 1928](https://www.rfc-editor.org/rfc/rfc1928)), so "SOCKS5 is TCP only" is
wrong. Two things keep it from closing the leak:

- Most residential exits do not implement it, or relay UDP from an address that is
  different from the TCP exit.
- A stock browser does not ask the proxy to relay WebRTC's UDP even when the proxy
  could. Its proxy setting covers TCP traffic, and the ICE stack sends UDP from the
  machine's own interface. A UDP-capable SOCKS5 proxy therefore does not stop the
  leak in a stock browser.

With this library the answer is different, and it is spelled out in
[Where this project stands](#where-this-project-stands).

## What about `disable_non_proxied_udp` and `media.peerconnection.enabled=false`?

These are the two answers you will find, and they are worth taking separately.

**`--force-webrtc-ip-handling-policy=disable_non_proxied_udp`** is a Chromium flag.
It stops WebRTC from using any network path that does not go through the proxy. Unless
the proxy relays UDP, STUN has no path, and most residential exits do not relay it, so
in practice no srflx candidate is gathered. You have converted failure state 2 into
failure state 3.
It does prevent the leak. It also produces a browser that cannot gather a normal
candidate set, which is a condition a real Chrome almost never ends up in.

**`media.peerconnection.enabled=false`** is the Firefox equivalent and it is blunter:
`RTCPeerConnection` stops existing. A page can detect that in one line, without
setting up a connection at all. Firefox has no equivalent of the Chromium handling
policy, which is part of why this preference gets recommended so often.

Both work as leak prevention. Neither is invisible, and which one you want depends
entirely on whether anything is looking. If you are protecting your own privacy while
browsing, disabling WebRTC is a reasonable trade and you should stop reading here. If
you are trying to look like an ordinary browser, you have swapped a leak for a
signature.

The third option is the harder one: let WebRTC run, and make sure the only public
address it can ever report is the proxy's exit address.

## Testing WebRTC: assert the positive shape, on the shipped configuration

A WebRTC check that only asserts the absence of a bad signal (the host candidate does
not expose the LAN address, no IPv6 candidate appears) passes on a browser whose WebRTC
is dead, because a feature that gathers nothing leaks nothing. Such a check cannot tell
"working correctly" from "not working".

Assert the positive shape instead: the page has to **complete**, the host candidate has
to be a `.local` name, and with a SOCKS5 proxy whose UDP relay works the srflx has to be
present and has to equal the proxy's exit address. An empty result is a failure.

> A check that only asserts the absence of a bad signal will pass when the feature is
> broken. Assert the presence of the right signal instead.

The same shape shows up in [fonts](headless-fonts-differ.md), in [canvas](canvas-fingerprint-noise.md), in [audio](audiocontext-fingerprinting.md). Anything you can suppress will
pass a suppression-based test.

There is a second, separate rule: test the configuration that ships. A test that sets
the WebRTC configuration itself, through explicit overrides, validates that
combination and not the default session a user gets with no extra arguments. Its
assertions can be fine and still aimed at a target nobody runs. Run the check against
the shipped defaults, with no overrides, so that a pass describes a real session.

## `media.peerconnection.ice.disableIPv6` does nothing in current Firefox

This one is worth stating plainly, because the preference is still recommended in a
lot of places.

The ICE gathering code of current Firefox does not read it. What exists is `RESOLVE_DISABLE_IPV6` for DNS and a `DISABLE_IPV6` flag
on TCP sockets. Neither is consulted when candidates are gathered.

The consequence, if your machine has a routable IPv6 address: WebRTC emits a host
candidate carrying that real global IPv6, and a proxy setting does nothing about it
because the proxy covers TCP traffic while ICE reads the machine's own interfaces. Your
IPv4 can be perfectly proxied while your IPv6 sits in the SDP in plain text.

Set the preference, verify it changed nothing, and then go looking for an actual
fix. In this library the browser does not read the machine's interfaces at all: the
session declares one plausible home network with no IPv6, so no IPv6 candidate is
produced, with or without a proxy, and the browser does no AAAA lookups.

## Why a stock Firefox behind a proxy can gather nothing

A page that has not been granted camera or microphone permission causes Firefox to
gather in a [restricted mode](https://www.rfc-editor.org/rfc/rfc8828): only the
address of the **default route**, instead of every local interface. That mode is a
privacy feature and it is the normal case, since most pages never ask for a camera.

In a stock Firefox, finding the default route works by opening a UDP socket
"connected" to the remote address of the document and reading back which local address
the OS picked. Behind a SOCKS proxy that remote host is not directly reachable, so the
probe can fail, and a proxied page can end up with no candidates at all. The lesson
holds even if you never touch browser source: **behind a proxy, a stock browser's
WebRTC can fail for reasons that have nothing to do with WebRTC**, and the error
surfaces as silence.

In this library the host candidates come from the network the session declares, so
they are present behind any proxy, as mDNS `<uuid>.local` names.

## Why a made-up candidate gives itself away

Suppose someone supplies the srflx candidate by hand, so it names the proxy's exit
address. The address is the easy part. The candidate carries several other fields, and
each of them is checkable:

- **Priority** is computed by [a documented formula](https://www.rfc-editor.org/rfc/rfc8445#section-5.1.2)
  from the candidate type, the interface preference and the component id. A value that
  no genuine candidate on that stack produces stands out on its own.
- **Foundation** is an identifier shared by candidates of the same type from the same
  base. A real one is stable; one that changes between runs, or collides with the
  foundation of a candidate of another type, is a tell.
- **The `raddr` and `rport` fields**, and the relationship between a candidate and the
  base it was derived from.
- **Timing.** A real srflx candidate arrives after a network round trip to a STUN
  server. One that shows up faster than that round trip is checkable.

A hand-made candidate is not one value to get right. It is a small structure that has
to be internally consistent and consistent with the other candidates in the same SDP.
This is the same lesson as every other fingerprint surface, arriving in a different
costume. This library does not go down that road: every candidate comes from Firefox's
own ICE code, from real STUN/TURN replies, and no address is rewritten.

## How to check your own setup

Run a WebRTC test page through the proxy you actually deploy with. Not localhost:
localhost bypasses the proxy and tests nothing. A public site that shows your IP and WebRTC candidates
lists the full candidate set, and a public fingerprint test page
shows what a real detector extracts from it.

What you want to see:

- The page **completes**. A stuck or empty result is a failure, not a pass.
- Host candidate is a `<uuid>.local` name. Not a LAN IP, and not absent.
- With a SOCKS5 proxy whose UDP relay works, an srflx candidate exists and its address
  equals the exit address of your proxy. With any other proxy there is no srflx, and
  gathering ends with one `icecandidateerror` 701 after about eleven seconds (see the
  table below).
- No IPv6 candidate.
- The same page in a stock Firefox, side by side, differing only in the IP (or, with a
  proxy that cannot carry UDP, in the missing srflx).

That last one is the check that catches everything else. If your browser's output
differs from a normal Firefox in any field except the address, that field is the
problem.

## Where this project stands

Candidates are produced by Firefox's own ICE code from real STUN/TURN replies. No
candidate is made up and no address is rewritten. The browser does not read the
machine's network interfaces: each session declares one plausible home network (one
adapter, one private IPv4 drawn from the seed), and the host candidates are mDNS
`<uuid>.local` names, never an address of the machine. No IPv6 candidate is produced,
with or without a proxy.

Before launch, and before a context that brings its own SOCKS5 proxy, the library
measures the proxy. It opens a SOCKS5 UDP ASSOCIATE, sends a STUN Binding request
through the relay, and compares the address the STUN server saw with the proxy's TCP
exit. Only if a reply comes back and the two addresses are equal does the browser send
WebRTC's UDP through the relay. The measurement fits in about three seconds and runs in
parallel with the exit discovery. Anything else (refused, no reply, a different
address, an IPv6 TCP exit, a proxy that is not SOCKS5) means the browser treats the
proxy as a router that drops UDP. With the defaults, the exit address is resolved at
launch by the library, in the same lookup that resolves the timezone, so the two cannot disagree.

| Session | Where the STUN datagram goes | `srflx` candidate | `host` candidates | ICE error |
|---|---|---|---|---|
| SOCKS5 proxy whose UDP relay works and exits from the same address as its TCP | Through the proxy's UDP relay (SOCKS5 UDP ASSOCIATE) | Present, and it is the proxy's exit address | mDNS names (`<uuid>.local`), UDP and TCP | None |
| SOCKS5 proxy that cannot carry UDP (ASSOCIATE refused, relay silent, UDP exits from another address), or SOCKS4 | Nowhere: no datagram leaves, neither through the proxy nor from the machine | Absent | mDNS names, UDP and TCP | One `icecandidateerror` 701 for the STUN request, after about eleven seconds, when gathering ends |
| HTTP or HTTPS proxy (CONNECT) | Nowhere, same as above: CONNECT carries no UDP | Absent | mDNS names, UDP and TCP | Same as above |
| No proxy | Directly from the machine, as in any browser | Present when the STUN server answers: the line's real public address | mDNS names | None |

With a proxy set, no datagram ever leaves directly from the machine: there is no
fallback to direct. The caller cannot override the measurement. There is no option that
forces UDP on, declares a public IP, rewrites a candidate or disables WebRTC.

So, to the question "if I use a UDP-capable SOCKS5 proxy there is no leak, right?":
with a stock browser, no, because the browser never sends WebRTC's UDP through the
proxy. With this library, the UDP goes through the relay when the relay works and exits
from the same address as the TCP, and otherwise no datagram leaves at all.

A few more details that hold in every proxied session:

- Each context uses its own proxy for WebRTC: a context created with
  `browser.new_context(proxy=...)` and its own SOCKS5 proxy gets its own `srflx`.
- The STUN servers used for the measurement can be chosen with
  `udp_stun_servers=["192.0.2.10:3478"]` on the constructor (IPv4 literals only); by
  default two public ones are used. They are used by the Python process only.
- The names of STUN/TURN servers in `iceServers` are resolved by the browser over
  DNS-over-HTTPS through the proxy of that context, never by the machine's resolver.
  Without a proxy the system resolver is used, as in retail Firefox.
- TURN over TCP goes through the context's proxy.
- HTTP/3 follows the same road: over the relay when the proxy carries coherent UDP,
  otherwise the browser falls back to HTTP/2.

Nothing here is turned off. The goal throughout is a browser that emits what a real
browser on that network would emit: behind a router that drops UDP, host candidates and
no server-reflexive one.

## Short answers to the questions that lead here

**Does a SOCKS5 proxy stop a WebRTC leak?** Not in a stock browser. SOCKS5 defines a UDP
mode (UDP ASSOCIATE), but most residential exits do not implement it, and a stock
browser does not route WebRTC's UDP through the proxy even when the proxy could. The
STUN request leaves from the real interface and the answer names the real address. The
proxy is working correctly; WebRTC is simply not going through it. With this library
the UDP goes through the relay when the relay works and exits from the same address as
the TCP; otherwise no datagram leaves at all.

**If I use a UDP-capable SOCKS5 proxy, there is no leak, right?** With a stock browser,
no: UDP-capable or not, the proxy is not asked to carry WebRTC's UDP. With this library
the proxy is measured first, and the UDP goes through the relay only if the relay
answers and exits from the same address as the proxy's TCP.

**Does an HTTP proxy stop it?** Not in a stock browser: CONNECT carries no UDP, so the
STUN request leaves from the real interface. With this library no datagram leaves at
all, and there is no srflx candidate.

**Does Playwright's `proxy=` option stop it?** Not in stock Playwright. It configures
the browser's TCP path. ICE gathering is a separate mechanism. In this library the
`proxy=` option of `InvisiblePlaywright` and of `browser.new_context` covers WebRTC as
described in the table above.

**Why does my IP leak in Firefox but not in Chrome, or the other way round?** The two
engines gather candidates differently and expose different controls. Chromium has a
handling policy flag; Firefox does not. Test the engine you actually ship.

**Is `media.peerconnection.ice.disableIPv6` the fix for an IPv6 leak?** Not in current
Firefox. See the section above: the preference is not read during ICE gathering. In
this library no IPv6 candidate is produced, with or without a proxy.

**Will a VPN fix it?** Usually yes, because a VPN moves the whole interface rather
than just the browser's TCP traffic, so STUN leaves through the tunnel too. That is
the actual difference between a VPN and a proxy here.

**Is it enough to check a leak test page?** Only if the page completes and shows a
full candidate set. A leak test that reports "no leak" over a browser with WebRTC
disabled is reporting the absence of a feature, not the presence of protection.

## Sources

- A read of the current Firefox WebRTC gathering code confirming that
  `media.peerconnection.ice.disableIPv6` is honored only for DNS resolution and TCP
  sockets, never on the ICE candidate gathering path.
- [RFC 8828, WebRTC IP Address Handling Requirements](https://www.rfc-editor.org/rfc/rfc8828),
  retrieved 2026-08-28, which defines the default-route restricted mode a page without
  camera or microphone permission triggers.
- [RFC 8445, Interactive Connectivity Establishment (ICE)](https://www.rfc-editor.org/rfc/rfc8445#section-5.1.2),
  retrieved 2026-08-28, section 5.1.2, which defines the candidate priority formula.
- [Chrome for Developers, `chrome.privacy` API reference](https://developer.chrome.com/docs/extensions/reference/api/privacy),
  retrieved 2026-08-28, documenting the `webRTCIPHandlingPolicy` values, including
  `disable_non_proxied_udp`, behind the Chromium flag.
- A public WebRTC test page that shows your IP and candidates, retrieved
  2026-08-28, for checking the full ICE candidate set through a live proxy.
- A public fingerprint test page, retrieved 2026-08-28, for what a
  real detector extracts from the candidate set.

**See also:** [when the timezone does not match the proxy](timezone-proxy-mismatch.md),
the other half of "the browser and the network disagree",
[what privacy.resistFingerprinting really does](resist-fingerprinting.md),
another case where the protective setting is itself visible, and
[the checklist for being detected on one site](playwright-detected-as-bot.md).

---

*From the notes of [invisible_playwright](https://github.com/oss-invisible/playwright).*
