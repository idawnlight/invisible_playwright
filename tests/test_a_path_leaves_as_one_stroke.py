"""A path of the cursor is ONE stroke: the pacer's instants on the client's clock, each point sent without waiting for the reply of the one
before it.

THE KNOWN-BAD, measured on a GitHub runner (hunt 43794672372): `Pointer` asked the `Pacer` for the instant of each point, but every point
left through `actions.move`, a lone command that WAITS for the engine's reply. A slow reply stopped the path (the page saw a 2.6 s pause)
and the points after it were then crowded together. The keys had the same defect before the stroke.

What is asserted here is who sends the path, never how long it took:

  * with a stand-in for the core's actions (`_pointer_fakes.FakeActions`): the approach, the walk before a `Mouse.click`, the wheel burst
    and the idle movement each go to `Actions.walk` as one timeline, and no single `move` is made for them;
  * with the real `Actions` over a connection that records when a command is sent and when a reply is waited for: every point of the
    path is on the wire before the first reply is waited for, all of them in one stroke, each at its instant (`at`); a command of the
    user that is alone (`mouse.down`, `mouse.up`, a one-step `move`) still waits for its own reply.

Nothing launches a browser, and nothing here reads the time.
"""
from __future__ import annotations

import pytest

from invisible_core.juggler import PageActs
from invisible_core.juggler.actions import Actions

from invisible_playwright import _cursor

from _pointer_fakes import BOX, FakeInjected, FakePage, VirtualClock, make_pointer, use_clock
from invisible_playwright._juggler.pointer import Pointer

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.delenv(_cursor.LANDING_ENV, raising=False)
    _cursor._reset_warnings()
    yield
    _cursor._reset_warnings()


# ═══════════════════════════════════════════════════════════════════════════
# 1. Through a stand-in for the core's actions: what is handed over
# ═══════════════════════════════════════════════════════════════════════════

def test_the_approach_is_one_walk_and_no_single_move(monkeypatch):
    use_clock(monkeypatch, VirtualClock())
    page, pointer = make_pointer()
    pointer.approach("main", "#b", None)
    assert len(page.walks) == 1
    assert len(page.walks[0]["events"]) > 3, "a path of one point is not a path"
    assert page.singles == [], "a point of the path was sent as a command of its own"
    assert page.walks[0]["emit_last"] is False, "the last hop is the action's"


def test_the_walk_before_a_mouse_click_is_one_walk_and_no_single_move(monkeypatch):
    use_clock(monkeypatch, VirtualClock())
    page, pointer = make_pointer()
    pointer.travel(900.0, 500.0)
    assert len(page.walks) == 1 and len(page.walks[0]["events"]) > 3
    assert page.singles == []


def test_the_wheel_burst_is_one_walk_with_its_notches_in_it(monkeypatch):
    use_clock(monkeypatch, VirtualClock())
    page, pointer = make_pointer()
    assert pointer.wheel(0.0, 900.0) is True
    assert len(page.walks) == 1
    notches = [e for e in page.walks[0]["events"] if e.is_wheel]
    assert len(notches) > 1
    assert sum(e.dy for e in notches) == pytest.approx(900.0)
    assert page.singles == []


def test_the_idle_movement_and_the_approach_are_each_one_walk(monkeypatch):
    clock = VirtualClock()
    use_clock(monkeypatch, clock)
    page, pointer = make_pointer()
    pointer.approach("main", "#b", None)
    page.walks.clear()
    clock.t += 30.0  # the script was busy elsewhere
    pointer.approach("main", "#b", None)
    assert len(page.walks) == 2, "the fidget and the approach are one path each"
    assert page.singles == []


def test_the_instants_handed_over_are_the_pacers_and_grow(monkeypatch):
    use_clock(monkeypatch, VirtualClock())
    page, pointer = make_pointer()
    pointer.approach("main", "#b", None)
    ts = [e.t_ms for e in page.walks[0]["events"]]
    assert ts == sorted(ts) and ts[-1] > 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 2. Through the real `Actions`: what goes on the wire
# ═══════════════════════════════════════════════════════════════════════════

class Reply:
    def __init__(self, engine, index) -> None:
        self.engine, self.index = engine, index

    def wait(self, timeout=None):
        self.engine.log.append(("wait", self.index))
        return {}, 0


class HeldEngine:
    """A connection that records when each command is sent and when its reply is waited for. A reply is only ever read at `wait`, so a
    command sent before the `wait` of the one before it was sent while that reply was still held."""

    def __init__(self) -> None:
        self.sent = []
        self.log = []

    def send_async(self, method, params, session=None):
        self.log.append(("send", len(self.sent)))
        self.sent.append((method, dict(params)))
        return Reply(self, len(self.sent) - 1)

    def send(self, method, params=None, session=None, timeout=None, **kw):
        return self.send_async(method, params or {}, session).wait(timeout)[0]

    def kinds(self):
        return [k for k, _i in self.log]

    def params(self, method):
        return [p for m, p in self.sent if m == method]


class Life:
    main_frame = "main"


class Injected(FakeInjected):
    def check_hit_target(self, frame_id, element, point):
        return "done"


def _real(monkeypatch, *, seed=4242, budget=1.5):
    use_clock(monkeypatch, VirtualClock())
    engine = HeldEngine()
    page = FakePage(box=BOX)
    actions = Actions(engine, "S", Life(), Injected(page), acts=PageActs(0), session_seed=seed,
                      motion_budget_s=budget, layout="us")
    pointer = Pointer(actions, Injected(page), 0, seed, budget, lambda: page.viewport_size)
    return engine, actions, pointer


def _one_stroke_in_order(params):
    assert len(params) > 3, "a path of one point is not a path"
    assert len({p["stroke"] for p in params}) == 1
    ats = [p["at"] for p in params]
    assert ats == sorted(ats) and len(set(ats)) == len(ats), "the instants of a path increase"


def test_every_point_of_the_approach_is_sent_before_any_reply_is_waited_for(monkeypatch):
    engine, actions, pointer = _real(monkeypatch)
    pointer.approach("main", "#b", None)
    n = len(engine.sent)
    assert engine.kinds() == ["send"] * n + ["wait"] * n, "a point waited for the reply of the one before it"
    _one_stroke_in_order(engine.params("Page.dispatchMouseEvent"))


def test_every_point_of_the_walk_before_a_click_is_in_one_stroke(monkeypatch):
    engine, actions, pointer = _real(monkeypatch)
    pointer.travel(900.0, 500.0)
    n = len(engine.sent)
    assert engine.kinds() == ["send"] * n + ["wait"] * n
    _one_stroke_in_order(engine.params("Page.dispatchMouseEvent"))


def test_the_wheel_burst_is_one_stroke_of_moves_and_notches(monkeypatch):
    engine, actions, pointer = _real(monkeypatch)
    assert pointer.wheel(0.0, 900.0) is True
    n = len(engine.sent)
    assert engine.kinds() == ["send"] * n + ["wait"] * n
    everything = [p for _m, p in engine.sent]
    assert len({p["stroke"] for p in everything}) == 1
    ats = [p["at"] for p in everything]
    assert ats == sorted(ats)
    assert sum(p["deltaY"] for p in engine.params("Page.dispatchWheelEvent")) == pytest.approx(900.0)


def test_the_path_of_one_gesture_is_not_the_stroke_of_the_next(monkeypatch):
    engine, actions, pointer = _real(monkeypatch)
    pointer.travel(900.0, 500.0)
    first = {p["stroke"] for p in engine.params("Page.dispatchMouseEvent")}
    pointer.travel(100.0, 100.0)
    ids = {p["stroke"] for p in engine.params("Page.dispatchMouseEvent")}
    assert len(first) == 1 and len(ids) == 2


def test_a_lone_press_release_and_one_step_move_each_wait_for_their_own_reply(monkeypatch):
    engine, actions, pointer = _real(monkeypatch)
    actions.move(10.0, 10.0)
    actions.mouse_down()
    actions.mouse_up()
    assert engine.kinds() == ["send", "wait"] * 3
    assert all("stroke" not in p and "at" not in p for _m, p in engine.sent)


def test_the_explicit_move_walks_its_path_as_a_stroke_and_then_is_one_command(monkeypatch):
    engine, actions, pointer = _real(monkeypatch)
    pointer.move(900.0, 500.0)
    actions.move(900.0, 500.0)
    n = len(engine.sent) - 1
    assert engine.kinds() == ["send"] * n + ["wait"] * n + ["send", "wait"]
