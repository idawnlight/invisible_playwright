---
title: "Documentation"
description: "How to install, configure and use invisible_playwright across Firefox 160 and Chromium 157: install and fetch the engines, the two-line switch from plain Playwright, proxy and timezone configuration, pinning specific fields, and the CLI."
has_children: true
nav_order: 2
---

# Documentation

Core setup and API reference for both `backend="firefox"` and `backend="chromium"`.
[Installation](installation.md) gets the wrapper and browser engines on disk,
[Quickstart](quickstart.md) shows the two-line switch from plain Playwright and how to
choose between Firefox and Chromium, [Configuration](configuration.md) covers backend
selection, proxies, timezone and environment variables,
[Pinning fingerprint fields](pinning.md) covers forcing specific values while leaving
the rest seed-derived, and the [CLI reference](cli-reference.md) covers `fetch` and
`version`.

If you're here to understand *why* a particular signal matters rather than how to
configure this package, that's [Guides](guides.md) instead.
