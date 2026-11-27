"""`wait_for_selector(state="hidden")` and `Locator.wait_for(state="hidden")` with NO element return, as in stock Playwright.

Stock `Frame.waitForSelector` treats a selector that resolves to nothing as already
hidden or detached: `if (!resolved) { if (state === "hidden" || state === "detached") return null; ...}`
(playwright 1.61, `coreBundle.js`), so the call returns `None` and does not poll. The core kept polling
until the timeout when nothing matched, so a script that waits for a spinner or a dialog to go away
(and the spinner never existed, or went before the call) stalled for the whole timeout and then raised.

The verdict is that the call returned, not how fast: every call is given a generous timeout and the test
does not read a clock. Until the wrapper pins a core with the fix these are red.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

PAGE = """<!doctype html><div id="shown">here</div><div id="invisible" style="display:none">x</div>"""


@pytest.fixture
def page(firefox_binary):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=42, binary_path=firefox_binary,
                             humanize=False, headless=True) as browser:
        page = browser.new_context().new_page()
        page.set_default_timeout(30000)
        page.set_content(PAGE)
        yield page


def test_hidden_with_no_element_returns_none(page):
    assert page.wait_for_selector("#does-not-exist", state="hidden", timeout=30000) is None


def test_a_locator_waiting_hidden_with_no_element_returns(page):
    assert page.locator("#does-not-exist").wait_for(state="hidden", timeout=30000) is None


def test_detached_with_no_element_returns_none(page):
    assert page.wait_for_selector("#does-not-exist", state="detached", timeout=30000) is None


def test_a_locator_waiting_detached_with_no_element_returns(page):
    assert page.locator("#does-not-exist").wait_for(state="detached", timeout=30000) is None


def test_hidden_on_a_present_invisible_element_returns_none(page):
    assert page.wait_for_selector("#invisible", state="hidden", timeout=30000) is None


def test_a_locator_waiting_hidden_on_a_present_invisible_element_returns(page):
    assert page.locator("#invisible").wait_for(state="hidden", timeout=30000) is None


def test_visible_on_a_present_visible_element_returns_it(page):
    handle = page.wait_for_selector("#shown", timeout=30000)
    assert handle is not None
    assert handle.text_content() == "here"
