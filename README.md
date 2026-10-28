# InvisiblePlaywright on Firefox 159

This branch pairs Firefox 159.0 with `invisible_core 159.49.0` and
`invisible_playwright 0.43.1.dev0`. The vendored Playwright Python client is 1.61.

The engine implements session identity and geometry, language and timezone
propagation, bundled fonts, graphics and media declarations, Juggler execution
worlds, native input, network interception and proxy routing.

## Install

Use the paired local build.

Build the paired engine checkout with `./fork/build.sh`. Install the paired
Python checkouts into one environment:

```sh
python -m pip install -e /path/to/invisible_core -e /path/to/invisible_playwright
```

Pass the executable from that build explicitly. Its version, BuildID and source
commit are recorded in the core's generated local seal:

```python
from invisible_playwright import InvisiblePlaywright

with InvisiblePlaywright(
    binary_path="/path/to/workspace/build/obj-firefox-stable-159/dist/firefox/firefox",
    seed=42,
    headless=True,
    locale="en-GB",
    timezone="Europe/Berlin",
) as browser:
    page = browser.new_page()
    page.goto("https://example.com/")
    print(page.title())
```

## Repository and release pairing

Maintain these three repositories together:

| Repository | Owns |
| --- | --- |
| Firefox engine | Native declarations, fonts, Juggler and platform builds |
| invisible_core | Profile generation, prefs, protocol client, seal and downloads |
| invisible_playwright | Playwright API, vendored client and Python server |

`INVISIBLE_FIREFOX_RELEASE_REPOSITORY=owner/repository` selects the engine's
GitHub release repository. Generate the seal from the built archives before
publishing a release, then update the exact core dependency in this package.

The `fetch` CLI command downloads an engine named by a published seal.
`version` prints the wrapper, core and engine versions. `invisible-core doctor`
reports the package and engine pair. This local build is selected through
`binary_path`.

## Tests

The existing tests run against the paired packages. Browser tests select the
engine through `INVPW_BINARY_PATH`; `scripts/run_e2e.py` runs their parallel and
serial timing passes. The source contracts read the native commit in the seal.

## Licenses

The wrapper's original code is MIT. Its vendored Playwright client is Apache-2.0.
Firefox and the native changes use the licenses recorded in the engine tree.
