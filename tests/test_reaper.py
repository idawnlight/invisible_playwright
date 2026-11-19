"""The reaper: does it kill our tree, and does it leave everything else alone.

These use REAL processes rather than mocks. The bug being fixed is about what
the operating system does with a re-parented process tree, and a mock of
psutil would only ever confirm that the code calls the functions it calls -
which is not the thing in doubt. Short-lived `python -c "sleep"` children stand
in for the browser: what matters is that they carry an inherited environment
block, and they do.

The negative case is the important one. A reaper that kills everything passes
every positive test ever written.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from invisible_core import process as core_process
from invisible_playwright._juggler import _reaped
from invisible_core.process import (
    JobObjectGuard, NullGuard, SessionToken, find_processes, guard_for,
)

pytestmark = pytest.mark.unit

psutil = pytest.importorskip("psutil")

# Long enough that nothing exits on its own during a test, short enough that a
# leaked child from a crashed run is gone before it bothers anyone.
_SLEEP = 30


def _spawn(token) -> subprocess.Popen:
    env = dict(os.environ)
    env.pop(core_process.TOKEN_VAR, None)
    if token:
        env = token.stamp(env)
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({_SLEEP})"], env=env
    )


def _reap_all(*procs: subprocess.Popen) -> None:
    for p in procs:
        try:
            p.kill()
            p.wait(timeout=5)
        except Exception:
            pass


def test_a_token_is_unique_per_session():
    """Two sessions must never share a token, or one reaps the other."""
    assert len({SessionToken.mint() for _ in range(200)}) == 200


def test_it_finds_the_process_carrying_the_token():
    token = SessionToken.mint()
    proc = _spawn(token)
    try:
        time.sleep(0.6)
        found = {p.pid for p in find_processes(token)}
        assert proc.pid in found
    finally:
        _reap_all(proc)


def test_a_child_inherits_the_token_and_is_reaped_with_the_parent():
    """The whole point on Windows: the tree, not the process we launched.

    A grandchild spawned by the marked process carries the same environment,
    so it is found and killed even though nothing ever recorded its pid.
    """
    token = SessionToken.mint()
    env = token.stamp(os.environ)
    parent = subprocess.Popen(
        [sys.executable, "-c",
         f"import subprocess,sys,time;"
         f"subprocess.Popen([sys.executable,'-c','import time; time.sleep({_SLEEP})']);"
         f"time.sleep({_SLEEP})"],
        env=env,
    )
    try:
        time.sleep(1.5)
        pids = {p.pid for p in find_processes(token)}
        assert len(pids) >= 2, f"grandchild not found, only {pids}"
        killed = guard_for().reap(token)
        assert killed >= 2
        assert core_process.wait_until_gone(token, timeout=8.0)
    finally:
        _reap_all(parent)


def test_it_does_not_touch_a_process_with_a_different_token():
    """The incident this design exists to prevent.

    Two sessions running side by side on the same binary. Reaping one must
    leave the other running - a leaked browser is a bug, killing someone
    else's is an incident, and only one of the two is recoverable.
    """
    mine, theirs = SessionToken.mint(), SessionToken.mint()
    a, b = _spawn(mine), _spawn(theirs)
    try:
        time.sleep(0.8)
        mine_pids = {p.pid for p in find_processes(mine)}
        assert guard_for().reap(mine) >= 1
        assert core_process.wait_until_gone(mine, timeout=8.0)
        assert b.poll() is None, "reaping one session killed the other's process"

        # The other session is still THERE, and nothing of the reaped one is.
        #
        # This used to assert `== {b.pid}` - exactly one process - which is a
        # hidden precondition, not the claim. On Windows a venv's `python.exe`
        # is a redirector that starts the real interpreter as a child, so a
        # spawn there is TWO processes and both carry the token. It passed on CI
        # (setup-python hands out a real interpreter) and on a system Python,
        # and failed the moment the suite was run from a venv - which is a shape
        # users have, and the shape this package's own e2e tests build.
        survivors = {p.pid for p in find_processes(theirs)}
        assert b.pid in survivors, (
            f"the surviving session is no longer findable by its token: {survivors}")
        assert not (survivors & mine_pids), (
            f"a reaped process is still answering to the other session's token: "
            f"{survivors & mine_pids}")
    finally:
        _reap_all(a, b)


def test_it_does_not_touch_an_unmarked_process():
    token = SessionToken.mint()
    marked, plain = _spawn(token), _spawn(None)
    try:
        time.sleep(0.8)
        guard_for().reap(token)
        assert core_process.wait_until_gone(token, timeout=8.0)
        assert plain.poll() is None, "an unmarked process was killed"
    finally:
        _reap_all(marked, plain)


def test_reaping_nothing_reports_nothing():
    """0 is the answer for a clean session, and it has to be distinguishable
    from the reaper not running at all - which is why reap() returns a count."""
    assert guard_for().reap(SessionToken.mint()) == 0


def test_an_empty_token_never_matches_even_a_process_that_carries_an_empty_one():
    """A falsy token must not degenerate into a match.

    The obvious version of this test - spawn nothing, assert an empty token
    finds nothing - passes with the guard REMOVED, because a process without
    the variable answers None and None never equals "". It proved nothing. The
    case that actually separates the two is a process whose token IS the empty
    string: without the guard that is a match, and a session that failed before
    minting a token would reap it.
    """
    victim = _spawn(None)
    try:
        # Set the variable to empty rather than leaving it out entirely.
        env = dict(os.environ)
        env[core_process.TOKEN_VAR] = ""
        empty = subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep({_SLEEP})"], env=env
        )
    except Exception:  # pragma: no cover
        _reap_all(victim)
        raise
    try:
        time.sleep(0.8)
        assert find_processes(SessionToken()) == []
        assert guard_for().reap(SessionToken()) == 0
        assert empty.poll() is None, "an empty token reaped a live process"
    finally:
        _reap_all(victim, empty)


def test_the_platform_is_tested_in_exactly_one_place():
    """guard_for is the seam. Every caller takes a LifetimeGuard and never asks
    which one, so no launcher code branches on the operating system."""
    assert isinstance(guard_for("posix"), NullGuard)
    assert isinstance(guard_for("java"), NullGuard)
    made = guard_for("nt") if os.name == "nt" else None
    if made is not None:
        assert isinstance(made, (JobObjectGuard, NullGuard))


def test_the_null_guard_says_it_guarantees_nothing_instead_of_implying_it():
    """A Null Object that silently does nothing is indistinguishable from one
    that worked. This one answers the question directly, so a caller can tell
    a platform without the mechanism from a platform where it succeeded."""
    null = NullGuard()
    assert null.guaranteed is False
    assert null.bind(SessionToken.mint()) == 0
    assert JobObjectGuard.guaranteed is True


def test_the_null_guard_still_reaps_because_that_part_needs_no_kernel():
    """Immediate cleanup is inherited, not stubbed out. A browser that refuses
    to close should not have to wait for this process to exit, on any OS."""
    token = SessionToken.mint()
    proc = _spawn(token)
    try:
        time.sleep(0.8)
        assert NullGuard().reap(token) >= 1
        assert core_process.wait_until_gone(token, timeout=8.0)
    finally:
        _reap_all(proc)


def test_a_token_is_a_value_not_a_string():
    """Equality is by value and an empty token is falsy, which is what stops
    a failed session from sweeping the machine."""
    a = SessionToken("abc")
    assert a == SessionToken("abc") and a != SessionToken("abd")
    assert bool(a) and not bool(SessionToken())
    assert len({SessionToken("x"), SessionToken("x")}) == 1
    assert a.stamp({"PATH": "/usr"})[core_process.TOKEN_VAR] == "abc"
    assert a.stamp({"PATH": "/usr"})["PATH"] == "/usr", "stamp dropped the env"


def test_an_empty_token_matches_nothing_at_the_matcher_itself():
    """Isolates SessionToken.matches rather than going through find_processes.

    find_processes ALSO refuses an empty token, so a test that went through it
    stayed green with this guard removed - the two checks covered for each
    other and neither was actually verified. Belt and braces are fine; a belt
    nothing tests is not.
    """
    class CarriesEmpty:
        def environ(self):
            return {core_process.TOKEN_VAR: ""}

    assert SessionToken().matches(CarriesEmpty()) is False
    assert SessionToken("real").matches(CarriesEmpty()) is False


def test_stamping_does_not_mutate_the_environment_it_was_given():
    """The launcher builds env once and passes it on; a stamp that mutated in
    place would leak this session's token into whatever else holds that dict."""
    original = {"PATH": "/usr"}
    SessionToken.mint().stamp(original)
    assert original == {"PATH": "/usr"}


def test_binding_reports_zero_rather_than_claiming_a_guarantee_it_lacks():
    """0 means the guarantee is NOT in place, and callers can tell.

    An empty token cannot identify anything, so there is nothing to bind. The
    honest answer is 0, not a silently successful no-op.
    """
    assert guard_for().bind(SessionToken(), wait=0.5) == 0


def test_a_process_whose_environment_cannot_be_read_is_left_alone():
    """Kill on POSITIVE identification only.

    AccessDenied is the normal answer for another user's or an elevated
    process. Answering 'not ours' is the safe direction, and it must not be an
    exception either - one unreadable process cannot abort the whole sweep.
    """
    class Unreadable:
        def environ(self):
            raise psutil.AccessDenied(pid=1)

    assert SessionToken('anything').matches(Unreadable()) is False


class _FakeReaper:
    """A launch's reaper as the core's: `then` keeps the step for the thread that ends the tree."""

    def __init__(self):
        self.steps = []
        self.finished = False

    def then(self, step):
        if self.finished:
            step()
        else:
            self.steps.append(step)

    def join(self, timeout=None):
        pass

    def finish(self):
        steps, self.steps = self.steps, []
        for step in steps:
            step()
        self.finished = True


def test_the_final_reap_with_a_reaper_runs_after_it_and_spares_the_sender_until_then():
    """⛔ THE KNOWN-BAD INPUT: the teardown's last step ended the session's tree at once, and with the core's reaper what is
    left at that moment is the sender of the shutdown pings: it was killed in the middle of its work and the reaper's grace
    was for nothing. A process that carries the token (the sender's stand-in) is alive after `final_reap` and until the
    reaper has finished; then the reap ends it."""
    from invisible_playwright._juggler import _reaped

    token = SessionToken.mint()
    sender = _spawn(token)
    reaper = _FakeReaper()
    _reaped._reapers[token.value] = [reaper]
    try:
        time.sleep(0.6)
        assert _reaped.final_reap(guard_for(), token) == 0
        assert sender.poll() is None, "the final reap killed the sender before the reaper had finished"
        assert sender.pid in {p.pid for p in find_processes(token)}
        reaper.finish()
        assert core_process.wait_until_gone(token, timeout=8.0), "the final reap did not run after the reaper"
    finally:
        _reaped._reapers.pop(token.value, None)
        _reap_all(sender)


def test_the_final_reap_without_a_reaper_is_immediate():
    """No reaper (a core before it, a launch that failed first): the reap ends the tree now, as it always did."""
    from invisible_playwright._juggler import _reaped

    token = SessionToken.mint()
    proc = _spawn(token)
    try:
        time.sleep(0.6)
        assert _reaped.final_reap(guard_for(), token) >= 1
        assert core_process.wait_until_gone(token, timeout=8.0)
    finally:
        _reap_all(proc)


def test_the_final_reap_with_two_reapers_runs_once_after_both():
    from invisible_playwright._juggler import _reaped

    calls = []

    class Guard(NullGuard):
        def reap(self, token, *, timeout: float = 5.0) -> int:
            calls.append(token.value)
            return 0

    token = SessionToken.mint()
    first, second = _FakeReaper(), _FakeReaper()
    _reaped._reapers[token.value] = [first, second]
    try:
        _reaped.final_reap(Guard(), token)
        first.finish()
        assert calls == []
        second.finish()
        assert calls == [token.value]
    finally:
        _reaped._reapers.pop(token.value, None)


def test_the_sync_teardown_leaves_the_final_reap_to_the_reaper_of_the_launch():
    from invisible_playwright import InvisiblePlaywright
    from invisible_playwright._juggler import _reaped

    reaped = []

    class Recording(NullGuard):
        def reap(self, token, *, timeout: float = 5.0) -> int:
            reaped.append(token.value)
            return 0

    session = InvisiblePlaywright(seed=7)
    session._session_token = SessionToken.mint()
    session._lifetime_guard = Recording()
    expected = session._session_token.value
    reaper = _FakeReaper()
    _reaped._reapers[expected] = [reaper]
    try:
        session._teardown()
        assert reaped == [], "the teardown reaped at once and did not leave it to the reaper"
        reaper.finish()
        assert reaped == [expected]
    finally:
        _reaped._reapers.pop(expected, None)
    # No reaper: immediate.
    session._session_token = SessionToken.mint()
    session._teardown()
    assert len(reaped) == 2


def test_the_final_reap_takes_its_reapers_out_of_the_registry_even_when_the_reap_raises():
    """⛔ A server that opens sessions without end kept an entry per session, with the reapers and their trees, until somebody
    asked `wait_reaped`. The entry goes with the last reap, in a `finally`; a launch registered under the same token in the
    meantime keeps its reaper."""
    from invisible_playwright._juggler import _reaped

    class Raising(NullGuard):
        def reap(self, token, *, timeout: float = 5.0) -> int:
            raise RuntimeError("the reap failed")

    token = SessionToken.mint()
    old, newer = _FakeReaper(), _FakeReaper()
    _reaped._reapers[token.value] = [old]
    try:
        _reaped.final_reap(Raising(), token)
        _reaped._reapers[token.value].append(newer)       # a launch of the same token, registered meanwhile
        with pytest.raises(RuntimeError):
            old.finish()
        assert _reaped._reapers[token.value] == [newer], "the snapshot's reapers were not taken out, or the newer one was"
    finally:
        _reaped._reapers.pop(token.value, None)


def test_the_final_reap_leaves_no_entry_when_it_was_the_only_launch():
    from invisible_playwright._juggler import _reaped

    token = SessionToken.mint()
    reaper = _FakeReaper()
    _reaped._reapers[token.value] = [reaper]
    _reaped.final_reap(NullGuard(), token)
    assert token.value in _reaped._reapers
    reaper.finish()
    assert token.value not in _reaped._reapers, "the entry of a finished session stays for ever"

# --- the removal handed to a reaper that has already finished (moved here from a file of its own, so the unit rows name it) ---
# The removal of the session's directories never runs on the shutdown thread, not even when the launch's reaper has finished.
#
# Found in a hunt on Windows (4 browsers of one process closed together): `SessionRemoval.close` hands the removal to the launch's reaper
# with `reaper.then`, and the core's `then` ran the step at once, on the caller, when the reaper had already finished. The caller was the
# `juggler-shutdown` thread, which `__aexit__` waits for, and the removal (four profile directories deleted together, 0.4 to 2.5 s) kept
# the user's close from returning: 12 of 24 closes came back more than 0.5 s after the browser had exited.
#
# The core's reaper (`invisible_core.process.Reaper`) now gives a step handed after its end to a continuation thread, covered by `join()`,
# `finished` and so by `wait_reaped`. This test uses the core's own reaper, finished, and no clock: the removal is held on an Event.
_FINISHED_TOKEN = "removal-after-a-finished-reaper"
#: A safety net for the waits of a test, never a measure.
_FINISHED_NET = 60


def test_a_close_with_a_finished_reaper_returns_while_the_removal_is_held_and_wait_reaped_waits_for_it():
    reaper = core_process.Reaper(None)
    reaper.start()
    reaper.join(_FINISHED_NET)
    assert reaper.finished

    removal_started, release, removed = threading.Event(), threading.Event(), threading.Event()
    where = {}

    def remove() -> None:
        where["thread"] = threading.current_thread()
        removal_started.set()
        assert release.wait(_FINISHED_NET)
        removed.set()

    conn = type("Conn", (), {"reaper": reaper, "close": lambda self, *a, **k: None})()
    removal = _reaped.SessionRemoval(remove, _FINISHED_TOKEN)
    returned = threading.Event()

    def shutdown() -> None:                  # the `juggler-shutdown` thread, which the user's close waits for
        removal.close(conn)
        returned.set()

    closer = threading.Thread(target=shutdown, name="juggler-shutdown", daemon=True)
    try:
        closer.start()
        assert removal_started.wait(_FINISHED_NET), "the removal never ran"
        # On a core before the fix the removal runs inside `close`, on this thread, and it cannot return while it is held.
        assert where["thread"] is not closer, "the removal ran on the shutdown thread"
        assert returned.wait(_FINISHED_NET), "the close did not return"
        assert not removed.is_set(), "the close returned only after the removal had ended"
        assert _reaped.wait_reaped(_FINISHED_TOKEN, timeout=0) is False, "wait_reaped said done while the removal was held"
        release.set()
        assert _reaped.wait_reaped(_FINISHED_TOKEN, timeout=_FINISHED_NET) is True
        assert removed.is_set()
    finally:
        release.set()
        closer.join(_FINISHED_NET)
        _reaped._reapers.pop(_FINISHED_TOKEN, None)
