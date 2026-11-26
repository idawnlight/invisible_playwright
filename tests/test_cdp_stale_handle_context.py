"""Unit tests ensuring stale remote object errors never wipe live execution contexts."""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

import pytest

from invisible_core.juggler import connection as juggler_conn
from invisible_core.juggler.injected import (
    CALLER_WORLD,
    InjectedScript,
    UTILITY_WORLD,
)
from invisible_playwright._cdp.bridge import CDPBridgeConnection, _PageSessionState
from invisible_playwright._juggler.dispatcher import Server
from invisible_playwright._juggler.server import ElementHandleDispatcher


class _FakeRawPipe:
    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.calls: List[Tuple[str, Dict[str, Any], Optional[str]]] = []
        self.posts: List[Tuple[str, Dict[str, Any], Optional[str]]] = []

    def send(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        payload = dict(params or {})
        self.calls.append((method, payload, session))
        return self._handler(method, payload, session)

    def post(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session: Optional[str] = None,
        on_result: Any = None,
    ) -> None:
        self.posts.append((method, dict(params or {}), session))


def _build_bridge(handler: Any) -> Tuple[CDPBridgeConnection, _FakeRawPipe, _PageSessionState]:
    bridge = object.__new__(CDPBridgeConnection)
    juggler_conn.EventListeners.__init__(bridge)
    bridge._lock = threading.RLock()
    bridge._ctx_cv = threading.Condition()
    bridge._closed = False
    bridge._intrinsics_ev = "__invpw_intrinsics_test"
    raw = _FakeRawPipe(handler)
    bridge._raw = raw
    state = _PageSessionState(session_id="page-1", target_id="target-1", context_id="ctx-1")
    state.main_frame_id = "frame-main"
    state.frame_sessions["frame-main"] = "page-1"
    bridge._pages = {"page-1": state}
    bridge._child_to_page = {}
    return bridge, raw, state


@pytest.mark.parametrize(
    "cdp_error",
    [
        "Runtime.callFunctionOn: Cannot find object with id",
        "Runtime.callFunctionOn: Argument should belong to the same JavaScript world as target object",
        "Runtime.callFunctionOn: Cannot find context with specified id",
        "Runtime.callFunctionOn: Execution context was destroyed.",
    ],
)
def test_call_function_with_stale_object_preserves_live_caller_context(
    cdp_error: str,
) -> None:
    def _handle(method: str, params: Dict[str, Any], _session: Optional[str]) -> Dict[str, Any]:
        if method == "Runtime.callFunctionOn":
            raise juggler_conn.ProtocolError(cdp_error)
        if method == "Runtime.evaluate" and params.get("contextId") == 42:
            return {"result": {"type": "number", "value": 1}}
        return {}

    bridge, _raw, _state = _build_bridge(_handle)
    injected = InjectedScript(bridge, "page-1")
    bridge._handle_cdp_event(
        "page-1",
        "Runtime.executionContextCreated",
        {
            "context": {
                "id": 42,
                "name": "",
                "auxData": {"frameId": "frame-main", "isDefault": True},
            }
        },
    )
    live_ctx = injected.contexts[("frame-main", CALLER_WORLD)]

    with pytest.raises(juggler_conn.ProtocolError, match=r"Cannot find object with id = stale-doc-a"):
        injected.call_raw(
            live_ctx,
            "(f, ...a) => f(...a)",
            [{"objectId": "fn-doc-b"}, {"objectId": "stale-doc-a"}],
            by_value=True,
        )

    assert injected.contexts.get(("frame-main", CALLER_WORLD)) == live_ctx
    assert injected.caller_context("frame-main", timeout=0.1) == live_ctx


def test_call_function_with_stale_object_preserves_live_utility_context() -> None:
    def _handle(method: str, params: Dict[str, Any], _session: Optional[str]) -> Dict[str, Any]:
        if method == "Runtime.callFunctionOn":
            raise juggler_conn.ProtocolError("Runtime.callFunctionOn: Cannot find object with id")
        if method == "Runtime.evaluate" and params.get("contextId") == 77:
            return {"result": {"type": "number", "value": 1}}
        return {}

    bridge, _raw, _state = _build_bridge(_handle)
    injected = InjectedScript(bridge, "page-1")
    bridge._handle_cdp_event(
        "page-1",
        "Runtime.executionContextCreated",
        {
            "context": {
                "id": 77,
                "name": UTILITY_WORLD,
                "auxData": {"frameId": "frame-main", "isDefault": False},
            }
        },
    )
    live_util = injected.contexts[("frame-main", UTILITY_WORLD)]

    with pytest.raises(juggler_conn.ProtocolError, match=r"Cannot find object with id = stale-node"):
        injected.call_raw(
            live_util,
            "(injected, el) => el.textContent",
            [{"objectId": "injected-script"}, {"objectId": "stale-node"}],
            by_value=True,
        )

    assert injected.contexts.get(("frame-main", UTILITY_WORLD)) == live_util
    assert injected.context_id("frame-main", timeout=0.1) == live_util


def test_call_function_on_dead_context_still_reports_destroyed() -> None:
    # 1. Context was superseded by a newer navigation before Runtime.callFunction ran.
    def _handle_superseded(
        method: str, _params: Dict[str, Any], _session: Optional[str]
    ) -> Dict[str, Any]:
        raise AssertionError(f"Should fail fast without sending {method}")

    bridge, _raw, _state = _build_bridge(_handle_superseded)
    bridge._handle_cdp_event(
        "page-1",
        "Runtime.executionContextCreated",
        {"context": {"id": 10, "name": "", "auxData": {"frameId": "frame-main", "isDefault": True}}},
    )
    old_ctx = bridge._pages["page-1"].caller_contexts["frame-main"]
    bridge._handle_cdp_event(
        "page-1",
        "Runtime.executionContextCreated",
        {"context": {"id": 11, "name": "", "auxData": {"frameId": "frame-main", "isDefault": True}}},
    )
    with pytest.raises(juggler_conn.ProtocolError, match="Execution context was destroyed!"):
        bridge.send(
            "Runtime.callFunction",
            {
                "executionContextId": old_ctx,
                "functionDeclaration": "(f) => f()",
                "args": [{"objectId": "fn-from-10"}],
            },
            session="page-1",
        )

    # 2. Context died inside Chromium before executionContextDestroyed event arrived.
    def _handle_raced(
        method: str, _params: Dict[str, Any], _session: Optional[str]
    ) -> Dict[str, Any]:
        if method == "Runtime.callFunctionOn":
            raise juggler_conn.ProtocolError("Runtime.callFunctionOn: Cannot find object with id")
        if method == "Runtime.evaluate":
            raise juggler_conn.ProtocolError("Runtime.evaluate: Cannot find context with specified id")
        return {}

    bridge2, _raw2, _state2 = _build_bridge(_handle_raced)
    bridge2._handle_cdp_event(
        "page-1",
        "Runtime.executionContextCreated",
        {"context": {"id": 20, "name": "", "auxData": {"frameId": "frame-main", "isDefault": True}}},
    )
    dying_ctx = bridge2._pages["page-1"].caller_contexts["frame-main"]
    with pytest.raises(juggler_conn.ProtocolError, match="Execution context was destroyed!"):
        bridge2.send(
            "Runtime.callFunction",
            {
                "executionContextId": dying_ctx,
                "functionDeclaration": "(f) => f()",
                "args": [{"objectId": "fn-from-20"}],
            },
            session="page-1",
        )


def test_element_handle_op_dispose_never_blocks_when_context_missing_or_superseded() -> None:
    class _FakeInjected:
        def __init__(self) -> None:
            self.contexts: Dict[Tuple[str, str], str] = {
                ("frame-1", CALLER_WORLD): "caller:frame-1:1",
                ("frame-1", UTILITY_WORLD): "101",
            }
            self.released: List[Tuple[str, str]] = []

        def caller_context(self, _frame_id: str, **_kw: Any) -> str:
            raise AssertionError("op_dispose must not block on caller_context()")

        def context_id(self, _frame_id: str, **_kw: Any) -> str:
            raise AssertionError("op_dispose must not block on context_id()")

        def dispose(self, _frame_id: str, _oid: str) -> None:
            raise AssertionError("op_dispose must not block on injected.dispose()")

        def release(self, ctx_id: str, object_id: str) -> None:
            self.released.append((ctx_id, object_id))

    class _FakePage:
        def __init__(self, injected: Any) -> None:
            self.injected = injected

    class _FakeFrame:
        def __init__(self, page: Any) -> None:
            self.guid = "frame@1"
            self.frame_id = "frame-1"
            self.page = page
            self.parent = None
            self._children: List[Any] = []

    server = Server()
    injected = _FakeInjected()
    frame = _FakeFrame(_FakePage(injected))

    # Same-document handles release their remote object non-blockingly.
    h_same_caller = ElementHandleDispatcher(server, frame, "oid-1", world="caller")
    h_same_util = ElementHandleDispatcher(server, frame, "oid-2", world="utility")
    h_same_caller.op_dispose({})
    h_same_util.op_dispose({})
    assert injected.released == [("caller:frame-1:1", "oid-1"), ("101", "oid-2")]

    # Handles whose document navigated to a newer epoch skip releasing against the new context.
    h_nav_caller = ElementHandleDispatcher(server, frame, "oid-3", world="caller")
    h_nav_iso = ElementHandleDispatcher(server, frame, "oid-4", world="isolated")
    injected.contexts[("frame-1", CALLER_WORLD)] = "caller:frame-1:2"
    injected.contexts[("frame-1", UTILITY_WORLD)] = "102"
    injected.released.clear()
    h_nav_caller.op_dispose({})
    h_nav_iso.op_dispose({})
    assert injected.released == []

    # Handles disposed when no context exists return immediately without blocking.
    h_gone_caller = ElementHandleDispatcher(server, frame, "oid-5", world="caller")
    h_gone_util = ElementHandleDispatcher(server, frame, "oid-6", world="utility")
    injected.contexts.clear()
    h_gone_caller.op_dispose({})
    h_gone_util.op_dispose({})
    assert injected.released == []
