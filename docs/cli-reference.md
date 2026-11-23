---
title: "CLI reference"
description: "The invisible-playwright command line: fetch and version across both Firefox 160 and Chromium 157 backends."
parent: "Documentation"
nav_order: 5
---


# CLI reference

The installed console script is `invisible-playwright` (with a hyphen).
`python -m invisible_playwright` runs the exact same entrypoint.

```bash
invisible-playwright fetch    # download/verify the selected backend's engine and print its executable path
invisible-playwright version  # print wrapper, core, Firefox & Chromium engine versions, and cache paths
```

## `fetch`

Ensures the selected backend's browser binary is downloaded, matches its sealed
SHA256 (and BuildID / `omni.ja` for Firefox), and prints the executable's
absolute path as the final line on stdout.

- By default (`INVPW_BACKEND` unset or `firefox`), `fetch` verifies every cached
  Firefox tree against the seal shipped in `invisible-core`, reports any stale
  tree on stderr, and downloads `firefox-160.0.1-r1` if needed:

```bash
FIREFOX_BIN="$(invisible-playwright fetch)"
```

- When `INVPW_BACKEND=chromium` is set, `fetch` downloads and SHA256-verifies
  the sealed Chromium release (`chromium-158.0.8127.36-r1`) and prints the
  verified `chrome` binary path:

```bash
CHROME_BIN="$(INVPW_BACKEND=chromium invisible-playwright fetch)"
```

Running `fetch` when the verified engine is already cached is a fast local
check with zero network traffic.

## `version`

```text
invisible_playwright 0.58.2
invisible_core       160.81.0   (declared: ==160.81.0)
engine               firefox-160.0.1-r1  Firefox 160.0.1  build 20261123020000
seal                 07989576c3f8  [.../invisible_core/seal.json]
chromium_engine      chromium-158.0.8127.36-r1  Chromium 158.0.8127.36
engines              /home/you/.cache/invisible-playwright
chromium_engines     /home/you/.cache/invisible-chromium
state                /home/you/.cache/invisible-playwright
```

Paste this output into bug reports:

- If package metadata disagrees with the `invisible_core` module imported at
  runtime, `version` prints a `STALE RECORD` diagnostic line that `pip check`
  alone cannot see.
- `engines` and `chromium_engines` show where the unpacked Firefox and Chromium
  builds live on disk so you can inspect or reclaim disk space directly.

## Why there are only two subcommands

Earlier versions had six commands (`fetch`, `fetch --force`, `path`, `version`,
`clear-cache`, `doctor`). Their useful behavior was folded into `fetch` and
`version`:

| Former command | Where it lives now |
|---|---|
| `doctor` | `fetch` verifies cached trees against the seal on every run. |
| `fetch --force` | Unnecessary once every run checks integrity against the seal. |
| `path` | `fetch` prints the verified executable path on its final stdout line. |
| `clear-cache` | Intentionally omitted so one wrapper never deletes another tool's cached engines; `version` prints the exact directories. |

## See also

[Installation](installation.md) for system dependencies and first-run setup, and
[Configuration](configuration.md) for cache and binary path environment variables.
