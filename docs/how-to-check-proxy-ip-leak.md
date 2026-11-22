---
title: "How to check if a proxy leaks your real IP"
description: "Check if a proxy leaks your real IP in Playwright by confirming the actual WebRTC, IPv6, DNS and timezone values, not just that a leak is absent."
parent: "Network, Proxy and WebRTC"
grand_parent: "Guides"
nav_order: 16
---


# How to check if a proxy leaks your real IP

The reliable proxy-leak check confirms the correct WebRTC, IPv6, DNS and timezone values
are present for the proxy's exit, not that the wrong ones are absent. Most checks do the
opposite: they are written as a list of things that must not appear - the real LAN
address must not show up, no IPv6 candidate, no DNS query outside the tunnel - and every
one of those assertions is also true of a browser that leaks nothing because it does
nothing.

That is the trap this page is about. A "no leak" result can mean the surface you were
testing is dead, and a dead surface is a worse tell than a leaky one, because no real
browser has it. Below is what to assert, where to run it, and runnable code.

## The check most people run, and why it can pass a dead browser

The usual WebRTC leak check asks two questions. Does a candidate contain my LAN IP? Does
an IPv6 address appear? If both answers are no, it prints "no leak" and stops.

A check that only asserts the absence of the real address also passes on a browser that
gathers nothing. The sensible negatives, the host candidate does not expose the LAN
address and zero IPv6 candidates appear, are all satisfied by a WebRTC that returns no
candidates of any kind, because a dead feature leaks nothing. Every negative assertion is
satisfied by a surface that never runs, so assert the positive shape instead.

The failure generalises past WebRTC. "No real IP in the DNS log" is satisfied by DNS that
never resolved. "Timezone does not disagree with the exit" is satisfied by a timezone API
that returned undefined. A checklist of absences cannot tell working from broken, which is
the one distinction that matters here.

## Positive form: confirm the values, do not just check they are gone

The fix is a rule you can apply to every surface: assert the presence of the right signal,
not the absence of a wrong one. A result that comes back empty, blocked, or still loading
is a failure, not a pass.

For WebRTC behind a proxy, the positive form has three parts, and all three must be
present for the check to pass:

- **A host candidate** whose address is a `<uuid>.local` mDNS name. A real Firefox
  obfuscates the LAN address this way by default. A raw `192.168.x.x` here is a leak; an
  absent host candidate is a broken browser.
- **The server-reflexive candidate that the proxy allows.** Behind a SOCKS5 proxy whose UDP
  relay works and exits from the same address as its TCP, expect a `typ srflx` whose address
  equals the proxy egress IP. This is what a real machine behind NAT produces, and its
  address is what a remote peer would see. Behind an HTTP or HTTPS proxy, SOCKS4, or a
  SOCKS5 proxy that cannot carry UDP, expect no `srflx` at all, and one `icecandidateerror`
  (701) for the STUN request when gathering ends, after about eleven seconds. No datagram
  leaves the machine in that case, so an absent `srflx` is the correct state, not a leak.
- **The gathering completes.** The page finishes, not "Computing" forever.

A patched engine driven through stock Playwright produces this shape on its own: the host
candidate is masked to `<uuid>.local`, and behind a SOCKS5 proxy whose UDP relay works the
server-reflexive candidate comes from a real STUN reply through that relay, so it carries the
proxy exit address. Your
job in a leak check is to confirm those values, not to hope for silence. For the full
account of why the standard "just disable WebRTC" advice fails this bar, see
[WebRTC leak with a proxy](webrtc-leak-proxy.md).

## Run it through the proxy, on a real page, not localhost

Two ways a leak check can be true and prove nothing.

**Localhost bypasses the proxy.** A page served from `127.0.0.1` is not routed through the
tunnel, so you measured the path production does not use. The egress you confirm has to be
the one your job actually exits through, which means loading a real remote page through the
same proxy configuration.

**A curl-level test is a different client.** Checking the exit IP with a command-line tool
tells you the tool's exit, not the browser's. Confirm the address from inside the browser,
on the same page, through the same proxy, because that is the client a site sees.

The general rule is to feed the check the same inputs production gets: the real proxy, a
real page, the browser you actually ship. Anything less is a test of something you do not
run.

## The four surfaces to confirm: WebRTC, IPv6, DNS, timezone

A real IP escapes through more than one channel, and each wants its own positive check.
At a glance, this is the value to confirm on each surface and the false "no leak" that
each one hides:

| Surface | Positive value to confirm | The false "no leak" it hides |
|---|---|---|
| WebRTC | Host masked to `.local`, gathering complete, and with a UDP-capable SOCKS5 proxy a server-reflexive equal to the egress | WebRTC gathered nothing at all |
| IPv6 | No IPv6 candidate, while the `.local` host candidates still appear | Gathering never ran, so no v6 could appear |
| DNS | Resolution happens at the proxy exit, not your local resolver | DNS never resolved |
| Timezone and locale | Browser clock matches the exit country | Timezone API returned undefined |

- **WebRTC** is the loud one, covered above: host masked to `.local`, gathering complete,
  and the server-reflexive equal to the egress when the proxy carries UDP.
- **IPv6** leaks past a v4-only proxy in a stock browser when the host has native IPv6 and a
  candidate carries a global v6 address. No IPv6 candidate is produced here, with or without
  a proxy. That is an absence you can only trust because the `.local` host candidates prove
  gathering ran.
- **DNS** should resolve through the proxy, not your local resolver. With a SOCKS proxy
  this is the [remote-DNS behaviour](does-a-proxy-leak-dns-doh-explained.md); confirm
  resolution happens at the exit rather than leaking your ISP's resolver. See
  [SOCKS5 versus HTTP proxy](socks5-vs-http-proxy-browser.md) for which schemes carry DNS
  through the tunnel.
- **Timezone and locale** are not an IP but they undo one. An exit in one country with a
  browser clock in another is a mismatch a detector cross-checks directly.
  [When the timezone does not match the proxy](timezone-proxy-mismatch.md) lists every
  surface that has to agree, and it is more than one value.

The header surface belongs on the same list, since request headers also have to line up
with the claimed platform: see [client hints and Sec-Fetch](client-hints-sec-fetch.md).

## A runnable check with invisible_playwright

Switching from stock Playwright is the launch line; every method after it is standard
Playwright. Install it first:

```bash
uv add invisible-playwright
```

Then gather ICE candidates through the proxy and assert the positive form. The `seed`
makes the run reproducible, so a failure can be replayed rather than guessed at:

```python
from invisible_playwright import InvisiblePlaywright

PROXY = {"server": "socks5://gate.example.com:1080", "username": "u", "password": "p"}

with InvisiblePlaywright(seed=42, proxy=PROXY) as browser:
    page = browser.new_page()
    page.goto("https://example.com")   # a real remote page, loaded through the proxy

    candidates = page.evaluate("""
        async () => {
            const pc = new RTCPeerConnection({
                iceServers: [{urls: 'stun:stun.l.google.com:19302'}]
            });
            pc.createDataChannel('probe');
            const lines = [], errors = [];
            pc.onicecandidate = e => { if (e.candidate) lines.push(e.candidate.candidate); };
            pc.onicecandidateerror = e => errors.push(e.errorCode);
            await pc.setLocalDescription(await pc.createOffer());
            // Wait for gathering to end; the 25 s is only a safety net (a proxy that cannot
            // carry UDP ends gathering, and delivers the 701, at about eleven seconds).
            await new Promise(r => {
                const cap = setTimeout(r, 25000);
                pc.onicegatheringstatechange = () => {
                    if (pc.iceGatheringState === 'complete') { clearTimeout(cap); r(); }
                };
            });
            pc.close();
            return {lines, errors};
        }
    """)
    errors = candidates["errors"]
    candidates = candidates["lines"]

    host  = [c for c in candidates if "typ host"  in c]
    srflx = [c for c in candidates if "typ srflx" in c]

    # POSITIVE form: gathering must COMPLETE and produce what this proxy allows.
    # This example assumes a SOCKS5 proxy whose UDP relay works: both kinds expected.
    # Behind a proxy that cannot carry UDP, expect host candidates only (see below).
    # An empty list here is a FAIL, not a clean pass.
    assert host,  "FAIL: no host candidate - WebRTC produced nothing"
    assert srflx, "FAIL: no server-reflexive candidate - this proxy carries no UDP, or gathering failed"

    print("host :", host[0])
    print("srflx:", srflx[0])
```

Now read the addresses out of the candidate strings and check them against the values they
should hold. A WebRTC candidate is a space-separated line, and the address is the fifth
field:

```python
    def address(candidate):
        # "candidate:<foundation> <comp> udp <prio> <ADDRESS> <port> typ ..."
        return candidate.split()[4]

    host_addr  = address(host[0])
    srflx_addr = address(srflx[0])

    # Fetch the exit IP through the SAME proxy, from inside the browser.
    egress = page.evaluate(
        "() => fetch('https://example.com/ip').then(r => r.text())"  # your own IP-echo endpoint
    ).strip()

    assert host_addr.endswith(".local"), f"FAIL: host is a raw LAN IP: {host_addr}"
    assert srflx_addr == egress,         f"FAIL: srflx {srflx_addr} != egress {egress}"

    # No IPv6 candidate should appear, but only trust that because
    # the host candidates above prove gathering actually ran.
    assert not any(":" in address(c) for c in candidates), \
        "FAIL: an IPv6 candidate leaked past the proxy"

    print("egress confirmed inside the browser:", egress)
```

Behind a proxy that cannot carry UDP (an HTTP or HTTPS proxy, SOCKS4, or a SOCKS5 proxy
whose relay refuses, stays silent or exits from another address), the same page gives the
other correct shape: host candidates only, no `srflx`, and one `icecandidateerror` (701)
for the STUN request after about eleven seconds, which the script above sees because it
listens to `icecandidateerror` and waits for gathering to end (`errors == [701]`). Assert that `host` is non-empty, that no
candidate is a real address of your machine, and that `srflx` is empty. The library decides
this itself by measuring the proxy before launch, and no option forces UDP on.

Finally, confirm the browser clock follows the exit rather than your own machine:

```python
    tz = page.evaluate("() => Intl.DateTimeFormat().resolvedOptions().timeZone")
    print("browser timezone:", tz)   # must match the exit country, not your host
```

Every one of these asserts a value that must be present and correct. None of them passes on
a browser that returned nothing, which is the whole point. Run the whole thing at least ten
times and open a screenshot of the leak page as well as reading the log, because a text log
shows what your code extracted and a screenshot shows what the page actually rendered.

## Conclusion

A proxy leak check is only as good as what it asserts. Written as a list of absences it
will, sooner or later, hand a clean bill to a browser whose WebRTC, DNS or timezone is
simply dead, and a dead surface is a stronger tell than a leaky one because no real browser
has it. Confirm the real values instead: a `.local` host candidate, a server-reflexive
address equal to the exit whenever the proxy carries UDP, DNS through the tunnel, a clock
that matches the country. Run it
through the proxy on a real page, repeat it, and read the screenshot. Do that and "no leak"
starts meaning what you wanted it to mean.

## Short answers to the questions that lead here

**How do I check if my proxy leaks my real IP?** Load a real page through the proxy, gather
WebRTC candidates in the browser, and assert the host candidate is a `.local` name and, when
the proxy carries UDP, that the server-reflexive address equals your exit IP. Confirming the
correct values beats checking that the wrong ones are absent.

**My leak test says no leak. Am I safe?** Not necessarily. "No leak" is also what a dead
WebRTC, a failed DNS lookup, or an undefined timezone produces. Check that the surface
actually returned the right value, not just that it returned nothing bad.

**Should I just disable WebRTC to stop the leak?** No. A browser with WebRTC switched off is
a rare and detectable state. Real browsers gather candidates; the fix is to mask the LAN
address and send WebRTC's UDP through the proxy when it can carry it, with no datagram
leaving the machine otherwise, not to kill the feature.

**Can I test the leak on localhost?** No. Localhost bypasses the proxy, so you measured the
path you do not deploy. Load a remote page through the same proxy the job uses and confirm
the egress from inside the browser.

**Why does the WebRTC IP differ from my proxy IP?** In a stock browser the server-reflexive
candidate is the line's real address: the proxy setting covers TCP, and ICE sends its UDP
from the machine's own interface. With this library a `srflx` is the proxy's exit or it is
absent. If it is absent, the proxy cannot carry UDP and no datagram left the machine, which
is correct for such a proxy. If there are no candidates at all, gathering was blocked, which
is a separate failure.

**Does an IPv6 address leak through an IPv4 proxy?** In a stock browser it can, when the host
has native IPv6 and the proxy only carries v4. Here no IPv6 candidate is produced, with or
without a proxy. Confirm none appears while the `.local` host candidates still do, so you
know gathering ran.

**See also:** [WebRTC leak with a proxy](webrtc-leak-proxy.md) for why the standard fixes
fail, [when the timezone does not match the proxy](timezone-proxy-mismatch.md) for the
surfaces that must agree with the exit, and
[how to test whether your browser is detected](how-to-test-bot-detection.md) for the
compare-against-a-real-browser method these checks sit inside.

## Sources

- The public leak and fingerprint pages read from their own output, including a site that shows your IP and WebRTC candidates
  for per-surface values and a fingerprint test page for how a suppressed signal is recorded rather than
  ignored.
- Playwright's documented [proxy configuration](https://playwright.dev/python/docs/network)
  and `page.evaluate`, used unchanged.

---

*Written while maintaining [invisible_playwright](https://github.com/oss-invisible/playwright),
a Firefox patched at the C++ level driven by stock Playwright. The rule is written in the
positive form because a list of absences cannot tell a working surface from a dead one.*
