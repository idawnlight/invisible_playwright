"""`InProcessTransport.connect` returns without waiting for any thread to start.

`connect` is a coroutine of the user's event loop, and `Thread.start` does not return until the new thread is running. Measured with
four browsers in one process under CPU load on Windows, the four worker starts held the loop ~200 ms per launch (4.4% of the main
thread's wall time). The workers are now started by a bootstrapper that is not the loop (and is itself begun without a
`Thread.start`), and a message sent right after `connect` waits in the queue until a worker runs.

NO BROWSER HERE: a scripted server behind the real transport, with `Thread.start` slowed to 300 ms, the way a loaded machine slows it.

Known-bad input: workers started inside `connect` -> `connect` takes 4 x 300 ms and fails the 50 ms bound.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from invisible_playwright._juggler.dispatcher import Server
from invisible_playwright._juggler.transport import InProcessTransport

pytestmark = pytest.mark.unit

START_DELAY_S = 0.3


class _Server(Server):
    """The registry's base class, whose `thread_starter` the transport starts its workers through, answering every call with its id."""

    def handle(self, message):
        return {"echo": message["id"]}


def test_connect_returns_before_any_worker_has_started_and_a_message_is_still_answered(monkeypatch):
    original = threading.Thread.start

    def slow_start(self):
        time.sleep(START_DELAY_S)
        return original(self)
    monkeypatch.setattr(threading.Thread, "start", slow_start)

    async def scenario():
        loop = asyncio.get_running_loop()
        transport = InProcessTransport(loop, _Server())
        replies: "asyncio.Queue" = asyncio.Queue()
        transport.on_message = replies.put_nowait

        began = time.perf_counter()
        await transport.connect()
        took = time.perf_counter() - began

        transport.send({"id": 7, "guid": "g", "method": "m", "params": {}})
        reply = await asyncio.wait_for(replies.get(), 10)
        transport.request_stop()
        await asyncio.wait_for(transport.wait_until_stopped(), 10)
        return took, reply

    took, reply = asyncio.run(scenario())
    assert took < 0.05, "connect() waited %.0f ms: a thread was started on the loop" % (took * 1000)
    assert reply == {"id": 7, "result": {"echo": 7}}


def test_a_transport_stopped_before_its_workers_started_ends_without_error(monkeypatch):
    original = threading.Thread.start

    def slow_start(self):
        time.sleep(START_DELAY_S)
        return original(self)
    monkeypatch.setattr(threading.Thread, "start", slow_start)

    async def scenario():
        transport = InProcessTransport(asyncio.get_running_loop(), _Server())
        await transport.connect()
        transport.request_stop()
        await asyncio.wait_for(transport.wait_until_stopped(), 10)

    asyncio.run(scenario())


def test_building_the_shared_starter_waits_for_no_thread_start(monkeypatch):
    """The starter every caller of `ThreadStarter` builds on its own thread must not itself cost a `Thread.start` there."""
    from invisible_playwright._juggler._threads import ThreadStarter

    original = threading.Thread.start

    def slow_start(self):
        time.sleep(START_DELAY_S)
        return original(self)
    monkeypatch.setattr(threading.Thread, "start", slow_start)

    began = time.perf_counter()
    starter = ThreadStarter(on_error=lambda why: None)
    ran = threading.Event()
    starter.start(target=ran.set, name="invpw-test-started")
    took = time.perf_counter() - began
    try:
        assert took < 0.05, "building the starter and queueing a thread waited %.0f ms" % (took * 1000)
        assert ran.wait(10), "the queued thread never ran"
    finally:
        starter.stop()


def test_a_browsers_reader_and_the_transports_workers_share_ONE_starter():
    """Each browser costs one starter thread, not two: the transport's workers and the browser's lease both go through the server's."""
    from invisible_playwright._juggler._threads import ThreadStarter, ThreadStarterLease

    async def scenario():
        server = _Server()
        transport = InProcessTransport(asyncio.get_running_loop(), server)
        # The starters of THIS scenario, by identity: the registry is the process's, and a starter an earlier test of the
        # same worker is still closing (or one it left) moved a count taken before and after (red in a pre-push of 15/11).
        before = set(ThreadStarter.open_starters())
        await transport.connect()
        lease = ThreadStarterLease(lambda: server.thread_starter)
        ran = threading.Event()
        lease.start(target=ran.set, name="invpw-test-started")
        assert ran.wait(30)
        opened, mine = [s for s in ThreadStarter.open_starters() if s not in before], server.thread_starter
        transport.request_stop()
        await asyncio.wait_for(transport.wait_until_stopped(), 30)
        return opened, mine

    opened, the_servers = asyncio.run(scenario())
    assert opened == [the_servers], opened
