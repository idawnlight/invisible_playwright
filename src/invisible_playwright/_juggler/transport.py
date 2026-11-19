"""The seam where Node stops being necessary.

⛔ THIS IS THE KEYSTONE OF THE WHOLE DETACHMENT, and its shape is the one
decision that makes the rest small. `_pw/_impl` is not replaced - 799 of its
methods never touch the channel at all, and throwing them away would mean
rewriting working logic for nothing. What is replaced is the ONE thing under
them: `PipeTransport`, which spawns `node cli.js run-driver` and speaks the
Playwright protocol over a pipe.

The seam is `Transport`, an abstract class with five methods. Everything above
it - channels, guids, `ChannelOwner`, the sync and async facades, the 823
generated API methods - keeps working unchanged, because from up there nothing
can tell whether the answer crossed a pipe or came from the next object in the
same process.

**THE PROTOCOL, MEASURED AND NOT DEDUCED.** What crosses this seam is recorded
by `scripts/capture_protocol.py` against the real Node driver, and the server is
written against that recording. Reading `_impl` tells you which initializer
fields are CONSUMED; it cannot tell you which are SENT, and the gap between
those two sets is exactly where a future code path breaks.

Four message shapes, in both directions:

    ->  {"id": N, "guid": G, "method": M, "params": P, "metadata": {...}}
    <-  {"id": N, "result": R}                     an answer
    <-  {"id": N, "error": {"error": {...}}}       a failure
    <-  {"guid": G, "method": "__create__",
         "params": {"type": T, "guid": G2, "initializer": I}}
    <-  {"guid": G, "method": M, "params": P}      an event

⛔ **THREADS, AND WHY THIS IS NOT OPTIONAL.** `send()` is called ON THE ASYNCIO
LOOP. Our Juggler operations block: `goto` waits for a load state, `click` waits
for actionability. Running them inline would block the loop, and under the sync
facade - which drives the loop from a greenlet - that is a deadlock, not a
slowdown. So `send()` only enqueues; a small pool of worker threads runs the
operations and posts every reply back with `loop.call_soon_threadsafe`, which is
the only way to touch the loop from another thread.

⛔ **AND ORDERING IS NOT GUARANTEED, WHICH IS CORRECT.** Replies are matched by
`id`, never by arrival order, so a slow `goto` cannot hold up a `title()` on
another page. What must stay ordered is `__create__` before any message that
names the guid it creates, and that is the dispatcher's job, not the pool's.
"""
from __future__ import annotations

import _thread as _raw_thread
import asyncio
import os
import queue
import threading
import traceback
from typing import Any, Dict, Optional

from invisible_core._offloop import off_the_loop
from invisible_playwright._pw._impl._helper import parse_error
from invisible_playwright._pw._impl._transport import Transport

from . import wire


def _error_payload(failure: BaseException) -> Dict[str, str]:
    """The `{name, message, stack}` shape both an error REPLY and a
    directly-translated exception are built from - one function, so the two
    do not drift the way rule 16 forbids.

    ⛔ EVERY TimeoutError IS NAMED "TimeoutError", whatever its subclass. The
    client picks its class by that exact name (`parse_error`), and the name
    used to be the subclass's own: a click whose element never became
    actionable raised the core's `ElementNotActionable`, and the caller got a
    plain Error where Playwright raises TimeoutError, so `except TimeoutError`
    around a click caught nothing (found 2026-10-11 with the launch timeout,
    which is the core's `ReplyTimeout`). The subclass is still in the stack."""
    name = type(failure).__name__
    if isinstance(failure, TimeoutError):
        name = "TimeoutError"
    return {
        "name": name,
        "message": str(failure) or type(failure).__name__,
        "stack": "".join(traceback.format_exception(
            type(failure), failure, failure.__traceback__))[-4000:],
    }


def _translate_exception(failure: BaseException) -> BaseException:
    """A `_juggler`-side exception, reshaped into the fork's own class - the
    same translation `reply_error` puts on the wire, taken directly instead of
    round-tripping through a message."""
    return parse_error(_error_payload(failure))


class InProcessTransport(Transport):
    """A `Transport` whose other end is a Python object, not a subprocess."""

    #: ⛔ More than one, because a blocking operation must not hold up the
    #: others, and exactly this few because our Juggler connection serialises
    #: on its own lock anyway: a bigger pool would only add contention.
    WORKERS = 4

    def __init__(self, loop: asyncio.AbstractEventLoop, server: Any) -> None:
        super().__init__(loop)
        self._server = server
        self._queue: queue.Queue = queue.Queue()
        self._stopped = threading.Event()
        self._stopped_future: asyncio.Future = loop.create_future()
        self._threads: list = []
        #: The thread that runs the server's shutdown (see `request_stop`); None until the stop is requested.
        self._shutdown_thread: Optional[threading.Thread] = None
        #: Set once the raw thread has begun (or failed to begin) the shutdown thread, so the join below knows whether to wait.
        self._shutdown_begun = threading.Event()
        server.attach(self)

    # ── the five methods the seam declares ──────────────────────────────────
    async def connect(self) -> None:
        """⛔ NO `Thread.start` ON THE LOOP, NOT EVEN ONE. `connect` is a coroutine of the caller's event loop, and
        `Thread.start` does not return until the new thread is running (`self._started.wait()`): measured with four
        browsers in one process under CPU load on Windows, the four starts held the loop ~200 ms per launch (4.4% of the
        main thread's wall time), the same wait the engine's reader is spared by the same starter.

        The four `Thread` objects are only BUILT here (building waits for nothing), so `request_stop` still knows how many
        sentinels the pool needs, and the server's one starter (`Server.thread_starter`, shared with every browser's reader) starts them. Messages sent before a worker runs wait in the queue and are
        answered when it does, which is what `send` always relied on."""
        self._threads = [
            threading.Thread(target=self._work, name="juggler-%d" % i, daemon=True)
            for i in range(self.WORKERS)]
        server_starter = self._server.thread_starter
        for t in self._threads:
            server_starter.start_built(t, self._workers_could_not_start)

    def _workers_could_not_start(self, why: str) -> None:
        """No worker may ever answer a queued message, so the session cannot go on: end it, the way a transport that lost
        its other end ends, instead of leaving every call to wait for a reply that cannot come."""
        self.request_stop()

    async def run(self) -> None:
        """⛔ Must not return until the transport is stopped: `Connection.run`
        awaits this and treats its return as the connection ending."""
        await self._stopped_future

    def request_stop(self) -> None:
        """⛔ THE SERVER'S SHUTDOWN IS NOT RUN BY THE CALLER, and the caller is usually the user's event loop. `server.shutdown()` runs the
        hooks of the session (the engine's `conn.close`, which waits for its processes to exit) and then the removal of its directories:
        seconds of waiting, and on the loop they stopped every other browser of the process for as long as they took (measured with
        four browsers in one process). It runs on a thread of its own, NOT a daemon: a close that the interpreter's exit could cut short
        is a browser left half closed, a leak. `wait_until_stopped` is where it is waited for, and off the loop too.

        The thread is begun by a raw thread, as the server's starter is: `Thread.start` does not return until the new thread runs, and
        that wait on a loaded machine is the loop's own (see `connect`)."""
        if self._stopped.is_set():
            return
        self._stopped.set()
        # One sentinel per worker: a thread blocked on `get()` does not notice
        # a flag.
        for _ in self._threads:
            self._queue.put(None)

        def shutdown() -> None:
            try:
                self._server.shutdown()
            except Exception:
                pass
            finally:
                # The stop is settled when the shutdown has ended, as it always was: `run` returns after the hooks.
                try:
                    self._loop.call_soon_threadsafe(self._settle)
                except RuntimeError:
                    pass              # the loop is closed: nobody waits for the stop any more

        thread = threading.Thread(target=shutdown, name="juggler-shutdown")
        self._shutdown_thread = thread
        # Not through the server's `ThreadStarter`: `Server.shutdown` stops it, and a starter that has stopped drops what it is handed,
        # which here would be the shutdown itself. A raw thread begins this one (the caller returns at once).
        try:
            _raw_thread.start_new_thread(self._begin, (thread, shutdown))
        except RuntimeError:
            self._shutdown_begun.set()
            shutdown()                # no thread can be made: the shutdown still has to happen, as it did before

    def _begin(self, thread: threading.Thread, shutdown: Any) -> None:
        try:
            thread.start()
        except BaseException:
            self._shutdown_begun.set()
            shutdown()                # on this raw thread, which is not the loop's
        else:
            self._shutdown_begun.set()

    def _settle(self) -> None:
        if not self._stopped_future.done():
            self._stopped_future.set_result(None)

    def _join_workers(self) -> None:
        """What `wait_until_stopped` waits for besides the stop itself: the workers (and the shutdown thread, which has settled the stop
        and is ending). Blocking, so never on the loop."""
        for t in self._threads:
            # A worker the starter never started (the transport stopped first) has nothing to join.
            if t.is_alive():
                t.join(timeout=5)
        shutdown = self._shutdown_thread
        if shutdown is not None:
            self._shutdown_begun.wait(5)  # the raw thread that begins it may be a moment late
            if shutdown.is_alive():
                shutdown.join(timeout=5)

    async def wait_until_stopped(self) -> None:
        await self._stopped_future
        await off_the_loop(self._join_workers)

    def send(self, message: Dict) -> None:
        """⛔ ENQUEUE ONLY. Running the operation here would run it on the
        event loop, and every Juggler operation blocks."""
        self._queue.put(message)

    # ── the worker side ─────────────────────────────────────────────────────
    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            message = item
            try:
                result = self._server.handle(message)
            except Exception as failure:
                self.reply_error(message.get("id"), failure)
            else:
                self.reply(message.get("id"), result)

    # ── what the server calls back ──────────────────────────────────────────
    def emit_message(self, message: Dict) -> None:
        """Hand one protocol message up to `Connection.dispatch`.

        ⛔ `call_soon_threadsafe` and never a direct call: `on_message` walks
        into asyncio futures, and touching those from a worker thread corrupts
        the loop in ways that surface much later and somewhere else.
        """
        if self._stopped.is_set():
            return
        try:
            self._loop.call_soon_threadsafe(self.on_message, message)
        except RuntimeError:
            # The loop is already closed: the session is going away, and a
            # message arriving now has nobody left to read it.
            pass

    def reply(self, msg_id: Optional[int], result: Any) -> None:
        if msg_id is None:
            return
        out: Dict[str, Any] = {"id": msg_id}
        if result is not None:
            out["result"] = result
        self.emit_message(out)

    def reply_error(self, msg_id: Optional[int], failure: BaseException) -> None:
        """⛔ The shape matters: `Connection.dispatch` reads
        `msg["error"]["error"]["message"]`, and a flatter error is silently
        turned into a generic one, which is how a precise server-side reason
        becomes "Protocol error" by the time a user sees it."""
        if msg_id is None:
            return
        out: Dict[str, Any] = {"id": msg_id,
                               "error": {"error": _error_payload(failure)}}
        # An error that carries details for the client (`ExpectNotMet`) sends
        # them where `Connection.dispatch` reads them.
        details = getattr(failure, "details", None)
        if details is not None:
            out["errorDetails"] = details
        # And its call log, where `Connection.dispatch` reads it: the client
        # prints it under "Call log:" ([B341]).
        log = getattr(failure, "log", None)
        if log:
            out["log"] = list(log)
        self.emit_message(out)

class JsonWireTransport(InProcessTransport):
    """`InProcessTransport` with the wire between the two ends: every message crosses as the
    JSON text a socket would carry, in both directions.

    ⛔ THIS IS THE CHECK, NOT THE SOCKET. The remote transport (`remote.py`) sends the same
    text over a WebSocket; this class sends it between two Python objects, so the whole
    existing suite can run on it with no network and no second process, and any value that
    JSON would change or lose (`wire.py`) fails here, at the line that built it, naming the
    path inside the message.

    A client message that is not JSON raises from `send` to its caller. A server REPLY that
    is not JSON becomes an error reply for the same id (so the call that caused it fails
    with the path in its message); a server EVENT that is not JSON cannot be answered, so it
    is dropped and recorded in `wire_errors`, which a test asserts empty.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, server: Any) -> None:
        super().__init__(loop, server)
        self.wire_errors: list = []

    def send(self, message: Dict) -> None:
        super().send(wire.round_trip(message))

    def emit_message(self, message: Dict) -> None:
        try:
            carried = wire.round_trip(message)
        except wire.WireError as problem:
            self.wire_errors.append(problem)
            msg_id = message.get("id") if isinstance(message, dict) else None
            if msg_id is not None and "error" not in message:
                super().emit_message({"id": msg_id, "error": {"error": {
                    "name": "Error", "message": "the reply is not JSON: %s" % problem,
                    "stack": ""}}})
            return
        super().emit_message(carried)


# ── making one ──────────────────────────────────────────────────────────────

#: `json` puts the wire between the client and the server (`JsonWireTransport`), as it is
#: for a remote client. Off by default: local use passes the dicts as it always did.
WIRE_ENV = "INVPW_WIRE"


def make_transport(loop: asyncio.AbstractEventLoop) -> "InProcessTransport":
    """The transport a `Connection` should speak through."""
    from .server import JugglerServer
    if (os.environ.get(WIRE_ENV) or "").strip().lower() == "json":
        return JsonWireTransport(loop, JugglerServer())
    return InProcessTransport(loop, JugglerServer())
