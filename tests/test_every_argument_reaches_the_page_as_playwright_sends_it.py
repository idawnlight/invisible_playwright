"""Every form of an argument reaches the page as Playwright sends it, or the call says it cannot: nothing is dropped in silence.

The client turns `page.evaluate(fn, arg)` into `{"value": <tagged>, "handles": [{guid}, ...]}`. Until this was written the server read it with
`_deserialize`, which kept a number, a string, a boolean, an array and an object and made EVERYTHING else `null`: a handle (`h`), a date (`d`), a URL (`u`),
a regular expression (`r`), an error (`e`), a typed array (`ta`), a BigInt (`bi`), a reference to an object already seen (`ref`, so a repeat and a cycle),
`undefined` (it became `null`), `-0` (it became `null`, and the Python client sends `-0` for EVERY float `0.0`, so `page.evaluate("x => x", 0.0)` was
`null`), and a bare Python `nan` went into the JSON. The call went on with a wrong argument and answered: a silent wrong answer.

What stock Playwright 1.61 does, read from the driver bundle (`coreBundle.js`, cited by function name):

* node side, `parseArgument` -> `parseSerializedValue`, then `evaluateExpression` serialises again with `serializeAsCallArgument` (a string, a number and
  a boolean stripped of their tag, a Date as `{d: toJSON()}`: milliseconds, an Error's stack normalised) and takes each handle through `adoptIfNeeded`;
* a handle of another context is refused with `JSHandles can be evaluated only in the context they were created!`, an element of another frame or world is
  adopted (`Page.adoptNode`) and when that answers nothing `Unable to adopt element handle from a different document`, and a disposed or unknown guid is
  `arg.handles[0]: no object with guid <guid>`;
* page side, `UtilityScript.evaluate` runs `parseEvaluationResultValue` on every argument: `h` is the handle, `d` a Date, `u` a URL, `r` a RegExp, `e` an
  Error with its name and stack, `ta` a typed array, `bi` a BigInt, `v` is undefined, null, NaN, Infinity, -Infinity or -0, `ref` is the same object
  (a repeat and a cycle), the key `__proto__` is skipped, and an `undefined` inside an object keeps its key.

Here the server does the same. A tree of plain JSON (number, string, boolean, null, array, object, no reference) goes as the protocol value it always went
as, byte for byte. Anything else is decoded IN THE PAGE by one extra `Runtime.callFunction` in the same caller context, whose declaration is stock's own
`parseEvaluationResultValue` (the drift test below compares it with the text the core ships and the bundle's), and what it made is the one argument of the
UNCHANGED `(f, ...a) => f(...a)`, released afterwards.

No browser here: the server's own dispatchers run against an injected script that records what the engine is asked. The browser half is
`test_every_argument_reaches_the_page_e2e.py`.
"""
from __future__ import annotations

import json
import math
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from invisible_core.juggler.connection import EventListeners
from invisible_core.juggler.lifecycle import Lifecycle
from invisible_playwright._juggler import server
from invisible_playwright._juggler.dispatcher import Dispatcher, ProtocolException
from invisible_playwright._juggler.server import (
    BindingCallDispatcher, ElementHandleDispatcher, FrameDispatcher, JugglerServer, PageDispatcher)

pytestmark = pytest.mark.unit

CALL = "(f, ...a) => f(...a)"
FUNCTION = {"type": "function", "objectId": "the-function"}
NOT_THE_CONTEXT = "JSHandles can be evaluated only in the context they were created!"
NOT_ADOPTED = "Unable to adopt element handle from a different document"


class _Engine:
    """The injected script, recording every command.

    Every `Runtime.callFunction` is a ("call", context, declaration, args, by_value) entry. The decoder is the call whose declaration holds stock's
    `parseEvaluationResultValue`; it answers a reference, `decoded`. `answers` are what the caller's function answers, call by call (default
    `{"value": "called"}`)."""

    def __init__(self):
        self.context = "world-1"
        self.contexts = None
        self.answers = []
        self.refuse_copy = set()
        self.refuse_adoption = set()
        self.log = []

    def caller_context(self, frame_id, **options):
        if self.contexts:
            return self.contexts.pop(0) if len(self.contexts) > 1 else self.contexts[0]
        return self.context

    def evaluate_raw(self, context, expression, **options):
        self.log.append(("evaluate", context, expression))
        return dict(FUNCTION)

    def call_raw(self, context, declaration, args, by_value=False, **options):
        self.log.append(("call", context, declaration, args, by_value))
        if "parseEvaluationResultValue" in declaration:
            return {"objectId": "decoded", "type": "object"}
        if declaration == server._ARRAY:
            return {"objectId": "els", "type": "object", "subtype": "array"}
        if declaration == CALL and self.answers:
            return self.answers.pop(0)
        return {"value": "called"}

    def release(self, context, object_id):
        self.log.append(("release", context, object_id))

    def dispose(self, frame_id, element):
        self.log.append(("dispose", frame_id, element))

    def node_to_caller(self, frame_id, element):
        self.log.append(("node_to_caller", frame_id, element))
        return None if element in self.refuse_copy else "copy-" + element

    def adopt(self, frame_id, element=None, *, into=None):
        self.log.append(("adopt", frame_id, element, into))
        return None if element in self.refuse_adoption else "moved-" + element

    def query_selector(self, frame_id, selector):
        return "u-1"

    def query_selector_all(self, frame_id, selector):
        return ["u-1", "u-2"]

    # what the tests ask of the log
    def calls(self):
        return [entry for entry in self.log if entry[0] == "call"]

    def decoders(self):
        return [entry for entry in self.calls() if "parseEvaluationResultValue" in entry[2]]

    def callers(self):
        return [entry for entry in self.calls() if entry[2] == CALL]

    def kinds(self):
        return [entry[0] for entry in self.log]


@pytest.fixture
def engine():
    return _Engine()


@pytest.fixture
def page(engine):
    srv = JugglerServer()
    srv.attach(SimpleNamespace(emit_message=lambda message: None))
    context = Dispatcher(srv, None)
    context.options = {}
    page = object.__new__(PageDispatcher)
    page.context = context
    page.lifecycle = Lifecycle(EventListeners(), "session")
    page.injected = engine
    page.actions = Mock()
    page._frames = {}
    page._told_attached = set()
    page._frames_lock = threading.RLock()
    page._announce_lock = threading.RLock()
    page._announced = True
    page._said_before_announcement = []
    page._navigation_requests = {}
    page._announced_closed = False
    Dispatcher.__init__(page, srv, context)
    page.frame = FrameDispatcher(srv, page, "main")
    for fid, parent in (("main", None), ("payment", "main")):
        page.lifecycle._on_event("Page.frameAttached", {"frameId": fid, "parentFrameId": parent})
    page.frame.enter_frames = lambda selector: ("main", selector)
    return page


def handle(page, frame_id, object_id, *, world="utility", node=True, remote=None, context=None):
    """A handle the client holds: `world` says where its object lives, `node` whether it is a node (a utility-world value that is not one cannot cross
    to another world), `context` which world of the caller it was born in."""
    made = ElementHandleDispatcher(page.server, page.frame_for(frame_id), object_id, world=world, remote=remote)
    made.node = node
    made.context = context
    return made


def arg(value, *guids):
    return {"value": value, "handles": [{"guid": g} for g in guids]}


def evaluate(page, argument, expression="f"):
    return page.frame.call("evaluateExpression", {"expression": expression, "arg": argument})


def the_function_call(engine):
    assert len(engine.callers()) == 1, engine.callers()
    return engine.callers()[0]


def the_spec(engine):
    """What the decoder was handed as its first argument, and its other arguments."""
    assert len(engine.decoders()) == 1, (
        "the page was never asked to decode the argument; the caller's function was sent %r" % [c[3] for c in engine.callers()])
    entry = engine.decoders()[0]
    return entry[3][0]["value"], entry[3][1:]


# --- a handle reaches the engine as the object of that handle ------------------------------------------------------------------

def test_a_caller_world_handle_alone_is_the_argument_itself(page, engine):
    """Stock: `h` is `handles[i]`. Known-bad: `_deserialize` answers `null` for it."""
    made = handle(page, "main", "obj-9", world="caller", node=False, context="world-1")
    evaluate(page, arg({"h": 0}, made.guid))
    assert the_function_call(engine)[3] == [{"objectId": "the-function"}, {"objectId": "obj-9"}]
    assert not engine.decoders()
    assert ("release", "world-1", "obj-9") not in engine.log, "the caller's own handle is not released by the call"


def test_an_element_of_the_utility_world_is_carried_to_the_caller_world_and_the_copy_released(page, engine):
    """Stock adopts a handle of another world (`adoptIfNeeded`) and disposes the adopted copy after the call."""
    made = handle(page, "main", "u-1")
    evaluate(page, arg({"h": 0}, made.guid))
    assert ("node_to_caller", "main", "u-1") in engine.log
    assert the_function_call(engine)[3] == [{"objectId": "the-function"}, {"objectId": "copy-u-1"}]
    assert engine.log.index(("release", "world-1", "copy-u-1")) > engine.log.index(engine.callers()[0])


def test_a_handle_nested_in_an_object_and_an_array_reaches_the_page_through_the_decoder(page, engine):
    first = handle(page, "main", "obj-9", world="caller", node=False, context="world-1")
    second = handle(page, "main", "obj-10", world="caller", node=False, context="world-1")
    tree = {"o": [{"k": "el", "v": {"h": 0}},
                  {"k": "list", "v": {"a": [{"h": 1}, {"n": 2}], "id": 2}}], "id": 1}
    evaluate(page, arg(tree, first.guid, second.guid))
    spec, objects = the_spec(engine)
    assert spec == {"o": [{"k": "el", "v": {"h": 0}},
                          {"k": "list", "v": {"a": [{"h": 1}, 2], "id": 2}}], "id": 1}
    assert objects == [{"objectId": "obj-9"}, {"objectId": "obj-10"}]
    assert the_function_call(engine)[3] == [{"objectId": "the-function"}, {"objectId": "decoded"}]
    assert engine.log.index(engine.decoders()[0]) < engine.log.index(engine.callers()[0])
    assert engine.log.index(("release", "world-1", "decoded")) > engine.log.index(engine.callers()[0])


def test_a_handle_the_page_asks_for_twice_is_one_object(page, engine):
    made = handle(page, "main", "u-1")
    evaluate(page, arg({"a": [{"h": 0}, {"h": 1}], "id": 1}, made.guid, made.guid))
    spec, objects = the_spec(engine)
    assert spec["a"][0] == {"h": 0} and spec["a"][1] == {"h": 0} and objects == [{"objectId": "copy-u-1"}]
    assert engine.log.count(("node_to_caller", "main", "u-1")) == 1


def test_an_element_of_another_frame_is_adopted_here_then_carried_and_nothing_is_left(page, engine):
    """Stock: `Page.adoptNode` into the context that runs the code. The copy in the utility world of this frame is only the road, and goes."""
    made = handle(page, "payment", "u-1")
    evaluate(page, arg({"h": 0}, made.guid))
    assert ("adopt", "payment", "u-1", "main") in engine.log
    assert ("node_to_caller", "main", "moved-u-1") in engine.log
    assert the_function_call(engine)[3] == [{"objectId": "the-function"}, {"objectId": "copy-moved-u-1"}]
    assert ("dispose", "main", "moved-u-1") in engine.log
    assert ("release", "world-1", "copy-moved-u-1") in engine.log


# --- a primitive of the caller's world is its value, as stock's `fallThrough: handle._value` ----------------------------------

@pytest.mark.parametrize("remote, nested, alone", [
    ({"value": 5}, 5, {"value": 5}),
    ({"value": "s"}, "s", {"value": "s"}),
    ({"value": True}, True, {"value": True}),
    ({"value": None}, {"v": "null"}, {"value": None}),
    ({"unserializableValue": "NaN"}, {"v": "NaN"}, {"unserializableValue": "NaN"}),
    ({"unserializableValue": "-0"}, {"v": "-0"}, {"unserializableValue": "-0"}),
    ({}, {"v": "undefined"}, {}),
], ids=["number", "string", "boolean", "null", "nan", "minus-zero", "undefined"])
def test_a_primitive_handle_of_the_callers_world_is_inlined_as_its_value(page, engine, remote, nested, alone):
    made = handle(page, "main", "", world="caller", node=False, remote=remote, context="world-1")
    evaluate(page, arg({"h": 0}, made.guid))
    assert the_function_call(engine)[3] == [{"objectId": "the-function"}, alone]
    assert not engine.decoders()
    engine.log.clear()
    evaluate(page, arg({"a": [{"h": 0}], "id": 1}, made.guid))
    spec, objects = the_spec(engine)
    assert spec == {"a": [nested], "id": 1} and objects == []


# --- the stock texts of a handle that cannot be used ---------------------------------------------------------------------------

def _refused(page, engine, argument, text):
    with pytest.raises(ProtocolException) as refused:
        evaluate(page, argument)
    assert str(refused.value) == text
    assert "evaluate" not in engine.kinds(), "the caller's code does not run when its argument is refused"
    assert not engine.callers()


def test_an_unknown_guid_is_stock_s_no_object_text(page, engine):
    _refused(page, engine, arg({"h": 0}, "elementhandle@999"), "arg.handles[0]: no object with guid elementhandle@999")


def test_a_disposed_handle_is_stock_s_no_object_text_and_names_its_place_in_the_list(page, engine):
    alive = handle(page, "main", "u-1")
    gone = handle(page, "main", "u-2")
    gone.dispose()
    _refused(page, engine, arg({"a": [{"h": 0}, {"h": 1}], "id": 1}, alive.guid, gone.guid),
             "arg.handles[1]: no object with guid %s" % gone.guid)
    assert ("release", "world-1", "copy-u-1") in engine.log, "what was carried before the refusal is let go"


def test_a_non_element_handle_of_another_frame_is_stock_s_context_text(page, engine):
    made = handle(page, "payment", "obj-1", world="caller", node=False, context="world-9")
    _refused(page, engine, arg({"h": 0}, made.guid), NOT_THE_CONTEXT)


def test_a_caller_world_handle_of_a_world_that_went_is_stock_s_context_text(page, engine):
    made = handle(page, "main", "obj-1", world="caller", node=False, context="world-0")
    _refused(page, engine, arg({"h": 0}, made.guid), NOT_THE_CONTEXT)


def test_a_utility_world_value_that_is_not_a_node_cannot_cross_and_says_so(page, engine):
    """`get_property` and `get_property_list` wrap their value as a handle of the utility world, a node or not: an object of that realm cannot be
    handed to the caller's, and the answer is stock's text, not an engine error about an object it has never seen."""
    made = handle(page, "main", "u-7", node=False)
    _refused(page, engine, arg({"h": 0}, made.guid), NOT_THE_CONTEXT)


@pytest.mark.parametrize("failing", ["copy", "adoption"])
def test_an_element_that_cannot_be_adopted_is_stock_s_adoption_text(page, engine, failing):
    here = failing == "copy"
    made = handle(page, "main" if here else "payment", "u-1")
    (engine.refuse_copy if here else engine.refuse_adoption).add("u-1")
    _refused(page, engine, arg({"h": 0}, made.guid), NOT_ADOPTED)


def test_an_element_of_another_frame_whose_carrying_fails_leaves_no_copy_behind(page, engine):
    made = handle(page, "payment", "u-1")
    engine.refuse_copy.add("moved-u-1")
    _refused(page, engine, arg({"h": 0}, made.guid), NOT_ADOPTED)
    assert ("dispose", "main", "moved-u-1") in engine.log


# --- every other form reaches the decoder intact ------------------------------------------------------------------------------

FORMS = [
    ("date", {"d": "2021-07-28T20:26:32.123456Z"}, {"d": "2021-07-28T20:26:32.123Z"}),
    ("date-in-ms", {"d": "2021-07-28T20:26:32.000000Z"}, {"d": "2021-07-28T20:26:32.000Z"}),
    ("url", {"u": "https://page.test/a?b=1"}, {"u": "https://page.test/a?b=1"}),
    ("regexp", {"r": {"p": "a+", "f": "gi"}}, {"r": {"p": "a+", "f": "gi"}}),
    ("error-with-its-own-stack", {"e": {"m": "boom", "n": "ValueError", "s": "ValueError: boom\n  at x"}},
     {"e": {"m": "boom", "n": "ValueError", "s": "ValueError: boom\n  at x"}}),
    ("error-stack-normalised", {"e": {"m": "boom", "n": "ValueError", "s": "Traceback (most recent call last)"}},
     {"e": {"m": "boom", "n": "ValueError", "s": "ValueError: boom\nTraceback (most recent call last)"}}),
    ("error-without-a-stack", {"e": {"m": "boom", "n": "Error", "s": ""}}, {"e": {"m": "boom", "n": "Error", "s": "Error: boom\n"}}),
    ("typed-array", {"ta": {"b": "AQID", "k": "ui8"}}, {"ta": {"b": "AQID", "k": "ui8"}}),
    ("bigint-in-an-array", {"a": [{"bi": "12345678901234567890"}], "id": 1}, {"a": [{"bi": "12345678901234567890"}], "id": 1}),
    ("tags-stripped", {"a": [{"s": "x"}, {"n": 1}, {"b": True}, {"n": 1.5}, {"v": "null"}, {"v": "undefined"}], "id": 1},
     {"a": ["x", 1, True, 1.5, {"v": "null"}, {"v": "undefined"}], "id": 1}),
    ("nan-inside", {"a": [{"v": "NaN"}, {"v": "Infinity"}, {"v": "-Infinity"}, {"v": "-0"}], "id": 1},
     {"a": [{"v": "NaN"}, {"v": "Infinity"}, {"v": "-Infinity"}, {"v": "-0"}], "id": 1}),
    ("undefined-in-an-object", {"o": [{"k": "a", "v": {"n": 1}}, {"k": "u", "v": {"v": "undefined"}}], "id": 1},
     {"o": [{"k": "a", "v": 1}, {"k": "u", "v": {"v": "undefined"}}], "id": 1}),
    ("undefined-in-an-array", {"a": [{"v": "undefined"}], "id": 1}, {"a": [{"v": "undefined"}], "id": 1}),
    ("a-repeat", {"o": [{"k": "x", "v": {"o": [{"k": "n", "v": {"n": 1}}], "id": 2}}, {"k": "y", "v": {"ref": 2}}], "id": 1},
     {"o": [{"k": "x", "v": {"o": [{"k": "n", "v": 1}], "id": 2}}, {"k": "y", "v": {"ref": 2}}], "id": 1}),
    ("a-cycle", {"o": [{"k": "self", "v": {"ref": 1}}], "id": 1}, {"o": [{"k": "self", "v": {"ref": 1}}], "id": 1}),
    ("a-key-the-page-skips", {"o": [{"k": "__proto__", "v": {"n": 1}}, {"k": "a", "v": {"n": 2}}], "id": 1},
     {"o": [{"k": "__proto__", "v": 1}, {"k": "a", "v": 2}], "id": 1}),
]


@pytest.mark.parametrize("name, tagged, spec", FORMS, ids=[f[0] for f in FORMS])
def test_a_form_reaches_the_decoder_intact_and_the_decoded_object_is_the_one_argument(page, engine, name, tagged, spec):
    """Known-bad: `_deserialize` reads the tag of each of these as `null`."""
    evaluate(page, arg(tagged))
    got, objects = the_spec(engine)
    assert got == spec and objects == []
    assert engine.decoders()[0][1] == "world-1" and engine.decoders()[0][4] is False, "in the caller's context, by reference"
    assert the_function_call(engine)[3] == [{"objectId": "the-function"}, {"objectId": "decoded"}]
    assert the_function_call(engine)[2] == CALL
    assert ("release", "world-1", "decoded") in engine.log, "the decoded object is the call's own and is let go"


@pytest.mark.parametrize("tagged", [
    {"d": "2021-07-28T20:26:32.123456Z"},
    {"u": "https://example.com/"},
    {"r": {"p": "a", "f": ""}},
    {"e": {"m": "m", "n": "Error", "s": "Error: m"}},
    {"ta": {"b": "AQID", "k": "ui8"}},
], ids=["date", "url", "regexp", "error", "typed-array"])
def test_a_top_level_object_form_is_decoded_and_released_even_when_the_callers_function_throws(page, engine, tagged):
    def throws(context, declaration, args, by_value=False, **options):
        engine.log.append(("call", context, declaration, args, by_value))
        if "parseEvaluationResultValue" in declaration:
            return {"objectId": "decoded", "type": "object"}
        raise RuntimeError("the page threw")
    engine.call_raw = throws
    with pytest.raises(RuntimeError):
        evaluate(page, arg(tagged))
    assert ("release", "world-1", "decoded") in engine.log
    assert ("release", "world-1", "the-function") in engine.log


@pytest.mark.parametrize("special", ["NaN", "Infinity", "-Infinity", "-0"])
def test_a_top_level_special_number_is_an_unserializable_argument_and_never_a_bare_nan(page, engine, special):
    """The engine's `CallFunctionArgument.unserializableValue`, which `evaluateFunction` reads back as the number."""
    evaluate(page, arg({"v": special}))
    assert the_function_call(engine)[3] == [{"objectId": "the-function"}, {"unserializableValue": special}]
    assert not engine.decoders()
    json.dumps(the_function_call(engine)[3], allow_nan=False)


def test_a_top_level_undefined_is_an_argument_without_a_value(page, engine):
    evaluate(page, arg({"v": "undefined"}))
    assert the_function_call(engine)[3] == [{"objectId": "the-function"}, {}]


def test_a_top_level_bigint_is_refused_by_name_because_the_protocol_cannot_carry_it(page, engine):
    """A BigInt result of the decoder would have to cross the engine's JSON: `callFunction` has no argument for it. Said, not sent as `null`."""
    with pytest.raises(ProtocolException, match="BigInt"):
        evaluate(page, arg({"bi": "12"}))
    assert not engine.callers()


# --- the fast path is what it always was ---------------------------------------------------------------------------------------

PLAIN = [
    ({"n": 7}, 7), ({"s": "hi"}, "hi"), ({"b": True}, True), ({"v": "null"}, None), (None, None),
    ({"a": [{"n": 1}, {"s": "x"}, {"v": "null"}, {"b": False}], "id": 1}, [1, "x", None, False]),
    ({"o": [{"k": "k", "v": {"n": 1.5}}, {"k": "nested", "v": {"o": [{"k": "l", "v": {"a": [], "id": 3}}], "id": 2}}], "id": 1},
     {"k": 1.5, "nested": {"l": []}}),
]


@pytest.mark.parametrize("tagged, plain", PLAIN, ids=["number", "string", "boolean", "null", "none", "array", "object"])
def test_plain_json_goes_as_the_protocol_value_it_always_went_as_byte_for_byte(page, engine, tagged, plain):
    evaluate(page, arg(tagged))
    assert json.dumps(the_function_call(engine)[3]) == json.dumps([{"objectId": "the-function"}, {"value": plain}])
    assert engine.kinds() == ["evaluate", "call", "release"], "no decoder, no copy, nothing else asked of the engine"


def test_the_call_the_caller_s_code_runs_in_is_byte_identical_whatever_the_argument(page, engine):
    """What the caller's code sees in `new Error().stack` is the text of this function: it does not change."""
    assert server._CALL == CALL
    for tagged in ({"n": 1}, {"d": "2021-07-28T20:26:32.123456Z"}, {"v": "NaN"}):
        engine.log.clear()
        evaluate(page, arg(tagged))
        assert the_function_call(engine)[2] == CALL


# --- the decoder is stock's function ------------------------------------------------------------------------------------------

def _normalised(text):
    text = re.sub(r"\b(value|result)2\b", r"\1", text)
    return [re.sub(r"^(var|const|let) ", "", line.strip()) for line in text.splitlines() if line.strip()]


def _piece(source, pattern):
    found = re.search(pattern, source, re.S)
    assert found, "the decoder's source has no %r" % pattern
    return _normalised(found.group(0))


PIECES = (r"typedArrayConstructors\s*=\s*\{.*?\n\s*\};",
          r"function base64ToTypedArray\(.*?\n\}",
          r"function parseEvaluationResultValue\(.*?\n\}")


def _lines_of(declaration):
    return _normalised(declaration)


def _contains(lines, piece):
    return any(lines[i:i + len(piece)] == piece for i in range(len(lines) - len(piece) + 1))


def _decoder_declaration(page, engine):
    evaluate(page, arg({"d": "2021-07-28T20:26:32.123456Z"}))
    [entry] = engine.decoders()
    return entry[2]


def test_the_decoder_declaration_holds_the_text_of_stock_s_parse_function(page, engine):
    """A drift test. The sources are the text the core ships (`invisible_core/juggler/injected.js`, which carries Playwright's utility script) and,
    where the stock driver is installed, its bundle (`coreBundle.js`, `parseEvaluationResultValue`, `base64ToTypedArray`, `typedArrayConstructors`).
    The declaration is the three pieces VERBATIM, and a function that takes the spec and the handles. Known-bad: a copy edited by hand, or a stock
    that moved on, is red here and not in a page."""
    declaration = _decoder_declaration(page, engine)
    lines = _lines_of(declaration)
    import invisible_core
    sources = [Path(invisible_core.__file__).parent / "juggler" / "injected.js"]
    try:
        import playwright
        sources.append(Path(playwright.__file__).parent / "driver" / "package" / "lib" / "coreBundle.js")
    except ImportError:
        pass
    seen = 0
    for path in sources:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in PIECES:
            assert _contains(lines, _piece(text, pattern)), "%s: %s is not in the decoder" % (path.name, pattern)
        seen += 1
    assert seen >= 1, "neither the core's injected.js nor the stock bundle could be read"
    assert re.match(r"\(spec, \.\.\.handles\) =>", declaration.strip()), declaration[:60]
    assert lines[-2:] == ["return parseEvaluationResultValue(spec, handles);", "}"], lines[-3:]


# --- every entry point that takes an argument ---------------------------------------------------------------------------------

DATE = {"d": "2021-07-28T20:26:32.123456Z"}
DATE_SPEC = {"d": "2021-07-28T20:26:32.123Z"}


def test_evaluate_handle_decodes_its_argument_too(page, engine):
    page.frame.call("evaluateExpressionHandle", {"expression": "f", "arg": arg(DATE)})
    assert the_spec(engine)[0] == DATE_SPEC
    assert the_function_call(engine)[3] == [{"objectId": "the-function"}, {"objectId": "decoded"}]
    assert ("release", "world-1", "decoded") in engine.log, "a by-reference result is the caller's to keep, the decoded argument is not"


def test_a_handle_s_own_evaluate_puts_the_receiver_first_and_the_decoded_argument_last(page, engine):
    receiver = handle(page, "main", "obj-1", world="caller", node=False, context="world-1")
    receiver.call("evaluateExpression", {"expression": "f", "arg": arg(DATE)})
    assert the_spec(engine)[0] == DATE_SPEC
    assert the_function_call(engine)[3] == [{"objectId": "the-function"}, {"objectId": "obj-1"}, {"objectId": "decoded"}]


def test_an_element_s_evaluate_decodes_in_the_world_the_element_is_carried_to(page, engine):
    element = handle(page, "main", "u-1")
    element.call("evaluateExpression", {"expression": "f", "arg": arg(DATE)})
    assert the_spec(engine)[0] == DATE_SPEC
    assert the_function_call(engine)[3] == [{"objectId": "the-function"}, {"objectId": "copy-u-1"}, {"objectId": "decoded"}]
    assert ("release", "world-1", "decoded") in engine.log and ("release", "world-1", "copy-u-1") in engine.log


def test_an_element_s_evaluate_takes_a_handle_argument(page, engine):
    element = handle(page, "main", "u-1")
    other = handle(page, "main", "u-2")
    element.call("evaluateExpression", {"expression": "f", "arg": arg({"h": 0}, other.guid)})
    assert the_function_call(engine)[3] == [{"objectId": "the-function"}, {"objectId": "copy-u-1"}, {"objectId": "copy-u-2"}]


def test_eval_on_selector_decodes_its_argument(page, engine):
    page.frame.call("evalOnSelector", {"selector": "#a", "expression": "f", "arg": arg(DATE)})
    assert the_spec(engine)[0] == DATE_SPEC
    assert the_function_call(engine)[3] == [{"objectId": "the-function"}, {"objectId": "copy-u-1"}, {"objectId": "decoded"}]


def test_eval_on_selector_all_decodes_its_argument(page, engine):
    page.frame.call("evalOnSelectorAll", {"selector": "li", "expression": "f", "arg": arg(DATE)})
    assert the_spec(engine)[0] == DATE_SPEC
    assert the_function_call(engine)[3] == [{"objectId": "the-function"}, {"objectId": "els"}, {"objectId": "decoded"}]


def test_eval_on_selector_takes_a_handle_argument(page, engine):
    other = handle(page, "main", "u-9")
    page.frame.call("evalOnSelector", {"selector": "#a", "expression": "f", "arg": arg({"h": 0}, other.guid)})
    assert the_function_call(engine)[3] == [{"objectId": "the-function"}, {"objectId": "copy-u-1"}, {"objectId": "copy-u-9"}]


def test_wait_for_function_decodes_once_and_keeps_the_decoded_object_across_turns(page, engine):
    """Stock keeps its parsed arguments across the turns of a poll. Decoded again each turn, the page would be asked to make a new Date every 50 ms."""
    engine.answers = [{"value": False}, {"value": False}, {"value": True}]
    page.frame.call("waitForFunction", {"expression": "f", "arg": arg(DATE), "timeout": 5000})
    assert len(engine.callers()) == 3 and len(engine.decoders()) == 1
    assert {tuple(map(json.dumps, entry[3])) for entry in engine.callers()} == {
        (json.dumps({"objectId": "the-function"}), json.dumps({"objectId": "decoded"}))}
    assert ("release", "world-1", "decoded") in engine.log
    assert engine.log.index(("release", "world-1", "decoded")) > engine.log.index(engine.callers()[-1])


def test_wait_for_function_decodes_again_in_the_world_that_replaces_the_one_it_was_decoded_in(page, engine):
    engine.contexts = ["world-1", "world-2", "world-2"]
    engine.answers = [{"value": False}, {"value": True}]
    page.frame.call("waitForFunction", {"expression": "f", "arg": arg(DATE), "timeout": 5000})
    assert [entry[1] for entry in engine.decoders()] == ["world-1", "world-2"]


def test_wait_for_function_takes_a_handle_argument(page, engine):
    other = handle(page, "main", "u-9")
    page.frame.call("waitForFunction", {"expression": "f", "arg": arg({"h": 0}, other.guid), "timeout": 5000})
    assert engine.callers()[0][3] == [{"objectId": "the-function"}, {"objectId": "copy-u-9"}]
    assert ("release", "world-1", "copy-u-9") in engine.log


def _binding(engine):
    call = object.__new__(BindingCallDispatcher)
    call.page = SimpleNamespace(_binding_ends={("ctx", "native"): "end-1"}, injected=engine)
    call.context_id, call.native, call.seq = "ctx", "native", 3
    call.dispose = lambda: None
    return call


def _delivered(engine):
    [entry] = [e for e in engine.calls() if e[2] == server._bindings.DELIVER]
    return entry


def test_the_result_of_an_exposed_function_is_decoded_in_the_context_that_answers(engine):
    """Stock hands a binding's result to the page through the same `parseEvaluationResultValue`: a Date comes back as a Date."""
    _binding(engine).op_resolve({"result": arg(DATE)})
    assert engine.decoders()[0][1] == "ctx" and the_spec(engine)[0] == DATE_SPEC
    assert _delivered(engine)[3] == [{"objectId": "end-1"}, {"value": 3}, {"value": True}, {"objectId": "decoded"}]
    assert ("release", "ctx", "decoded") in engine.log


def test_the_plain_result_of_an_exposed_function_goes_as_it_always_went(engine):
    _binding(engine).op_resolve({"result": arg({"o": [{"k": "a", "v": {"n": 1}}], "id": 1})})
    assert _delivered(engine)[3] == [{"objectId": "end-1"}, {"value": 3}, {"value": True}, {"value": {"a": 1}}]
    assert not engine.decoders()


def test_a_special_number_as_the_result_of_an_exposed_function_is_not_a_bare_nan(engine):
    _binding(engine).op_resolve({"result": arg({"v": "NaN"})})
    assert _delivered(engine)[3][3] == {"unserializableValue": "NaN"}


def test_a_handle_in_the_result_of_an_exposed_function_rejects_the_page_s_promise_with_the_reason(engine):
    """Its object lives in the caller's world and the answer is delivered into the page's own: said to the page, not delivered as `null`."""
    _binding(engine).op_resolve({"result": arg({"h": 0}, "elementhandle@1")})
    delivered = _delivered(engine)[3]
    assert delivered[2] == {"value": False} and "JSHandle" in delivered[3]["value"], delivered


# --- what this server cannot give the page is said, not sent as null ----------------------------------------------------------

def test_a_handle_in_an_event_init_is_refused_by_name(page):
    """`dispatchEvent` runs in the utility world through the core: a handle cannot be passed there without a change of the core."""
    with pytest.raises(ProtocolException, match="eventInit.*JSHandle|JSHandle.*eventInit"):
        page.frame.call("dispatchEvent", {"selector": "#a", "type": "click",
                                          "eventInit": arg({"o": [{"k": "relatedTarget", "v": {"h": 0}}], "id": 1}, "elementhandle@1")})
    page.actions.dispatch_event.assert_not_called()


def test_a_handle_in_an_expected_value_is_refused_by_name(page):
    page.injected = Mock()
    with pytest.raises(ProtocolException, match="expectedValue.*JSHandle|JSHandle.*expectedValue"):
        page.frame._expect_probe("to.have.js.property", "#a", {
            "expression": "to.have.js.property", "expressionArg": "a",
            "expectedValue": arg({"h": 0}, "elementhandle@1")})
    assert not page.injected.call.called


def test_a_minus_zero_that_stays_a_plain_value_is_minus_zero_and_not_null():
    """The consumers that keep `_deserialize` (an event's init, an expected value) carry a value as JSON: `-0` is a number there."""
    value = server._deserialize({"v": "-0"})
    assert value == 0.0 and math.copysign(1.0, value) == -1.0


# --- the tags: every one the protocol validator of the installed driver accepts, and the one its page side adds --------------------------------

#: One value of each tag, as the client sends it (`{"value": <this>, "handles": [...]}`); the value is the tag's own node, or a container that holds it where a
#: node alone cannot stand (a BigInt is refused as the whole argument).
_SAMPLES = {
    "n": lambda page: ({"n": 1.5}, []),
    "b": lambda page: ({"b": True}, []),
    "s": lambda page: ({"s": "x"}, []),
    "v": lambda page: ({"v": "NaN"}, []),
    "d": lambda page: ({"d": "2021-07-28T20:26:32.123456Z"}, []),
    "u": lambda page: ({"u": "https://page.test/a"}, []),
    "bi": lambda page: ({"a": [{"bi": "12345678901234567890"}], "id": 1}, []),
    "ab": lambda page: ({"ab": {"b": "AAEC"}}, []),
    "ta": lambda page: ({"ta": {"b": "AQID", "k": "ui8"}}, []),
    "e": lambda page: ({"e": {"m": "boom", "n": "Error", "s": "Error: boom\n"}}, []),
    "r": lambda page: ({"r": {"p": "a+", "f": "gi"}}, []),
    "a": lambda page: ({"a": [{"n": 1}], "id": 1}, []),
    "o": lambda page: ({"o": [{"k": "k", "v": {"n": 1}}], "id": 1}, []),
    "h": lambda page: ({"h": 0}, [handle(page, "main", "obj-9", world="caller", node=False, context="world-1").guid]),
    "ref": lambda page: ({"o": [{"k": "self", "v": {"ref": 1}}], "id": 1}, []),
}


def _installed_bundle():
    """The driver bundle of the INSTALLED `playwright` package: the dev dependency is always there, and a missing one is a red, never a skip."""
    try:
        import playwright
    except ImportError:
        pytest.fail("the `playwright` dev dependency is not installed: the protocol validator that the tags are read from cannot be found")
    path = Path(playwright.__file__).parent / "driver" / "package" / "lib" / "coreBundle.js"
    if not path.exists():
        pytest.fail("the installed `playwright` has no driver bundle at %s: the protocol validator that the tags are read from cannot be found" % path)
    return path.read_text(encoding="utf-8")


def _validator_tags():
    """The keys of `scheme.SerializedValue = tObject({...})` in the bundle: what the node side of stock accepts of a value, read at brace depth 0."""
    text = _installed_bundle()
    start = text.find("scheme.SerializedValue = tObject({")
    assert start >= 0, "the bundle has no `scheme.SerializedValue = tObject({...})`: the validator moved, the test must follow it"
    body_start = start + len("scheme.SerializedValue = tObject({")
    depth, keys = 0, []
    for token in re.finditer(r"[{(\[]|[})\]]|\b(\w+):", text[body_start:]):
        piece = token.group(0)
        if piece in "{([":
            depth += 1
        elif piece in "})]":
            if depth == 0:
                return keys
            depth -= 1
        elif depth == 0:
            keys.append(token.group(1))
    pytest.fail("the bundle's SerializedValue validator never ends")


def test_the_samples_are_the_tags_this_module_handles_and_nothing_else():
    from invisible_playwright._juggler import _arguments
    assert set(_SAMPLES) == set(_arguments.VALUE_TAGS), "a tag with no sample here, or a sample of a tag this module does not name"


@pytest.mark.parametrize("tag", sorted(_SAMPLES))
def test_a_value_of_every_tag_reaches_the_page_and_none_is_refused(page, engine, tag):
    """Known-bad: `ab` (an ArrayBuffer, which the page side of stock decodes) was `Attempting to deserialize unexpected value`."""
    tagged, guids = _SAMPLES[tag](page)
    evaluate(page, arg(tagged, *guids))
    assert len(engine.callers()) == 1, "the call was not made"


def test_an_array_buffer_reaches_the_decoder_as_the_client_sent_it(page, engine):
    evaluate(page, arg({"ab": {"b": "AAEC"}}))
    assert the_spec(engine)[0] == {"ab": {"b": "AAEC"}}


def test_a_node_that_is_no_tag_is_refused_by_name(page, engine):
    with pytest.raises(ProtocolException, match="Attempting to deserialize unexpected value"):
        evaluate(page, arg({"zz": 1}))
    assert not engine.callers()


def test_every_tag_the_protocol_validator_accepts_is_a_tag_this_module_handles():
    """The validator of the installed driver (`scheme.SerializedValue`) lists `n b s v d u bi ta e r a o h id ref`: `id` only marks an array or an object,
    every other is a value. Its PAGE side (`parseEvaluationResultValue`) decodes one more, `ab`, which the validator does not list and the decoder here
    embeds. The set this module handles is exactly those: a tag a newer driver adds is red here until it is handled and given a sample above."""
    from invisible_playwright._juggler import _arguments
    validator = set(_validator_tags())
    assert {"n", "b", "s", "v", "d", "u", "bi", "ta", "e", "r", "a", "o", "h", "id", "ref"} <= validator, validator
    page_side = {"ab"} if '"ab" in value' in _installed_bundle() else set()
    assert page_side == {"ab"}, "the page side of the driver no longer decodes `ab`"
    assert set(_arguments.VALUE_TAGS) == (validator - {"id"}) | page_side


# --- the divergences from stock, each pinned by the TEXT it answers with -------------------------------------------------------------------

REFUSED_HANDLE = ("%s cannot carry a JSHandle: it is read in the utility world, through the core, where the handle's object does not exist, and used to "
                  "arrive as null. Pass the handle's value (json_value) or its properties instead")


def test_a_utility_world_value_that_is_not_a_node_is_refused_with_the_exact_context_text(page, engine):
    """(1) What `get_property` hands back cannot cross to the caller's world; stock's own text, word for word, is the answer (the same test as above pins
    it through `_refused`, which compares the whole text; this one names the divergence and holds the literal)."""
    made = handle(page, "main", "u-7", node=False)
    with pytest.raises(ProtocolException) as refused:
        evaluate(page, arg({"h": 0}, made.guid))
    assert str(refused.value) == "JSHandles can be evaluated only in the context they were created!"


def test_a_handle_in_an_event_init_is_refused_with_the_exact_text(page):
    """(2) `dispatch_event`'s `eventInit`."""
    with pytest.raises(ProtocolException) as refused:
        page.frame.call("dispatchEvent", {"selector": "#a", "type": "click",
                                          "eventInit": arg({"o": [{"k": "relatedTarget", "v": {"h": 0}}], "id": 1}, "elementhandle@1")})
    assert str(refused.value) == REFUSED_HANDLE % "dispatch_event's eventInit"


def test_a_handle_in_an_expected_value_is_refused_with_the_exact_text(page):
    """(2) `expect`'s `expectedValue`."""
    page.injected = Mock()
    with pytest.raises(ProtocolException) as refused:
        page.frame._expect_probe("to.have.js.property", "#a", {
            "expression": "to.have.js.property", "expressionArg": "a", "expectedValue": arg({"h": 0}, "elementhandle@1")})
    assert str(refused.value) == REFUSED_HANDLE % "expect's expectedValue"


def test_a_bigint_as_the_whole_argument_is_refused_with_the_exact_text(page, engine):
    """(3) The protocol's JSON has no BigInt; inside an array or an object it goes."""
    with pytest.raises(ProtocolException) as refused:
        evaluate(page, arg({"bi": "12"}))
    assert str(refused.value) == ("a BigInt as the whole argument cannot be handed to the page: the engine's protocol carries no BigInt. Put it in an "
                                  "array or an object, or pass it as a string")


def test_a_handle_in_the_result_of_an_exposed_function_rejects_the_promise_with_the_exact_reason(engine):
    """(4) The reason the page's promise is rejected with, word for word."""
    _binding(engine).op_resolve({"result": arg({"h": 0}, "elementhandle@1")})
    assert _delivered(engine)[3][2:] == [
        {"value": False},
        {"value": "result carries a JSHandle, which cannot be delivered here: its object lives in the caller's world, and this answer is read in the "
                  "page's own"}]
    assert not engine.decoders()


# --- where the decoder runs ---------------------------------------------------------------------------------------------------------------

THE_CALLERS_WORLD = "the-callers-world"


class _OnlyTheCallersWorld(_Engine):
    """An engine whose caller's world has a name nothing else has, and which has NO other way to ask for a world: a call that reached for the page's
    main world (or the utility one) would not find the method."""

    def __init__(self):
        super().__init__()
        self.context = THE_CALLERS_WORLD

    def __getattr__(self, name):
        raise AssertionError("the argument asked the injected script for %r: only `caller_context` names the world the decoder runs in" % name)


def _entry_points(page):
    element = handle(page, "main", "u-1")
    return {
        "evaluate": lambda: evaluate(page, arg(DATE)),
        "evaluate_handle": lambda: page.frame.call("evaluateExpressionHandle", {"expression": "f", "arg": arg(DATE)}),
        "element_evaluate": lambda: element.call("evaluateExpression", {"expression": "f", "arg": arg(DATE)}),
        "eval_on_selector": lambda: page.frame.call("evalOnSelector", {"selector": "#a", "expression": "f", "arg": arg(DATE)}),
        "eval_on_selector_all": lambda: page.frame.call("evalOnSelectorAll", {"selector": "li", "expression": "f", "arg": arg(DATE)}),
        "wait_for_function": lambda: page.frame.call("waitForFunction", {"expression": "f", "arg": arg(DATE), "timeout": 5000}),
    }


@pytest.mark.parametrize("entry", ["evaluate", "evaluate_handle", "element_evaluate", "eval_on_selector", "eval_on_selector_all", "wait_for_function"])
def test_the_decoder_runs_in_the_callers_world_and_leaves_no_global(page, entry):
    """(a) The decoder is called in the context `injected.caller_context(...)` names, the one the caller's own code runs in, and never in the page's
    main-world context: every command of the call (the decoder, the caller's function, the releases) is aimed at that one context. It is a function
    declaration called with the spec: it writes nothing on `window`, `globalThis`, `self` or `this`."""
    engine = _OnlyTheCallersWorld()
    page.injected = engine
    _entry_points(page)[entry]()
    assert len(engine.decoders()) == 1
    assert engine.decoders()[0][1] == THE_CALLERS_WORLD
    assert {e[1] for e in engine.log if e[0] in ("call", "evaluate", "release")} == {THE_CALLERS_WORLD}, engine.log
    assert [e for e in engine.log if e[0] == "evaluate" and "parseEvaluationResultValue" in e[2]] == [], "no script is evaluated to define the decoder"
    declaration = engine.decoders()[0][2]
    assert re.search(r"\b(window|globalThis|self|top|this)\b\s*(\.|\[)[^;\n]*=(?!=)", declaration) is None, "the decoder writes a global"
    assert not re.search(r"\b(globalThis|window\.|self\.)", declaration)
    assert ("release", THE_CALLERS_WORLD, "decoded") in engine.log


def test_a_plain_result_of_an_exposed_function_is_one_call_the_delivery_and_nothing_else(engine):
    """(b) Plain JSON goes as `{"value": ...}` in the very call that delivers it: no decoder, no other command."""
    engine.__class__ = _OnlyTheCallersWorld
    _binding(engine).op_resolve({"result": arg({"o": [{"k": "a", "v": {"n": 1}}, {"k": "l", "v": {"a": [{"s": "x"}], "id": 2}}], "id": 1})})
    assert [e[:3] for e in engine.log] == [("call", "ctx", server._bindings.DELIVER)]
    assert engine.log[0][3] == [{"objectId": "end-1"}, {"value": 3}, {"value": True}, {"value": {"a": 1, "l": ["x"]}}]


def test_a_result_that_is_not_plain_is_decoded_in_the_binding_s_context_then_delivered_there_then_released(engine):
    """(b) Only a value that is not plain JSON asks the decoder, in the context of the binding (the page's), before the delivery in the same one."""
    engine.__class__ = _OnlyTheCallersWorld
    _binding(engine).op_resolve({"result": arg(DATE)})
    assert [e[:3] if e[0] == "call" else e for e in engine.log] == [
        ("call", "ctx", engine.decoders()[0][2]), ("call", "ctx", server._bindings.DELIVER), ("release", "ctx", "decoded")]
