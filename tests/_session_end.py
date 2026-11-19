"""The end of a session, for the e2e that count what a browser asks or connects to: the next test starts when this one's browser is gone.

⛔ THE KNOWN-BAD INPUT: `close()` returns when the BROWSER has exited (invisible-core's reaper), while the sender of the shutdown pings
(`firefox --backgroundtask pingsender`) lives on for about half a second and asks the resolver of the machine for the telemetry host. The
sessions without a proxy of the launch (`context_only`) may do that, and the test after them is a parametrized neighbour with a resolver trap
(or a watcher of direct connections) of its own, started within milliseconds: it counted the question of the session before as its own, and five
runs of `test_nothing_leaves_but_the_proxy_e2e.py` were red for a browser that was not theirs (measured by the kernel: the questions arrived 50-66
ms after the next test began).

So a session of those tests ENDS before the test lets go of its place: `EndingPlaywright` is an `InvisiblePlaywright` whose `__exit__` waits for the
launch's reaper (`wait_reaped(token)`: the sender is finished or ended, the launch released, the session's directories gone) and for the session's
processes to be gone (`wait_until_gone`). The 30 s are a safety net, never a measure: the wait ends when the reaper does. A reaper that does not
finish within them FAILS the test, with the processes that are left: going on in silence would put the next test back in the same room.

Every session of the e2e files that run a resolver trap or a watcher is made with this class (`tests/test_a_session_ends_before_the_next_test_starts.py`
reads those files and fails on one that is not).
"""
from __future__ import annotations

import pytest

from invisible_core.process import SessionToken, find_processes, wait_until_gone
from invisible_playwright import InvisiblePlaywright
from invisible_playwright._juggler._reaped import wait_reaped

#: How long a session may take to end after its close, in seconds. A safety net: the wait ends when the reaper does.
SAFETY_NET_S = 30.0


def ensure_ended(token, *, timeout: float = SAFETY_NET_S) -> None:
    """Waits until what follows the close of the session ``token`` is over: the reaper has finished and nothing carries the token.

    Fails the test, with the reason, when it is not over within ``timeout`` seconds. An empty token (a session that never launched) is nothing to wait for."""
    token = SessionToken(getattr(token, "value", token))
    if not token:
        return
    if not wait_reaped(token, timeout=timeout):
        pytest.fail(
            "the session %r did not end within %s s of its close: the reaper of its launch has not finished (processes of the session still "
            "alive: %s). The next test would start with this one's processes alive and count what they ask." % (
                token, timeout, _left(token)), pytrace=False)
    if not wait_until_gone(token, timeout=timeout):
        pytest.fail(
            "the reaper of the session %r finished and processes of the session are still alive after %s s: %s" % (
                token, timeout, _left(token)), pytrace=False)


def _left(token) -> list:
    """(pid, name) of what still carries the token, for a message."""
    left = []
    for proc in find_processes(token):
        try:
            left.append((proc.pid, proc.name()))
        except Exception:
            left.append((getattr(proc, "pid", None), "?"))
    return left


class EndingPlaywright(InvisiblePlaywright):
    """An `InvisiblePlaywright` whose close returns when the session has ENDED, not when its browser has exited."""

    #: The token of the launch, kept at its start: the teardown empties the session's own.
    _end_token = SessionToken()

    def __enter__(self):
        try:
            return super().__enter__()
        finally:
            self._end_token = SessionToken(self._session_token.value)

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            token, self._end_token = self._end_token, SessionToken()
            ensure_ended(token)
