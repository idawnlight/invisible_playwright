"""`Frame.tap` of a context without touch is refused with stock's sentence, before anything is looked up or sent.

Stock (driver bundle `coreBundle.js`, `Frame.tap`): `if (!this._page.browserContext._options.hasTouch) throw new Error("The page does not support tap. Use hasTouch context option
to enable touch support.")`: the first line of the method, before the selector is resolved. Until now the server went on to look the element up and send the tap, which the
page cannot hear without touch: it answered success and did nothing, the one outcome stock never gives. `ElementHandle.tap` has no such check in stock (its `_tap` goes straight
to the touchscreen) and is not changed.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from invisible_core.juggler.connection import EventListeners
from invisible_core.juggler.lifecycle import Lifecycle
from invisible_playwright._juggler.dispatcher import Dispatcher, ProtocolException
from invisible_playwright._juggler.server import FrameDispatcher, JugglerServer, PageDispatcher

pytestmark = pytest.mark.unit

STOCK = "The page does not support tap. Use hasTouch context option to enable touch support."


def _page(options):
    server = JugglerServer()
    server.attach(SimpleNamespace(emit_message=lambda message: None))
    context = Dispatcher(server, None)
    context.options = options
    page = object.__new__(PageDispatcher)
    page.context = context
    page.lifecycle = Lifecycle(EventListeners(), "session")
    page.injected = Mock()
    page.actions = Mock()
    page.send = Mock(return_value={})
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
    page.lifecycle._on_event("Page.frameAttached", {"frameId": "main", "parentFrameId": None})
    return page


@pytest.mark.parametrize("options", [{}, {"hasTouch": False}])
def test_a_tap_of_a_context_without_touch_is_stocks_error(options):
    """Known-bad: the tap goes on to the actions and answers success."""
    page = _page(options)
    with pytest.raises(ProtocolException) as failure:
        page.frame.call("tap", {"selector": "#b"})
    assert str(failure.value) == STOCK
    page.actions.tap.assert_not_called()
    page.injected.assert_not_called()


def test_a_tap_of_a_context_with_touch_goes_on_to_the_actions():
    page = _page({"hasTouch": True})
    page.frame.call("tap", {"selector": "#b", "noWaitAfter": True})
    page.actions.tap.assert_called_once()
