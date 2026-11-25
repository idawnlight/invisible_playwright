# InvisiblePlaywright on Firefox 160.0.1 and Chromium 158.0.8127.36

`invisible_playwright` is the unified Python Playwright entrypoint for two
native stealth browser engines:

- **`backend="firefox"` (default)**: Firefox 160.0.1
  ([`firefox-160.0.1-r1`](https://github.com/idawnlight/firefox_antidetect_patch/releases/tag/firefox-160.0.1-r1)),
  declaring a coherent Windows 10/11 desktop persona on Linux through native
  Gecko declarations, 119 bundled Windows fonts, and isolated Juggler worlds.
- **`backend="chromium"`**: Chromium 158.0.8127.36
  (`chromium-158.0.8127.36-r2`), presenting a genuine Linux Desktop Chromium
  session that normalizes server CPU/RAM/V8-heap tiers and GPU-less Mesa
  `llvmpipe` WebGL1/2 parameters while driving `--remote-debugging-pipe` with
  `navigator.webdriver === false` and zero `Runtime.enable` leaks.

This release pairs `invisible_core 160.81.0` with `invisible_playwright 0.58.5`
and bundles the Playwright Python 1.61 client (both `sync_api` and `async_api`).

## Install

Supported platforms: **Linux x86_64**;
Python **3.11+** and Git are required. The Firefox archive is approximately
280 MB (and the Chromium archive is similarly sized); each engine is
downloaded on first use, verified against its pinned SHA256, and cached locally.

Install both Python packages from their GitHub tags:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install \
  "invisible-core @ git+https://github.com/idawnlight/invisible_core.git@v160.81.0" \
  "invisible-playwright @ git+https://github.com/idawnlight/invisible_playwright.git@v0.58.5"
```

See [the installation guide](docs/installation.md) for Ubuntu/Linux system
packages (Xvfb, Openbox, GTK/NSS/Mesa libraries, and desktop fonts), cache
paths, and offline installation.

## Quickstart

Switch between Firefox and Chromium with `backend="firefox"` or
`backend="chromium"` (or set `INVPW_BACKEND=chromium` in the environment) while
keeping the exact same Playwright API, selectors, `expect()` assertions, and
Bezier-curve humanized input:

```python
from invisible_playwright import InvisiblePlaywright

# Windows desktop persona on patched Firefox 160.0.1 (default)
with InvisiblePlaywright(seed=42, headless=True, backend="firefox") as browser:
    page = browser.new_page()
    page.goto("https://example.com/")
    print("Firefox:", browser.version, page.title())

# Linux desktop persona on patched Chromium 158.0.8127.36
with InvisiblePlaywright(seed=42, headless=True, backend="chromium") as browser:
    page = browser.new_page()
    page.goto("https://example.com/")
    print("Chromium:", browser.version, page.title())
```

The async API (`invisible_playwright.async_api.InvisiblePlaywright`) accepts
the exact same arguments and can even run both engines concurrently in one
event loop; see [Quickstart](docs/quickstart.md) and
[Configuration](docs/configuration.md).

### Serving one browser to many clients

`launch_server()` (and `launch_server_async()`) serves one stealth Firefox or
Chromium browser over a WebSocket to any Playwright client (Python, Node.js,
Java, .NET, Go). Install the WebSocket extra with `pip install "websockets>=13"`
or `uv add "invisible-playwright[remote]"`:

```python
# server.py
from invisible_playwright import launch_server

with launch_server(seed=42, headless=True, backend="firefox") as server:
    print(server.ws_endpoint)          # ws://127.0.0.1:<port>/<secret>
    input("serving, press Enter to stop")
```

```python
# client.py: any Playwright client; pass p.chromium.connect(...) when backend="chromium"
import sys
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.firefox.connect(sys.argv[1])
    page = browser.new_page()
    page.goto("https://example.com")
    print(page.title())
    browser.close()
```

Closing a client connection leaves the shared browser running; the browser
belongs to the server and shuts down on `server.close()` (or when exiting the
`with` block).

### Built-in Playwright capabilities across both backends

Both `backend="firefox"` and `backend="chromium"` support `Locator.screenshot()`
and `ElementHandle.screenshot()` (including `scale="css"` and child frames),
`page.expose_function()` / `context.expose_function()` with zero extra
main-world globals, `context.set_offline()`, downloads (`page.expect_download()`),
dialogs, popups, `preload_newtab`, and server-side Bezier pointer humanization.

## CLI

Prefetch either browser binary ahead of first launch and inspect installed
versions and cache directories:

```bash
python -m invisible_playwright fetch
INVPW_BACKEND=chromium python -m invisible_playwright fetch
python -m invisible_playwright version
```

On Linux, `headless=True` starts an isolated Xvfb display automatically (plus
an Openbox desktop workarea for Chromium). A custom local executable can be
passed via `binary_path`.

## Documentation

- [Installation](docs/installation.md) - Linux runtime packages, first launch,
  cache directories, and offline wheelhouse setup
- [Quickstart](docs/quickstart.md) - Sync/async Playwright usage, backend
  selection, and deterministic `seed` replay
- [Configuration](docs/configuration.md) - Proxies, egress-derived timezone,
  backend selection, and environment variables
- [Pinning fingerprint fields](docs/pinning.md) - Overriding specific hardware,
  GPU, or screen fields on top of a seed
- [CLI reference](docs/cli-reference.md) - `fetch` and `version` subcommands
- [Differences from Playwright](docs/differences-from-playwright.md) - Intentional
  stealth safeguards and unsupported upstream calls

## Repositories

| Repository | Owns |
| --- | --- |
| [invisible_playwright](https://github.com/idawnlight/invisible_playwright) | **User entrypoint**: Playwright sync/async API, bundled client, and dual-engine Juggler/CDP server |
| [invisible_core](https://github.com/idawnlight/invisible_core) | Profile generation, GeoIP/timezone resolution, humanized motion/typing, Juggler client, and release verification |
| [Firefox engine](https://github.com/idawnlight/firefox_antidetect_patch) | Native Gecko 160.0.1 declarations, bundled Windows fonts, Juggler worlds, and `firefox-160.0.1-r1` builds |
| [Chromium patches](https://github.com/idawnlight/chromium_antidetect_patch) | Native Chromium 158 hardware/WebGL normalization, CDP/V8 stealth patches, and `chromium-158.0.8127.36-r2` builds |

The native repositories maintain Firefox 160 on `release/160` and Chromium 158
on `release/158`; `invisible_core` and `invisible_playwright` use `main`.

## Tests

Default unit and contract suites run via `pytest`. Real-browser dual-backend
parity tests live in `tests/test_dual_backend_parity_e2e.py` (`pytest -m e2e`).

## Licenses

The wrapper's original code is MIT. Its bundled Playwright client is Apache-2.0.
Firefox and Chromium native changes use the licenses recorded in their engine
trees.
