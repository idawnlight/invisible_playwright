"""An ElementHandle's pointer action walks onto its element the way a selector's does: ONE click path, ONE fingerprint.

THE DEFECT. `page.query_selector("#go").click()` went straight to `Actions.click(element_id=...)` and never through the server's hand (`Frame._by_hand` ->
`Pointer.act`). The core's approach is, with `engine_approach` False (the server never sets it), ONE `mousemove` onto the exact centre of the element's quad, a
settle, and the press on that same point. So a click through a handle was a jump to the exact geometric centre of the box, identical in every install, where the
same click through a selector is a drawn path with a landing point of the session's own. Two click paths are two fingerprints, which is the one thing the pointer
exists not to have, and the comment above the handle's actions said the opposite.

Nothing here launches a browser. The page is a real `PageDispatcher` shape over the core's real `Actions` (with its `PageActs`, `CursorMotion` and `Pacer`) and
the wrapper's real `Pointer`; only the connection (it RECORDS `Page.dispatchMouseEvent` and `Page.dispatchTapEvent`) and the injected script (it answers the
element's box, its hit test and its state) are stand-ins, on a clock that advances only when something sleeps. The operations called are the dispatchers' own
(`FrameDispatcher.op_click` for the selector, `ElementHandleDispatcher.op_click` for the handle), so what is asserted is the wiring and not a copy of it.

The e2e twin, in a real browser, is `test_a_handle_action_walks_like_a_selector_action_e2e.py`.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from invisible_core.juggler import PageActs
from invisible_core.juggler import actions as core_actions
from invisible_core.juggler.actions import Actions
from invisible_core.juggler.connection import EventListeners
from invisible_core.juggler.lifecycle import Lifecycle

from invisible_playwright import _cursor
from invisible_playwright._juggler import pointer as pointer_mod
from invisible_playwright._juggler.dispatcher import Dispatcher
from invisible_playwright._juggler.pointer import Pointer
from invisible_playwright._juggler.server import (
    ElementHandleDispatcher, FrameDispatcher, JugglerServer, PageDispatcher)

pytestmark = pytest.mark.unit

VIEWPORT = {"width": 1280, "height": 720}
BUDGET = 1.5
#: A control whose exact quad centre is (450.0, 320.0).
BOX = {"x": 400.0, "y": 300.0, "width": 100.0, "height": 40.0}
CENTRE = (450.0, 320.0)
HANDLE_ID = "el-1"
#: A spread of boxes the way a page lays out buttons and links: wide, tall, small, near the edge of the viewport.
BOXES = [
    {"x": 400.0, "y": 300.0, "width": 100.0, "height": 40.0},
    {"x": 880.5, "y": 120.25, "width": 160.0, "height": 36.0},
    {"x": 300.0, "y": 5.0, "width": 60.0, "height": 18.0},
    {"x": 500.0, "y": 40.0, "width": 90.0, "height": 30.0},
]
SEEDS = range(1, 121)


# -- the stand-ins ---------------------------------------------------------------------------------------------------------------------------------

class VirtualClock:
    """Advances only when something sleeps, for the pointer's pacer and for the pauses the core's actions take (press settle, dwell)."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)


class RecordingConnection:
    """The engine's side: records what the page would receive, answers the quad of the one box."""

    def __init__(self, box) -> None:
        self.box = box
        self.log = []

    def send(self, method, params=None, session=None, timeout=None, **kw):
        params = params or {}
        if method in ("Page.dispatchMouseEvent", "Page.dispatchTapEvent"):
            self.log.append({"m": method, "type": params.get("type", "tap"), "x": params["x"], "y": params["y"]})
        if method == "Page.getContentQuads":
            b = self.box
            return {"quads": [{
                "p1": {"x": b["x"], "y": b["y"]},
                "p2": {"x": b["x"] + b["width"], "y": b["y"]},
                "p3": {"x": b["x"] + b["width"], "y": b["y"] + b["height"]},
                "p4": {"x": b["x"], "y": b["y"] + b["height"]}}]}
        if method == "Page.dispatchMouseEvent" and "landsOn" in params:
            return {"landing": {"landed": True, "on": "el"}}
        return {}


class FakeInjected:
    """The page's script: the element's box, a hit test that accepts every point of the box, the state of a checkbox that is unchecked until clicked."""

    def __init__(self, conn) -> None:
        self.conn = conn
        self.queried = []
        self.disposed = []
        self.measured = []
        self.state_reads = 0

    def query_selector(self, f, selector, strict=False):
        self.queried.append(selector)
        return "resolved-from-the-selector"

    def bounding_box(self, f, el):
        self.measured.append(el)
        return dict(self.conn.box)

    def dispose(self, f, el):
        self.disposed.append(el)

    def element_states(self, f, el, states):
        return {"ok": True}

    def element_state(self, f, el, state):
        self.state_reads += 1
        return self.state_reads > 1

    def evaluate(self, f, expr, **kw):
        return {"w": VIEWPORT["width"], "h": VIEWPORT["height"]}

    def check_hit_target(self, f, el, point):
        b = self.conn.box
        inside = b["x"] <= point[0] < b["x"] + b["width"] and b["y"] <= point[1] < b["y"] + b["height"]
        return "done" if inside else "other"

    def scroll_into_view(self, *a, **k):
        raise AssertionError("not expected: the box is on screen")


class Life:
    main_frame = "main"


def build_page(box=BOX, *, seed=4242, number=0, budget=BUDGET):
    """A page the dispatchers act on: real `Actions` and `Pointer`, as `PageDispatcher._build` assembles them."""
    server = JugglerServer()
    server.attach(SimpleNamespace(emit_message=lambda message: None))
    context = Dispatcher(server, None)
    context.options = {"hasTouch": True}
    conn = RecordingConnection(box)
    injected = FakeInjected(conn)
    actions = Actions(conn, "sess", Life(), injected, acts=PageActs(number), session_seed=seed, motion_budget_s=budget, layout="us")
    page = object.__new__(PageDispatcher)
    page.context = context
    page.lifecycle = Lifecycle(EventListeners(), "session")
    page.injected = injected
    page.actions = actions
    page.viewport_size = dict(VIEWPORT)
    page.pointer = Pointer(actions, injected, number, seed, budget, lambda: page.viewport_size)
    page._frames = {}
    page._navigation_requests = {}
    page._announced_closed = False
    Dispatcher.__init__(page, server, context)
    page.frame = FrameDispatcher(server, page, "main")
    page.handle = ElementHandleDispatcher(server, page.frame, HANDLE_ID)
    page.engine = conn
    return page


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    c = VirtualClock()
    monkeypatch.setattr(pointer_mod, "_CLOCK", c)
    # the core pauses with `time.sleep` and counts a stroke's instants on `time.monotonic`: both are this clock
    monkeypatch.setattr(core_actions, "time", SimpleNamespace(sleep=c.sleep, monotonic=c.now, perf_counter=time.perf_counter))
    monkeypatch.delenv(_cursor.ENGINE_ENV, raising=False)
    monkeypatch.delenv(_cursor.LANDING_ENV, raising=False)
    _cursor._reset_warnings()
    yield c
    _cursor._reset_warnings()


def act(how, kind, *, box=BOX, seed=4242, number=0, **params):
    """One action, through the dispatcher a client's call arrives at: `how` is "selector" (the Frame's op) or "handle" (the ElementHandle's).
    Returns the page, whose `conn.log` is what the engine received."""
    page = build_page(box, seed=seed, number=number)
    if how == "selector":
        page.frame.call(kind, {"selector": "#b", "noWaitAfter": True, **params})
    else:
        page.handle.call(kind, {"noWaitAfter": True, **params})
    return page


def moves(page):
    return [(e["x"], e["y"]) for e in page.engine.log if e["type"] == "mousemove"]


def presses(page):
    return [(e["x"], e["y"]) for e in page.engine.log if e["type"] in ("mousedown", "tap")]


def moves_before_the_press(page):
    out = []
    for e in page.engine.log:
        if e["type"] in ("mousedown", "tap"):
            break
        if e["type"] == "mousemove":
            out.append((e["x"], e["y"]))
    return out


def entries(box, points, start):
    """How many times the pointer ENTERS the box: maximal runs of consecutive positions inside it, from `start` (what a page counts as `mouseover`)."""
    def inside(p):
        return box["x"] <= p[0] < box["x"] + box["width"] and box["y"] <= p[1] < box["y"] + box["height"]
    n, was = 0, False
    for p in [start] + list(points):
        now = inside(p)
        if now and not was:
            n += 1
        was = now
    return n


# -- 1. a handle's pointer action is a walk, not a jump to the centre ------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["click", "dblclick"])
def test_a_handle_press_is_preceded_by_a_walk_and_not_a_single_jump(kind):
    """The defect, stated plainly. Today: ONE `mousemove` (onto the exact centre) and then the press."""
    for seed in (4242, 1, 2, 3, 77):
        page = act("handle", kind, seed=seed)
        before = moves_before_the_press(page)
        assert len(before) > 1, "seed %d: a handle %s made %d mousemove before its press: %r" % (seed, kind, len(before), before)


@pytest.mark.parametrize("kind", ["click", "dblclick", "tap"])
def test_a_handle_press_does_not_land_on_the_exact_centre(kind):
    """The point a press lands on is the session's own landing point, not the centre of the quad that every install would share."""
    centred = []
    for seed in SEEDS:
        for box in BOXES:
            page = act("handle", kind, box=box, seed=seed)
            point = presses(page)[0]
            if point == (box["x"] + box["width"] / 2.0, box["y"] + box["height"] / 2.0):
                centred.append((seed, box))
    assert not centred, "%d of %d handle %ss pressed on the exact centre of the box: %r" % (len(centred), len(SEEDS) * len(BOXES), kind, centred[:3])


def test_a_handle_hover_walks_and_ends_off_the_centre():
    for seed in (4242, 1, 2, 3, 77):
        page = act("handle", "hover", seed=seed)
        path = moves(page)
        assert len(path) > 1, "seed %d: a handle hover made %d mousemove: %r" % (seed, len(path), path)
        assert path[-1] != CENTRE, "seed %d: a handle hover ended on the exact centre" % seed


@pytest.mark.parametrize("kind", ["check", "uncheck"])
def test_a_handle_check_and_uncheck_walk_and_press_off_the_centre(kind):
    # the stand-in box is not in the wanted state until it has been clicked, which is what both actions look for
    page = act("handle", kind)
    assert len(moves_before_the_press(page)) > 1, "a handle %s jumped to its press" % kind
    assert presses(page)[0] != CENTRE, "a handle %s pressed on the exact centre" % kind


# -- 2. one click path: the handle's walk is the selector's -------------------------------------------------------------------------------------------

def test_a_handle_and_a_selector_enter_the_box_alike_over_the_same_seeds_and_boxes():
    """The same seed and the same boxes: the statistics of entries into the box per action are the same through a handle and through a selector."""
    histograms = {"selector": {}, "handle": {}}
    for seed in SEEDS:
        for box in BOXES:
            for how in histograms:
                page = act(how, "click", box=box, seed=seed)
                start = page.pointer.hand.start_point(VIEWPORT["width"], VIEWPORT["height"])
                n = entries(box, moves(page), start)
                histograms[how][n] = histograms[how].get(n, 0) + 1
    assert histograms["handle"] == histograms["selector"], "entries into the box -> actions: through a handle %r, through a selector %r" % (
        dict(sorted(histograms["handle"].items())), dict(sorted(histograms["selector"].items())))


@pytest.mark.parametrize("kind", ["click", "dblclick", "hover", "tap", "check"])
def test_a_handle_action_draws_the_same_events_as_the_selector_action_of_the_name(kind):
    """One path, not two similar ones: same seed, same page, same box, the engine receives the same events at the same points."""
    for seed in (4242, 9, 31):
        by_selector = act("selector", kind, seed=seed)
        by_handle = act("handle", kind, seed=seed)
        assert by_handle.engine.log == by_selector.engine.log, "seed %d: %s differs between the handle and the selector" % (seed, kind)


# -- 3. what the handle's action must keep -----------------------------------------------------------------------------------------------------------

def test_the_walk_aims_at_the_handle_s_node_and_never_looks_a_selector_up_or_disposes_the_handle():
    page = act("handle", "click")
    assert len(moves(page)) > 1
    assert page.injected.queried == [], "the handle's node is given; a selector was queried: %r" % page.injected.queried
    assert page.injected.disposed == [], "the caller's handle was disposed: %r" % page.injected.disposed
    assert set(page.injected.measured) == {HANDLE_ID}, page.injected.measured
    # and the same handle acts again: it is still the caller's
    page.handle.call("click", {"noWaitAfter": True})
    assert page.injected.disposed == []


def test_a_selector_walk_still_disposes_its_own_probe():
    page = act("selector", "click")
    assert page.injected.disposed and set(page.injected.disposed) == {"resolved-from-the-selector"}


def test_a_position_the_caller_pinned_is_the_press_point_of_a_handle_too():
    page = act("handle", "click", position={"x": 10.0, "y": 5.0})
    assert presses(page) == [(410.0, 305.0)]
    assert len(moves_before_the_press(page)) > 1, "the pinned point is where the press lands; the pointer still gets there by a path"


def test_a_trial_run_through_a_handle_moves_nothing():
    page = act("handle", "click", trial=True)
    assert page.engine.log == []
    assert page.injected.disposed == []


def test_without_a_session_seed_a_handle_action_is_the_plain_action():
    """Humanising off keeps meaning no walk: the one event the action itself makes."""
    page = build_page()
    page.pointer = Pointer(page.actions, page.injected, 0, None, None, lambda: VIEWPORT)
    assert not page.pointer.active
    page.handle.call("click", {"noWaitAfter": True})
    assert moves(page) == [CENTRE]


# -- 4. the audit: the keyboard and text actions of a handle are the selector's, argument for argument -----------------------------------------------

#: (wire method, the core `Actions` method both doors end in, the params of the request, the keyword arguments that name the target)
KEYBOARD_AND_TEXT = [
    ("fill", "fill", {"value": "v", "timeout": 4000, "force": True}),
    ("type", "type_text", {"text": "ab", "delay": 30, "timeout": 4000}),
    ("press", "press", {"key": "Enter", "delay": 20, "timeout": 4000, "noWaitAfter": True}),
    ("focus", "focus", {"timeout": 4000}),
    ("selectText", "select_text", {"timeout": 4000}),
]


@pytest.mark.parametrize("wire,method,params", KEYBOARD_AND_TEXT, ids=[k[0] for k in KEYBOARD_AND_TEXT])
def test_a_handle_s_keyboard_and_text_actions_reach_the_core_as_the_selector_s_do(wire, method, params):
    """The cadence of typing (the pause before the first key, the focus, the key delay) lives in the core's action, which both doors call: the audit's conclusion,
    held. If a door grew a rhythm of its own, or dropped an argument, the two calls would differ here."""
    page = build_page()
    page.actions = Mock()
    page.frame.call(wire, {"selector": "#b", **params})
    page.handle.call(wire, dict(params))
    by_selector, by_handle = getattr(page.actions, method).call_args_list
    assert by_handle.args[1:] == by_selector.args[1:]
    drop = {"frame_id", "element_id"}
    assert {k: v for k, v in by_handle.kwargs.items() if k not in drop} == {k: v for k, v in by_selector.kwargs.items() if k not in drop}
    assert by_handle.kwargs["element_id"] == HANDLE_ID and "element_id" not in by_selector.kwargs
