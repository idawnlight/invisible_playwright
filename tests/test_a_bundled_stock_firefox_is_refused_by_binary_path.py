"""A test that needs a stock Firefox bundled with Playwright on the machine that runs it.

``test_real_stock_firefox_via_binary_path_is_refused`` looks for ``ms-playwright/firefox-*`` in the user's cache; a clean CI runner has none, so the test
skips, and a skip is red in the whole unit suite of the candidate. It lives in its own file, out of ``tests/test_seal_binary_path.py`` (whose other
cases build their own tree and run everywhere), and is a file of the environment of ``tests/test_the_unit_suite_is_accounted_for.py``.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from invisible_core.seal import EngineMismatch, read_engine_identity
from invisible_playwright import _engine

from test_seal_binary_path import ENTRY_REL, active  # noqa: F401  (``active`` is a fixture)


def test_real_stock_firefox_via_binary_path_is_refused(active):  # noqa: F811  (the fixture imported above)
    """The cell as it exists on this machine, not a synthetic stand-in. Read
    only; skips cleanly when no bundled Playwright Firefox is present."""
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData/Local")
    roots = [Path(local) / "ms-playwright", Path.home() / ".cache" / "ms-playwright"]
    entries = [d / "firefox" / ENTRY_REL for root in roots if root.is_dir()
               for d in sorted(root.glob("firefox-*"))]
    entries = [e for e in entries if e.exists()]
    if not entries:
        pytest.skip("no bundled stock Firefox on this machine")
    for entry in entries:
        ident = read_engine_identity(entry)
        with pytest.raises(EngineMismatch):
            _engine.resolve_executable(entry)
        assert ident.marked_entries == 0, f"{entry} unexpectedly carries our marker"
