"""The argument of `page.evaluate(fn, arg)` and its kin, on its way into the page.

The client sends `{"value": <tagged>, "handles": [{guid}, ...]}`. THE ONE PLACE it is read for the caller's code: `_run_callers_code`, the element-scoped
calls, `wait_for_function` and the result of an exposed function all take it through `Argument`.

⛔ UNTIL THIS MODULE, THE SERVER KEPT A NUMBER, A STRING, A BOOLEAN, AN ARRAY AND AN OBJECT AND MADE EVERYTHING ELSE `null`: a handle (`h`), a date
(`d`), a URL (`u`), a regular expression (`r`), an error (`e`), a typed array (`ta`), a BigInt (`bi`), a reference to an object already seen (`ref`:
a repeat and a cycle), `undefined`, and `-0` (the Python client sends `-0` for EVERY float `0.0`). A bare Python `nan` went into the JSON. The call
went on with the wrong argument and answered.

What stock Playwright 1.61 does (driver bundle `coreBundle.js`, by function name):

* NODE SIDE. `parseArgument` -> `parseSerializedValue` builds the real values; `evaluateExpression` serialises them again with `serializeAsCallArgument`
  (a number, a string and a boolean lose their tag, a Date is `{d: toJSON()}`, which is milliseconds, an Error's stack is normalised) and takes each
  handle through `adoptIfNeeded`. A primitive handle falls through as its value (`fallThrough: handle._value`).
* The handle errors are thrown there: a handle that is not an element and is of another context or world is `JSHandles can be evaluated only in the
  context they were created!`; an element of another frame or world is adopted (`Page.adoptNode`), and if the adoption answers nothing it is `Unable to
  adopt element handle from a different document`; a disposed or unknown guid is `arg.handles[0]: no object with guid <guid>` (the path starts with the
  parameter's name).
* PAGE SIDE. `UtilityScript.evaluate` runs `parseEvaluationResultValue` on every argument, BEFORE it evaluates the caller's text.

HERE, the same two steps. A tree of plain JSON (number, string, boolean, null, array, object; no reference, no handle, nothing special) goes as the
protocol value it always went as. A top-level NaN, Infinity, -Infinity or -0 is the engine's `unserializableValue`; a top-level `undefined` is an
argument with no `value`; a top-level object handle is its `objectId`. EVERYTHING ELSE is decoded in the page by one extra `Runtime.callFunction` in the
same caller context, whose declaration is stock's own `parseEvaluationResultValue` (below, verbatim: a test compares it with the text the core ships
and the bundle's), called with the spec and the handles' objects; what it made is the ONE argument of the unchanged `(f, ...a) => f(...a)` and is
released after the call. The decoder runs before the caller's function and leaves no global.

LIMITS, SAID. A BigInt as the WHOLE argument cannot be decoded here: the decoder would have to return a BigInt, and the protocol's JSON has no BigInt
(inside an array or an object it is fine). A utility-world value that is not a node (the properties `get_property` and `get_property_list` hand back)
cannot cross to the caller's world, which stock never needed because its handles are all of one world.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

from .dispatcher import ProtocolException

#: The text stock throws, node side.
NOT_IN_THE_CONTEXT = "JSHandles can be evaluated only in the context they were created!"
NOT_ADOPTED = "Unable to adopt element handle from a different document"

#: Playwright's `parseEvaluationResultValue`, `base64ToTypedArray` and the typed-array table, VERBATIM from the utility script the core ships
#: (`invisible_core/juggler/injected.js`, the same text as the driver bundle's `packages/isomorphic/utilityScriptSerializers.ts`).
_STOCK = r'''const typedArrayConstructors = {
  i8: Int8Array,
  ui8: Uint8Array,
  ui8c: Uint8ClampedArray,
  i16: Int16Array,
  ui16: Uint16Array,
  i32: Int32Array,
  ui32: Uint32Array,
  // TODO: add Float16Array once it's in baseline
  f32: Float32Array,
  f64: Float64Array,
  bi64: BigInt64Array,
  bui64: BigUint64Array
};
function base64ToTypedArray(base64, TypedArrayConstructor) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++)
    bytes[i] = binary.charCodeAt(i);
  return new TypedArrayConstructor(bytes.buffer);
}
function parseEvaluationResultValue(value, handles = [], refs = /* @__PURE__ */ new Map()) {
  if (Object.is(value, void 0))
    return void 0;
  if (typeof value === "object" && value) {
    if ("ref" in value)
      return refs.get(value.ref);
    if ("v" in value) {
      if (value.v === "undefined")
        return void 0;
      if (value.v === "null")
        return null;
      if (value.v === "NaN")
        return NaN;
      if (value.v === "Infinity")
        return Infinity;
      if (value.v === "-Infinity")
        return -Infinity;
      if (value.v === "-0")
        return -0;
      return void 0;
    }
    if ("d" in value) {
      return new Date(value.d);
    }
    if ("u" in value)
      return new URL(value.u);
    if ("bi" in value)
      return BigInt(value.bi);
    if ("e" in value) {
      const error = new Error(value.e.m);
      error.name = value.e.n;
      error.stack = value.e.s;
      return error;
    }
    if ("r" in value)
      return new RegExp(value.r.p, value.r.f);
    if ("a" in value) {
      const result = [];
      refs.set(value.id, result);
      for (const a of value.a)
        result.push(parseEvaluationResultValue(a, handles, refs));
      return result;
    }
    if ("o" in value) {
      const result = {};
      refs.set(value.id, result);
      for (const { k, v } of value.o) {
        if (k === "__proto__")
          continue;
        result[k] = parseEvaluationResultValue(v, handles, refs);
      }
      return result;
    }
    if ("h" in value)
      return handles[value.h];
    if ("ta" in value)
      return base64ToTypedArray(value.ta.b, typedArrayConstructors[value.ta.k]);
    if ("ab" in value)
      return base64ToTypedArray(value.ab.b, Uint8Array).buffer;
  }
  return value;
}'''

#: The declaration of the one extra call: the spec first, then the objects of the handles, in the order the spec names them (`{"h": i}` is the
#: i-th). Run in the caller's context, by reference.
DECODER = "(spec, ...handles) => {\n" + _STOCK + "\nreturn parseEvaluationResultValue(spec, handles);\n}"

#: THE VALUE TAGS THIS MODULE HANDLES, each one a node `_to_spec` knows. They are the keys of the driver's protocol validator for a serialized value (`scheme.SerializedValue`
#: in `coreBundle.js`: `n b s v d u bi ta e r a o h ref`; `id` only marks an array or an object and is no value) and one more, `ab` (an ArrayBuffer): the validator
#: does not list it, the page side of stock (`parseEvaluationResultValue`, embedded above) decodes it, so it is passed to the decoder as it came. A test reads the
#: validator from the installed driver and compares it with this set.
VALUE_TAGS = ("n", "b", "s", "v", "d", "u", "bi", "ab", "ta", "e", "r", "a", "o", "h", "ref")

_SPECIAL_NUMBERS = ("NaN", "Infinity", "-Infinity", "-0")
_ISO_MILLISECONDS = re.compile(r"^(\d{4,6}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z$")
_BIGINT_REMOTE = re.compile(r"^-?\d+n$")


def split(raw: Any):
    """`(tagged value, handle channels)` of what the client sent: the envelope `{value, handles}`, or a bare tagged value, or nothing."""
    if isinstance(raw, dict) and "value" in raw and "handles" in raw:
        return raw["value"], list(raw.get("handles") or [])
    return raw, []


def refuse_handles(raw: Any, what: str, where: str) -> None:
    """A call whose argument stays a plain value (it goes into the page as JSON, through a command that has no object to carry) says so when
    the caller passed a handle, instead of sending `null` in its place."""
    handles = split(raw)[1]
    if handles:
        raise ProtocolException(
            "%s cannot carry a JSHandle: it is read in %s, where the handle's object does not exist, and used to arrive as null. Pass the "
            "handle's value (json_value) or its properties instead" % (what, where))


def _is_plain(node: Any) -> bool:
    if not isinstance(node, dict):
        return True
    if "ref" in node or "h" in node:
        return False
    for tag in ("n", "s", "b"):
        if tag in node:
            value = node[tag]
            return not (isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))))
    if "v" in node:
        return node["v"] == "null"
    if "a" in node:
        return all(_is_plain(x) for x in node["a"])
    if "o" in node:
        return all(entry["k"] != "__proto__" and _is_plain(entry["v"]) for entry in node["o"])
    return False


def _plain(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    for tag in ("n", "s", "b"):
        if tag in node:
            return node[tag]
    if "v" in node:
        return None
    if "a" in node:
        return [_plain(x) for x in node["a"]]
    return {entry["k"]: _plain(entry["v"]) for entry in node["o"]}


def _milliseconds(iso: Any) -> Any:
    """A date as the node side re-emits it: `Date.prototype.toJSON`, milliseconds, truncated."""
    found = _ISO_MILLISECONDS.match(iso) if isinstance(iso, str) else None
    if not found:
        return iso
    return "%s.%sZ" % (found.group(1), ((found.group(2) or "") + "000")[:3])


def _error(entry: Dict) -> Dict:
    """`{e: {n, m, s}}` as `serializeAsCallArgument` emits it: the stack stays when it starts with `name: message`, else the head is put on it."""
    name, message, stack = entry.get("n"), entry.get("m"), entry.get("s")
    name = "" if name is None else str(name)
    message = "" if message is None else str(message)
    stack = "" if stack is None else str(stack)
    head = name + ": " + message
    return {"e": {"n": name, "m": message, "s": stack if stack.startswith(head) else head + "\n" + stack}}


def _argument_of(remote: Dict) -> Dict:
    """A RemoteObject as the engine's `CallFunctionArgument`."""
    if remote.get("objectId"):
        return {"objectId": remote["objectId"]}
    if "unserializableValue" in remote:
        return {"unserializableValue": remote["unserializableValue"]}
    if "value" in remote:
        return {"value": remote["value"]}
    return {}


class Argument:
    """One argument, resolved against the caller's world `context` of the frame `frame_id` and ready to be the last argument of a call there.

    Built before the caller's code runs (the handle errors are stock's, thrown before the script), so the code never runs on a refused argument. The
    one extra command, the decoder, is made at `call_argument()` and once: a poll asks it every turn and gets the same object. `release()` lets go
    of what this made (the decoded object and the copies of the elements it carried) and nothing of the caller's own handles; it is safe twice."""

    def __init__(self, injected, context: str, frame_id: Optional[str], raw: Any, lookup: Optional[Callable[[str], Any]] = None, *,
                 what: str = "arg", handles_allowed: bool = True) -> None:
        self.injected = injected
        self.context = context
        self.frame_id = frame_id
        self.lookup = lookup
        self.what = what
        self.handles_allowed = handles_allowed
        tagged, self._channels = split(raw)
        self._plain: Any = None
        self._spec: Any = None
        self._root_is_handle = False
        self._objects: List[str] = []
        self._by_guid: Dict[str, Any] = {}
        self._copies: List[str] = []
        self._decoded: Optional[str] = None
        self._argument: Optional[Dict] = None
        self._is_plain = _is_plain(tagged)
        try:
            if self._is_plain:
                self._plain = _plain(tagged)
            else:
                self._root_is_handle = isinstance(tagged, dict) and "h" in tagged and "ref" not in tagged
                self._spec = self._to_spec(tagged)
                if isinstance(self._spec, dict) and set(self._spec) == {"bi"}:
                    raise ProtocolException(
                        "a BigInt as the whole argument cannot be handed to the page: the engine's protocol carries no BigInt. Put it in an "
                        "array or an object, or pass it as a string")
        except BaseException:
            self.release()
            raise

    # ── the spec: what the page side reads ───────────────────────────────────
    def _to_spec(self, node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        if "ref" in node:
            return {"ref": node["ref"]}
        if "n" in node:
            number = node["n"]
            if isinstance(number, float) and number != number:
                return {"v": "NaN"}
            if isinstance(number, float) and number in (float("inf"), float("-inf")):
                return {"v": "Infinity" if number > 0 else "-Infinity"}
            return number
        if "s" in node:
            return node["s"]
        if "b" in node:
            return node["b"]
        if "v" in node:
            return {"v": node["v"]}
        if "d" in node:
            return {"d": _milliseconds(node["d"])}
        if "u" in node:
            return {"u": node["u"]}
        if "bi" in node:
            return {"bi": node["bi"]}
        if "e" in node:
            return _error(node["e"])
        if "r" in node:
            return {"r": {"p": node["r"].get("p"), "f": node["r"].get("f")}}
        if "ta" in node:
            return {"ta": {"b": node["ta"]["b"], "k": node["ta"]["k"]}}
        if "ab" in node:
            return {"ab": node["ab"]}
        if "a" in node:
            spec = {"a": [self._to_spec(x) for x in node["a"]]}
            if "id" in node:
                spec["id"] = node["id"]
            return spec
        if "o" in node:
            spec = {"o": [{"k": entry["k"], "v": self._to_spec(entry["v"])} for entry in node["o"]]}
            if "id" in node:
                spec["id"] = node["id"]
            return spec
        if "h" in node:
            return self._handle(node["h"])
        raise ProtocolException("Attempting to deserialize unexpected value: %r" % (node,))

    # ── the handles ──────────────────────────────────────────────────────────
    def _handle(self, index: int) -> Any:
        """`{"h": position}` (the position among the objects the decoder gets), or the value of a primitive handle."""
        try:
            channel = self._channels[index]
        except (IndexError, TypeError):
            raise ProtocolException("%s.handles[%s]: no object with guid undefined" % (self.what, index)) from None
        guid = channel.get("guid") if isinstance(channel, dict) else None
        if guid not in self._by_guid:
            self._by_guid[guid] = self._resolve(index, guid)
        kind, value = self._by_guid[guid]
        if kind == "inline":
            return value
        return {"h": value}

    def _resolve(self, index: int, guid: Optional[str]):
        if not self.handles_allowed:
            raise ProtocolException(
                "%s carries a JSHandle, which cannot be delivered here: its object lives in the caller's world, and this answer is read in the "
                "page's own" % self.what)
        missing = ProtocolException("%s.handles[%d]: no object with guid %s" % (self.what, index, guid))
        try:
            handle = self.lookup(guid) if self.lookup is not None else None
        except ProtocolException:
            handle = None
        if handle is None or getattr(handle, "disposed", False):
            raise missing
        frame = getattr(handle, "frame", None)
        if frame is None:
            raise missing
        frame_id = frame.frame_id
        if getattr(handle, "world", "utility") == "caller":
            if not handle.object_id:
                return ("inline", self._primitive(handle.remote or {}))
            held_in = getattr(handle, "context", None)
            if frame_id != self.frame_id or (held_in is not None and held_in != self.context):
                raise ProtocolException(NOT_IN_THE_CONTEXT)
            return ("object", self._place(handle.object_id))
        if not getattr(handle, "node", True):
            raise ProtocolException(NOT_IN_THE_CONTEXT)
        if frame_id == self.frame_id:
            copy = self.injected.node_to_caller(self.frame_id, handle.object_id)
        else:
            road = self.injected.adopt(frame_id, handle.object_id, into=self.frame_id)
            if not road:
                raise ProtocolException(NOT_ADOPTED)
            try:
                copy = self.injected.node_to_caller(self.frame_id, road)
            finally:
                self.injected.dispose(self.frame_id, road)
        if not copy:
            raise ProtocolException(NOT_ADOPTED)
        self._copies.append(copy)
        return ("object", self._place(copy))

    def _place(self, object_id: str) -> int:
        self._objects.append(object_id)
        return len(self._objects) - 1

    @staticmethod
    def _primitive(remote: Dict) -> Any:
        """A primitive of the caller's world as a node of the spec: stock's `fallThrough: handle._value`."""
        special = remote.get("unserializableValue")
        if special in _SPECIAL_NUMBERS:
            return {"v": special}
        if isinstance(special, str) and _BIGINT_REMOTE.match(special):
            return {"bi": special[:-1]}
        if "value" in remote:
            value = remote["value"]
            if value is None:
                return {"v": "null"}
            if isinstance(value, float) and value != value:
                return {"v": "NaN"}
            return value
        return {"v": "undefined"}

    # ── the call ─────────────────────────────────────────────────────────────
    def call_argument(self) -> Dict:
        """The engine's `CallFunctionArgument` for this argument. The decoder is called here, once."""
        if self._argument is None:
            self._argument = self._build()
        return self._argument

    def _build(self) -> Dict:
        if self._is_plain:
            return {"value": self._plain}
        spec = self._spec
        if self._root_is_handle and isinstance(spec, dict) and set(spec) == {"h"}:
            return {"objectId": self._objects[spec["h"]]}
        if isinstance(spec, dict) and set(spec) == {"v"}:
            if spec["v"] in _SPECIAL_NUMBERS:
                return {"unserializableValue": spec["v"]}
            return {"value": None} if spec["v"] == "null" else {}
        if not isinstance(spec, dict):
            return {"value": spec}
        decoded = self.injected.call_raw(
            self.context, DECODER, [{"value": spec}] + [{"objectId": o} for o in self._objects], by_value=False)
        if decoded.get("objectId"):
            self._decoded = decoded["objectId"]
        return _argument_of(decoded)

    def release(self) -> None:
        decoded, copies = self._decoded, self._copies
        self._decoded, self._copies = None, []
        if decoded:
            self.injected.release(self.context, decoded)
        for copy in copies:
            self.injected.release(self.context, copy)
