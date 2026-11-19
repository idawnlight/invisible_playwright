"""A page's handler runs after its input event was born, as with a real
keyboard and mouse ([B299]).

A handler can read by how much: `event.timeStamp` against its own
`performance.now()`. Measured on retail Windows Firefox 151 with a real
keyboard and mouse: 3 to 18 ms below. Until firefox-45 ours were born at
their dispatch and the two were equal, and a tracker that keeps only events
from the past (`isTrusted && !repeat && 0 < timeStamp && timeStamp < now`)
dropped 25 keys of 30 and some of the mouse moves: measured 5 keydown of 15
kept on the Windows build, 1 of 15 on the Linux one. The wheel stayed born at
its dispatch on firefox-45 ([B301]): 10 of 20 kept, 0 to 2 ms below, where a
real wheel on retail Windows Firefox is 6 to 14 ms below.
"""
from __future__ import annotations

import statistics

import pytest

pytestmark = pytest.mark.e2e

#: The latency the core declares for an input event (invisible_core.juggler._behaviour.InputLatency.draw keeps it between these), and
#: the precision of the page's timer.
LATENCY_MIN_MS, LATENCY_MAX_MS = 2.0, 30.0
TIMER_PRECISION_MS = 1.0

PAGE = """<!doctype html><body style="margin:40px"><input id=q style="width:300px">
<script>
window.L = [];
for (const t of ['keydown', 'keyup', 'mousemove', 'wheel']) document.addEventListener(t, e => {
  const now = performance.now();
  L.push([t, e.timeStamp - now,
          e.isTrusted && !e.repeat && 0 < e.timeStamp && e.timeStamp < now]);
}, {capture: true, passive: true});
</script>"""


@pytest.mark.wall_clock
def test_every_key_and_move_is_born_before_its_handler_runs(firefox_binary):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=42, binary_path=firefox_binary,
                             headless=True) as browser:
        page = browser.new_page()
        page.set_content(PAGE)
        page.click("#q")
        page.keyboard.type("scarpe da corsa", delay=120)
        for i in range(20):
            page.mouse.move(100 + i * 7, 150 + i * 3)
        for _ in range(10):
            page.mouse.wheel(0, 100)
            page.wait_for_timeout(100)
        log = page.evaluate("window.L")
    for kind in ("keydown", "keyup", "mousemove", "wheel"):
        rows = [r for r in log if r[0] == kind]
        assert rows, kind
        kept = [r for r in rows if r[2]]
        assert len(kept) == len(rows), (kind, rows)
        # EVERY event is born before its handler runs: the sign of its age, for each key and each move. An order, not a duration,
        # so no load of the machine can turn it: a handler that runs late only makes the age more negative.
        ages = [r[1] for r in rows]
        assert all(age < 0.0 for age in ages), (kind, rows)
        # Not born at its dispatch: the TYPICAL event (the median of the stream) is at least the least declared latency old (the core
        # draws it between 2 and 30 ms, invisible_core.juggler._behaviour.InputLatency; the page's timer has a precision of 1 ms).
        # The load of the machine can only move this median away from zero, never towards it.
        assert statistics.median(ages) <= -(LATENCY_MIN_MS - TIMER_PRECISION_MS), (kind, statistics.median(ages), rows)
        # Not born further in the past than declared: AT LEAST ONE event of the stream is within the greatest declared latency.
        # A late handler adds its delay to the age of its event (a loaded machine has a median delay above zero), so neither a
        # bound on each event nor one on the median is the product's; one event handled without delay proves the birth is not
        # backdated beyond the declared latency, and the load would have to delay every handler of the run to turn it red.
        youngest = max(ages)
        assert youngest >= -(LATENCY_MAX_MS + TIMER_PRECISION_MS), (kind, youngest, rows)
