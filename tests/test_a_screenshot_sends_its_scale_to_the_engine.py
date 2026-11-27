"""`screenshot(scale="css")` asks the engine for CSS pixels; `"device"` and no `scale` ask for device pixels.

The wire parameter is `scale`, and stock Playwright's Firefox page sends `omitDeviceScaleFactor: scale === "css"`
(`ffPage.takeScreenshot` in the driver bundle). `PageDispatcher.op_screenshot` sent `omitDeviceScaleFactor: False`
whatever the caller asked for, and never read `scale`: at a viewport of 1280x720 and a device scale factor of 1.25
the picture was 1600x900 where stock gives 1280x720. The page, the full page, a clip and an element all end in that one
call (the element's rectangle is handed to the page's screenshot), so they are checked here at the wire.

No browser: the dispatchers are built the way tests/test_request_gc_uses_the_page_session.py builds one, with the
sender replaced by a mock that answers like the engine.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from invisible_core.juggler.connection import EventListeners
from invisible_core.juggler.lifecycle import Lifecycle
from invisible_playwright._juggler.dispatcher import Dispatcher
from invisible_playwright._juggler.server import (
    ElementHandleDispatcher,
    FrameDispatcher,
    JugglerServer,
    PageDispatcher,
)

pytestmark = pytest.mark.unit

BOX = {"x": 0, "y": 0, "width": 1280, "height": 720, "fullWidth": 1280, "fullHeight": 3000}


def make_page():
    server = JugglerServer()
    server.attach(SimpleNamespace(emit_message=lambda m: None))
    context = Dispatcher(server, None)
    context.options = {}
    page = object.__new__(PageDispatcher)
    page.context = context
    page.lifecycle = Lifecycle(EventListeners(), "session")
    page.injected = Mock()
    page.injected.evaluate.return_value = dict(BOX)
    page.actions = Mock()
    page._frames = {}
    page._navigation_requests = {}
    page._announced_closed = False
    Dispatcher.__init__(page, server, context)
    page.frame = FrameDispatcher(server, page, "main")
    page.lifecycle._on_event("Page.frameAttached", {"frameId": "main", "parentFrameId": None})
    page.send = Mock(return_value={"data": "AAAA"})
    return page


def make_element(page):
    handle = ElementHandleDispatcher(page.server, page.frame, "object-1")
    page.injected.bounding_box.return_value = {"x": 10, "y": 20, "width": 30, "height": 40}
    return handle


def _sent(page):
    (method, params), _kw = page.send.call_args
    assert method == "Page.screenshot"
    return params


def _page_shot(page, params):
    return page.op_screenshot(params)


def _full_page_shot(page, params):
    return page.op_screenshot(dict(params, fullPage=True))


def _clip_shot(page, params):
    return page.op_screenshot(dict(params, clip={"x": 5, "y": 6, "width": 100, "height": 50}))


def _element_shot(page, params):
    return make_element(page).op_screenshot(params)


KINDS = [_page_shot, _full_page_shot, _clip_shot, _element_shot]
KIND_IDS = ["page", "full_page", "clip", "element"]


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_scale_css_omits_the_device_scale_factor(kind):
    page = make_page()
    assert kind(page, {"scale": "css"}) == {"binary": "AAAA"}
    assert _sent(page)["omitDeviceScaleFactor"] is True


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_scale_device_keeps_the_device_scale_factor(kind):
    page = make_page()
    kind(page, {"scale": "device"})
    assert _sent(page)["omitDeviceScaleFactor"] is False


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_no_scale_is_device(kind):
    page = make_page()
    kind(page, {})
    assert _sent(page)["omitDeviceScaleFactor"] is False


def test_the_element_hands_its_rectangle_and_its_scale_to_the_same_call():
    page = make_page()
    make_element(page).op_screenshot({"scale": "css", "type": "jpeg", "quality": 40})
    params = _sent(page)
    assert params["omitDeviceScaleFactor"] is True
    assert params["clip"] == {"x": 10, "y": 20, "width": 30, "height": 40}
    assert (params["mimeType"], params["quality"]) == ("image/jpeg", 40)
