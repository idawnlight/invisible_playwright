---
title: "Quickstart"
description: "Two-line switch from Playwright with parallel Firefox 160 and Chromium 157 stealth backends across sync and async APIs."
parent: "Documentation"
nav_order: 2
---


# Quickstart

**100% Playwright-compatible** - sync and async, zero new page or locator methods to
learn beyond how the browser is launched. Switching existing Playwright code takes two
lines:

```diff
- from playwright.sync_api import sync_playwright
- with sync_playwright() as p:
-     browser = p.firefox.launch()
+ from invisible_playwright import InvisiblePlaywright
+ with InvisiblePlaywright() as browser:
```

Every session gets a deterministic or fresh hardware/graphics identity and Bezier-curve
humanized mouse and keyboard input out of the box.

## Choosing a backend (`firefox` vs `chromium`)

`InvisiblePlaywright` and `invisible_playwright.async_api.InvisiblePlaywright` both
accept `backend="firefox"` (the default) or `backend="chromium"` (or the
`INVPW_BACKEND` environment variable):

| Backend | Engine | OS & Hardware Persona | Best When |
|---|---|---|---|
| `backend="firefox"` (default) | Patched Firefox 160.0 (`-juggler-pipe`) | Coherent **Windows 10/11 64-bit** desktop persona on Linux (119 bundled Windows fonts, ANGLE GPU personas, WebGL readback projection, native SOCKS5/ICE routing) | You want diverse Windows desktop identities (~400 fields) from a Linux host |
| `backend="chromium"` | Patched Chromium 157.0.8103.78 (`--remote-debugging-pipe`) | Authentic **Linux x86_64 Desktop** Chromium with server CPU/RAM/V8-heap tiers normalized (`2..32 GiB`), GPU-less `llvmpipe` normalized to Intel Mesa `iris` OpenGL 4.6, `webdriver === false`, and zero `Runtime.enable` leaks | Target sites require Blink/Chromium behavior or TLS/HTTP3 stack while running in GPU-less Linux containers |

## Sync

```python
from invisible_playwright import InvisiblePlaywright

with InvisiblePlaywright(
    seed=42,
    backend="chromium",  # or "firefox" (default)
    headless=True,
    proxy={"server": "socks5://gate.example.com:1080", "username": "u", "password": "p"},
) as browser:
    page = browser.new_page()
    page.goto("https://example.com")
    page.click("#submit")   # mouse arcs to the button along a paced Bezier curve
```

## Async (including simultaneous dual-engine sessions)

The same constructor shape works under `asyncio`, and both backends can even run side
by side in the same process and event loop:

```python
import asyncio
from invisible_playwright.async_api import InvisiblePlaywright

async def main() -> None:
    async with (
        InvisiblePlaywright(seed=42, backend="firefox", headless=True) as fx_browser,
        InvisiblePlaywright(seed=42, backend="chromium", headless=True) as cr_browser,
    ):
        fx_page = await fx_browser.new_page()
        cr_page = await cr_browser.new_page()
        await asyncio.gather(
            fx_page.goto("https://example.com"),
            cr_page.goto("https://example.com"),
        )
        await fx_page.click("a")
        await cr_page.click("a")

asyncio.run(main())
```

The `browser` object yielded by the context manager (or `BrowserContext` when
`profile_dir=` is passed) is a genuine `playwright.sync_api.Browser` /
`playwright.async_api.Browser` - locators, `frame_locator`, `expect()`, `page.route()`,
dialogs, screenshots, and cookies work identically across both backends.

## Logging the seed to replay a run

Every session is generated from an integer seed, whether you pass one or let the
launcher draw one. Log `sf.seed` to replay the exact same session persona later:

```python
sf = InvisiblePlaywright(backend="firefox")
with sf as browser:
    print("seed =", sf.seed)
```

## Reproducible fingerprint

Pass `seed=` explicitly and every seed-derived parameter comes back identical across
runs:

```python
with InvisiblePlaywright(seed=42, backend="chromium") as browser:
    ...   # deterministic hardware tier, memory/heap limit, and GPU persona every run
```

See [giving an agent a reproducible browser identity](reproducible-agent-browser-identity-seed.md)
for what a browser seed pins and what still depends on network egress and profile state.

## Short answers to common questions

**Do I need different Playwright code when switching between `firefox` and `chromium`?**
No. Only `backend="firefox"` vs `backend="chromium"` (or `INVPW_BACKEND`) changes; your
page, locator, routing, and `expect()` calls stay identical.

**How do I persist cookies and `localStorage` across restarts?** Pass
`profile_dir="/path/to/profile"`. In persistent-profile mode the context manager yields
a `BrowserContext` directly (`page = ctx.new_page()`), and works on both `firefox` and
`chromium`.

**Where do I configure proxies, timezones, cache paths, or pinned hardware fields?**
See [Configuration](configuration.md) for proxies, egress-derived timezones, and
environment variables, and [Pinning fingerprint fields](pinning.md) for overriding
individual fields on top of a seed.
