"""Juggler keeps the creating global of a node whose document has no window.

A createHTMLDocument/DOMParser document has defaultView=None, while its
elements still belong to the page's global. Exercise the native commands
directly: public select_option performs actionability checks and cannot reach
a select in such a document.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

FACTORIES = [
    pytest.param("document.createElement('select')", True, False, id="live-document"),
    pytest.param(
        "document.implementation.createHTMLDocument('detached').createElement('select')",
        False,
        False,
        id="create-html-document",
    ),
    pytest.param(
        "new DOMParser().parseFromString('<select></select>', 'text/html')"
        ".querySelector('select')",
        False,
        True,
        id="dom-parser",
    ),
]

SETUP = """(() => {
  window.detachedSelect = MAKE;
  detachedSelect.innerHTML = '<option value="a" selected>A</option>'
      + '<option value="b">B</option>';
  window.selectEvents = [];
  for (const type of ['input', 'change']) {
    detachedSelect.addEventListener(type, event => selectEvents.push({
      type: event.type, trusted: event.isTrusted,
      value: detachedSelect.value,
    }));
  }
  window.readDetachedSelect = () => ({
    value: detachedSelect.value,
    connected: detachedSelect.isConnected,
    hasDefaultView: !!detachedSelect.ownerDocument.defaultView,
    events: selectEvents,
  });
  return detachedSelect;
})()"""


@pytest.fixture(scope="module")
def native_page(firefox_binary):
    from invisible_core import get_default_stealth_prefs
    from invisible_core.juggler.instance import launch

    prefs = get_default_stealth_prefs(
        seed=42, locale="en-US", timezone="Etc/UTC", humanize=False)
    browser = launch(firefox_binary, prefs=prefs, headless=True)
    try:
        yield browser.new_context().new_page()
    finally:
        browser.close()


def _main_context(page):
    return page.injected.contexts[(page.main_frame_id, "")]


def _main_read(page):
    # Read in the PAGE's own realm, including the listeners' captured values.
    return page.injected.evaluate_raw(
        _main_context(page), "readDetachedSelect()", by_value=True)["value"]


def _make_select(page, factory, has_default_view, connected):
    remote = page.injected.evaluate_raw(
        _main_context(page), SETUP.replace("MAKE", factory), by_value=False)
    assert _main_read(page) == {
        "value": "a", "connected": connected,
        "hasDefaultView": has_default_view, "events": [],
    }
    return remote["objectId"]


@pytest.mark.parametrize("factory,has_default_view,connected", FACTORIES)
def test_element_atom_accepts_a_select_whose_document_has_no_window(
        native_page, factory, has_default_view, connected):
    page = native_page
    object_id = _make_select(page, factory, has_default_view, connected)
    try:
        result = page.send("Page.elementAtom", {
            "frameId": page.main_frame_id, "objectId": object_id,
            "atom": "isElementDisplayed",
        }, timeout=10)
        # None is displayed in a window; the atom answers false, not an error.
        assert result == {"result": False}
        assert _main_read(page)["events"] == []
    finally:
        page.injected.release(_main_context(page), object_id)


@pytest.mark.parametrize("factory,has_default_view,connected", FACTORIES)
def test_select_options_changes_the_value_and_delivers_trusted_events_in_that_global(
        native_page, factory, has_default_view, connected):
    page = native_page
    object_id = _make_select(page, factory, has_default_view, connected)
    command = {"frameId": page.main_frame_id, "objectId": object_id, "indices": [1]}
    try:
        assert page.send("Page.selectOptions", command, timeout=10) == {"changed": True}
        expected = {
            "value": "b", "connected": connected,
            "hasDefaultView": has_default_view,
            "events": [
                {"type": "input", "trusted": True, "value": "b"},
                {"type": "change", "trusted": True, "value": "b"},
            ],
        }
        assert _main_read(page) == expected
        # Selecting the existing value does not manufacture another event pair.
        assert page.send("Page.selectOptions", command, timeout=10) == {"changed": False}
        assert _main_read(page) == expected
    finally:
        page.injected.release(_main_context(page), object_id)
