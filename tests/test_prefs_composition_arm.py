"""The wrapper's arm of the core's prefs-composition comparison.

The core compares its two entry points against each other
(`invisible_core/tests/test_prefs_composition.py`). This is the third: the
wrapper's `build_prefs`, which was the third place stacking layers on top of
`translate_profile_to_prefs` in its own order while nothing compared the
results.

It lives here rather than there because this package pins `invisible-core==`
exactly - the core's suite cannot import a consumer, and a consumer may only use
what the index has.
"""
from __future__ import annotations

import pytest

from invisible_core import generate_profile, get_default_stealth_prefs
from invisible_playwright._cursor import ENGINE_PYTHON
from invisible_playwright._session import build_prefs

pytestmark = pytest.mark.unit

SEED = 424242
LOCALE = "en-US"
TZ = "America/New_York"

#: What the wrapper's path is ALLOWED to differ by from the public API's, and
#: why. An exact set: a new divergence has to be added here deliberately.
DELIBERATE_VS_PUBLIC_API = {
    # The wrapper draws its own trajectories, so the binary's generator must be
    # OFF or every waypoint it sends would itself be expanded into a path.
    # get_default_stealth_prefs exists for a caller who has no generator of
    # their own, so it leaves the binary's on.
    "stealthfox.humanize.maxTime",
    # Same reason, third sibling: it governs the CADENCE of the binary's
    # generator, which only the public API turns on. Declared since 2026-08-25 -
    # before that nobody wrote it and the engine used its own default of
    # 10 ms, which measured gave 79% of the intervals between two `mousemove`
    # tighter than a real 60 Hz can deliver.
    "stealthfox.humanize.stepMs",
    # Not a pref of the browser: the server's mark that this dict IS a composed
    # session (server.COMPOSED_PREF). `op_launch` takes it out before `user.js`
    # and composes a session for a launch that does not carry it; the public API
    # returns prefs for a caller's own launch, which has no such server.
    "invisible.server.composed",
}


def test_the_wrapper_composes_what_the_public_api_composes():
    profile = generate_profile(SEED)
    mine = build_prefs(profile=profile, locale=LOCALE, timezone=TZ,
                       extra_prefs=None, virtual_display=False,
                       cursor_engine=ENGINE_PYTHON, humanize=True)
    public = get_default_stealth_prefs(SEED, locale=LOCALE, timezone=TZ)

    difference = set(mine) ^ set(public)
    assert difference == DELIBERATE_VS_PUBLIC_API, (
        "the wrapper and the public API no longer build the same prefs.\n"
        f"  only in build_prefs:                {sorted(set(mine) - set(public))}\n"
        f"  only in get_default_stealth_prefs:  {sorted(set(public) - set(mine))}\n"
        "If a new one is deliberate, add it to DELIBERATE_VS_PUBLIC_API.")

    differing = {k: (mine[k], public[k]) for k in set(mine) & set(public)
                 if mine[k] != public[k] and k != "stealthfox.humanize"}
    assert not differing, (
        "same keys, different values - worse than a missing key, because "
        f"nothing looks wrong: {differing}")


def test_the_python_engine_leaves_the_browser_expansion_off():
    """The delegation must not change what the engine writes.

    ENGINE_PYTHON means the wrapper draws the path, so the pref is off and no
    cap is written, whatever `humanize` the caller gave.
    """
    profile = generate_profile(SEED)
    for humanize in (True, 2.5, 0):
        python = build_prefs(profile=profile, locale=LOCALE, timezone=TZ,
                             extra_prefs=None, virtual_display=False,
                             cursor_engine=ENGINE_PYTHON, humanize=humanize)
        assert python["stealthfox.humanize"] is False
        assert "stealthfox.humanize.maxTime" not in python
