"""A session of the e2e that count what a browser asks ends before the test lets go of its place: the sender of the shutdown pings is gone.

⛔ THE KNOWN-BAD INPUT (measured with the kernel's own tools): `close()` returns when the browser has exited, and the sender of the
shutdown pings (`firefox --backgroundtask pingsender`) of the session lives on for about half a second, asking the resolver of the machine
for the telemetry host. The test after it (the next parameter of `test_nothing_leaves_but_the_proxy_e2e.py`, with a resolver trap of its own)
counted those questions as its own: 50-66 ms after it began. So the fixture that closes the session waits for the launch's reaper
(`EndingPlaywright`, `tests/_session_end.py`).

No browser here: a stand-in process of the session (registered under the session's token as the core registers the engine's tree) is alive
when the close returns, as the sender is, and the reaper of the launch ends it once its grace is over. Before: the close returned at once
and the process was alive. Now: the session's exit returns when the reaper has ended it, and fails, saying so, when the reaper does not
finish in time.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from invisible_core.process import EngineTree, SessionToken, guard_for, reap_session, register_tree
from invisible_playwright import InvisiblePlaywright
from invisible_playwright._juggler import _reaped

import _session_end
from _session_end import EndingPlaywright, ensure_ended

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent

#: The suites whose tests run a resolver trap or a watcher of direct connections: every session they make ends before the next test starts.
TRAPPED_SUITES = ("wrapper.e2e.doh", "wrapper.e2e.context.egress", "wrapper.e2e.context.sink", "wrapper.e2e.realness")


class _Reaper(threading.Thread):
    """The reaper of a launch as the core's: once its grace is over it ends the session's tree, runs the steps handed to `then`, and has finished."""

    def __init__(self, token, grace_over):
        super().__init__(daemon=True)
        self._token = token
        self._grace_over = grace_over
        self._lock = threading.Lock()
        self._steps = []
        self.finished = False

    def then(self, step):
        with self._lock:
            if not self.finished:
                self._steps.append(step)
                return
        step()

    def run(self):
        self._grace_over.wait(60)
        reap_session(self._token, timeout=5.0)
        with self._lock:
            steps, self._steps = self._steps, []
        for step in steps:
            step()
        with self._lock:
            self.finished = True


class _TheSessionBefore:
    """A session of the test before, without a browser: its `__enter__` starts the stand-in of the sender (registered under the session's token as
    the core registers the engine's tree) and its `__exit__` does what the close of the launcher does when the browser has exited: it hands the
    launch to a reaper, which ends the stand-in after its grace, and returns at once."""

    def __init__(self, monkeypatch, grace_over):
        self.grace_over = grace_over
        self.process = None
        self.reaper = None
        self.token = None
        monkeypatch.setattr(InvisiblePlaywright, "__enter__", lambda session: self._enter(session))
        monkeypatch.setattr(InvisiblePlaywright, "__exit__", lambda session, *exc: self._exit(session, *exc))

    def _enter(self, session):
        session._session_token = SessionToken.mint()
        self.token = SessionToken(session._session_token.value)
        self.process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        register_tree(self.token, EngineTree(self.process.pid))
        return object()

    def _exit(self, session, *exc):
        conn = SimpleNamespace(close=lambda: None, reaper=_Reaper(self.token, self.grace_over))
        self.reaper = conn.reaper
        _reaped.SessionRemoval(lambda: None, self.token.value).close(conn)
        conn.reaper.start()
        _reaped.final_reap(guard_for(), self.token)
        session._session_token = SessionToken()

    def clean_up(self):
        self.grace_over.set()
        if self.reaper is not None:
            self.reaper.join(10)
        if self.process is not None:
            self.process.kill()
            self.process.wait(10)
        if self.token is not None:
            _reaped._reapers.pop(self.token.value, None)


@pytest.fixture
def the_session_before(monkeypatch):
    session = _TheSessionBefore(monkeypatch, threading.Event())
    try:
        yield session
    finally:
        session.clean_up()


def _a_session(cls):
    return cls(seed=7, binary_path=os.devnull, headless=True)


def test_without_the_wait_the_process_of_the_session_before_is_alive_when_the_close_returns(the_session_before):
    """The old fixtures: the close returned, and the process of the session before was still there, to ask its questions of the next test."""
    with _a_session(InvisiblePlaywright):
        pass
    assert the_session_before.process.poll() is None, "the stand-in left before the close returned: it does not reproduce the sender"
    assert not the_session_before.reaper.finished


def test_the_exit_of_a_session_returns_when_the_reaper_has_ended_the_process_of_the_session_before(the_session_before):
    """The new fixtures: the exit waits for the reaper (its grace is over 0.3 s after the close), and nothing of the session is alive when it returns."""
    timer = threading.Timer(0.3, the_session_before.grace_over.set)
    timer.daemon = True
    timer.start()
    try:
        with _a_session(EndingPlaywright):
            pass
    finally:
        timer.cancel()
    assert the_session_before.reaper.finished, "the exit returned before the reaper of the launch had finished"
    assert the_session_before.process.poll() is not None, "the exit returned with the process of the session before alive"


def test_a_reaper_that_does_not_finish_fails_the_session_with_the_reason(the_session_before, monkeypatch):
    """Not in silence: the next test would start with the process of this one alive."""
    monkeypatch.setattr(_session_end, "ensure_ended", lambda token: ensure_ended(token, timeout=0.3))
    with pytest.raises(pytest.fail.Exception) as failure:
        with _a_session(EndingPlaywright):
            pass
    message = str(failure.value)
    assert "did not end within 0.3 s of its close" in message and "the reaper of its launch has not finished" in message, message
    assert the_session_before.process.poll() is None


def test_a_token_that_never_launched_is_nothing_to_wait_for():
    ensure_ended(SessionToken(), timeout=0.1)
    ensure_ended("", timeout=0.1)


def _tests_of(suite):
    suites = json.loads((ROOT / "ci" / "required-suites.json").read_text(encoding="utf-8"))
    entry = next(s for s in suites if s["id"] == suite)
    return sorted({ROOT / t.split("::")[0] for t in entry["tests"]})


@pytest.mark.parametrize("suite", TRAPPED_SUITES)
def test_every_session_of_the_trapped_suites_is_an_ending_one(suite):
    """A session made with the plain class would let the next test start while its sender is alive: the files of these suites make theirs with `EndingPlaywright`."""
    plain = []
    for path in _tests_of(suite):
        assert path.is_file(), path
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"(?<![A-Za-z_])InvisiblePlaywright\(", line) and not line.lstrip().startswith("#"):
                plain.append("%s:%d" % (path.name, number))
    assert plain == [], "a session that does not wait for its end: %s" % plain
