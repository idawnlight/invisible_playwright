"""What follows the exit of the browser, for the in-process server: the session's directories go with the launch's reaper.

⛔ THE CLOSE OF A BROWSER RETURNS WHEN THE BROWSER HAS EXITED (invisible-core's `Connection.close`). What stays after it is
the sender of the shutdown pings, and the launch's `reaper` (`invisible_core.process.Reaper`, a thread that is not a
daemon) waits for it within its grace, ends what is left, releases the launch and runs the steps handed to `reaper.then`.
The removal of the session's directories is such a step: `SessionFiles.remove` waits for the sender too, so run by the
server after the shutdown it kept the wrapper's close as slow as it was before the core stopped waiting.

A core before that has no `reaper` on its connection: the removal runs after the shutdown, as it always did.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

_lock = threading.Lock()
#: Session token (its value) -> the reapers of the launches made under it, the ones that have not finished at the last look.
_reapers: Dict[str, List[Any]] = {}


def reaper_of(conn: Any) -> Any:
    """The launch's reaper, or None: the core has none, or the connection was never closed."""
    return getattr(conn, "reaper", None)


class SessionRemoval:
    """The removal of ONE launch's directories, run once, by the reaper when the connection has one and otherwise by the
    server after its hooks."""

    def __init__(self, remove: Callable[[], None], token: str = "") -> None:
        self._remove = remove
        self._token = token
        self._lock = threading.Lock()
        self._done = False
        self._handed = False

    def run(self) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
        self._remove()

    def after_shutdown(self) -> None:
        """The step the server runs once its hooks have: the removal itself, unless the reaper has been handed it.

        ⛔ ON THIS ROUTE THERE IS NO LAUNCH THREAD TO HAND THE STEP TO: `close` did not hand it, because the connection has no
        reaper (a core before the one that makes them, or a close that raised), so the removal runs where it is, here. The
        session's other steps (`after_the_reapers`) are the ones that wait for the reapers."""
        with self._lock:
            if self._handed:
                return
        self.run()

    def close(self, conn: Any) -> None:
        """The hook of the shutdown: closes the connection (which returns when the browser has exited) and, when the core
        gives the launch a reaper, hands the removal to it, so the shutdown does not wait for the sender or the tree."""
        conn.close()
        reaper = reaper_of(conn)
        if reaper is None:
            return
        with self._lock:
            if self._handed:
                return
            self._handed = True
        if self._token:
            with _lock:
                kept = [r for r in _reapers.get(self._token, ()) if not getattr(r, "finished", False)]
                kept.append(reaper)
                _reapers[self._token] = kept
        reaper.then(self.run)


def wait_reaped(token: Any, timeout: Optional[float] = None) -> bool:
    """Waits for what follows the exit of the browsers closed under ``token`` (a `SessionToken` or its value): the
    processes that outlived them are finished or ended, the launches are released and the session's directories are gone.
    True when it is all done (also when the core has no reaper: nothing is left to wait for), False at ``timeout``. Costs
    nothing when nothing was left. Only after the close."""
    value = getattr(token, "value", token)
    with _lock:
        reapers = list(_reapers.get(value, ()))
    deadline = None if timeout is None else time.monotonic() + timeout
    for reaper in reapers:
        reaper.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
    done = all(getattr(r, "finished", True) for r in reapers)
    if done:
        with _lock:
            kept = [r for r in _reapers.get(value, ()) if r not in reapers]
            if kept:
                _reapers[value] = kept
            else:
                _reapers.pop(value, None)
    return done


def after_the_reapers(step: Callable[[], Any], *tokens: Any, forget: bool = False) -> Any:
    """Runs ``step`` once, after the last of the reapers of the launches made under ``tokens`` (a `SessionToken` or its
    value each), and returns at once with None when it was handed over. With no reaper at all it runs now, on the caller,
    and its result is returned: the way it always ran.

    ⛔ THE ONE PLACE FOR "AFTER THE SESSION'S REAPERS". A step that the server's shutdown thread or the user's close would run
    waits there for what the reaper waits for (the sender of the shutdown pings, the tree), and a folder's removal is as slow
    as the disk. Handed to the reapers (`then`) it runs on the thread that ends the tree, after the grace, and nothing is
    waited for. Nothing here assumes which thread runs the step: that is `Reaper.then`'s to say.

    ``forget``: the reapers of this snapshot are taken out of the tokens' entries with the step (even when it raises): a
    process that opens sessions without end (the server) would keep one entry per session, with its reapers and their trees.
    Only the reapers of THIS snapshot go: a launch registered under the same token meanwhile keeps its own. Left False, the
    entries stay for `wait_reaped`, which is what joins the reapers and takes them out. Never raises on the reapers' side;
    the step's own errors reach whoever runs it."""
    values = [getattr(token, "value", token) for token in tokens]
    with _lock:
        reapers = [r for value in values for r in _reapers.get(value, ())]
    if not reapers:
        return step()
    pending = [len(reapers)]
    gate = threading.Lock()

    def after_the_last() -> None:
        with gate:
            pending[0] -= 1
            if pending[0]:
                return
        try:
            step()
        finally:
            if forget:
                for value in values:
                    _forget(value, reapers)

    for reaper in reapers:
        reaper.then(after_the_last)
    return None


def final_reap(guard: Any, token: Any) -> int:
    """The teardown's last step: whatever still carries the session's ``token`` is ended.

    ⛔ WHEN THE SESSION'S LAUNCHES HAVE A REAPER THE LAST REAP IS ITS TO RUN. `guard.reap` ends the session's tree at once, and
    what is left at that moment is the sender of the shutdown pings: ended here, the pings were lost and the grace the reaper
    gives it was for nothing. The reap is handed to the reapers (`after_the_reapers`), so it runs after the grace and the end of
    what is left, and it is the net it always was for what the reaper did not end. Returns at once then, with 0: nothing is
    waited for.

    With no reaper (a core before the one that makes them, a launch that failed before it had one, an error path that never
    reached the shutdown) it runs now, as it always did. Never raises on the reapers' side; the guard's own errors reach the
    caller as they did."""
    return after_the_reapers(lambda: guard.reap(token), token, forget=True) or 0


def _forget(value: str, reapers: List[Any]) -> None:
    """Takes ``reapers`` out of the token's entry, and the entry with them when nothing else is in it."""
    with _lock:
        kept = [r for r in _reapers.get(value, ()) if not any(r is gone for gone in reapers)]
        if kept:
            _reapers[value] = kept
        else:
            _reapers.pop(value, None)
