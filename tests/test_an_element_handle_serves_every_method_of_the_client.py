"""An element handle answers every method the client sends it, with what Playwright's own server answers, through the paths the rest of the server already has.

THE CLIENT'S `ElementHandle` (`_pw/_impl/_element_handle.py`) SENDS 55 WIRE METHODS AND `ElementHandleDispatcher` HAD A ROW FOR 38. The other 17 were answered
"ElementHandle has no method 'x', and 'x' is INSIDE the perimeter this package covers - so this is a gap" (`Dispatcher.call`): `handle.query_selector`, the three
`is_*` of the state, `handle.dblclick()`, `handle.wait_for_element_state()`, `handle.evaluate_handle()`... all of them ordinary Playwright, and the first person to learn
it was a user whose script died. `test_every_wire_method_is_served_or_refused.py` found them and put them in a budget; this file is what takes them out.

What stock Playwright 1.61 does, read from the driver bundle (`coreBundle.js`, cited by function name):

* `ElementHandleDispatcher.querySelector` / `querySelectorAll` / `evalOnSelector` / `evalOnSelectorAll` run the SELECTOR with the element as ROOT
  (`FrameSelectors.query` / `queryAll`: `injected.querySelector(info.parsed, scope || document, info.strict)`); the root applies to the first frame hop only
  (`resolveFrameForSelector`: `scope: i === 0 ? scope : undefined`, and `if (frame !== this.frame) scope = undefined`). `evalOnSelector` that finds nothing is
  `Failed to find element matching selector "<selector>"`.
* `isVisible` / `isHidden` / `isEnabled` / `isDisabled` / `isEditable` / `isChecked` are the Frame's, with the selector `:scope` and the handle as the root
  (`Frame.isVisibleInternal`, `_elementState`): one read, no wait. A node that is not attached is `false` for visible (`isHidden` is its negation) and for the
  four others `Element is not attached to the DOM` (`throwElementIsNotAttached`).
* `waitForElementState` is `ElementHandle.waitForElementState`: `checkElementStates(node, [state])` until it is met, retried after 0, 20, 100, 100, 500 ms
  (`_retryAction`), `Element is not attached to the DOM` at once when the node is gone (`throwRetargetableDOMError`).
* `dblclick`, `tap`, `selectText` and `dispatchEvent` are `ElementHandle._dblclick` / `_tap` (`_retryPointerAction`), `selectText` (`_retryAction`) and
  `Frame.dispatchEvent` with `:scope`: the action on THAT node, the same checks as the selector's.
* `evaluateExpressionHandle` is `JSHandleDispatcher.evaluateExpressionHandle`: `fn(element, arg)` in the page's world, and a HANDLE of what it returned
  (`fromJSOrElementHandle`).

Here the server answers each through the path its sibling already has: the element-scoped actions (`op_click`'s: `Actions` with `element_id`, never a selector looked
up again, never the caller's handle disposed), the Frame's reads and queries with the element as root, `_call_on_elements` and `_handle_from_caller` for the caller's
code and the handle it makes. A second path for any of them would be a second fingerprint.

No browser here: the dispatchers run against an injected script and actions that record what they are asked. The browser half is
`test_an_element_handle_serves_every_method_of_the_client_e2e.py`.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from invisible_core.juggler.connection import EventListeners
from invisible_core.juggler.injected import EvaluationError
from invisible_core.juggler.keyboard import MODIFIER_MASK
from invisible_core.juggler.lifecycle import Lifecycle
from invisible_playwright._juggler.dispatcher import Dispatcher, ProtocolException
from invisible_playwright._juggler.server import (
    ElementHandleDispatcher, FrameDispatcher, JugglerServer, PageDispatcher)

pytestmark = pytest.mark.unit

HANDLE = "<element handle>"
NOT_ATTACHED = "Element is not attached to the DOM"

#: The wire methods this file serves (the client's `ElementHandle` sends 17 that the table lacked; `waitForSelector` is the one still named in
#: `tests/gates/wire_method_debt.json`: the core's `Actions.wait_for_selector` has no root to scope it to).
SERVED = [
    "querySelector", "querySelectorAll", "evalOnSelector", "evalOnSelectorAll", "evaluateExpressionHandle",
    "isVisible", "isHidden", "isEnabled", "isDisabled", "isChecked", "isEditable", "waitForElementState",
    "dblclick", "tap", "selectText", "dispatchEvent",
]


@pytest.fixture
def page():
    server = JugglerServer()
    server.attach(SimpleNamespace(emit_message=lambda message: None))
    context = Dispatcher(server, None)
    context.options = {}
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
    for fid, parent in (("main", None), ("payment", "main")):
        page.lifecycle._on_event("Page.frameAttached", {"frameId": fid, "parentFrameId": parent})
    return page


@pytest.fixture
def element(page):
    """The handle the client holds: `el-1`, a node of the main frame's utility world."""
    return ElementHandleDispatcher(page.server, page.frame, "el-1")


def handle_of(page, answer):
    return page.server._objects[answer["guid"]]


def arg(value):
    return {"value": value, "handles": []}


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  The table
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("method", SERVED)
def test_the_client_s_method_is_on_the_table_and_not_a_gap(method):
    """Known-bad: the method is not in `METHODS`, and `Dispatcher.call` answers that it is a gap."""
    assert method in ElementHandleDispatcher.METHODS, "%s is sent by the client and the table has no row for it" % method


def test_a_call_of_the_client_is_not_answered_as_a_gap(element):
    """The sentence a user read: asked through `call()`, as the transport asks."""
    try:
        element.call("isVisible", {})
    except ProtocolException as failure:
        assert "gap" not in str(failure), str(failure)


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  Queries rooted at the element
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def test_query_selector_looks_inside_the_element_and_hands_back_a_handle_of_its_frame(page, element):
    page.injected.call.return_value = "child-1"
    answer = element.call("querySelector", {"selector": "span.x"})
    call = page.injected.call.call_args
    assert call.args[0] == "main" and call.args[2] == {"objectId": "el-1"} and call.args[3] == "span.x", call
    assert call.kwargs["by_value"] is False
    page.injected.query_selector.assert_not_called()      # the document-rooted query is the Frame's, and would find a sibling's match
    child = handle_of(page, answer["element"])
    assert child.object_id == "child-1" and child.frame is page.frame and child.parent is page.frame


def test_query_selector_forwards_strict(page, element):
    page.injected.call.return_value = "child-1"
    element.call("querySelector", {"selector": "span", "strict": True})
    assert page.injected.call.call_args.args[4] is True
    element.call("querySelector", {"selector": "span"})
    assert page.injected.call.call_args.args[4] is False


def test_query_selector_that_finds_nothing_answers_no_element(page, element):
    page.injected.call.return_value = None
    assert element.call("querySelector", {"selector": "span"}) == {"element": None}


def listed(*object_ids):
    """What `InjectedScript.properties` answers for an array of nodes: one property per place, the node as the RemoteObject the engine made of it."""
    return [{"name": str(i), "value": {"type": "object", "subtype": "node", "objectId": oid}} for i, oid in enumerate(object_ids)]


def test_query_selector_all_looks_inside_the_element_and_hands_back_every_match(page, element):
    page.injected.call.side_effect = ["list-1"]
    page.injected.context_id.return_value = "utility-1"
    page.injected.properties.return_value = listed("child-1", "child-2")
    answer = element.call("querySelectorAll", {"selector": "li"})
    first = page.injected.call.call_args_list[0]
    assert first.args[0] == "main" and first.args[2] == {"objectId": "el-1"} and first.args[3] == "li", first
    page.injected.query_selector_all.assert_not_called()
    page.injected.properties.assert_called_once_with("utility-1", "list-1")
    assert [handle_of(page, e).object_id for e in answer["elements"]] == ["child-1", "child-2"]
    assert all(handle_of(page, e).frame is page.frame for e in answer["elements"])
    page.injected.dispose.assert_called_once_with("main", "list-1")     # the list that held them is not left in the page


def test_query_selector_all_with_no_match_answers_an_empty_list(page, element):
    page.injected.call.side_effect = ["list-1"]
    page.injected.properties.return_value = []
    assert element.call("querySelectorAll", {"selector": "li"}) == {"elements": []}
    page.injected.dispose.assert_called_once_with("main", "list-1")


@pytest.mark.parametrize("matches", [0, 1, 2, 25])
def test_the_list_of_a_rooted_query_is_read_in_one_command_whatever_its_length(page, element, matches):
    """The core's own `query_selector_all` asks for the length and then for each place, N + 2 commands; the rooted path reads the array once
    (`Runtime.getObjectProperties`, which is the core's `properties`) after the query that made it: two commands, then the release of the list."""
    page.injected.call.side_effect = ["list-1"]
    page.injected.properties.return_value = listed(*["child-%d" % i for i in range(matches)])
    answer = element.call("querySelectorAll", {"selector": "li"})
    assert page.injected.call.call_count == 1, page.injected.call.call_args_list
    assert page.injected.properties.call_count == 1
    assert [handle_of(page, e).object_id for e in answer["elements"]] == ["child-%d" % i for i in range(matches)]


def test_the_matches_of_a_rooted_query_come_in_document_order_whatever_order_the_engine_lists_them(page, element):
    page.injected.call.side_effect = ["list-1"]
    shuffled = listed(*["child-%d" % i for i in range(12)])
    page.injected.properties.return_value = [shuffled[i] for i in (10, 2, 0, 11, 1, 9, 3, 8, 4, 7, 5, 6)]
    answer = element.call("querySelectorAll", {"selector": "li"})
    assert [handle_of(page, e).object_id for e in answer["elements"]] == ["child-%d" % i for i in range(12)]


def test_a_rooted_list_is_released_even_when_it_cannot_be_read(page, element):
    page.injected.call.side_effect = ["list-1"]
    page.injected.properties.side_effect = RuntimeError("the world went away")
    with pytest.raises(RuntimeError):
        element.call("querySelectorAll", {"selector": "li"})
    page.injected.dispose.assert_called_once_with("main", "list-1")


def test_the_element_is_the_root_of_the_first_frame_hop_only(page, element):
    """Stock: `scope: i === 0 ? scope : undefined`. The owner of the frame is looked for inside the element, and what is inside the frame is the frame's."""
    page.injected.call.return_value = "owner-1"
    page.send.return_value = {"contentFrameId": "payment"}
    page.injected.query_selector.return_value = "inner-1"
    answer = element.call("querySelector", {"selector": "iframe >> internal:control=enter-frame >> li"})
    owner_query = page.injected.call.call_args_list[0]
    assert owner_query.args[2] == {"objectId": "el-1"} and owner_query.args[3] == "iframe"
    page.injected.query_selector.assert_called_once_with("payment", "li")
    inner = handle_of(page, answer["element"])
    assert inner.object_id == "inner-1" and inner.frame is page.frame_for("payment")


def test_eval_on_selector_runs_the_callers_code_on_the_match_inside_the_element(page, element):
    page.injected.call.return_value = "child-1"
    page.frame._call_on_elements = Mock(return_value=42)
    params = {"selector": "span", "expression": "(e, n) => e.id + n", "isFunction": True, "arg": arg({"n": 1})}
    assert element.call("evalOnSelector", params) == {"value": {"n": 42}}
    assert page.injected.call.call_args.args[2] == {"objectId": "el-1"}
    page.frame._call_on_elements.assert_called_once_with("main", ["child-1"], params)
    page.injected.dispose.assert_called_once_with("main", "child-1")


def test_eval_on_selector_that_finds_nothing_says_what_stock_says(page, element):
    page.injected.call.return_value = None
    with pytest.raises(ProtocolException) as failure:
        element.call("evalOnSelector", {"selector": "span.none", "expression": "e => e"})
    assert str(failure.value) == 'Failed to find element matching selector "span.none"'


def test_eval_on_selector_all_runs_the_callers_code_on_the_matches_inside_the_element_as_one_array(page, element):
    page.injected.call.side_effect = ["list-1"]
    page.injected.properties.return_value = listed("child-1", "child-2")
    page.frame._call_on_elements = Mock(return_value=2)
    params = {"selector": "li", "expression": "els => els.length", "isFunction": True, "arg": arg({"v": "null"})}
    assert element.call("evalOnSelectorAll", params) == {"value": {"n": 2}}
    assert page.injected.call.call_args_list[0].args[2] == {"objectId": "el-1"}
    page.frame._call_on_elements.assert_called_once_with("main", ["child-1", "child-2"], params, as_array=True)
    disposed = [c.args for c in page.injected.dispose.call_args_list]
    assert ("main", "child-1") in disposed and ("main", "child-2") in disposed and ("main", "list-1") in disposed


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  The state of the element
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("method, state", [
    ("isVisible", "visible"), ("isEnabled", "enabled"), ("isDisabled", "disabled"),
    ("isEditable", "editable"), ("isChecked", "checked")])
@pytest.mark.parametrize("answer", [True, False])
def test_a_state_is_read_of_this_node_and_answered_as_it_is(page, element, method, state, answer):
    page.injected.element_state.return_value = answer
    assert element.call(method, {}) == {"value": answer}
    page.injected.element_state.assert_called_once_with("main", "el-1", state)
    page.injected.read_match.assert_not_called()          # the selector-rooted read is the Frame's: this is the handle's node


@pytest.mark.parametrize("visible", [True, False])
def test_is_hidden_is_the_negation_of_is_visible(page, element, visible):
    page.injected.element_state.return_value = visible
    assert element.call("isHidden", {}) == {"value": not visible}
    page.injected.element_state.assert_called_once_with("main", "el-1", "visible")


def test_a_node_that_is_not_attached_is_not_visible_and_is_hidden(page, element):
    page.injected.element_state.side_effect = EvaluationError("elementState(visible): error:notconnected")
    assert element.call("isVisible", {}) == {"value": False}
    assert element.call("isHidden", {}) == {"value": True}


@pytest.mark.parametrize("method", ["isEnabled", "isDisabled", "isEditable", "isChecked"])
def test_a_node_that_is_not_attached_has_no_such_state_and_the_error_is_stock_s(page, element, method):
    page.injected.element_state.side_effect = EvaluationError("elementState(x): error:notconnected")
    with pytest.raises(ProtocolException) as failure:
        element.call(method, {})
    assert str(failure.value) == NOT_ATTACHED


def test_a_failure_that_is_not_about_the_node_is_not_turned_into_not_attached(page, element):
    page.injected.element_state.side_effect = EvaluationError("Not a checkbox or radio button")
    with pytest.raises(EvaluationError, match="Not a checkbox"):
        element.call("isChecked", {})


@pytest.mark.parametrize("state", ["visible", "hidden", "stable", "enabled", "disabled", "editable"])
def test_wait_for_element_state_asks_the_actionability_check_of_this_node(page, element, state):
    page.injected.element_states.return_value = {"ok": True}
    assert element.call("waitForElementState", {"state": state}) is None
    page.injected.element_states.assert_called_once_with("main", "el-1", [state])


def test_wait_for_element_state_tries_again_until_the_state_is_met(page, element):
    page.injected.element_states.side_effect = [
        {"ok": False, "missing": "visible"}, {"ok": False, "missing": "visible"}, {"ok": True}]
    assert element.call("waitForElementState", {"state": "visible", "timeout": 5000}) is None
    assert page.injected.element_states.call_count == 3


def test_wait_for_element_state_on_a_node_that_left_the_document_fails_at_once_with_stock_s_error(page, element):
    """Stock: `throwRetargetableDOMError`. Known-bad: the node is retried until the timeout and the error is a timeout."""
    page.injected.element_states.return_value = {"ok": False, "missing": "error:notconnected"}
    with pytest.raises(ProtocolException) as failure:
        element.call("waitForElementState", {"state": "visible", "timeout": 5000})
    assert str(failure.value) == NOT_ATTACHED
    assert page.injected.element_states.call_count == 1


#: What the injected script answers `checkElementStates(node, [state])` for a node that is not in the document, per state. Read from stock's `InjectedScript.elementState`
#: (`if (!element || !element.isConnected) { if (state === "hidden") return { matches: true, ... }; return { matches: false, received: "error:notconnected" } }`) and
#: `checkElementStates` (no `missingState` for a match, "error:notconnected" for the rest), and MEASURED on the core's own `injected.js` run on a detached node:
#: visible, enabled, disabled and editable answer `"error:notconnected"`; hidden answers nothing (met); stable answers nothing either, because a detached element
#: has a box that does not move (`retarget` does not lose it: only a text node without a parent does). The core's `element_states` turns them into these verdicts.
GONE = {"error:notconnected": {"ok": False, "missing": "error:notconnected"}, "met": {"ok": True}}
STATE_OF_A_NODE_OUT_OF_THE_DOCUMENT = {
    "visible": "error:notconnected", "enabled": "error:notconnected", "disabled": "error:notconnected", "editable": "error:notconnected",
    "hidden": "met", "stable": "met"}


@pytest.mark.parametrize("state", sorted(STATE_OF_A_NODE_OUT_OF_THE_DOCUMENT))
def test_wait_for_element_state_of_a_node_that_left_the_document_answers_as_stock_does_for_each_state(page, element, state, monkeypatch):
    """`hidden` is met at once (the node is not shown), `stable` is met (its box does not move), the four others are `Element is not attached to the DOM` at once:
    one question asked, no pause between two, and the timeout is never the answer."""
    page.injected.element_states.return_value = GONE[STATE_OF_A_NODE_OUT_OF_THE_DOCUMENT[state]]
    paused = []
    monkeypatch.setattr("invisible_playwright._juggler.server.time.sleep", paused.append)
    params = {"state": state, "timeout": 5000}
    if STATE_OF_A_NODE_OUT_OF_THE_DOCUMENT[state] == "met":
        assert element.call("waitForElementState", params) is None
    else:
        with pytest.raises(ProtocolException) as failure:
            element.call("waitForElementState", params)
        assert str(failure.value) == NOT_ATTACHED
    assert page.injected.element_states.call_count == 1
    assert paused == []


def test_wait_for_element_state_that_is_never_met_ends_in_a_timeout(page, element):
    page.injected.element_states.return_value = {"ok": False, "missing": "enabled"}
    with pytest.raises(TimeoutError, match="Timeout 150ms exceeded"):
        element.call("waitForElementState", {"state": "enabled", "timeout": 150})


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  The gestures: the actions of the node, as `op_click`'s
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def the_call(mock):
    assert mock.call_count == 1, mock.call_args_list
    return mock.call_args


def test_dblclick_is_the_actions_dblclick_on_this_node(page, element):
    assert element.call("dblclick", {"position": {"x": 3, "y": 4}, "button": "right", "modifiers": ["Shift"],
                                     "delay": 40, "timeout": 5000}) is None
    call = the_call(page.actions.dblclick)
    assert call.args == (HANDLE,)
    assert call.kwargs == {"position": {"x": 3, "y": 4}, "button": 2, "modifiers": MODIFIER_MASK["Shift"], "delay_ms": 40,
                           "timeout": 5.0, "frame_id": "main", "element_id": "el-1",
                           "trial": False, "strict": False, "force": False}, call.kwargs
    page.injected.query_selector.assert_not_called()      # the node is not looked up again, and the caller's handle is not disposed
    page.injected.dispose.assert_not_called()


def test_dblclick_carries_trial_and_force(page, element):
    element.call("dblclick", {"trial": True, "force": True})
    kwargs = the_call(page.actions.dblclick).kwargs
    assert kwargs["trial"] is True and kwargs["force"] is True and kwargs["element_id"] == "el-1"
    assert kwargs["timeout"] == 30.0 and kwargs["position"] is None


def test_tap_is_the_actions_tap_on_this_node(page, element):
    assert element.call("tap", {"position": {"x": 1, "y": 2}, "timeout": 7000, "trial": True}) is None
    call = the_call(page.actions.tap)
    assert call.args == (HANDLE,)
    assert call.kwargs == {"position": {"x": 1, "y": 2}, "timeout": 7.0, "frame_id": "main", "element_id": "el-1",
                           "trial": True, "strict": False, "force": False}, call.kwargs


def test_select_text_is_the_actions_select_text_on_this_node(page, element):
    assert element.call("selectText", {"force": True, "timeout": 2000}) is None
    call = the_call(page.actions.select_text)
    assert call.args == (HANDLE,)
    assert call.kwargs == {"timeout": 2.0, "frame_id": "main", "element_id": "el-1",
                           "trial": False, "strict": False, "force": True}, call.kwargs


def test_dispatch_event_is_the_actions_dispatch_event_on_this_node_with_the_init_decoded(page, element):
    init = arg({"o": [{"k": "bubbles", "v": {"b": True}}, {"k": "detail", "v": {"n": 3}}]})
    assert element.call("dispatchEvent", {"type": "click", "eventInit": init, "timeout": 4000}) is None
    call = the_call(page.actions.dispatch_event)
    assert call.args == (HANDLE, "click", {"bubbles": True, "detail": 3})
    assert call.kwargs == {"timeout": 4.0, "frame_id": "main", "element_id": "el-1",
                           "trial": False, "strict": False, "force": False}, call.kwargs


def test_dispatch_event_with_no_init_dispatches_an_empty_one(page, element):
    element.call("dispatchEvent", {"type": "focus", "eventInit": arg({"v": "undefined"})})
    assert the_call(page.actions.dispatch_event).args == (HANDLE, "focus", {})


def test_a_handle_in_the_event_init_is_refused_by_name_as_the_frame_s_is(page, element):
    other = ElementHandleDispatcher(page.server, page.frame, "el-2")
    init = {"value": {"o": [{"k": "relatedTarget", "v": {"h": 0}}]}, "handles": [{"guid": other.guid}]}
    with pytest.raises(ProtocolException, match="eventInit.*JSHandle|JSHandle.*eventInit"):
        element.call("dispatchEvent", {"type": "click", "eventInit": init})
    page.actions.dispatch_event.assert_not_called()


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  The caller's code on the element, returning a handle
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
FUNCTION = {"type": "function", "objectId": "the-function"}


@pytest.fixture
def world(page):
    """The caller's world: the expression is a function, the elements cross as copies, and what the function returns is `result`."""
    injected = page.injected
    injected.caller_context.return_value = "world-1"
    injected.evaluate_raw.return_value = dict(FUNCTION)
    injected.node_to_caller.side_effect = lambda frame_id, node: "copy-" + node
    injected.call_raw.return_value = {"objectId": "result-1", "type": "object"}
    return injected


def calls_of_the_function(injected):
    return [c for c in injected.call_raw.call_args_list if c.args[1] == "(f, ...a) => f(...a)"]


def test_evaluate_expression_handle_runs_the_function_on_this_element_and_hands_back_a_handle(page, element, world):
    answer = element.call("evaluateExpressionHandle", {"expression": "(e, n) => ({id: e.id, n})", "isFunction": True, "arg": arg({"n": 1})})
    (call,) = calls_of_the_function(world)
    assert call.args[2] == [{"objectId": "the-function"}, {"objectId": "copy-el-1"}, {"value": 1}], call.args[2]
    assert call.kwargs["by_value"] is False                # a handle of the result, not its value
    world.release.assert_any_call("world-1", "copy-el-1")  # the copy of the node is released, the caller's handle is not
    made = handle_of(page, answer["handle"])
    assert made.object_id == "result-1" and made.world == "caller" and made.frame is page.frame


def test_evaluate_expression_handle_that_returns_a_node_hands_back_the_element(page, element, world):
    world.call_raw.return_value = {"objectId": "node-9", "type": "object", "subtype": "node"}
    world.adopt.return_value = "u-9"
    answer = element.call("evaluateExpressionHandle", {"expression": "e => e.firstChild", "isFunction": True, "arg": arg({"v": "null"})})
    world.adopt.assert_called_once_with("main", "node-9")
    made = handle_of(page, answer["handle"])
    assert made.object_id == "u-9" and made.world == "utility"


def test_evaluate_expression_handle_on_a_handle_of_the_callers_world_takes_the_object_itself(page, world):
    """A handle born in the caller's world (`evaluate_handle`'s result) is the receiver as it is: it is not carried anywhere."""
    thing = ElementHandleDispatcher(page.server, page.frame, "obj-5", world="caller", node=False, context="world-1")
    answer = thing.call("evaluateExpressionHandle", {"expression": "o => o.items", "isFunction": True, "arg": arg({"v": "null"})})
    (call,) = calls_of_the_function(world)
    assert call.args[2][:2] == [{"objectId": "the-function"}, {"objectId": "obj-5"}], call.args[2]
    assert call.kwargs["by_value"] is False
    world.node_to_caller.assert_not_called()
    assert handle_of(page, answer["handle"]).object_id == "result-1"


def test_the_callers_code_of_evaluate_expression_handle_is_sent_once(page, element, world):
    element.call("evaluateExpressionHandle", {"expression": "e => e", "isFunction": True, "arg": arg({"v": "null"})})
    assert world.evaluate_raw.call_count == 1 and len(calls_of_the_function(world)) == 1
