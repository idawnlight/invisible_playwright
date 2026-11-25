"""Unit tests for Chromium CDP target crash handling and Sad Tab recovery."""
from __future__ import annotations

import os
import threading
from typing import Any, Dict, List, Optional, Tuple

import pytest

from invisible_core.juggler import connection as juggler_conn
from invisible_playwright._cdp.bridge import (
    CDPBridgeConnection,
    _PageSessionState,
    _RawCDPPipe,
)


def test_raw_cdp_pipe_fail_sessions_pending_wakes_only_matching_sessions() -> None:
    c_read, p_write = os.pipe()
    p_read, c_write = os.pipe()
    pipe = _RawCDPPipe(p_write, p_read, lambda *_a: None, lambda: None)
    try:
        pending_crashed = pipe.send_async("Runtime.evaluate", {"expression": "1"}, session="sid-dead")
        pending_alive = pipe.send_async("Runtime.evaluate", {"expression": "2"}, session="sid-live")

        pipe.fail_sessions_pending({"sid-dead"}, "Target crashed")

        with pytest.raises(juggler_conn.ProtocolError, match="Runtime.evaluate: Target crashed"):
            pending_crashed.wait(timeout=0.5)

        assert not pending_alive._done.is_set()
        os.write(c_write, b'{"id":2,"result":{"result":{"value":2}}}\x00')
        assert pending_alive.wait(timeout=1.0) == {"result": {"value": 2}}
    finally:
        for fd in (c_read, c_write):
            try:
                os.close(fd)
            except OSError:
                pass
        pipe.close()


class _FakeRawPipe:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any], Optional[str]]] = []
        self.failed_batches: List[Tuple[set[str], str, bool]] = []

    def fail_sessions_pending(
        self, sessions: set[str], reason: str, *, closed: bool = False
    ) -> None:
        self.failed_batches.append((set(sessions), reason, closed))

    def send(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        self.calls.append((method, dict(params or {}), session))
        if method == "Page.navigate":
            return {"loaderId": "loader-recovered"}
        return {}

    def send_async(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session: Optional[str] = None,
    ) -> Any:
        self.calls.append((method, dict(params or {}), session))
        raise AssertionError("Should not reach raw send_async while session is crashed")


def _build_crash_bridge() -> Tuple[CDPBridgeConnection, _FakeRawPipe, List[Tuple[str, Dict[str, Any], Optional[str]]]]:
    bridge = object.__new__(CDPBridgeConnection)
    juggler_conn.EventListeners.__init__(bridge)
    bridge._lock = threading.RLock()
    bridge._ctx_cv = threading.Condition()
    bridge._closed = False
    raw = _FakeRawPipe()
    bridge._raw = raw
    state = _PageSessionState(session_id="page-1", target_id="target-1", context_id="ctx-1")
    state.main_frame_id = "frame-main"
    state.nav_loaders["frame-main"] = "nav-inflight"
    state.held_key_modifiers["ControlLeft"] = 2
    state.child_sessions.add("oopif-1")
    bridge._pages = {"page-1": state}
    bridge._child_to_page = {"oopif-1": state}
    events: List[Tuple[str, Dict[str, Any], Optional[str]]] = []
    bridge.add_listener(lambda m, p, s: events.append((m, dict(p), s)))
    return bridge, raw, events


def test_page_crash_aborts_inflight_rejects_renderer_calls_and_recovers_on_navigate() -> None:
    bridge, raw, events = _build_crash_bridge()

    bridge._handle_cdp_event("page-1", "Inspector.targetCrashed", {})

    assert raw.failed_batches == [({"page-1", "oopif-1"}, "Target crashed", False)]
    assert bridge._pages["page-1"].held_key_modifiers == {}
    assert ("Page.navigationAborted", {"frameId": "frame-main", "navigationId": "nav-inflight", "errorText": "Page crashed"}, "page-1") in events
    assert ("Page.crashed", {}, "page-1") in events

    with pytest.raises(juggler_conn.ProtocolError, match="Target crashed"):
        bridge.send(
            "Page.screenshot",
            {"clip": {"x": 0, "y": 0, "width": 100, "height": 100}},
            session="page-1",
            timeout=1.0,
        )

    with pytest.raises(juggler_conn.ProtocolError, match="Target crashed"):
        bridge.send_async("Page.dispatchKeyEvent", {"type": "keydown", "key": "a"}, session="page-1")

    # Browser-managed navigation clears crashed state so Sad Tab recovery works.
    nav_res = bridge.send(
        "Page.navigate",
        {"url": "https://example.test/", "frameId": "frame-main"},
        session="page-1",
        timeout=1.0,
    )
    assert nav_res == {"navigationId": "loader-recovered"}
    assert "page-1" not in bridge._pages["page-1"].crashed_sessions


def test_oopif_crash_does_not_emit_top_level_page_crashed() -> None:
    bridge, raw, events = _build_crash_bridge()

    bridge._handle_cdp_event("oopif-1", "Inspector.targetCrashed", {})

    assert raw.failed_batches == [({"oopif-1"}, "Target crashed", False)]
    assert all(ev[0] != "Page.crashed" for ev in events)
    assert "page-1" not in bridge._pages["page-1"].crashed_sessions
    assert "oopif-1" in bridge._pages["page-1"].crashed_sessions
