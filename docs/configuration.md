---
title: "Configuration"
description: "Backend selection, proxy schemes, DNS handling, timezone auto-derived from egress IP, and every environment variable the wrapper reads."
parent: "Documentation"
nav_order: 3
---


# Configuration

Configuration covers four surfaces: choosing the browser backend (`firefox` or
`chromium`), proxy schemes and credentials, how the session's timezone and locale are
derived from the egress IP, and optional environment variables for caching and binary
paths. None of these are required for a first run - `InvisiblePlaywright()` works with
defaults.

## Backend selection (`firefox` vs `chromium`)

Select the engine per session with `backend=` on `InvisiblePlaywright` (both sync and
async), or process-wide via `INVPW_BACKEND`:

```python
from invisible_playwright import InvisiblePlaywright

# Explicit per-session choice ("firefox" is the default when omitted)
with InvisiblePlaywright(seed=42, backend="chromium", headless=True) as browser:
    ...
```

```bash
# Or switch an existing script without editing code:
INVPW_BACKEND=chromium python script.py
```

If you use the lower-level `sync_playwright()` / `async_playwright()` API directly,
both `p.firefox` and `p.chromium` are registered on the `Playwright` object.

## Proxies

```python
proxy = {
    "server": "socks5://gate.example.com:1080",
    "username": "user",
    "password": "pass",
}
with InvisiblePlaywright(proxy=proxy, backend="firefox") as browser:
    ...
```

Schemes supported: `socks5`, `socks4`, `http`, `https`. The endpoint must include an
explicit port (`socks5://gate.example.com` without `:1080` raises a `ValueError` rather
than launching unproxied). On Firefox, SOCKS5 DNS and WebRTC routing are handled natively
in Necko; on Chromium, proxy routing is configured on the browser context / `--proxy-server`
flag alongside WebRTC non-proxied UDP protection (`disable_non_proxied_udp`). See
[Playwright SOCKS5 proxy with authentication](playwright-socks5-proxy-authentication.md)
for details.

## Timezone and locale

By default (`timezone="auto"`, `locale="auto"`), the wrapper probes your egress IP
(through `proxy` when one is set, or direct otherwise) and derives the matching IANA
timezone and regional `Accept-Language` / `navigator.language`:

```python
# Default: auto-derived from egress IP
with InvisiblePlaywright(proxy=proxy) as browser:
    ...

# Explicit IANA timezone and locale always take precedence
with InvisiblePlaywright(
    proxy=proxy,
    timezone="Europe/Berlin",
    locale="en-US",
    backend="chromium",
) as browser:
    ...
```

On Firefox, timezone and locale propagate through `StealthSessionDeclaration` across
Window, Worker, ICU, and HTTP headers. On Chromium, the launcher sets process `TZ` and
`--lang` / `Accept-Language` before spawning the browser so V8/ICU and Linux libc
`localtime_r` (`document.lastModified`) agree. See
[timezone and proxy mismatch](timezone-proxy-mismatch.md).

## Environment variables

Both engines download on first use, verify their SHA256 against the bundled seal, and
reuse the verified cache on later runs. Every variable below is optional:

| Variable | When you need it |
|---|---|
| `INVPW_BACKEND` | Default backend when `backend=` is omitted (`firefox` or `chromium`) |
| `INVISIBLE_PLAYWRIGHT_CACHE_DIR` | Relocate the Firefox engine and shared state cache (GeoIP database, font manifest); defaults to `~/.cache/invisible-playwright` |
| `INVISIBLE_PLAYWRIGHT_ENGINE_DIR` | Separate read-only Firefox engine store from writable state directory |
| `INVISIBLE_CHROMIUM_CACHE_DIR` | Relocate the cached Chromium release directory; defaults to `~/.cache/invisible-chromium` |
| `INVPW_BINARY_PATH` | Point Firefox at an existing binary and skip downloading |
| `INVPW_CHROMIUM_BINARY_PATH` | Point Chromium at an existing `chrome` binary and skip downloading |
| `STEALTHFOX_GITHUB_TOKEN` | Authenticate GitHub API/Release requests on rate-limited corporate or CI networks |
| `INVISIBLE_PLAYWRIGHT_SKEW=allow` | Allow running against a Playwright version outside the seal's tested range |
| `INVPW_CURSOR_ENGINE` | Cursor trajectory planner: `python` (default), `binary`, or `off` |
| `INVISIBLE_DOWNLOAD_DEADLINE` | Maximum seconds allowed for a single engine download (default `1800`; `0` disables the upper bound) |

```bash
export INVISIBLE_PLAYWRIGHT_CACHE_DIR=/mnt/engines/firefox
export INVISIBLE_CHROMIUM_CACHE_DIR=/mnt/engines/chromium
```

### Why the download has a deadline

Per-socket timeouts only bound silence between packets, not slow transfers. Checking an
overall transfer deadline between chunks prevents a stalled connection from hanging CI
or a headless launch indefinitely, while the `1800` second default accommodates links
down to ~140 KB/s for the ~236-245 MB browser archives.

## Next

[Pinning fingerprint fields](pinning.md) covers overriding specific GPU, screen, or
CPU concurrency values while keeping the rest of the session seed-derived.
