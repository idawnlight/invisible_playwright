"""The helpers of ``test_popups_reach_the_client_e2e.py``, without a browser.

* ``_quiet`` says a set of processes is quiet after a COUNT of equal reads, never after seconds of a clock that a stalled machine keeps running.
* ``_in_a_thread_of_its_own`` runs the second sync client of a worker where no asyncio loop is running: an open sync client leaves its loop
  marked as running in its thread (the module's ``browser`` fixture keeps one open for the whole module), and a second sync client started in the
  same thread refuses with "Playwright Sync API inside the asyncio loop" (run 43206169241, a setup error of this file's process case).
"""
from __future__ import annotations

import asyncio
import types

import pytest

import test_popups_reach_the_client_e2e as popups

pytestmark = pytest.mark.unit


class _Clock:
    def __init__(self, stall_at_pause: int, stall: float) -> None:
        self.now = 0.0
        self.pauses = 0
        self._stall_at = stall_at_pause
        self._stall = stall

    def sleep(self, seconds: float) -> None:
        self.pauses += 1
        self.now += seconds + (self._stall if self.pauses == self._stall_at else 0.0)

    def module(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(time=lambda: self.now, monotonic=lambda: self.now, sleep=self.sleep)


def test_a_stall_is_one_read_and_not_three_seconds_of_quiet(monkeypatch):
    """The machine stalls 5 s between the 2nd and the 3rd read, and the process that the browser was about to start appears at the 4th. A clock of
    "3 s unchanged" had run out during the stall and answered with the set before the process; a count of reads waits for it."""
    clock = _Clock(stall_at_pause=2, stall=5.0)
    reads = {"n": 0}
    before, after = frozenset({1}), frozenset({1, 2})

    def read(_engine_dir):
        reads["n"] += 1
        return before if reads["n"] <= 3 else after

    monkeypatch.setattr(popups, "time", clock.module())
    monkeypatch.setattr(popups, "_content_processes", read)

    assert popups._quiet("engine") == after, "quiet was declared before the process that the stall had held back"


def test_a_set_that_never_settles_is_given_back_at_the_deadline_and_not_waited_for_ever(monkeypatch):
    clock = _Clock(stall_at_pause=0, stall=0.0)
    reads = {"n": 0}

    def read(_engine_dir):
        reads["n"] += 1
        return frozenset({reads["n"]})

    monkeypatch.setattr(popups, "time", clock.module())
    monkeypatch.setattr(popups, "_content_processes", read)

    assert popups._quiet("engine", limit=2.0) == frozenset({reads["n"]})
    assert clock.now >= 2.0


def test_the_second_sync_client_runs_where_no_loop_is_running():
    """Inside a running loop (what an open sync client leaves in the thread of the module's browser) a call that needs "no loop here" would be
    refused; through ``_in_a_thread_of_its_own`` it sees none."""
    def asks_for_a_loop():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return "no loop"
        return "a loop is running"

    async def inside_a_loop():
        assert asks_for_a_loop() == "a loop is running", "the stand-in for an open sync client must have a loop running here"
        return popups._in_a_thread_of_its_own(asks_for_a_loop, timeout=30)

    assert asyncio.run(inside_a_loop()) == "no loop"


def test_what_the_thread_raises_comes_back_to_the_test():
    def fails():
        raise ValueError("the browser of the process case said no")

    with pytest.raises(ValueError, match="said no"):
        popups._in_a_thread_of_its_own(fails, timeout=30)
