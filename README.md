# InvisiblePlaywright on Firefox 159

This fork pairs Firefox 159.0 (`firefox-159.0-r1`) with
`invisible_core 159.49.2` and `invisible_playwright 0.43.2`.
The bundled Playwright Python client is 1.61.

The engine implements session identity and geometry, language and timezone
propagation, bundled fonts, graphics and media declarations, Juggler execution
worlds, native input, network interception and proxy routing.

## Install

Supported platforms: **Linux x86_64**;
Python **3.11+** and Git are required. The Firefox download is approximately
233 MiB (245 MB) and is cached after its first download.

Install both Python packages from their GitHub tags:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install \
  "invisible-core @ git+https://github.com/idawnlight/invisible_core.git@v159.49.2" \
  "invisible-playwright @ git+https://github.com/idawnlight/invisible_playwright.git@v0.43.2"
```

Use [the installation guide](docs/installation.md) for Linux system dependencies,
Xvfb, cache settings, and preloaded builds. Firefox downloads automatically from
[this fork's GitHub Release](https://github.com/idawnlight/firefox_antidetect_patch/releases/tag/firefox-159.0-r1)
on first launch:

```python
from invisible_playwright import InvisiblePlaywright

with InvisiblePlaywright(seed=42, headless=True) as browser:
    page = browser.new_page()
    page.goto("https://example.com/")
    print(page.title())
```

You can prefetch the browser and inspect the installed versions:

```bash
python -m invisible_playwright fetch
python -m invisible_playwright version
```

On Linux, `headless=True` starts Xvfb automatically. A local build can be passed
through `binary_path`; details are in the installation guide.

## Repositories

| Repository | Owns |
| --- | --- |
| [Firefox engine](https://github.com/idawnlight/firefox_antidetect_patch) | Native declarations, fonts, Juggler and platform builds |
| [invisible_core](https://github.com/idawnlight/invisible_core) | Profile generation, prefs, protocol client, release record and downloads |
| [invisible_playwright](https://github.com/idawnlight/invisible_playwright) | Playwright API, bundled client and Python server |

The engine maintains Firefox 159 on `release/159`; core and wrapper each use
`main`. Firefox is built and distributed separately from the Python packages.

## Tests

The existing tests run against the paired packages. Browser tests select the
engine through `INVPW_BINARY_PATH`; `scripts/run_e2e.py` runs their parallel and
serial timing passes. The source contracts read the native commit in the seal.

## Licenses

The wrapper's original code is MIT. Its bundled Playwright client is Apache-2.0.
Firefox and the native changes use the licenses recorded in the engine tree.
