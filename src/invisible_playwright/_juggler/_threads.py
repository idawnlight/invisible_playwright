"""The one way this package starts a thread from a place that must not wait for it.

`Thread.start` does not return until the new thread is running (`self._started.wait()`), and on Windows under load that wait is
milliseconds long. Two places in the package cannot afford it: the engine connection's reader (an event is measured late by every
millisecond the reader is late) and the user's event loop (`InProcessTransport.connect`: four worker starts held the loop ~200 ms per
launch with four browsers in one process). Both hand the thread to `ThreadStarter`.
"""
from __future__ import annotations

import _thread as _raw_thread
import queue
import threading
from typing import Any, Callable, Optional


class ThreadStarter:
    """The ONE long-lived thread that starts the other threads it is handed.

    ⛔ NO `Thread.start` ON THE CALLER, AND THAT INCLUDES BUILDING THE STARTER. `start` and `start_built` only queue (a `Thread` that is
    only BUILT waits for nothing); this thread starts what is queued. The starter's own thread is begun with `_thread.start_new_thread`
    running `Thread.start`: the OS makes a thread and the caller returns at once, and the wait for the starter to run happens on that
    raw thread, not on the caller. The starter is still a `threading.Thread` named `invpw-thread-starter` (it is alive a moment after
    the constructor returns, not before).

    It starts threads and does not run the work itself, on purpose: an adoption waits for the engine as long as the engine takes (a popup
    whose destination never answers is waited for without a limit), and a worker that ran it would hold every later adoption behind it,
    `new_page()` included. Each piece of work keeps the thread of its own and the name it always had.
    """

    #: Every starter built and not yet ended (from the constructor, which returns before its thread runs, to the end of that thread).
    #: Without it a starter whose thread has not begun yet is in no `threading.enumerate()` and nobody can count it: the census of
    #: tests/_browser_census.py missed the leaked starters on a machine where the raw thread was late to run.
    _open: "set" = set()
    _open_lock = threading.Lock()

    @classmethod
    def open_starters(cls) -> list:
        with cls._open_lock:
            return list(cls._open)

    def __init__(self, on_error: Callable[[str], None]) -> None:
        with ThreadStarter._open_lock:
            ThreadStarter._open.add(self)
        self._queue: "queue.SimpleQueue" = queue.SimpleQueue()
        self._on_error = on_error
        self._stopped = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="invpw-thread-starter")
        _raw_thread.start_new_thread(self._begin, ())

    def _begin(self) -> None:
        try:
            self._thread.start()
        except BaseException as failure:
            self._ended()
            self._report("%s could not be started: %s" % (self._thread.name, failure))

    def _ended(self) -> None:
        with ThreadStarter._open_lock:
            ThreadStarter._open.discard(self)

    def start(self, **thread_arguments: Any) -> None:
        """Called from any thread, the reader's and the loop's included: builds a daemon thread and queues it. Never waits."""
        if self._stopped:
            return
        self._queue.put((threading.Thread(daemon=True, **thread_arguments), None))

    def start_built(self, thread: threading.Thread, on_failure: Optional[Callable[[str], None]] = None) -> None:
        """Queue a thread the caller built (its name, its daemon flag, its target). Never waits.

        `on_failure(why)` runs on the starter if this thread cannot be started, besides the starter's own report."""
        if self._stopped:
            return
        self._queue.put((thread, on_failure))

    def stop(self) -> None:
        self._stopped = True
        self._queue.put(None)

    def _report(self, why: str) -> None:
        try:
            self._on_error(why)
        except Exception:
            pass

    def _run(self) -> None:
        try:
            self._loop()
        finally:
            self._ended()

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            thread, on_failure = item
            try:
                thread.start()
            except Exception as failure:
                why = "%s could not be started: %s" % (thread.name, failure)
                self._report(why)
                if on_failure is not None:
                    try:
                        on_failure(why)
                    except Exception:
                        pass


class ThreadStarterLease:
    """One browser's use of the connection's starter: `start` while the browser's connection lives, nothing after it.

    The starter is the SERVER's (one thread per connection, however many browsers it launches, `Server.thread_starter`) and ends with
    the server. A browser's connection ends earlier than that: from then on a thread its events ask for must not start, which is
    what `stop` on the browser's own starter used to give. The starter is asked for on the first `start`, so a browser that never
    needs a thread makes none."""

    def __init__(self, get_starter: Callable[[], ThreadStarter]) -> None:
        self._get_starter = get_starter
        self._stopped = False

    def start(self, **thread_arguments: Any) -> None:
        if not self._stopped:
            self._get_starter().start(**thread_arguments)

    def stop(self) -> None:
        self._stopped = True
