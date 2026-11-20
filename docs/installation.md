---
title: "Installation"
description: "Install the paired Python packages from GitHub tags and run either Firefox 160 or Chromium 157 with automatic SHA256-verified binary caching."
parent: "Documentation"
nav_order: 1
---

# Installation

This guide installs `invisible_playwright 0.55.1` and `invisible_core 160.76.0`
(tags `v0.55.1` and `v160.76.0`), supporting both native stealth engines out of
the box:

- **Firefox 160.0** (`backend="firefox"`, default) from
  [`firefox-160.0-r2`](https://github.com/idawnlight/firefox_antidetect_patch/releases/tag/firefox-160.0-r2)
  (~280 MB compressed)
- **Chromium 157.0.8103.78** (`backend="chromium"`) from
  [`chromium-157.0.8103.78-r1`](https://github.com/idawnlight/chromium_antidetect_patch/releases/tag/chromium-157.0.8103.78-r1)
  (~236 MB compressed)

Supported platforms: **Linux x86_64**. Use Python **3.11 or newer** and Git.

## 1. Linux runtime dependencies

On Ubuntu 24.04, install the shared Xvfb/desktop packages plus the GTK/NSS/Mesa
runtime libraries and desktop font families so both `backend="firefox"` and
`backend="chromium"` work cleanly on headless servers and GPU-less containers:

```bash
sudo apt-get update
sudo apt-get install -y \
  python3 python3-venv git xvfb xauth openbox x11-xserver-utils x11-utils \
  libgtk-3-0t64 libdbus-glib-1-2 libasound2t64 libx11-xcb1 libxt6t64 \
  libnss3 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 libdrm2 \
  libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
  libgbm1 libpango-1.0-0 libcairo2 libgl1-mesa-dri \
  fonts-ubuntu fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji
```

With `headless=True`, the wrapper starts its own Xvfb display on Linux (and
starts an Openbox desktop workarea automatically for Chromium so
`screen.availHeight < screen.height` holds). Run your script directly with
`python`; an outer `xvfb-run` is unnecessary. With `headless=False`, the browser
opens on your desktop's active `DISPLAY`.

## 2. Install the Python packages

Create a fresh virtual environment and install both pinned Git tags in a single
command:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install \
  "invisible-core @ git+https://github.com/idawnlight/invisible_core.git@v160.76.0" \
  "invisible-playwright @ git+https://github.com/idawnlight/invisible_playwright.git@v0.55.1"
```

The wrapper requires `invisible-core==160.76.0`; supplying both references
selects the exact paired core directly. Add `[remote]` to `invisible-playwright`
(or `pip install "websockets>=13"`) if you plan to serve a browser over
WebSocket with `launch_server()`. The Playwright Python client is bundled
inside `invisible-playwright` (no `playwright install` or Node.js driver is
used).

## 3. First launch

Save this as `smoke.py`:

```python
from invisible_playwright import InvisiblePlaywright

for backend in ("firefox", "chromium"):
    with InvisiblePlaywright(seed=42, headless=True, backend=backend) as browser:
        page = browser.new_page()
        page.goto("https://example.com/")
        print(backend, browser.version, page.title())
```

Run it:

```bash
python smoke.py
```

On first launch of each backend, the wrapper downloads the corresponding archive
from its public GitHub Release, verifies the exact byte size and SHA256 from the
bundled seal, extracts it into the local cache, and launches the browser.
Subsequent launches verify and reuse the cached tree without network access.

To prefetch either engine ahead of first launch (for example during Docker
image build):

```bash
# Prefetch Firefox 160.0 (default backend)
python -m invisible_playwright fetch

# Prefetch Chromium 157.0.8103.78
INVPW_BACKEND=chromium python -m invisible_playwright fetch
```

In both cases the last line printed on stdout is the verified browser
executable path. The installed console script `invisible-playwright fetch`
behaves identically.

## 4. Versions and cache locations

```bash
python -m invisible_playwright version
python -m pip check
```

`version` prints the wrapper and core versions, both sealed engine releases
(`firefox-160.0-r2` and `chromium-157.0.8103.78-r1`), and both cache roots:

- Firefox cache root: `INVISIBLE_PLAYWRIGHT_CACHE_DIR` (defaults to
  `~/.cache/invisible-playwright`)
- Chromium cache root: `INVISIBLE_CHROMIUM_CACHE_DIR` (defaults to
  `~/.cache/invisible-chromium`)

To relocate both caches onto another volume:

```bash
export INVISIBLE_PLAYWRIGHT_CACHE_DIR=/mnt/browser-cache/firefox
export INVISIBLE_CHROMIUM_CACHE_DIR=/mnt/browser-cache/chromium
python -m invisible_playwright fetch
INVPW_BACKEND=chromium python -m invisible_playwright fetch
```

## 5. Preloaded binaries and offline installation

For air-gapped or pre-provisioned hosts, unpack the release archive on the
target machine and pass the executable via `binary_path`:

```python
from invisible_playwright import InvisiblePlaywright

with InvisiblePlaywright(
    backend="chromium",
    binary_path="/opt/invisible-chromium/chrome",
    seed=42,
    headless=True,
) as browser:
    page = browser.new_page()
```

For an offline Python installation, build a wheelhouse on a connected Linux
x86_64 host with the same Python minor version, transfer `wheelhouse/` alongside
the browser release archives, and install without network access:

```bash
python -m pip wheel --wheel-dir wheelhouse \
  "invisible-core @ git+https://github.com/idawnlight/invisible_core.git@v160.76.0" \
  "invisible-playwright @ git+https://github.com/idawnlight/invisible_playwright.git@v0.55.1"

# On the offline target machine:
python -m pip install --no-index --find-links wheelhouse \
  invisible-core==160.76.0 invisible-playwright==0.55.1
```

## Update

Upgrade by installing the next release's paired `invisible-core` and
`invisible-playwright` Git tags together. See [Quickstart](quickstart.md) for
sync/async usage and backend selection.
