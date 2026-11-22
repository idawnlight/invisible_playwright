"""A read whose probe ran out of time ran out of the CALLER's time.

`page.inner_text("#never", timeout=300)` must fail with Playwright's
"Timeout 300ms exceeded ...". The read gives its probe exactly what is left of
the caller's deadline, so the probe's `ReplyTimeout` is the deadline's expiry.
It used to be turned into the read's timeout only when a SECOND reading of the
clock said the deadline had passed; on Windows a threading wait can return
before the monotonic clock (steps of about 15 ms) says the budget is spent, and
the core's "Runtime.callFunction: no response in 0s ..." reached the caller
(whole-e2e runs 44419689591 and 44184494839).
"""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from invisible_core.juggler import connection as juggler
from invisible_core.juggler import deadline as deadline_module
from invisible_core.juggler.connection import EventListeners
from invisible_core.juggler.lifecycle import Lifecycle
from invisible_playwright._juggler.dispatcher import Dispatcher
from invisible_playwright._juggler.server import (
    FrameDispatcher,
    JugglerServer,
    PageDispatcher,
)

pytestmark = pytest.mark.unit

CORE_MESSAGE = ("Runtime.callFunction: no response in 0s. If this is the "
                "FIRST command, look at the readiness signal before the pipe.")


@pytest.fixture
def frame():
    server = JugglerServer()
    server.attach(SimpleNamespace(emit_message=lambda message: None))
    context = Dispatcher(server, None)
    context.options = {}
    page = object.__new__(PageDispatcher)
    page.context = context
    page.lifecycle = Lifecycle(EventListeners(), "session")
    page.injected = Mock()
    page.actions = Mock()
    page._frames = {}
    page._told_attached = set()
    page._frames_lock = threading.RLock()
    page._announce_lock = threading.RLock()
    page._announced = True
    page._said_before_announcement = []
    page._navigation_requests = {}
    page._announced_closed = False
    Dispatcher.__init__(page, server, context)
    page.frame = FrameDispatcher(server, page, "main")
    page.lifecycle._on_event("Page.frameAttached",
                             {"frameId": "main", "parentFrameId": None})
    page.frame.enter_frames = Mock(return_value=("main", "#never"))
    return page.frame


def test_a_probe_that_ran_out_is_the_read_timing_out_whatever_the_clock_says(
        frame, monkeypatch):
    """The losing interleaving, on purpose: the probe's wait ended, and the
    clock the deadline reads has not moved at all, so `deadline.passed()` is
    False."""
    monkeypatch.setattr(deadline_module, "time",
                        SimpleNamespace(monotonic=lambda: 100.0))
    frame.injected.read_match.side_effect = juggler.ReplyTimeout(CORE_MESSAGE)

    with pytest.raises(TimeoutError) as caught:
        frame.op_inner_text({"selector": "#never", "timeout": 300})

    assert not isinstance(caught.value, juggler.ReplyTimeout)
    assert str(caught.value) == (
        "Timeout 300ms exceeded waiting for '#never' to match an element")
    assert frame.injected.read_match.call_count == 1


def test_a_read_with_no_limit_gave_its_probe_no_bound_and_keeps_what_it_gets(
        frame):
    """`timeout=0` is no limit: the probe was given no bound, so a
    `ReplyTimeout` is not the caller's deadline and travels as it is."""
    frame.injected.read_match.side_effect = juggler.ReplyTimeout(CORE_MESSAGE)

    with pytest.raises(juggler.ReplyTimeout, match="no response in 0s"):
        frame.op_inner_text({"selector": "#never", "timeout": 0})
    assert frame.injected.read_match.call_args.kwargs["timeout"] is None
