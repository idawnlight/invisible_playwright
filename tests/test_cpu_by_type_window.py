"""The window of ``CpuSampler`` ends where ``end()`` is called, not a moment later.

``end()`` used to sleep 0.3 s before its last sample, so the CPU of a window included a third of a second of what the browser did AFTER the
work the test measured (more on a slower machine: the sleep is a clock, the CPU is not). The cumulative counters need no wait.

No browser and no process: ``engine_processes`` is a stand-in whose CPU is the time of a fake clock that ``time.sleep`` advances, so a sleep inside
the window shows as CPU seconds that the test never spent.
"""
from __future__ import annotations

import types

import pytest

import _cpu_by_type as cpu

pytestmark = pytest.mark.unit


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_the_window_holds_the_cpu_until_end_is_called_and_not_a_sleep_more(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(cpu, "time", types.SimpleNamespace(sleep=clock.sleep))
    monkeypatch.setattr(cpu, "engine_processes", lambda _root: {7: ("parent", clock.now)})

    sampler = cpu.CpuSampler(root_pid=1, period=3600)        # the thread's own samples are not the subject: it waits for the stop
    sampler.begin()
    clock.now += 2.0                                         # the measured work: the process used 2.0 s of CPU
    delta = sampler.end()

    assert delta["parent"] == pytest.approx(2.0), (
        "the window of the sampler held %.2f s of CPU for 2.0 s of work: end() waited before its last sample" % delta["parent"])


def test_a_process_born_in_the_window_counts_from_zero_and_one_that_ended_keeps_its_last_value(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(cpu, "time", types.SimpleNamespace(sleep=clock.sleep))
    seen = {"processes": {7: ("parent", 5.0), 8: ("tab", 1.0)}}
    monkeypatch.setattr(cpu, "engine_processes", lambda _root: dict(seen["processes"]))

    sampler = cpu.CpuSampler(root_pid=1, period=3600)
    sampler.begin()
    seen["processes"] = {7: ("parent", 6.5), 9: ("tab", 0.75)}       # 8 ended (its last value stays), 9 was born after the start
    delta = sampler.end()

    assert delta["parent"] == pytest.approx(1.5)
    assert delta["tab"] == pytest.approx(0.75)
