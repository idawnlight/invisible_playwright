"""Unit tests for Juggler -> Chromium CDP keyboard modifier state and key mapping."""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

import pytest

from invisible_core.juggler import PageActs
from invisible_core.juggler import connection as juggler_conn
from invisible_core.juggler.keyboard import Keyboard
from invisible_playwright._cdp.bridge import CDPBridgeConnection, _PageSessionState


class _CompletedHandle:
    def wait(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        return {}


class _RecordingRawCDP:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any], Optional[str]]] = []

    def send(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        self.calls.append((method, dict(params or {}), session))
        return {}

    def send_async(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session: Optional[str] = None,
    ) -> _CompletedHandle:
        self.calls.append((method, dict(params or {}), session))
        return _CompletedHandle()


def _make_bridge(*session_ids: str) -> Tuple[CDPBridgeConnection, _RecordingRawCDP]:
    bridge = object.__new__(CDPBridgeConnection)
    juggler_conn.EventListeners.__init__(bridge)
    bridge._lock = threading.RLock()
    bridge._raw = _RecordingRawCDP()
    bridge._pages = {
        sid: _PageSessionState(session_id=sid, target_id=f"t-{sid}", context_id=None)
        for sid in session_ids
    }
    bridge._child_to_page = {}
    return bridge, bridge._raw


def test_keyboard_press_control_a_and_control_v_preserve_cdp_modifiers_and_omit_native_vk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("invisible_core.juggler.keyboard._wait", lambda _s: None)
    bridge, raw = _make_bridge("page-1")
    kb = Keyboard(bridge, "page-1", acts=PageActs())

    kb.press("Control+KeyA", dwell_ms=0)
    kb.press("Control+KeyV", dwell_ms=0)

    assert len(raw.calls) == 8
    for method, kp, sid in raw.calls:
        assert method == "Input.dispatchKeyEvent"
        assert sid == "page-1"
        # Regression guard: nativeVirtualKeyCode=86 on Linux X11 maps to KP_Add,
        # turning Ctrl+V into browser zoom-in (Ctrl++) instead of Paste.
        assert "nativeVirtualKeyCode" not in kp

    ctrl_down_a, a_down, a_up, ctrl_up_a, ctrl_down_v, v_down, v_up, ctrl_up_v = [
        kp for _, kp, _ in raw.calls
    ]

    assert ctrl_down_a["type"] == "rawKeyDown"
    assert ctrl_down_a["key"] == "Control"
    assert ctrl_down_a["code"] == "ControlLeft"
    assert ctrl_down_a["location"] == 1
    assert ctrl_down_a["modifiers"] == 2

    assert a_down["type"] == "rawKeyDown"
    assert a_down["key"] == "a"
    assert a_down["code"] == "KeyA"
    assert a_down["windowsVirtualKeyCode"] == 65
    assert a_down["modifiers"] == 2
    assert "text" not in a_down
    assert a_down["unmodifiedText"] == "a"

    assert a_up["type"] == "keyUp"
    assert a_up["key"] == "a"
    assert a_up["modifiers"] == 2

    assert ctrl_up_a["type"] == "keyUp"
    assert ctrl_up_a["key"] == "Control"
    assert ctrl_up_a["modifiers"] == 0

    assert ctrl_down_v["modifiers"] == 2
    assert v_down["type"] == "rawKeyDown"
    assert v_down["key"] == "v"
    assert v_down["code"] == "KeyV"
    assert v_down["windowsVirtualKeyCode"] == 86
    assert v_down["modifiers"] == 2
    assert "text" not in v_down
    assert v_down["unmodifiedText"] == "v"
    assert v_up["modifiers"] == 2
    assert ctrl_up_v["modifiers"] == 0


def test_left_and_right_modifier_keys_track_independently_and_preserve_shifted_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("invisible_core.juggler.keyboard._wait", lambda _s: None)
    bridge, raw = _make_bridge("page-1")
    kb = Keyboard(bridge, "page-1", acts=PageActs())

    kb.down("Shift")
    kb.down("KeyA")
    kb.up("KeyA")
    kb.up("Shift")

    events = [kp for _, kp, _ in raw.calls]
    assert [e["modifiers"] for e in events] == [8, 8, 8, 0]
    assert events[0]["location"] == 1
    assert events[1]["type"] == "keyDown"
    assert events[1]["text"] == "A"
    assert events[1]["unmodifiedText"] == "A"

    # Also verify physical Left/Right slots directly at the bridge layer: releasing
    # ShiftLeft while ShiftRight remains down keeps Shift (8) active until ShiftRight up.
    raw.calls.clear()
    for p in (
        {"type": "keydown", "key": "Shift", "code": "ShiftLeft", "keyCode": 16, "location": 1, "repeat": False},
        {"type": "keydown", "key": "Shift", "code": "ShiftRight", "keyCode": 16, "location": 2, "repeat": False},
        {"type": "keyup", "key": "Shift", "code": "ShiftLeft", "keyCode": 16, "location": 1, "repeat": False},
        {"type": "keydown", "key": "A", "code": "KeyA", "keyCode": 65, "location": 0, "repeat": False, "text": "A"},
        {"type": "keyup", "key": "A", "code": "KeyA", "keyCode": 65, "location": 0, "repeat": False},
        {"type": "keyup", "key": "Shift", "code": "ShiftRight", "keyCode": 16, "location": 2, "repeat": False},
    ):
        bridge.send("Page.dispatchKeyEvent", p, session="page-1")
    slot_events = [kp for _, kp, _ in raw.calls]
    assert [e["modifiers"] for e in slot_events] == [8, 8, 8, 8, 8, 0]
    assert slot_events[0]["location"] == 1
    assert slot_events[1]["location"] == 2
    assert slot_events[3]["text"] == "A"
    assert slot_events[3]["unmodifiedText"] == "A"


def test_all_four_modifier_bits_enter_suppression_numpad_keypad_and_page_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("invisible_core.juggler.keyboard._wait", lambda _s: None)
    bridge, raw = _make_bridge("page-1", "page-2")
    kb1 = Keyboard(bridge, "page-1", acts=PageActs())
    kb2 = Keyboard(bridge, "page-2", acts=PageActs())

    # Plain Enter carries \r text; Control+Enter stays rawKeyDown without \r text.
    kb1.press("Enter", dwell_ms=0)
    kb1.press("Control+Enter", dwell_ms=0)
    # Numpad location=3 maps to isKeypad=True, location=0 (never location=3 == Left|Right).
    kb1.press("NumpadEnter", dwell_ms=0)

    # Holding Alt+Meta+Shift on page-1 must not leak modifiers onto page-2.
    kb1.down("AltLeft")
    kb1.down("MetaLeft")
    kb1.down("ShiftLeft")
    kb1.down("KeyS")
    kb2.press("KeyB", dwell_ms=0)

    p1 = [kp for _, kp, sid in raw.calls if sid == "page-1"]
    p2 = [kp for _, kp, sid in raw.calls if sid == "page-2"]

    enter_down, _enter_up, _ctrl_d, ctrl_enter_down, _ctrl_enter_u, _ctrl_u, numpad_down, _numpad_up = p1[:8]
    assert enter_down["type"] == "keyDown"
    assert enter_down["text"] == "\r"
    assert enter_down["unmodifiedText"] == "\r"
    assert enter_down["modifiers"] == 0

    assert ctrl_enter_down["type"] == "rawKeyDown"
    assert "text" not in ctrl_enter_down
    assert ctrl_enter_down["modifiers"] == 2

    assert numpad_down["type"] == "keyDown"
    assert numpad_down["isKeypad"] is True
    assert numpad_down["location"] == 0

    # Alt(1) | Meta(4) | Shift(8) == 13; non-Shift modifier suppresses text, keeps unmodifiedText.
    alt_meta_shift_s = p1[-1]
    assert alt_meta_shift_s["modifiers"] == (1 | 4 | 8)
    assert alt_meta_shift_s["type"] == "rawKeyDown"
    assert "text" not in alt_meta_shift_s
    assert alt_meta_shift_s["unmodifiedText"] == "S"

    # page-2 stays modifier-clean.
    assert [e["modifiers"] for e in p2] == [0, 0]
    assert p2[0]["type"] == "keyDown"
    assert p2[0]["text"] == "b"

    # Renderer crash resets held modifiers on page-1.
    bridge._handle_cdp_event("page-1", "Inspector.targetCrashed", {})
    raw.calls.clear()
    _, kp_after_crash, _ = bridge._build_cdp_input_command(
        "page-1",
        "Page.dispatchKeyEvent",
        {"type": "keydown", "key": "a", "code": "KeyA", "keyCode": 65, "location": 0, "repeat": False, "text": "a"},
    )
    assert kp_after_crash["modifiers"] == 0
    assert kp_after_crash["type"] == "keyDown"
    assert kp_after_crash["text"] == "a"


def test_unknown_key_event_type_raises_protocol_error() -> None:
    bridge, _ = _make_bridge("page-1")
    with pytest.raises(juggler_conn.ProtocolError, match="Unknown type keypress"):
        bridge._build_cdp_input_command(
            "page-1",
            "Page.dispatchKeyEvent",
            {"type": "keypress", "key": "a", "code": "KeyA", "keyCode": 65},
        )
