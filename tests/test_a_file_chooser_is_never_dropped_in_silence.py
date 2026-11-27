"""A file chooser the server cannot announce leaves a trace: it is never dropped in silence.

`PageDispatcher._announce_file_chooser` turns the engine's `Page.fileChooserOpened` into the client's `fileChooser`. Four of its exits are a bare
`return`: no objectId in the element, no executionContextId, a context that belongs to no known frame, a node the engine could not adopt. The
client then waits for an event nobody will send and dies on its own timeout, and nothing says why. Every other failure of the same function is
written to `conn.handler_errors` (the place the repository keeps a dropped event: the adoption of a target does the same), and so must these.

No browser: the method is run on a stand-in of the page with a stand-in of the injected script, and what a caller can observe is read:
`conn.handler_errors` (the trace) and the `fileChooser` events emitted (none, in every case here).

Known-bad input: the four bare `return`s -> `handler_errors` stays empty. The control (the adoption RAISING) is the one exit that already
leaves a trace, and proves that the observation reads the right place.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from invisible_playwright._juggler.server import PageDispatcher

pytestmark = pytest.mark.unit

CONTEXT = "ctx-1"
FRAME = "frame-1"
ELEMENT = {"objectId": "input-1"}


def _page(frame_of_context=FRAME, adopt=None, adopt_raises=None):
    emitted = []

    def adopt_in(frame_id, object_id):
        if adopt_raises is not None:
            raise adopt_raises
        return adopt

    injected = SimpleNamespace(frame_of_context=lambda context_id: frame_of_context, adopt=adopt_in)
    page = SimpleNamespace(conn=SimpleNamespace(handler_errors=[]), injected=injected,
                           emit=lambda method, params=None: emitted.append((method, params)))
    return page, emitted


def _announce(page, params):
    PageDispatcher._announce_file_chooser(page, params)


def _traced(page):
    return [e for e in page.conn.handler_errors if "fileChooser" in str(e)]


def test_a_chooser_without_an_object_id_leaves_a_trace():
    page, emitted = _page()
    _announce(page, {"element": {}, "executionContextId": CONTEXT})
    assert emitted == []
    assert _traced(page), "the chooser was dropped in silence: no objectId, and handler_errors is %r" % (page.conn.handler_errors,)


def test_a_chooser_without_an_execution_context_leaves_a_trace():
    page, emitted = _page()
    _announce(page, {"element": ELEMENT})
    assert emitted == []
    assert _traced(page), "the chooser was dropped in silence: no executionContextId, and handler_errors is %r" % (page.conn.handler_errors,)


def test_a_chooser_in_a_context_of_no_known_frame_leaves_a_trace():
    page, emitted = _page(frame_of_context=None)
    _announce(page, {"element": ELEMENT, "executionContextId": CONTEXT})
    assert emitted == []
    assert _traced(page), "the chooser was dropped in silence: context %s belongs to no frame, and handler_errors is %r" % (CONTEXT, page.conn.handler_errors)


def test_a_chooser_whose_node_cannot_be_adopted_leaves_a_trace():
    page, emitted = _page(adopt=None)
    _announce(page, {"element": ELEMENT, "executionContextId": CONTEXT})
    assert emitted == []
    assert _traced(page), "the chooser was dropped in silence: the adoption answered nothing, and handler_errors is %r" % (page.conn.handler_errors,)


def test_control_a_chooser_whose_adoption_raises_already_leaves_a_trace():
    page, emitted = _page(adopt_raises=RuntimeError("no such context"))
    _announce(page, {"element": ELEMENT, "executionContextId": CONTEXT})
    assert emitted == []
    assert _traced(page) == ["fileChooser: no such context"]
