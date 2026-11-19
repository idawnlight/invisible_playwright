"""The arm of the prefs composition that exists on Windows only: the hidden desktop.

``test_a_hidden_desktop_brings_its_two_sandbox_keys_and_nothing_hides_by_pref`` is skipped on every other system (the hidden desktop is a Windows path),
and a skip is red in the whole unit suite of the candidate, so it is a file of its own and only the row of Windows lists it (``tests/test_prefs_composition_arm.py``
keeps what runs on both).
"""
from __future__ import annotations

import sys

import pytest

from invisible_core import generate_profile
from invisible_playwright._cursor import ENGINE_PYTHON
from invisible_playwright._session import build_prefs

from test_prefs_composition_arm import LOCALE, SEED, TZ

pytestmark = pytest.mark.unit


@pytest.mark.skipif(sys.platform != "win32",
                    reason="the hidden desktop is a Windows path")
def test_a_hidden_desktop_brings_its_two_sandbox_keys_and_nothing_hides_by_pref():
    """The window is hidden by WHERE the browser is created, not by a pref.

    ⛔ `zoom.stealth.cloak_windows` must never come back: from 2026-06-11 to
    2026-09-20 it switched on a DWMWA_CLOAK inside the binary, and the owner
    chose a stock engine on that surface. The two keys that DO ride with a
    hidden desktop are the sandbox workarounds measured in 2026-05 (§P16,
    #18 Bug A), emitted only when the desktop was actually created, with
    setdefault so an explicit override still wins.
    """
    without = build_prefs(profile=generate_profile(SEED), locale=LOCALE,
                          timezone=TZ, extra_prefs=None,
                          virtual_display=False,
                          cursor_engine=ENGINE_PYTHON, humanize=True)
    hidden = build_prefs(profile=generate_profile(SEED), locale=LOCALE,
                         timezone=TZ, extra_prefs=None,
                         virtual_display=True,
                         cursor_engine=ENGINE_PYTHON, humanize=True)
    overridden = build_prefs(profile=generate_profile(SEED), locale=LOCALE,
                             timezone=TZ,
                             extra_prefs={"security.sandbox.gpu.level": 1},
                             virtual_display=True,
                             cursor_engine=ENGINE_PYTHON, humanize=True)

    for prefs in (without, hidden, overridden):
        assert "zoom.stealth.cloak_windows" not in prefs, (
            "the in-binary cloak pref is back")
    assert "security.sandbox.gpu.level" not in without
    assert "security.sandbox.content.level" not in without
    assert hidden["security.sandbox.gpu.level"] == 0
    assert hidden["security.sandbox.content.level"] == 4
    assert overridden["security.sandbox.gpu.level"] == 1, (
        "the desktop workaround overwrote an explicit override")
