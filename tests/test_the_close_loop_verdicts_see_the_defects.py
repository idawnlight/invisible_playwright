"""The verdicts of `test_closing_one_of_four_browsers_does_not_hold_the_loop_e2e.py` are red on the defect and green on the fix, without a browser.

The e2e judges no duration: a loop that is held is told by WHERE its thread was, and a browser that answers by the ORDER of the events.
Here the instrument of that test (its own source, taken out of the client script) runs on a loop in a thread of this process:

* a step of the close that waits, run on the loop's thread, is seen there and the verdict is red; the same step run in a thread of its
  own is not on the loop's stack and the verdict is green;
* the control that holds the loop on purpose is seen in the function that holds it, and one that does not hold it is a watcher that
  could not see (red);
* a loop the machine did not schedule is not charged for the time it waited: what is charged never exceeds what the same samples saw run;
* the other browsers: calls that did not begin and end inside the close, or a removal of the profile that only went on at its deadline
  because nothing could answer while it waited, are red;
* the removal of the profile that the client holds still: the thread that reaches it is read off the stack the client records (here
  with a real `Reaper` of invisible-core, alive and finished: `run` and `_continue`), and it is red when it is the loop's thread, a
  thread inside a step of the close or a thread that is not the reaper's; the close that returns only after the release, and the
  removal that is never reached, are red.

Every wait here ends when the thing it waits for happened (30 s is the deadline of a hang): nothing decides by a number of milliseconds.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import threading
import time
import traceback

import psutil
import pytest

from invisible_core.process import Reaper

import _close_loop_verdicts as verdicts
import test_closing_one_of_four_browsers_does_not_hold_the_loop_e2e as e2e

pytestmark = pytest.mark.unit

_CLOSE_STEP_FILE = "/checkout/src/invisible_playwright/_juggler/transport.py"
_STEP_SOURCE = (
    "import time\n"
    "def shutdown(done):\n"
    "    while not done():\n"
    "        time.sleep(0.005)\n"
)
_HOLDER_SOURCE = (
    "import time\n"
    "def hold_the_loop(done):\n"
    "    while not done():\n"
    "        time.sleep(0.005)\n"
)


def _function(source, filename, name):
    scope = {}
    exec(compile(source, filename, "exec"), scope)
    return scope[name]


def _instrument_class(main_id):
    """The `Instrument` of the client, exactly as the e2e writes it, bound to the thread that will run the loop."""
    text = e2e._CLIENT
    source = text[text.index("class Instrument:"):text.index("def engines():")]
    scope = {"asyncio": asyncio, "json": json, "os": os, "shutil": shutil, "sys": sys, "threading": threading,
             "time": time, "traceback": traceback, "psutil": psutil, "main_id": main_id}
    exec(compile(source, "client_instrument", "exec"), scope)
    return scope["Instrument"]


def _where_the_removal_runs(main_id):
    """`where_the_removal_runs` of the client, exactly as the e2e writes it, bound to the thread that is the loop's."""
    text = e2e._CLIENT
    source = text[text.index("def where_the_removal_runs"):text.index("async def pinger")]
    scope = {"os": os, "threading": threading, "traceback": traceback, "main_id": main_id}
    exec(compile(source, "client_where", "exec"), scope)
    return scope["where_the_removal_runs"]


def _run_on_a_loop(scenario):
    """Run ``scenario(Instrument)`` (a coroutine function) as the main coroutine of a loop that lives in a thread of its own."""
    out = {}

    def target():
        Instrument = _instrument_class(threading.get_ident())
        try:
            out["value"] = asyncio.run(asyncio.wait_for(scenario(Instrument), 60))
        except BaseException as failure:  # noqa: BLE001 - handed to the test
            out["failure"] = failure

    thread = threading.Thread(target=target, name="the-loop-under-test")
    thread.start()
    thread.join(90)
    assert not thread.is_alive(), "the scenario did not end"
    if "failure" in out:
        raise out["failure"]
    return out["value"]


def test_the_client_script_compiles():
    compile(e2e._CLIENT, "client.py", "exec")


# ---------------------------------------------------------------------------------------------------------------------------------
# a step of the close that waits: on the loop's thread (red), in a thread of its own (green)
# ---------------------------------------------------------------------------------------------------------------------------------

def test_a_step_of_the_close_run_on_the_loop_is_seen_and_the_verdict_is_red():
    shutdown = _function(_STEP_SOURCE, _CLOSE_STEP_FILE, "shutdown")

    async def scenario(Instrument):
        instrument = Instrument()
        await instrument.start()
        # Held until the watcher has seen the thread inside it: the loop's thread is what is seen, whatever the speed of the machine.
        end = time.monotonic() + 30
        shutdown(lambda: bool(verdicts.blocked_by(instrument.inside)) or time.monotonic() > end)
        await instrument.finish()
        return instrument.report()

    report = _run_on_a_loop(scenario)
    problem = verdicts.close_on_the_loop_problem(report["inside"])
    assert problem is not None
    assert _CLOSE_STEP_FILE + ":shutdown" in problem
    assert "sleep" in report["on_the_loop"]


def test_the_same_step_in_a_thread_of_its_own_leaves_the_loop_free_and_the_verdict_is_green():
    shutdown = _function(_STEP_SOURCE, _CLOSE_STEP_FILE, "shutdown")

    async def scenario(Instrument):
        instrument = Instrument()
        await instrument.start()
        ended = threading.Event()
        began = threading.Event()

        def step():
            began.set()
            # The same step, the same wait, until the loop has gone on twice (the sentinel has beaten after it began): the loop was
            # served while it waited.
            beats = len(instrument.gaps)
            shutdown(lambda: len(instrument.gaps) >= beats + 2)
            ended.set()

        threading.Thread(target=step, name="the-step").start()
        while not ended.is_set():
            await asyncio.sleep(0.01)
        await instrument.finish()
        return instrument.report()

    report = _run_on_a_loop(scenario)
    assert verdicts.close_on_the_loop_problem(report["inside"]) is None, verdicts.close_on_the_loop_problem(report["inside"])
    assert report["on_the_loop"] == []


# ---------------------------------------------------------------------------------------------------------------------------------
# the control that holds the loop on purpose
# ---------------------------------------------------------------------------------------------------------------------------------

_HOLDER = (("/client.py", ("hold_the_loop",)),)


def test_the_control_sees_a_loop_held_in_the_function_that_holds_it():
    hold_the_loop = _function(_HOLDER_SOURCE, "/tmp/client.py", "hold_the_loop")

    async def scenario(Instrument):
        instrument = Instrument()
        await instrument.start()
        end = time.monotonic() + 30
        hold_the_loop(lambda: bool(verdicts.blocked_by(instrument.inside, _HOLDER)) or time.monotonic() > end)
        await instrument.finish()
        return instrument.report()

    report = _run_on_a_loop(scenario)
    assert verdicts.blind_instrument_problem(report["inside"], _HOLDER) is None


def test_a_watcher_that_never_saw_the_held_loop_is_red():
    # An instrument that saw nothing of where the thread was (the loop idle, or a watcher that reads no stack) is not a control.
    assert verdicts.blind_instrument_problem({}, _HOLDER) is not None
    assert verdicts.blind_instrument_problem({"/tmp/other.py:main": {"n": 3, "stack": []}}, _HOLDER) is not None

    async def scenario(Instrument):
        instrument = Instrument()
        await instrument.start()
        await asyncio.sleep(0.1)
        await instrument.finish()
        return instrument.report()

    assert verdicts.blind_instrument_problem(_run_on_a_loop(scenario)["inside"], _HOLDER) is not None


# ---------------------------------------------------------------------------------------------------------------------------------
# a loop the machine did not schedule
# ---------------------------------------------------------------------------------------------------------------------------------

def test_a_loop_the_machine_did_not_schedule_is_charged_no_more_than_the_time_it_ran():
    """The second control of the e2e, on the real instrument: late by the pause of the sentinel, then a callback that runs."""

    async def scenario(Instrument):
        unscheduled = Instrument()
        await unscheduled.start()
        await asyncio.sleep(0.05)
        unscheduled.pause_once = 0.3
        ran = threading.Event()

        def burst():
            # Runs until the watcher has seen the thread run for 45 ms in a row (30 s at most): the callback is what holds the loop.
            end = time.monotonic() + 30
            while unscheduled.busy_run < 0.045 and time.monotonic() < end:
                pass
            ran.set()

        asyncio.get_running_loop().call_later(0.15, burst)
        while not ran.is_set():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.4)
        await unscheduled.finish()
        return unscheduled.report()

    report = _run_on_a_loop(scenario)
    assert report["gap"] > 0.25, "the sentinel was not late: %r" % report["gap"]
    assert report["busy_run"] > 0, "the watcher never saw the callback run"
    problem = verdicts.charged_more_than_ran_problem(report["held"], report["busy_run"])
    assert problem is None, problem


def test_charging_the_wait_in_the_selector_as_held_is_red():
    # What the first instrument did (candidate 43727128285): 145 ms in the selector and a 45 ms callback charged as 195 ms.
    assert verdicts.charged_more_than_ran_problem(0.195, 0.045) is not None
    assert verdicts.charged_more_than_ran_problem(0.094, 0.0) is not None
    assert verdicts.charged_more_than_ran_problem(0.045, 0.045) is None
    assert verdicts.charged_more_than_ran_problem(0.0, 0.0) is None


# ---------------------------------------------------------------------------------------------------------------------------------
# which frames are a step of the close that waits
# ---------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("frame", [
    "C:/w/src/invisible_playwright/_juggler/transport.py:shutdown",
    "C:/w/src/invisible_playwright/_juggler/transport.py:_join_workers",
    "C:/w/src/invisible_playwright/_juggler/server.py:op_close",
    "C:/venv/Lib/site-packages/invisible_core/process.py:reap",
    "C:/venv/Lib/site-packages/psutil/__init__.py:wait_procs",
    "C:/Python312/Lib/shutil.py:rmtree",
    "C:/Python312/Lib/threading.py:join",
])
def test_a_step_that_waits_is_a_blocking_step(frame):
    assert verdicts.blocked_by({frame: {"n": 1, "stack": [frame]}}) == [frame]


@pytest.mark.parametrize("frame", [
    "C:/Python312/Lib/asyncio/base_events.py:shutdown_asyncgens",
    "C:/Python312/Lib/concurrent/futures/thread.py:shutdown",
    "C:/Python312/Lib/asyncio/tasks.py:wait_for",
    "C:/w/src/invisible_playwright/async_api.py:_teardown",
    "C:/w/src/invisible_playwright/_juggler/transport.py:request_stop",
    "C:/w/src/invisible_playwright/_juggler/transport.py:wait_until_stopped",
    "C:/venv/Lib/site-packages/invisible_core/_offloop.py:off_the_loop",
])
def test_what_the_loop_runs_between_two_awaits_of_the_close_is_not_a_blocking_step(frame):
    assert verdicts.blocked_by({frame: {"n": 9, "stack": [frame]}}) == []


# ---------------------------------------------------------------------------------------------------------------------------------
# the other browsers
# ---------------------------------------------------------------------------------------------------------------------------------

def _others(inside=(2, 2, 2), removal=(1, 1, 1)):
    return [{"inside": a, "during_removal": b, "slowest": 9.9} for a, b in zip(inside, removal)]


def test_the_others_that_answered_inside_the_close_and_during_the_removal_are_green_whatever_the_seconds_they_took():
    # The calls took 9.9 s each in these records: the seconds decide nothing.
    assert verdicts.others_answered_problem(_others(), {"reached": True, "released_by": "answered"}) is None


def test_a_browser_that_answered_nothing_inside_the_close_is_red():
    problem = verdicts.others_answered_problem(_others(inside=(4, 0, 4)), {"reached": True, "released_by": "answered"})
    assert problem is not None and "[1]" in problem


def test_a_removal_that_went_on_only_at_its_deadline_is_red():
    # The loop could not run while the removal waited for the answers: the removal ran on it. Calls that began and ended inside the
    # close after the deadline do not make it green.
    problem = verdicts.others_answered_problem(_others(inside=(5, 5, 5), removal=(0, 0, 0)), {"reached": True, "released_by": "deadline"})
    assert problem is not None and "deadline" in problem


def test_the_hold_of_the_removal_is_not_asked_when_no_removal_was_reached():
    # Another verdict says the removal must have been reached (`removal_never_reached_problem`); this one still asks for the calls
    # inside the close.
    assert verdicts.others_answered_problem(_others(), {"reached": False, "released_by": None}) is None
    assert verdicts.others_answered_problem(_others(inside=(1, 1, 0)), {"reached": False, "released_by": None}) is not None


def test_fewer_than_three_other_browsers_followed_is_red():
    assert verdicts.others_answered_problem(_others()[:2], {"reached": True, "released_by": "answered"}) is not None


def test_a_browser_that_answered_nothing_while_the_removal_was_held_is_red():
    problem = verdicts.others_answered_problem(_others(removal=(2, 0, 1)), {"reached": True, "released_by": "answered"})
    assert problem is not None and "[1]" in problem


# ---------------------------------------------------------------------------------------------------------------------------------
# the removal of the profile that the client holds still: WHO reaches it
# ---------------------------------------------------------------------------------------------------------------------------------

class _Tree:
    """The tree of a reaper that is still working: `end` returns when the test lets it (the sender of the pings still alive)."""

    def __init__(self):
        self.go = threading.Event()

    def end(self, *, grace=0.0, timeout=5.0):
        self.go.wait(30)

    def release(self):
        pass


def _removal_by(where, how):
    """What the client records of a removal reached the way ``how(step)`` runs it: a step that calls the hook (`slow_rmtree`, one
    frame above `where_the_removal_runs`) and keeps what it recorded."""
    got = []

    def slow_rmtree():
        got.append(dict(where(), path="/tmp/invisible_profile_x"))

    how(slow_rmtree)
    assert len(got) == 1, "the step did not run"
    return {"reached": True, "released_by": "answered", "order": ["reached", "close_returned", "released"], "removal": got[0]}


def _on_a_reaper_that_is_alive(step):
    tree = _Tree()
    reaper = Reaper(tree)
    reaper.start()
    reaper.then(step)               # handed over while it works: the step runs on its own thread, after the tree
    tree.go.set()
    reaper.join(30)
    assert reaper.finished


def _on_a_reaper_that_has_finished(step):
    reaper = Reaper(None)
    reaper.start()
    reaper.join(30)
    assert reaper.finished
    reaper.then(step)               # handed over after the end: the step runs on a continuation of it
    reaper.join(30)
    assert reaper.finished


@pytest.mark.parametrize("how, frame", [(_on_a_reaper_that_is_alive, "process.py:run"), (_on_a_reaper_that_has_finished, "process.py:_continue")])
def test_a_removal_reached_by_the_reaper_or_its_continuation_is_green(how, frame):
    # The frames are the real ones of invisible-core's `Reaper`: the verdict reads the stack the way the client records it.
    gate = _removal_by(_where_the_removal_runs(threading.get_ident()), how)
    assert any(f.endswith(frame) for f in gate["removal"]["stack"]), gate["removal"]["stack"]
    assert gate["removal"]["ident"] != gate["removal"]["loop_ident"]
    assert verdicts.removal_on_the_users_thread_problem(gate) is None, verdicts.removal_on_the_users_thread_problem(gate)


def test_a_removal_reached_on_the_loops_thread_is_red():
    gate = _removal_by(_where_the_removal_runs(threading.get_ident()), lambda step: step())
    problem = verdicts.removal_on_the_users_thread_problem(gate)
    assert problem is not None and "thread of the loop" in problem


def test_a_removal_reached_inside_a_step_of_the_close_is_red():
    # The shutdown of the server (a thread of the close that the user waits on) removes the profile itself: no reaper was handed it.
    shutdown = _function("def shutdown(step):\n    step()\n", _CLOSE_STEP_FILE, "shutdown")
    gate = _removal_by(_where_the_removal_runs(threading.get_ident()),
                       lambda step: _join(threading.Thread(target=shutdown, args=(step,), name="invisible-teardown")))
    assert gate["removal"]["ident"] != gate["removal"]["loop_ident"]
    problem = verdicts.removal_on_the_users_thread_problem(gate)
    assert problem is not None and _CLOSE_STEP_FILE + ":shutdown" in problem


def test_a_removal_reached_on_a_thread_that_is_not_the_reapers_is_red():
    gate = _removal_by(_where_the_removal_runs(threading.get_ident()),
                       lambda step: _join(threading.Thread(target=step, name="invisible-teardown")))
    problem = verdicts.removal_on_the_users_thread_problem(gate)
    assert problem is not None and "not the reaper's" in problem


def test_a_removal_that_was_reached_and_left_no_thread_is_red():
    assert verdicts.removal_on_the_users_thread_problem({"reached": True, "removal": None}) is not None
    assert verdicts.removal_on_the_users_thread_problem({"reached": False, "removal": None}) is None


def _join(thread):
    thread.start()
    thread.join(30)
    assert not thread.is_alive()


# ---------------------------------------------------------------------------------------------------------------------------------
# the order: the close returns while the removal is held
# ---------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("order", [
    ["reached", "close_returned", "released"],
    ["close_returned", "reached", "released"],
])
def test_a_close_that_returned_before_the_release_is_green_whenever_the_removal_was_reached(order):
    assert verdicts.close_waited_for_the_removal_problem({"order": order, "released_by": "answered"}) is None


def test_a_close_that_returned_only_after_the_release_is_red():
    # The removal was reached on a thread the close waits on: the close could not return while it was held, the deadline released
    # it and only then the close returned.
    problem = verdicts.close_waited_for_the_removal_problem(
        {"order": ["reached", "released", "close_returned"], "released_by": "deadline"})
    assert problem is not None and "deadline" in problem
    assert verdicts.close_waited_for_the_removal_problem({"order": ["reached"], "released_by": None}) is not None


# ---------------------------------------------------------------------------------------------------------------------------------
# the removal that is never reached
# ---------------------------------------------------------------------------------------------------------------------------------

def test_a_removal_that_was_never_reached_is_red():
    problem = verdicts.removal_never_reached_problem({"reached": False, "released_by": None, "order": ["close_returned"], "removal": None})
    assert problem is not None and "never" in problem
    assert verdicts.removal_never_reached_problem({"reached": True}) is None
