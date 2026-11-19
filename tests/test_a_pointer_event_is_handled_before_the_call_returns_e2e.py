"""A pointer call answers once the page has HANDLED the event, not when it was sent.

[B230]. `Page.dispatchMouseEvent` used to answer when the event was handed to
the widget. A mouse event then waited in the content process - a press behind
a coalesced move waits for the refresh tick - while the next call, on another
channel, could run first: under load a `mousedown` sent before a scroll was
hit-tested after it, 39 px off, and a drag never started; a script read after a
double click saw two clicks out of three. Measured with a probe: 18 reads in
600 before the press was handled, against 0 in 1200 once the engine waited for
the renderer's ack (firefox-39).

That race needs load to show, so a test of it would be green on a broken
engine nineteen times in twenty. This test asks the contract itself, which is
deterministic: a page whose handler keeps it busy for 300 ms. Once the call
answers, the handler has run, so the call cannot answer sooner than the
handler takes and its effect is already there. An engine that answers on
sending answers in a few milliseconds, with the effect still missing.
"""
from __future__ import annotations

import time

import pytest

from _waits import wait_until

PAGE = """<!doctype html><html><body style="margin:0">
<button id="b" style="width:300px;height:200px">b</button>
<script>
const busy = ms => { const end = performance.now() + ms; while (performance.now() < end) {} };
const b = document.getElementById('b');
window.handled = [];
b.addEventListener('mousedown', () => { busy(300); window.handled.push('mousedown'); });
b.addEventListener('mouseup', () => { busy(300); window.handled.push('mouseup'); });
b.addEventListener('mousemove', () => { if (!window.moved) { window.moved = true; busy(300);
  window.handled.push('mousemove'); } });
</script></body></html>"""

#: The handler's time, less a margin for the clock: the call cannot answer
#: sooner than the page took to handle the event.
_HANDLER_S = 0.3 * 0.9


@pytest.mark.e2e
def test_each_pointer_call_answers_after_the_page_handled_its_event(firefox_binary):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=4247, binary_path=firefox_binary, headless=True,
                             humanize=False) as browser:
        page = browser.new_context().new_page()
        page.set_content(PAGE)
        for step, call in (("mousemove", lambda: page.mouse.move(150, 100)),
                           ("mousedown", page.mouse.down),
                           ("mouseup", page.mouse.up)):
            started = time.perf_counter()
            call()
            took = time.perf_counter() - started
            handled = page.evaluate("window.handled")
            assert step in handled, (
                "page.mouse answered for %s before the page handled it: the next "
                "call saw %r" % (step, handled))
            assert took >= _HANDLER_S, (
                "%s answered in %.0f ms while its handler takes 300: the call did "
                "not wait for the page" % (step, took * 1000))


@pytest.mark.e2e
def test_a_click_that_opens_a_dialog_still_answers(firefox_binary):
    """The wait ends when the event's handler opens a dialog: the handler runs
    the dialog inside itself, so the page handles the event only once the
    dialog is closed - and closing it is the caller's business, after the call
    has answered. Without that end, this click would hang until the engine's
    bound, which is 5 s and an error (`PageHandler._awaitBounded`): the call
    raises instead of answering. The dialog stays open until the call has
    returned, so the call can have answered only by the dialog's end of the
    wait, not because the handler finished; no clock is read."""
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=4248, binary_path=firefox_binary, headless=True,
                             humanize=False) as browser:
        page = browser.new_context().new_page()
        page.set_content('<button id="b" style="width:300px;height:200px" '
                         'onclick="alert(1); window.after = true">b</button>')
        opened = []
        page.on("dialog", lambda dialog: opened.append(dialog))     # a plain function: the client tags its handlers
        # raises (the engine's "did not handle ... within 5 s") if the call waited for the handler
        page.mouse.click(150, 100)
        # the dialog event comes before the reply of the call; the pump only lets a late one be read
        assert wait_until(lambda: opened, 30, lambda: page.wait_for_timeout(50)), (
            "the click answered without the dialog having been heard")
        assert [d.message for d in opened] == ["1"], opened
        opened[0].dismiss()
        page.wait_for_function("window.after === true", timeout=30_000)
