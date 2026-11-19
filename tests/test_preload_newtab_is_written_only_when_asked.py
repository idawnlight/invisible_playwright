"""``preload_newtab`` of InvisiblePlaywright: the pref is written only when the caller asked.

``None`` (default) is the caller saying nothing, and the core writes nothing; ``False`` / ``True`` arrive as given; the async class and
``launch_server`` carry it too.
"""
import inspect

import pytest

from invisible_playwright import InvisiblePlaywright

pytestmark = pytest.mark.unit
KEY = "browser.newtab.preload"


def _built(**kw):
    from invisible_core import decide_session_locale

    session = InvisiblePlaywright(seed=7, binary_path="firefox", **kw)
    session._timezone, session._locale = "Asia/Tokyo", decide_session_locale("en-US")
    return session._build_prefs()


def test_by_default_no_pref_is_written_about_it():
    assert KEY not in _built()


def test_asked_to_turn_it_off_the_pref_is_false():
    assert _built(preload_newtab=False)[KEY] is False
    assert _built(preload_newtab=True)[KEY] is True


def test_the_option_is_a_parameter_of_every_entry_point():
    from invisible_playwright import async_api
    from invisible_playwright._juggler import remote

    for fn in (InvisiblePlaywright.__init__, async_api.InvisiblePlaywright.__init__, remote.launch_server):
        assert "preload_newtab" in inspect.signature(fn).parameters, fn
