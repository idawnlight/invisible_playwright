"""The handle of an element answers every method the client sends it, in a real browser, as Playwright 1.61 does.

The unit file next to it (`test_an_element_handle_serves_every_method_of_the_client.py`) pins what the server asks the engine and the actions; this one asks the
page what happened. Until the server had a row for them, `handle.query_selector(...)`, `handle.is_visible()`, `handle.dblclick()`, `handle.wait_for_element_state(...)`
and the rest of the 17 answered "ElementHandle has no method ... INSIDE the perimeter ... a gap", and the script died.

Each expectation is stock's, from the driver bundle (`coreBundle.js`, by function name): the queries run with the element as ROOT (`FrameSelectors.query`, `queryAll`),
the states are the Frame's with `:scope` and the handle (`Frame._elementState`, `isVisibleInternal`), `waitForElementState` is `ElementHandle.waitForElementState`
(`throwRetargetableDOMError`: `Element is not attached to the DOM`), the gestures are `_dblclick`, `_tap`, `selectText` and `Frame.dispatchEvent` on that node, and
`evaluateExpressionHandle` is `JSHandleDispatcher`'s. `wait_for_selector` of a handle is not here: the server still answers it as a gap (the core's
`Actions.wait_for_selector` has no root).
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

PAGE = """<!doctype html>
<div id="box"><span class="x" id="s1">a</span><span class="x" id="s2">b</span><p id="p">text</p>
<iframe id="f" srcdoc="&lt;!doctype html&gt;&lt;div id='inner'&gt;in&lt;/div&gt;"></iframe></div>
<span class="x" id="outside">out</span>
<button id="go" type="button">go</button><button id="off" type="button" disabled>off</button>
<input id="chk" type="checkbox" checked><input id="unchk" type="checkbox">
<input id="ro" readonly value="v"><input id="txt" value="hello world">
<div id="hid" style="display:none">h</div><div id="gone">g</div>
<script>
  window.seen = {click: 0, dblclick: 0, touchstart: 0, trusted: [], pings: []};
  const go = document.getElementById('go');
  go.addEventListener('click', e => { window.seen.click++; window.seen.trusted.push(e.isTrusted); });
  go.addEventListener('dblclick', e => { window.seen.dblclick++; window.seen.trusted.push(e.isTrusted); });
  go.addEventListener('touchstart', () => { window.seen.touchstart++; });
  document.getElementById('box').addEventListener('ping', e => window.seen.pings.push([e.type, e.bubbles, e.isTrusted]));
</script>"""


@pytest.fixture(scope="module")
def browser(firefox_binary):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=42, binary_path=firefox_binary, humanize=False, headless=True) as browser:
        yield browser


def _page(browser, **options):
    page = browser.new_context(**options).new_page()
    page.set_default_timeout(30000)
    page.set_content(PAGE)
    return page


@pytest.fixture
def page(browser):
    page = _page(browser)
    yield page
    page.context.close()


def ids(elements):
    return [e.get_attribute("id") for e in elements]


# --- the queries run inside the element -------------------------------------------------------------------------------------------------------------------
def test_query_selector_finds_inside_the_element_and_not_outside_it(page):
    box = page.query_selector("#box")
    assert box.query_selector(".x").get_attribute("id") == "s1"
    assert box.query_selector("#outside") is None


def test_query_selector_enters_a_frame_inside_the_element(page):
    box = page.query_selector("#box")
    inner = box.query_selector("iframe >> internal:control=enter-frame >> #inner")
    assert inner.text_content() == "in"


def test_query_selector_all_finds_every_match_inside_the_element_and_not_outside_it(page):
    box = page.query_selector("#box")
    assert ids(box.query_selector_all(".x")) == ["s1", "s2"]
    assert box.query_selector_all("#outside") == []


def test_query_selector_all_inside_the_element_keeps_the_document_order_of_a_long_list(page):
    """A list longer than ten: read as one array, its places are 0, 1, ... 9, 10, ... and not '0', '1', '10', '11', ... '2'."""
    page.evaluate("""() => {
        const list = document.createElement('div'); list.id = 'long';
        for (let i = 0; i < 25; i++) { const li = document.createElement('i'); li.id = 'n' + i; list.appendChild(li); }
        document.body.appendChild(list);
    }""")
    found = page.query_selector("#long").query_selector_all("i")
    assert ids(found) == ["n%d" % i for i in range(25)]


def test_eval_on_selector_runs_the_function_on_the_match_inside_the_element(page):
    box = page.query_selector("#box")
    assert box.eval_on_selector(".x", "(e, suffix) => e.id + suffix", "!") == "s1!"


def test_eval_on_selector_that_finds_nothing_says_what_stock_says(page):
    from invisible_playwright.sync_api import Error

    box = page.query_selector("#box")
    with pytest.raises(Error, match='Failed to find element matching selector "#outside"'):
        box.eval_on_selector("#outside", "e => e.id")


def test_eval_on_selector_all_runs_the_function_on_every_match_inside_the_element(page):
    box = page.query_selector("#box")
    assert box.eval_on_selector_all(".x", "(els, n) => els.map(e => e.id).concat([els.length + n])", 10) == ["s1", "s2", 12]


# --- the state of the element -----------------------------------------------------------------------------------------------------------------------------
def test_is_visible_and_is_hidden_read_this_element(page):
    assert page.query_selector("#go").is_visible() is True
    assert page.query_selector("#go").is_hidden() is False
    assert page.query_selector("#hid").is_visible() is False
    assert page.query_selector("#hid").is_hidden() is True


def test_is_enabled_and_is_disabled_read_this_element(page):
    assert page.query_selector("#go").is_enabled() is True
    assert page.query_selector("#go").is_disabled() is False
    assert page.query_selector("#off").is_enabled() is False
    assert page.query_selector("#off").is_disabled() is True


def test_is_checked_reads_this_element(page):
    assert page.query_selector("#chk").is_checked() is True
    assert page.query_selector("#unchk").is_checked() is False


def test_is_editable_reads_this_element(page):
    assert page.query_selector("#txt").is_editable() is True
    assert page.query_selector("#ro").is_editable() is False


def test_is_editable_of_an_element_that_cannot_be_edited_is_stocks_error(page):
    # stock (injected `elementState`, "editable"): `getReadonly` answers "error" for a node that is not an <input>, <textarea>, <select> or
    # [contenteditable] with a role that allows [aria-readonly], a disabled <button> among them, and that is thrown, not answered `false`.
    from invisible_playwright.sync_api import Error

    with pytest.raises(Error, match=r"Element is not an <input>, <textarea>, <select> or \[contenteditable\] and does not have a role allowing \[aria-readonly\]"):
        page.query_selector("#off").is_editable()


def test_a_node_that_left_the_document_is_hidden_and_has_no_other_state(page):
    from invisible_playwright.sync_api import Error

    gone = page.query_selector("#gone")
    page.evaluate("document.getElementById('gone').remove()")
    assert gone.is_visible() is False
    assert gone.is_hidden() is True
    with pytest.raises(Error, match="Element is not attached to the DOM"):
        gone.is_enabled()
    with pytest.raises(Error, match="Element is not attached to the DOM"):
        gone.wait_for_element_state("visible")


@pytest.mark.parametrize("state", ["hidden", "stable", "visible", "enabled", "disabled", "editable"])
def test_wait_for_element_state_of_a_node_removed_from_the_document_answers_as_stock_does_for_each_state(page, state):
    """Stock's `checkElementStates` on a node that is not connected: `hidden` is met at once, `stable` is met (the box of a removed node does not move), the four others are
    `Element is not attached to the DOM`, at once and not after the timeout."""
    from invisible_playwright.sync_api import Error

    handle = page.query_selector("#gone")
    page.evaluate("e => e.remove()", handle)
    if state in ("hidden", "stable"):
        handle.wait_for_element_state(state, timeout=30000)
    else:
        with pytest.raises(Error, match="Element is not attached to the DOM") as failure:
            handle.wait_for_element_state(state, timeout=30000)
        assert "Timeout" not in str(failure.value)


def test_wait_for_element_state_returns_when_the_state_is_met(page):
    page.query_selector("#go").wait_for_element_state("visible")
    page.query_selector("#go").wait_for_element_state("stable")
    page.query_selector("#go").wait_for_element_state("enabled")
    page.query_selector("#off").wait_for_element_state("disabled")
    page.query_selector("#txt").wait_for_element_state("editable")
    page.query_selector("#hid").wait_for_element_state("hidden")


def test_wait_for_element_state_waits_for_a_state_the_page_reaches(page):
    page.evaluate("requestAnimationFrame(() => { document.getElementById('hid').style.display = 'block'; })")
    page.query_selector("#hid").wait_for_element_state("visible")
    assert page.query_selector("#hid").is_visible() is True


def test_wait_for_element_state_that_is_never_met_is_a_timeout(page):
    from invisible_playwright.sync_api import TimeoutError as PlaywrightTimeout

    with pytest.raises(PlaywrightTimeout):
        page.query_selector("#hid").wait_for_element_state("visible", timeout=500)


# --- the gestures: on this node, as a person does them ----------------------------------------------------------------------------------------------------
def test_dblclick_double_clicks_this_element(page):
    page.query_selector("#go").dblclick()
    seen = page.evaluate("window.seen")
    assert (seen["click"], seen["dblclick"]) == (2, 1)
    assert all(seen["trusted"]), "a double click made by the server is a user's: %r" % seen["trusted"]


def test_a_page_tap_of_a_context_without_touch_is_stocks_error(page):
    # stock's `Frame.tap` (not the handle's) refuses first, whatever the selector
    from invisible_playwright.sync_api import Error

    with pytest.raises(Error, match="The page does not support tap. Use hasTouch context option to enable touch support."):
        page.tap("#go")
    assert page.evaluate("window.seen.touchstart") == 0


def test_select_text_selects_the_text_of_this_element(page):
    page.query_selector("#txt").select_text()
    assert page.evaluate("[document.getElementById('txt').selectionStart, document.getElementById('txt').selectionEnd]") == [0, 11]


def test_dispatch_event_dispatches_on_this_element(page):
    box = page.query_selector("#box")
    box.dispatch_event("ping")
    box.dispatch_event("ping", {"bubbles": False})
    # stock `injectedScript.dispatchEvent`: an event of a type with no interface is `new Event(type, {bubbles, cancelable, composed, ...init})`, not a user's
    assert page.evaluate("window.seen.pings") == [["ping", True, False], ["ping", False, False]]


def test_dispatch_event_of_a_click_reaches_the_listener(page):
    page.query_selector("#go").dispatch_event("click")
    assert page.evaluate("[window.seen.click, window.seen.trusted]") == [1, [False]]


def test_a_handle_in_the_event_init_is_refused_by_name(page):
    from invisible_playwright.sync_api import Error

    with pytest.raises(Error, match="eventInit.*JSHandle|JSHandle.*eventInit"):
        page.query_selector("#box").dispatch_event("ping", {"relatedTarget": page.query_selector("#go")})


# --- the caller's code on the element, returning a handle -------------------------------------------------------------------------------------------------
def test_evaluate_handle_hands_back_a_handle_of_what_the_function_returned(page):
    box = page.query_selector("#box")
    made = box.evaluate_handle("(e, n) => ({kids: e.children.length + n, id: e.id})", 1)
    assert made.json_value() == {"kids": 5, "id": "box"}
    assert made.get_property("id").json_value() == "box"


def test_evaluate_handle_that_returns_a_node_hands_back_the_element(page):
    box = page.query_selector("#box")
    element = box.evaluate_handle("e => e.querySelector('#p')").as_element()
    assert element is not None and element.text_content() == "text"
    assert element.is_visible() is True
