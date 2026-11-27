"""In a real browser, a pointer action through an ElementHandle reaches the page the way the same action through a selector does: a path, and a landing point
that is not the exact centre of the box. ONE click path, one fingerprint.

The page is asked, which is the only reader that matters: it counts every `mousemove` before the press, and keeps the point of the press (`clientX`, `clientY`).
Before the fix a handle's click was one `mousemove` onto the exact centre of the element's box, a settle, and the press on that same point: count 1, point (400,
240) for the button below, in every install. The unit test (`test_a_handle_action_walks_like_a_selector_action`) holds what the engine is sent for each action;
this holds what the page hears. Each test is one browser, humanising on, closed at the end; the selector's control is on a second page of the same browser.
"""
from __future__ import annotations

import pytest

from _waits import wait_until

#: A button whose exact centre is (400, 240).
CENTRE = (400.0, 240.0)

PAGE = """<!doctype html><html><body style="margin:0">
<button id="b" style="position:absolute;left:300px;top:200px;width:200px;height:80px">b</button>
<script>
window.moves = 0; window.beforePress = null; window.down = null; window.up = null; window.clicks = 0; window.dblclicks = 0; window.over = 0;
document.addEventListener('mousemove', e => { window.moves++; window.last = [e.clientX, e.clientY]; });
const b = document.getElementById('b');
b.addEventListener('mouseover', () => window.over++);
b.addEventListener('mousedown', e => { if (window.down === null) { window.beforePress = window.moves; window.down = [e.clientX, e.clientY]; } });
b.addEventListener('click', e => { window.clicks++; });
b.addEventListener('dblclick', e => { window.dblclicks++; });
</script></body></html>"""

SEED = 4261


def _read(page):
    return page.evaluate("({moves: window.moves, beforePress: window.beforePress, down: window.down, last: window.last || null, "
                         "clicks: window.clicks, dblclicks: window.dblclicks, over: window.over})")


def _fresh(browser):
    page = browser.new_context().new_page()
    page.set_content(PAGE)
    return page


def _shape(seen, what):
    """The two things a jump to the centre cannot show: a path before the press, and a press that is not on the exact centre."""
    assert seen["beforePress"] is not None, "%s: the page never saw a press: %r" % (what, seen)
    assert seen["beforePress"] > 1, "%s: the page counted %d mousemove before the press (a jump, not a walk): %r" % (what, seen["beforePress"], seen)
    assert tuple(seen["down"]) != CENTRE, "%s: the press landed on the exact centre of the box %r: %r" % (what, CENTRE, seen)


@pytest.mark.e2e
def test_a_handle_click_walks_and_lands_off_the_centre_like_a_selector_click(firefox_binary):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=SEED, binary_path=firefox_binary, headless=True, humanize=True) as browser:
        by_handle = _fresh(browser)
        by_handle.query_selector("#b").click()
        seen = _read(by_handle)
        assert seen["clicks"] == 1, seen
        _shape(seen, "ElementHandle.click")

        by_selector = _fresh(browser)
        by_selector.click("#b")
        control = _read(by_selector)
        assert control["clicks"] == 1, control
        _shape(control, "page.click (the control)")


@pytest.mark.e2e
def test_a_handle_dblclick_walks_and_lands_off_the_centre_like_a_selector_dblclick(firefox_binary):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=SEED, binary_path=firefox_binary, headless=True, humanize=True) as browser:
        by_handle = _fresh(browser)
        by_handle.query_selector("#b").dblclick()
        seen = _read(by_handle)
        assert seen["dblclicks"] == 1, seen
        _shape(seen, "ElementHandle.dblclick")

        by_selector = _fresh(browser)
        by_selector.dblclick("#b")
        control = _read(by_selector)
        assert control["dblclicks"] == 1, control
        _shape(control, "page.dblclick (the control)")


@pytest.mark.e2e
def test_a_handle_hover_walks_and_ends_off_the_centre_like_a_selector_hover(firefox_binary):
    """A hover has no press: its shape is the path the page counts and the point its last `mousemove` stops on."""
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=SEED, binary_path=firefox_binary, headless=True, humanize=True) as browser:
        for how in ("handle", "selector"):
            page = _fresh(browser)
            if how == "handle":
                page.query_selector("#b").hover()
            else:
                page.hover("#b")
            assert wait_until(lambda: _read(page)["over"] >= 1, 30, lambda: page.wait_for_timeout(50)), _read(page)
            seen = _read(page)
            assert seen["moves"] > 1, "%s hover: the page counted %d mousemove (a jump, not a walk): %r" % (how, seen["moves"], seen)
            assert tuple(seen["last"]) != CENTRE, "%s hover ended on the exact centre of the box: %r" % (how, seen)
