"""Motion never fails an action for what the engine or the page answers, and never hides a bug of ours.

⛔ THE KNOWN-BAD. `Pointer.act`, `travel` and `wheel` ended in `except Exception`, so that "motion never fails an action" would hold.
It held for everything, including a path that could not leave at all: a `Pointer` over an `actions` with no `walk` (a core older than
the one that makes a path a stroke) raised AttributeError on every point, the exception was swallowed, one warning was written and the
click went out with nothing before it - a page saw a click with no cursor path and every test of the action passed. A bug of ours is
not an error of the engine; it has to come out.

What stays swallowed is `_PROBE_ERRORS`: the element gone, the page navigating, the target closed, a command refused or unanswered.
The action meets the same state itself and raises its own error, so the motion in front of it never has to.
"""
from __future__ import annotations

import pytest

from invisible_core.juggler.connection import ProtocolError, ReplyTimeout, TargetClosedError
from invisible_core.juggler.injected import ContextDestroyed, FrameDetached

from invisible_playwright import _cursor

from _pointer_fakes import FakeActions, VirtualClock, make_pointer, use_clock

pytestmark = [pytest.mark.unit,
              # the guard cases are the ones that warn, once each: it is what the motion says when it lets the action run
              pytest.mark.filterwarnings("ignore:invisible_playwright:RuntimeWarning")]


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.delenv(_cursor.LANDING_ENV, raising=False)
    _cursor._reset_warnings()
    yield
    _cursor._reset_warnings()


class NoWalk(FakeActions):
    """The actions of a core that has no `walk`."""

    def __getattribute__(self, name):
        if name == "walk":
            raise AttributeError("'NoWalk' object has no attribute 'walk'")
        return super().__getattribute__(name)


class WalkRaises(FakeActions):
    def __init__(self, page, failure) -> None:
        super().__init__(page)
        self.failure = failure

    def walk(self, events, **kw):
        raise self.failure


def _pointer(monkeypatch, actions_of):
    use_clock(monkeypatch, VirtualClock())
    page, pointer = make_pointer()
    pointer.actions = actions_of(page)
    return page, pointer


ENGINE_AND_PAGE_ERRORS = [
    ProtocolError("the browser refused the command"),
    ReplyTimeout("no answer"),
    TargetClosedError("the target is closed"),
    ContextDestroyed("Failed to find execution context"),
    FrameDetached("Frame was detached"),
    TimeoutError("deadline"),
]
IDS = [type(e).__name__ for e in ENGINE_AND_PAGE_ERRORS]


# ═══════════════════════════════════════════════════════════════════════════
# 1. A bug of ours comes out of the action
# ═══════════════════════════════════════════════════════════════════════════

def test_an_action_over_actions_without_a_walk_raises_instead_of_clicking_with_no_path(monkeypatch):
    page, pointer = _pointer(monkeypatch, NoWalk)
    ran = []
    with pytest.raises(AttributeError, match="walk"):
        pointer.act("main", "#b", None, 5.0, lambda position, timeout: ran.append(1))
    assert ran == [], "the click went out although its path could not"


def test_the_walk_before_a_mouse_click_raises_instead_of_clicking_with_no_path(monkeypatch):
    page, pointer = _pointer(monkeypatch, NoWalk)
    ran = []
    with pytest.raises(AttributeError, match="walk"):
        pointer.click_at(300.0, 200.0, lambda: ran.append(1))
    assert ran == []


def test_a_wheel_burst_over_actions_without_a_walk_raises(monkeypatch):
    page, pointer = _pointer(monkeypatch, NoWalk)
    with pytest.raises(AttributeError, match="walk"):
        pointer.wheel(0.0, 900.0)


# ═══════════════════════════════════════════════════════════════════════════
# 2. GUARD: what the engine and the page answer does not fail the action
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("failure", ENGINE_AND_PAGE_ERRORS, ids=IDS)
def test_an_engine_error_during_the_approach_leaves_the_action_to_run(monkeypatch, failure):
    page, pointer = _pointer(monkeypatch, lambda p: WalkRaises(p, failure))
    got = pointer.act("main", "#b", None, 5.0, lambda position, timeout: "ran")
    assert got == "ran"


@pytest.mark.parametrize("failure", ENGINE_AND_PAGE_ERRORS, ids=IDS)
def test_an_engine_error_during_the_walk_before_a_mouse_click_leaves_the_click_to_run(monkeypatch, failure):
    page, pointer = _pointer(monkeypatch, lambda p: WalkRaises(p, failure))
    assert pointer.click_at(300.0, 200.0, lambda: "clicked") == "clicked"


@pytest.mark.parametrize("failure", ENGINE_AND_PAGE_ERRORS, ids=IDS)
def test_an_engine_error_during_a_wheel_burst_leaves_the_plain_wheel_to_the_caller(monkeypatch, failure):
    page, pointer = _pointer(monkeypatch, lambda p: WalkRaises(p, failure))
    assert pointer.wheel(0.0, 900.0) is False
