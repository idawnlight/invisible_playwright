---
title: "Installation"
description: "Install the paired Python packages from GitHub tags. Firefox downloads from the fork's GitHub Release on first launch and is reused from the local cache."
parent: "Documentation"
nav_order: 1
---

# Installation

This guide installs `invisible_playwright 0.43.1`, `invisible_core 159.49.1`,
and Firefox 159.0 from the `firefox-159.0-r1` engine release. The Python tags
are `v0.43.1` and `v159.49.1`.

Supported platforms: **Linux x86_64**. Use Python **3.11 or newer** and Git.

## Linux runtime dependencies

On Ubuntu 24.04:

```bash
sudo apt-get update
sudo apt-get install -y \
  python3 python3-venv git xvfb xauth \
  libgtk-3-0t64 libdbus-glib-1-2 libasound2t64 libx11-xcb1 libxt6t64
```

With `headless=True`, the wrapper starts its own Xvfb display on Linux.
Run your script with `python`; an outer `xvfb-run` is optional. With
`headless=False`, Firefox opens on your desktop's display.

## Install the Python packages

Create a fresh environment and install both Git references in the same command:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install \
  "invisible-core @ git+https://github.com/idawnlight/invisible_core.git@v159.49.1" \
  "invisible-playwright @ git+https://github.com/idawnlight/invisible_playwright.git@v0.43.1"
```

The wrapper requires `invisible-core==159.49.1`; supplying both references
selects the fork's paired core directly. Its Playwright Python client is bundled.

## First launch

Save this as `smoke.py`:

```python
from invisible_playwright import InvisiblePlaywright

with InvisiblePlaywright(seed=42, headless=True) as browser:
    page = browser.new_page()
    page.goto("https://example.com/")
    print(page.title())
```

Run it:

```bash
python smoke.py
```

On first launch, the core downloads Firefox from
[idawnlight/firefox_antidetect_patch](https://github.com/idawnlight/firefox_antidetect_patch/releases/tag/firefox-159.0-r1),
checks the archive's SHA256, unpacks it, and starts the browser. Later launches
check and reuse the cached engine. The normal installation uses the release
record bundled with the core; it needs no `binary_path` or environment settings.
The Python installation and browser download happen at separate times.

To download the browser ahead of the first launch, run:

```bash
python -m invisible_playwright fetch
```

Its last output line is the executable path. The equivalent console command is
`invisible-playwright fetch`.

## Versions and cache

```bash
python -m invisible_playwright version
python -m pip check
```

`version` prints the wrapper/core versions, Firefox release and BuildID, plus
the engine and state cache directories. To put the cache on another disk, set
this before launching or fetching:

```bash
export INVISIBLE_PLAYWRIGHT_CACHE_DIR=/mnt/browser-cache
python -m invisible_playwright fetch
```

## Preloaded or locally built Firefox

For machines where the browser is distributed separately, download the release
archive on another machine and extract it. Pass its Firefox executable directly:

```python
with InvisiblePlaywright(
    binary_path="/opt/invisible-firefox/firefox",
    seed=42,
    headless=True,
) as browser:
    page = browser.new_page()
```

The release build uses the same bundled release record. For a different local
build, set `INVISIBLE_SEAL_FILE` to that build's generated seal before importing
the package. `binary_path` is an API argument; `INVPW_BINARY_PATH` belongs to the
repository's test runner.

For an offline Python installation, build a wheel directory on a machine with
the same Python/platform, copy it with the browser archive, then install from it:

```bash
python -m pip wheel --wheel-dir wheelhouse \
  "invisible-core @ git+https://github.com/idawnlight/invisible_core.git@v159.49.1" \
  "invisible-playwright @ git+https://github.com/idawnlight/invisible_playwright.git@v0.43.1"
# On the destination machine:
python -m pip install --no-index --find-links wheelhouse \
  invisible-core==159.49.1 invisible-playwright==0.43.1
```

## Update

Install the next release's two documented Git references together. Their Python
versions and the core's selected Firefox release determine the installed pair.
See [Quickstart](quickstart.md) for the sync and async APIs.
