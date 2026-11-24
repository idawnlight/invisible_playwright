"""Juggler-compatible connection bridge over Chromium --remote-debugging-pipe.

Allows the entire Playwright dispatcher graph in `_juggler/server.py` and the
shared engines in `invisible_core.juggler` (`Lifecycle`, `InjectedScript`,
`Actions`, `Keyboard`) to drive patched Linux Desktop Chromium over POSIX fd 3/4.
"""
from __future__ import annotations

import base64
import collections
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import queue
import secrets
import signal
import subprocess
import sys
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

from invisible_core.juggler import connection as juggler_conn
from invisible_core.juggler.deadline import Deadline
from invisible_core.juggler.injected import CALLER_WORLD, UTILITY_WORLD

NUL = b"\x00"
_BIND_END_SENTINEL = "__invpw_bind_end__"

_LANDING_BOOTSTRAP_JS = """(() => {
  if (globalThis.__invpwLanding) return true;
  const counts = { mousemove: 0, mousedown: 0, mouseup: 0 };
  const armedSeq = { mousemove: 0, mousedown: 0, mouseup: 0 };
  const ring = { mousemove: [], mousedown: [], mouseup: [] };
  const armed = new Map();
  let nextToken = 0;
  for (const type of ['mousemove', 'mousedown', 'mouseup']) {
    window.addEventListener(type, (ev) => {
      if (!ev.isTrusted) return;
      const seq = ++counts[type];
      const path = typeof ev.composedPath === 'function' ? ev.composedPath() : [];
      const rawTarget = path.length ? path[0] : ev.target;
      const arr = ring[type];
      arr.push({ seq, doc: document, target: rawTarget, path });
      if (arr.length > 16) arr.shift();
    }, { capture: true, passive: true });
  }
  const describe = (t) => {
    if (!t) return 'nothing';
    if (t.nodeType === 3) t = t.parentNode;
    if (!t || !t.tagName) return t && t.nodeName ? t.nodeName : 'nothing';
    const id = t.id ? '#' + t.id : '';
    const cls = t.classList && t.classList.length ? '.' + Array.from(t.classList).join('.') : '';
    return t.tagName.toLowerCase() + id + cls;
  };
  globalThis.__invpwLanding = {
    arm(node, type) {
      const token = ++nextToken;
      if (armed.size > 16) armed.delete(armed.keys().next().value);
      const since = counts[type] || 0;
      const expectedSeq = (armedSeq[type] = Math.max(armedSeq[type] || 0, since) + 1);
      armed.set(token, {
        node,
        doc: node && node.ownerDocument ? node.ownerDocument : document,
        type,
        since,
        expectedSeq
      });
      return token;
    },
    async read(token) {
      const entry = armed.get(token);
      armed.delete(token);
      if (!entry || !entry.node) {
        return { landed: false, on: 'target detached before verdict' };
      }
      const { node, doc, type, since, expectedSeq } = entry;
      if ((counts[type] || 0) < expectedSeq) {
        await new Promise((r) => setTimeout(r, 0));
      }
      if ((counts[type] || 0) <= since) {
        return { landed: false, on: 'no ' + type + ' has reached this document since the landing was armed' };
      }
      const arr = ring[type] || [];
      const hit = arr.find((r) => r.seq === expectedSeq) || arr.find((r) => r.seq > since) || arr[arr.length - 1];
      if (!hit) {
        return { landed: false, on: 'no ' + type + ' has reached the page' };
      }
      if (hit.doc !== doc) {
        return { landed: false, on: 'a previous document; none has reached this one' };
      }
      const t = hit.target && hit.target.nodeType === 3 ? hit.target.parentNode : hit.target;
      const inPath = Array.isArray(hit.path) && hit.path.includes(node);
      const landed = inPath || (!!t && (t === node || (typeof node.contains === 'function' && node.contains(t))));
      return { landed: !!landed, on: landed ? '' : describe(t) };
    }
  };
  return true;
})()"""

_SW_BLOCK_SCRIPT = (
    "(() => {"
    " if (typeof navigator !== 'undefined' && navigator.serviceWorker) {"
    "   Object.defineProperty(navigator.serviceWorker, 'register', {"
    "     configurable: true, writable: true,"
    "     value: function register() { return Promise.reject(new TypeError('Failed to register a ServiceWorker')); }"
    "   });"
    " }"
    "})();"
)

# Juggler bitmask: Alt=1, Control=2, Shift=4, Meta=8
# Chromium CDP bitmask: Alt=1, Control=2, Meta=4, Shift=8
_JUGGLER_TO_CDP_MODS = (
    (1, 1),  # Alt
    (2, 2),  # Control
    (4, 8),  # Shift -> 8
    (8, 4),  # Meta -> 4
)

_CDP_KEY_MODIFIER_BITS = {
    "Alt": 1,
    "Control": 2,
    "Meta": 4,
    "Shift": 8,
}

_CDP_CODE_MODIFIER_BITS = {
    "AltLeft": 1,
    "AltRight": 1,
    "ControlLeft": 2,
    "ControlRight": 2,
    "MetaLeft": 4,
    "MetaRight": 4,
    "OSLeft": 4,
    "OSRight": 4,
    "ShiftLeft": 8,
    "ShiftRight": 8,
}

_MOUSE_BUTTON_NAMES = {
    0: "left",
    1: "middle",
    2: "right",
    "left": "left",
    "middle": "middle",
    "right": "right",
    "none": "none",
}

_ABORT_REASON_MAP = {
    "aborted": "Aborted",
    "accessdenied": "AccessDenied",
    "addressunreachable": "AddressUnreachable",
    "blockedbyclient": "BlockedByClient",
    "blockedbyresponse": "BlockedByResponse",
    "connectionaborted": "ConnectionAborted",
    "connectionclosed": "ConnectionClosed",
    "connectionfailed": "ConnectionFailed",
    "connectionrefused": "ConnectionRefused",
    "connectionreset": "ConnectionReset",
    "internetdisconnected": "InternetDisconnected",
    "namenotresolved": "NameNotResolved",
    "timedout": "TimedOut",
    "failed": "Failed",
}

_JUGGLER_TO_CDP_PERMISSIONS = {
    "geo": "geolocation",
    "geolocation": "geolocation",
    "desktop-notification": "notifications",
    "notifications": "notifications",
    "persistent-storage": "durableStorage",
    "durableStorage": "durableStorage",
    "screen-wake-lock": "wakeLockScreen",
    "wakeLockScreen": "wakeLockScreen",
    "push": "notifications",
    "clipboard-read": "clipboardReadWrite",
    "clipboard-write": "clipboardSanitizedWrite",
}


def _cdp_modifiers(juggler_mask: int) -> int:
    out = 0
    mask = int(juggler_mask or 0)
    for j_bit, c_bit in _JUGGLER_TO_CDP_MODS:
        if mask & j_bit:
            out |= c_bit
    return out


def _format_proxy_spec(
    proxy: Dict[str, Any], *, where: str
) -> tuple[str, Optional[str], Optional[Dict[str, str]]]:
    p_type = str(proxy.get("type") or "http").lower()
    p_host = str(proxy.get("host") or "").strip()
    p_port = proxy.get("port")
    has_creds = bool(proxy.get("username") or proxy.get("password"))
    if p_type in ("socks", "socks5", "socks4") and has_creds:
        raise juggler_conn.ProtocolError(
            f"{where}: Chromium backend does not support SOCKS proxy username/password "
            "authentication; use HTTP/HTTPS proxy authentication or backend='firefox'"
        )
    if not p_host:
        raise juggler_conn.ProtocolError(f"{where}: proxy host cannot be empty")
    scheme = "socks5" if p_type == "socks" else p_type
    server_arg = f"{scheme}://{p_host}:{p_port}" if p_port else f"{scheme}://{p_host}"
    raw_bypass = proxy.get("bypass")
    bypass_str: Optional[str] = None
    if isinstance(raw_bypass, (list, tuple)):
        joined = ",".join(str(x).strip() for x in raw_bypass if str(x).strip())
        bypass_str = joined or None
    elif isinstance(raw_bypass, str) and raw_bypass.strip():
        bypass_str = raw_bypass.strip()
    proxy_auth: Optional[Dict[str, str]] = None
    if proxy.get("username") is not None and proxy.get("username") != "":
        proxy_auth = {
            "username": str(proxy.get("username") or ""),
            "password": str(proxy.get("password") or ""),
        }
    return server_arg, bypass_str, proxy_auth


def _format_console_arg(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {"type": "string", "value": str(raw)}
    rtype = str(raw.get("type") or "object")
    subtype = raw.get("subtype")
    if rtype == "undefined":
        return {"type": "undefined", "preview": "undefined"}
    if subtype == "null":
        return {"type": "object", "subtype": "null", "preview": "null"}
    if rtype == "boolean" and "value" in raw:
        return {"type": "boolean", "preview": "true" if raw["value"] else "false"}
    if "unserializableValue" in raw:
        return {
            "type": rtype,
            "unserializableValue": str(raw["unserializableValue"]),
        }
    if "value" in raw:
        return {"type": rtype, "value": raw["value"]}
    desc = raw.get("description")
    if isinstance(desc, str) and desc:
        return {"type": rtype, "preview": desc}
    return {"type": rtype, "preview": "JSHandle@object" if rtype == "object" else rtype}


def _extract_cdp_location(
    stack_trace: Optional[Dict[str, Any]],
    fallback_url: str = "",
    fallback_line: int = 0,
    fallback_col: int = 0,
) -> Dict[str, Any]:
    frames = (stack_trace or {}).get("callFrames") or []
    if frames and isinstance(frames[0], dict):
        top = frames[0]
        return {
            "url": str(top.get("url") or fallback_url or ""),
            "lineNumber": int(top.get("lineNumber") or 0),
            "columnNumber": int(top.get("columnNumber") or 0),
        }
    return {
        "url": str(fallback_url or ""),
        "lineNumber": int(fallback_line or 0),
        "columnNumber": int(fallback_col or 0),
    }


def _wrap_binding_script(native: str, ev_name: str, script: str) -> str:
    """Wrap `globalThis[native]` before `_bindings.install_script` deletes it.

    Captures the function passed to `send(endFn)` via a one-shot capture
    `CustomEvent` listener on `window` and notifies V8 with a string sentinel,
    leaving zero extra properties on `window` / `globalThis`.
    """
    wrapper = (
        "(() => {"
        f" const raw = globalThis[{json.dumps(native)}];"
        " if (typeof raw !== 'function') return;"
        f" globalThis[{json.dumps(native)}] = function(arg) {{"
        "   if (typeof arg === 'function') {"
        "     const endFn = arg;"
        f"     addEventListener({json.dumps(ev_name)}, (ev) => {{"
        "       ev.stopImmediatePropagation();"
        "       if (ev.detail && typeof ev.detail === 'function') ev.detail(endFn);"
        "     }, { capture: true, once: true });"
        f"     raw({json.dumps(_BIND_END_SENTINEL)});"
        "     return;"
        "   }"
        "   return raw(String(arg));"
        " };"
        " })();\n"
    )
    return wrapper + script


_INTRINSICS_NAMES = (
    "MutationObserver",
    "ResizeObserver",
    "IntersectionObserver",
    "PerformanceObserver",
    "XMLSerializer",
    "DOMParser",
    "CustomEvent",
    "Event",
    "TreeWalker",
    "NodeFilter",
    "Range",
    "getComputedStyle",
    "requestAnimationFrame",
    "cancelAnimationFrame",
    "setTimeout",
    "clearTimeout",
    "setInterval",
    "clearInterval",
    "queueMicrotask",
)
_INTRINSICS_DESTRUCTURE = ", ".join(_INTRINSICS_NAMES)


def _build_intrinsics_bootstrap_js(ev_name: str) -> str:
    fields = ", ".join(
        f"{n}: typeof {n} !== 'undefined' ? {n} : undefined"
        for n in _INTRINSICS_NAMES
    )
    return (
        "(() => {"
        f" const intr = {{ {fields} }};"
        f" addEventListener({json.dumps(ev_name)}, (ev) => {{"
        "   ev.stopImmediatePropagation();"
        "   if (ev && typeof ev.detail === 'function') ev.detail(intr);"
        " }, { capture: true });"
        "})();"
    )


class _PosixSpawnProcess:
    """Popen-compatible process handle around an `os.posix_spawn(setsid=True)` child."""

    def __init__(
        self,
        pid: int,
        stderr_fd: int,
        *,
        guardian_pid: Optional[int] = None,
        guardian_write_fd: int = -1,
    ) -> None:
        self.pid = pid
        self.stderr = os.fdopen(stderr_fd, "rb", buffering=0) if stderr_fd >= 0 else None
        self.returncode: Optional[int] = None
        self._wait_lock = threading.Lock()
        self._guardian_pid = guardian_pid
        self._guardian_write_fd = guardian_write_fd
        self._guardian_lock = threading.Lock()

    def close_guardian(self) -> None:
        with self._guardian_lock:
            wfd = self._guardian_write_fd
            self._guardian_write_fd = -1
            gpid = self._guardian_pid
            self._guardian_pid = None
        if gpid is not None:
            try:
                os.kill(gpid, signal.SIGKILL)
            except OSError:
                pass
        if wfd >= 0:
            try:
                os.close(wfd)
            except OSError:
                pass
        if gpid is not None:
            deadline = time.monotonic() + 2.0
            while True:
                try:
                    wpid, _ = os.waitpid(gpid, os.WNOHANG)
                    if wpid != 0:
                        break
                except ChildProcessError:
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.01)

    def poll(self) -> Optional[int]:
        with self._wait_lock:
            if self.returncode is not None:
                return self.returncode
            try:
                wpid, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                self.returncode = 0
                return self.returncode
            if wpid == 0:
                return None
            self.returncode = os.waitstatus_to_exitcode(status)
            return self.returncode

    def wait(self, timeout: Optional[float] = None) -> int:
        if self.poll() is not None:
            return int(self.returncode or 0)
        deadline = None if timeout is None else (time.monotonic() + timeout)
        while True:
            rc = self.poll()
            if rc is not None:
                return rc
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(cmd="chromium", timeout=timeout or 0.0)
            time.sleep(0.02)

    def terminate(self) -> None:
        if self.poll() is not None:
            return
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except (PermissionError, OSError):
            try:
                os.kill(self.pid, signal.SIGTERM)
            except OSError:
                pass

    def kill(self) -> None:
        if self.poll() is not None:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except OSError:
                pass
            return
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except (PermissionError, OSError):
            try:
                os.kill(self.pid, signal.SIGKILL)
            except OSError:
                pass


def _dup_above_fd(fd: int, floor: int = 5) -> tuple[int, List[int]]:
    """Ensure `fd >= floor` so `POSIX_SPAWN_DUP2` onto 0..4 never overwrites a source fd."""
    spares: List[int] = []
    cur = fd
    while cur < floor:
        nxt = os.dup(cur)
        if cur != fd:
            spares.append(cur)
        cur = nxt
    return cur, spares


class _StderrDrain:
    """Compatible stand-in for `juggler.connection.ProcessOutput`."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._lines: List[str] = []
        self._cv = threading.Condition()
        self._eof = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        try:
            for raw in iter(self._stream.readline, b""):
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                with self._cv:
                    if text.strip():
                        self._lines.append(text)
                        if len(self._lines) > 200:
                            del self._lines[:-200]
                    self._cv.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self._cv:
                self._eof = True
                self._cv.notify_all()

    def settle(self) -> None:
        end = time.monotonic() + 0.5
        with self._cv:
            while not self._eof and time.monotonic() < end:
                self._cv.wait(0.05)

    def tail(self, n: int = 15) -> List[str]:
        with self._cv:
            return list(self._lines)[-n:]


class _RawPending:
    """Handle for an in-flight raw CDP command written via `_RawCDPPipe.send_async`."""

    __slots__ = ("_pipe", "method", "_msg_id", "_done", "_box")

    def __init__(
        self,
        pipe: "_RawCDPPipe",
        method: str,
        msg_id: int,
        done: threading.Event,
        box: List[Any],
    ) -> None:
        self._pipe = pipe
        self.method = method
        self._msg_id = msg_id
        self._done = done
        self._box = box

    def wait(self, timeout: Optional[float] = 30.0) -> Dict[str, Any]:
        if not self._done.wait(timeout=timeout):
            with self._pipe._lock:
                self._pipe._pending.pop(self._msg_id, None)
            raise juggler_conn.ReplyTimeout(
                f"{self.method} timed out after {timeout:.1f}s"
            )
        kind, payload = self._box[0]
        if kind == "closed":
            raise juggler_conn.TargetClosedError(str(payload))
        if kind == "error":
            err_msg = payload.get("message") or str(payload)
            err_data = payload.get("data")
            if err_data:
                err_msg = f"{err_msg} ({err_data})"
            if any(
                phrase in err_msg
                for phrase in (
                    "Session with given id not found",
                    "Target closed",
                )
            ):
                raise juggler_conn.TargetClosedError(f"{self.method}: {err_msg}")
            raise juggler_conn.ProtocolError(f"{self.method}: {err_msg}")
        return payload or {}


class _CDPPendingReply:
    """Juggler `PendingReply`-compatible handle returned by `CDPBridgeConnection.send_async`."""

    __slots__ = ("_conn", "_raw_pending", "_transform", "_done", "_box")

    def __init__(
        self,
        conn: "CDPBridgeConnection",
        *,
        raw_pending: Optional[_RawPending] = None,
        transform: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        done: Optional[threading.Event] = None,
        box: Optional[List[Any]] = None,
    ) -> None:
        self._conn = conn
        self._raw_pending = raw_pending
        self._transform = transform
        self._done = done
        self._box = box

    def wait(self, timeout: Optional[float] = 30.0) -> tuple[Dict[str, Any], int]:
        if self._raw_pending is not None:
            raw_res = self._raw_pending.wait(timeout=timeout)
            res = self._transform(raw_res) if self._transform is not None else {}
            return res, self._conn.delivered
        assert self._done is not None and self._box is not None
        if not self._done.wait(timeout=timeout):
            raise juggler_conn.ReplyTimeout(
                f"Async CDP command timed out after {timeout:.1f}s"
            )
        ok, value = self._box[0]
        if not ok:
            raise value
        return value, self._conn.delivered


class _RawCDPPipe:
    """Low-level NUL-delimited JSON transport to Chromium fd 3 / fd 4."""

    def __init__(
        self,
        write_fd: int,
        read_fd: int,
        on_event: Any,
        on_closed: Any,
    ) -> None:
        self._write_fd = write_fd
        self._read_fd = read_fd
        self._on_event = on_event
        self._on_closed = on_closed
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: Dict[
            int, tuple[threading.Event, List[Any], str, Optional[str]]
        ] = {}
        self._callbacks: Dict[int, Callable[[Dict[str, Any]], None]] = {}
        self.closed = False
        self._reader = threading.Thread(
            target=self._read_loop, name="invpw-cdp-reader", daemon=True
        )
        self._reader.start()

    def post(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session: Optional[str] = None,
        on_result: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        with self._lock:
            if self.closed:
                return
            self._next_id += 1
            msg_id = self._next_id
            if on_result is not None:
                self._callbacks[msg_id] = on_result
        try:
            self._write_frame(msg_id, method, params, session)
        except OSError:
            if on_result is not None:
                with self._lock:
                    self._callbacks.pop(msg_id, None)

    def send_async(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session: Optional[str] = None,
    ) -> _RawPending:
        done = threading.Event()
        box: List[Any] = []
        with self._lock:
            if self.closed:
                raise juggler_conn.TargetClosedError(
                    f"Target page, context or browser has been closed ({method})"
                )
            self._next_id += 1
            msg_id = self._next_id
            self._pending[msg_id] = (done, box, method, session)

        try:
            self._write_frame(msg_id, method, params, session)
        except OSError as exc:
            with self._lock:
                self._pending.pop(msg_id, None)
            raise juggler_conn.TargetClosedError(
                f"Browser pipe closed while sending {method}: {exc}"
            ) from exc

        return _RawPending(self, method, msg_id, done, box)

    def send(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session: Optional[str] = None,
        timeout: Optional[float] = 30.0,
    ) -> Dict[str, Any]:
        return self.send_async(method, params, session=session).wait(timeout=timeout)

    def _write_frame(
        self,
        msg_id: int,
        method: str,
        params: Optional[Dict[str, Any]],
        session: Optional[str],
    ) -> None:
        msg: Dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            msg["params"] = params
        if session:
            msg["sessionId"] = session
        encoded = json.dumps(msg, separators=(",", ":")).encode("utf-8") + NUL
        with self._write_lock:
            if self._write_fd < 0:
                raise OSError("pipe already closed")
            view = memoryview(encoded)
            while view:
                written = os.write(self._write_fd, view)
                view = view[written:]

    def _read_loop(self) -> None:
        buf = bytearray()
        try:
            while not self.closed:
                chunk = os.read(self._read_fd, 65536)
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    idx = buf.find(0)
                    if idx < 0:
                        break
                    raw = bytes(buf[:idx])
                    del buf[: idx + 1]
                    if raw:
                        self._handle_raw(raw)
        except OSError:
            pass
        finally:
            try:
                if self._read_fd >= 0:
                    os.close(self._read_fd)
                    self._read_fd = -1
            except OSError:
                pass
            self._fail_pending("Browser connection closed")
            self._on_closed()

    def _handle_raw(self, raw: bytes) -> None:
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            return
        msg_id = msg.get("id")
        if msg_id is not None:
            with self._lock:
                waiter = self._pending.pop(msg_id, None)
                cb = self._callbacks.pop(msg_id, None)
            if waiter is not None:
                done, box, _, _ = waiter
                if "error" in msg:
                    box.append(("error", msg["error"]))
                else:
                    box.append(("ok", msg.get("result") or {}))
                done.set()
            elif cb is not None and "error" not in msg:
                try:
                    cb(msg.get("result") or {})
                except Exception:
                    pass
            return
        method = msg.get("method")
        if method:
            self._on_event(msg.get("sessionId"), method, msg.get("params") or {})

    def resolve_pending_inputs(self, session: Optional[str]) -> None:
        if not session:
            return
        matched: List[tuple[threading.Event, List[Any]]] = []
        with self._lock:
            for mid, (done, box, mname, sid) in list(self._pending.items()):
                if sid == session and mname.startswith("Input."):
                    self._pending.pop(mid, None)
                    matched.append((done, box))
        for done, box in matched:
            if not box:
                box.append(("ok", {}))
            done.set()

    def _fail_pending(self, reason: str) -> None:
        with self._lock:
            self.closed = True
            pending = list(self._pending.values())
            self._pending.clear()
            self._callbacks.clear()
        for done, box, _, _ in pending:
            if not box:
                box.append(("closed", reason))
            done.set()

    def close(self) -> None:
        with self._write_lock:
            if self._write_fd >= 0:
                try:
                    os.close(self._write_fd)
                except OSError:
                    pass
                self._write_fd = -1
        self._fail_pending("Browser connection closed")


@dataclass
class _ContextState:
    context_id: Optional[str]
    cdp_context_id: Optional[str] = None
    dispose_on_detach: bool = True
    has_created_pages: bool = False
    proxy_server: Optional[str] = None
    proxy_bypass: Optional[str] = None
    proxy_auth: Optional[Dict[str, str]] = None
    viewport: Optional[Dict[str, Any]] = None
    timezone_id: Optional[str] = None
    locale: Optional[str] = None
    accept_languages: Optional[str] = None
    color_scheme: Optional[str] = None
    reduced_motion: Optional[str] = None
    forced_colors: Optional[str] = None
    user_agent: Optional[str] = None
    extra_headers: Dict[str, str] = field(default_factory=dict)
    offline: bool = False
    geolocation: Optional[Dict[str, Any]] = None
    http_credentials: Optional[Dict[str, str]] = None
    ignore_https_errors: bool = False
    bypass_csp: bool = False
    js_disabled: bool = False
    service_workers_blocked: bool = False
    has_touch: bool = False
    cache_disabled: bool = False
    intercepting: bool = False
    init_scripts: List[str] = field(default_factory=list)
    download_options: Optional[Dict[str, Any]] = None
    # native -> (wrapped_script, ev_name)
    bindings: Dict[str, tuple[str, str]] = field(default_factory=dict)


@dataclass
class _PageSessionState:
    session_id: str
    target_id: str
    context_id: Optional[str]
    main_frame_id: str = ""
    dpr: float = 1.0
    intercepting: bool = False
    applied_offline: Dict[str, bool] = field(default_factory=dict)
    applied_cache_disabled: Dict[str, bool] = field(default_factory=dict)
    applied_ignore_https: Dict[str, bool] = field(default_factory=dict)
    applied_js_disabled: Dict[str, bool] = field(default_factory=dict)
    applied_sw_bypass: Dict[str, bool] = field(default_factory=dict)
    fetch_enabled_sessions: set[str] = field(default_factory=set)
    page_init_scripts: List[Dict[str, Any]] = field(default_factory=list)
    # native -> (wrapped_script, ev_name)
    page_bindings: Dict[str, tuple[str, str]] = field(default_factory=dict)
    installed_raw_bindings: Dict[str, set[str]] = field(default_factory=dict)
    runtime_enabled_sessions: set[str] = field(default_factory=set)
    utility_world_sessions: set[str] = field(default_factory=set)
    installed_doc_scripts: Dict[str, List[str]] = field(default_factory=dict)
    # OOPIF child CDP session ids attached beneath this page
    child_sessions: set[str] = field(default_factory=set)
    # Dedicated worker CDP session ids attached beneath this page or its OOPIFs
    worker_sessions: set[str] = field(default_factory=set)
    # worker_sid -> owning frame_id for requests initiated inside that worker
    worker_frames: Dict[str, str] = field(default_factory=dict)
    # child_sid -> root frame_id of that OOPIF session
    session_root_frame: Dict[str, str] = field(default_factory=dict)
    # frame_id -> CDP session_id owning that frame (defaults to session_id)
    frame_sessions: Dict[str, str] = field(default_factory=dict)
    # utility_ctx_id -> CDP session_id
    ctx_sessions: Dict[str, str] = field(default_factory=dict)
    # objectId -> CDP session_id
    obj_sessions: Dict[str, str] = field(default_factory=dict)
    # requestId -> CDP session_id
    req_sessions: Dict[str, str] = field(default_factory=dict)
    # requestId -> (frame_id, is_navigation)
    req_frames: Dict[str, tuple[str, bool]] = field(default_factory=dict)
    # requestId -> exact Blink ResourceType ("Fetch" vs "XHR") from Network.requestWillBeSent
    req_types: Dict[str, str] = field(default_factory=dict)
    sent_requests: set[str] = field(default_factory=set)
    unpaused_requests: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    deferred_paused: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    responded_requests: set[str] = field(default_factory=set)
    finished_requests: set[str] = field(default_factory=set)
    completed_req_order: "collections.deque[str]" = field(
        default_factory=collections.deque
    )
    had_extra_headers: set[str] = field(default_factory=set)
    # (session_id, utility_ctx_id) where __invpwLanding is already bootstrapped
    landing_ready: set[tuple[str, str]] = field(default_factory=set)
    # (session_id, int(utility_ctx_id)) internal contexts excluded from user console/pageerror
    utility_context_keys: set[tuple[str, int]] = field(default_factory=set)
    # (session_id, int(v8_ctx_id)) -> juggler executionContextId (utility or caller)
    session_v8_to_juggler: Dict[tuple[str, int], str] = field(default_factory=dict)
    # auth loop guard: f"{networkId}:{source}" already answered with credentials once
    attempted_auth: set[str] = field(default_factory=set)
    # frame_id -> utility executionContextId (numeric int as str)
    utility_contexts: Dict[str, str] = field(default_factory=dict)
    # frame_id -> caller synthetic executionContextId ("caller:<fid>:<epoch>")
    caller_contexts: Dict[str, str] = field(default_factory=dict)
    # caller_ctx -> cached window objectId in that frame's main world
    caller_windows: Dict[str, str] = field(default_factory=dict)
    # caller_ctx -> (session_id, V8 main-world int executionContextId)
    caller_v8_contexts: Dict[str, tuple[str, int]] = field(default_factory=dict)
    # caller_ctx -> RemoteObject objectId of pristine native constructors captured at DidClearWindowObject
    caller_intrinsics: Dict[str, str] = field(default_factory=dict)
    # (session_id, V8 main-world int executionContextId) -> frame_id
    main_world_to_frame: Dict[tuple[str, int], str] = field(default_factory=dict)
    # CDP Fetch requestId <-> Network requestId tracking
    paused_requests: Dict[str, str] = field(default_factory=dict)
    paused_sessions: Dict[str, str] = field(default_factory=dict)
    pending_fetch_events: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    attachment_requests: set[str] = field(default_factory=set)
    nav_loaders: Dict[str, str] = field(default_factory=dict)
    screencast_ack_ids: "collections.deque[int]" = field(
        default_factory=lambda: collections.deque(maxlen=64)
    )
    # Physical code/key -> active CDP modifier bit (Alt=1, Control=2, Meta=4, Shift=8)
    held_key_modifiers: Dict[str, int] = field(default_factory=dict)
    ready_emitted: bool = False
    dialog_open: bool = False
    window_opened_seq: int = 0
    epoch: int = 0


class CDPBridgeConnection(juggler_conn.EventListeners):
    """Translates Juggler commands/events onto Chromium Pipe CDP."""

    def __init__(
        self,
        process: Any,
        write_fd: int,
        read_fd: int,
        *,
        ready_timeout: Optional[float] = 60.0,
        proxy_auth: Optional[Dict[str, str]] = None,
        proxy_server: Optional[str] = None,
        persistent: bool = False,
    ) -> None:
        super().__init__()
        self._process = process
        self.output = _StderrDrain(process.stderr) if process.stderr else None
        self._startup = Deadline(ready_timeout)
        self._proxy_auth = proxy_auth
        self._launch_proxy_server = proxy_server
        self._persistent = persistent
        self._lock = threading.RLock()
        self._ctx_cv = threading.Condition()
        self._contexts: Dict[Optional[str], _ContextState] = {
            None: _ContextState(context_id=None, cdp_context_id=None)
        }
        self._cdp_to_juggler_cid: Dict[str, str] = {}
        self._pages: Dict[str, _PageSessionState] = {}
        self._child_to_page: Dict[str, _PageSessionState] = {}
        self._target_to_session: Dict[str, str] = {}
        self._target_init_errors: Dict[str, Exception] = {}
        self._event_errors: "collections.deque[str]" = collections.deque(maxlen=32)
        self._initial_blank_targets: set[str] = set()
        self._seen_downloads: set[str] = set()
        self._finished_downloads: set[str] = set()
        self._download_ctx_by_guid: Dict[str, Optional[str]] = {}
        self._adopt_started = False
        self._closed = False
        self._close_called = False
        self._version_product = ""
        self._user_agent = ""
        self._intrinsics_ev = f"__invpw_intr_{secrets.token_hex(16)}"
        self._intrinsics_bootstrap = _build_intrinsics_bootstrap_js(
            self._intrinsics_ev
        )

        self._ev_queue: "queue.Queue[Any]" = queue.Queue()
        self._ev_worker = threading.Thread(
            target=self._event_worker_loop, name="invpw-cdp-events", daemon=True
        )
        self._ev_worker.start()

        self._raw = _RawCDPPipe(
            write_fd=write_fd,
            read_fd=read_fd,
            on_event=self._enqueue_cdp_event,
            on_closed=self._on_pipe_closed,
        )

        ver = self._raw.send("Browser.getVersion", timeout=self._startup.left())
        self._version_product = ver.get("product") or ""
        self._user_agent = ver.get("userAgent") or ""

    @classmethod
    def launch(
        cls,
        executable: str | Path,
        profile_dir: str | Path,
        *,
        env: Dict[str, str],
        argv_extra: List[str],
        ready_timeout: Optional[float],
        proxy: Optional[Dict[str, Any]],
        persistent: bool = False,
        no_sandbox: bool = False,
        window_size: tuple[int, int] = (1280, 800),
    ) -> "CDPBridgeConnection":
        proxy_server_arg: Optional[str] = None
        proxy_bypass_str: Optional[str] = None
        proxy_auth: Optional[Dict[str, str]] = None
        if proxy:
            proxy_server_arg, proxy_bypass_str, proxy_auth = _format_proxy_spec(
                proxy, where="launch(proxy=...)"
            )

        profile_path = Path(profile_dir)
        default_dir = profile_path / "Default"
        default_dir.mkdir(parents=True, exist_ok=True)
        prefs_file = default_dir / "Preferences"
        prefs_data: Dict[str, Any] = {}
        if prefs_file.is_file():
            try:
                prefs_data = json.loads(prefs_file.read_text(encoding="utf-8"))
            except Exception:
                prefs_data = {}
        prefs_data.setdefault("credentials_enable_service", False)
        prefs_data.setdefault("profile", {})["password_manager_enabled"] = False
        if proxy:
            prefs_data.setdefault("webrtc", {})[
                "ip_handling_policy"
            ] = "disable_non_proxied_udp"
        prefs_file.write_text(json.dumps(prefs_data), encoding="utf-8")

        merged_extra: List[str] = []
        merged_disable_features = False
        for item in argv_extra:
            if item.startswith("--disable-features="):
                raw_feats = [
                    f.strip()
                    for f in item.split("=", 1)[1].split(",")
                    if f.strip()
                ]
                if "PaintHolding" not in raw_feats:
                    raw_feats.append("PaintHolding")
                merged_extra.append(f"--disable-features={','.join(raw_feats)}")
                merged_disable_features = True
            else:
                merged_extra.append(item)

        w, h = window_size
        args = [
            str(executable),
            f"--user-data-dir={profile_path}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            "--allow-pre-commit-input",
            "--no-startup-window",
            "--remote-debugging-pipe",
            f"--window-size={w},{h}",
            "--ozone-platform=x11",
            "--use-gl=angle",
            "--use-angle=gl",
        ]
        if not merged_disable_features:
            args.append("--disable-features=PaintHolding")
        if no_sandbox:
            args.append("--no-sandbox")

        if proxy_server_arg:
            args.append(f"--proxy-server={proxy_server_arg}")
            if proxy_bypass_str:
                args.append(f"--proxy-bypass-list={proxy_bypass_str}")
            args.append(
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"
            )

        args.extend(merged_extra)

        launch_env = dict(env)
        if (
            not Path("/dev/dri/renderD128").exists()
            and "LIBGL_ALWAYS_SOFTWARE" not in launch_env
        ):
            launch_env["LIBGL_ALWAYS_SOFTWARE"] = "1"

        c_read_fd, p_write_fd = os.pipe()
        p_read_fd, c_write_fd = os.pipe()
        err_read_fd, err_write_fd = os.pipe()
        spares: List[int] = []

        try:
            r_src, s1 = _dup_above_fd(c_read_fd, 5)
            spares.extend(s1)
            w_src, s2 = _dup_above_fd(c_write_fd, 5)
            spares.extend(s2)
            e_src, s3 = _dup_above_fd(err_write_fd, 5)
            spares.extend(s3)

            file_actions = [
                (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
                (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0),
                (os.POSIX_SPAWN_DUP2, e_src, 2),
                (os.POSIX_SPAWN_DUP2, r_src, 3),
                (os.POSIX_SPAWN_DUP2, w_src, 4),
            ]
            pid = os.posix_spawn(
                str(executable),
                args,
                launch_env,
                file_actions=file_actions,
                setsid=True,
            )
            guardian_pid: Optional[int] = None
            life_w = -1
            if os.path.exists("/bin/sh"):
                life_r, life_w = os.pipe()
                try:
                    lr_src, gs = _dup_above_fd(life_r, 3)
                    g_script = (
                        f"reap() {{ trap '' TERM INT HUP; kill -KILL -{pid} 2>/dev/null || true; "
                        f"i=0; while kill -0 -{pid} 2>/dev/null && [ \"$i\" -lt 250 ]; do "
                        f"sleep 0.01 2>/dev/null || sleep 1; i=$((i + 1)); done; exit 0; }}; "
                        f"trap reap TERM INT HUP; IFS= read -r _ || true; reap"
                    )
                    guardian_pid = os.posix_spawn(
                        "/bin/sh",
                        ["/bin/sh", "-c", g_script],
                        launch_env,
                        file_actions=[
                            (os.POSIX_SPAWN_DUP2, lr_src, 0),
                            (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0),
                            (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0),
                        ],
                        setsid=True,
                    )
                except BaseException:
                    if life_w >= 0:
                        try:
                            os.close(life_w)
                        except OSError:
                            pass
                        life_w = -1
                finally:
                    for gfd in {life_r, *(gs if "gs" in locals() else [])}:
                        try:
                            os.close(gfd)
                        except OSError:
                            pass
                    if "lr_src" in locals() and lr_src != life_r:
                        try:
                            os.close(lr_src)
                        except OSError:
                            pass
            proc: Any = _PosixSpawnProcess(
                pid,
                err_read_fd,
                guardian_pid=guardian_pid,
                guardian_write_fd=life_w,
            )
            err_read_fd = -1
        except BaseException:
            os.close(p_write_fd)
            os.close(p_read_fd)
            if err_read_fd >= 0:
                os.close(err_read_fd)
            raise
        finally:
            for fd in {c_read_fd, c_write_fd, err_write_fd, *spares}:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if "r_src" in locals() and r_src not in {c_read_fd, *spares}:
                try:
                    os.close(r_src)
                except OSError:
                    pass
            if "w_src" in locals() and w_src not in {c_write_fd, *spares}:
                try:
                    os.close(w_src)
                except OSError:
                    pass
            if "e_src" in locals() and e_src not in {err_write_fd, *spares}:
                try:
                    os.close(e_src)
                except OSError:
                    pass

        try:
            return cls(
                proc,
                write_fd=p_write_fd,
                read_fd=p_read_fd,
                ready_timeout=ready_timeout,
                proxy_auth=proxy_auth,
                proxy_server=proxy_server_arg,
                persistent=persistent,
            )
        except BaseException:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
            proc.close_guardian()
            raise

    @property
    def version_number(self) -> str:
        if "/" in self._version_product:
            return self._version_product.split("/", 1)[1].strip()
        return self._version_product

    def startup_send(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return self.send(method, params, timeout=self._startup.left())

    def _session_for_frame(
        self, state: _PageSessionState, frame_id: Optional[str]
    ) -> str:
        if frame_id:
            sid = state.frame_sessions.get(frame_id)
            if sid:
                return sid
        return state.session_id

    def _parse_util_ctx(
        self, state: _PageSessionState, ctx_id: str
    ) -> tuple[str, int]:
        if ctx_id.startswith("caller:"):
            v8_pair = state.caller_v8_contexts.get(ctx_id)
            if v8_pair is not None:
                return v8_pair
            raise juggler_conn.ProtocolError("Execution context was destroyed!")
        if ":" in ctx_id:
            sid_part, int_part = ctx_id.rsplit(":", 1)
            return sid_part, int(int_part)
        sid = state.ctx_sessions.get(ctx_id) or state.session_id
        return sid, int(ctx_id)

    def _session_for_object(
        self, state: _PageSessionState, object_id: Optional[str]
    ) -> str:
        if object_id:
            sid = state.obj_sessions.get(object_id)
            if sid:
                return sid
        return state.session_id

    def _record_result_object(
        self, state: _PageSessionState, sid: str, obj: Optional[Dict[str, Any]]
    ) -> None:
        if isinstance(obj, dict):
            oid = obj.get("objectId")
            if isinstance(oid, str) and oid:
                state.obj_sessions[oid] = sid

    def post(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session: Optional[str] = None,
    ) -> None:
        if method == "Runtime.disposeObject" and params and params.get("objectId"):
            oid = params["objectId"]
            target_sid = session
            if session:
                with self._lock:
                    st = self._pages.get(session)
                    if st is not None:
                        target_sid = st.obj_sessions.pop(oid, session)
            self._raw.post(
                "Runtime.releaseObject",
                {"objectId": oid},
                session=target_sid,
            )
            return
        if method == "Page.stopScreencast":
            self._raw.post("Page.stopScreencast", {}, session=session)
            return
        if method == "Page.screencastFrameAck":
            if session:
                with self._lock:
                    st = self._pages.get(session)
                    ack_id = (
                        st.screencast_ack_ids.popleft()
                        if (st is not None and st.screencast_ack_ids)
                        else None
                    )
                if ack_id is not None:
                    self._raw.post(
                        "Page.screencastFrameAck",
                        {"sessionId": ack_id},
                        session=session,
                    )
            return
        try:
            self.send(method, params, session=session, timeout=5.0)
        except juggler_conn.TargetClosedError:
            pass
        except Exception as exc:
            self._record_bridge_error(f"post({method})", exc)

    def _arm_mouse_landing(
        self,
        state: _PageSessionState,
        j_ev_type: str,
        lands_spec: Any,
    ) -> Optional[tuple[str, int, str, Optional[str], int]]:
        if not isinstance(lands_spec, dict):
            return None
        fid = str(lands_spec.get("frameId") or state.main_frame_id)
        oid = lands_spec.get("objectId")
        if not oid:
            return None
        util_ctx = state.utility_contexts.get(fid)
        if not util_ctx:
            return None
        util_sid, util_int = self._parse_util_ctx(state, util_ctx)
        nav_before = state.nav_loaders.get(fid)
        win_open_before = state.window_opened_seq
        try:
            if (util_sid, util_ctx) not in state.landing_ready:
                self._raw.send(
                    "Runtime.evaluate",
                    {
                        "expression": _LANDING_BOOTSTRAP_JS,
                        "contextId": util_int,
                        "returnByValue": True,
                    },
                    session=util_sid,
                    timeout=5.0,
                )
                state.landing_ready.add((util_sid, util_ctx))
            arm_res = self._raw.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": oid,
                    "functionDeclaration": (
                        "function(evType) {"
                        "  return globalThis.__invpwLanding"
                        "    ? globalThis.__invpwLanding.arm(this, evType)"
                        "    : 0;"
                        "}"
                    ),
                    "arguments": [{"value": j_ev_type}],
                    "returnByValue": True,
                },
                session=util_sid,
                timeout=5.0,
            )
            token = int(((arm_res.get("result") or {}).get("value")) or 0)
            if token > 0:
                return (util_sid, util_int, fid, nav_before, win_open_before * 1000000 + token)
        except (juggler_conn.TargetClosedError, juggler_conn.ProtocolError):
            return None
        return None

    def _read_mouse_landing(
        self,
        state: _PageSessionState,
        armed_info: Optional[tuple[str, int, str, Optional[str], int]],
    ) -> Optional[Dict[str, Any]]:
        if state.dialog_open or self._closed:
            return None
        with self._lock:
            if state.session_id not in self._pages:
                return None
        if armed_info is None:
            return None
        util_sid, util_int, fid, nav_before, packed = armed_info
        win_open_before = packed // 1000000
        token = packed % 1000000
        if state.window_opened_seq != win_open_before:
            return None
        if state.nav_loaders.get(fid) != nav_before:
            return None
        try:
            res = self._raw.send(
                "Runtime.evaluate",
                {
                    "expression": f"globalThis.__invpwLanding && globalThis.__invpwLanding.read({token})",
                    "contextId": util_int,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
                session=util_sid,
                timeout=5.0,
            )
        except (juggler_conn.TargetClosedError, juggler_conn.ProtocolError):
            return None
        if state.dialog_open or state.nav_loaders.get(fid) != nav_before:
            return None
        verdict = (res.get("result") or {}).get("value")
        if isinstance(verdict, dict) and "landed" in verdict:
            return {
                "landed": bool(verdict.get("landed")),
                "on": str(verdict.get("on") or ""),
            }
        return None

    def _build_cdp_input_command(
        self, session_id: str, method: str, p: Dict[str, Any]
    ) -> Optional[tuple[str, Dict[str, Any], Optional[Callable[[Dict[str, Any]], Dict[str, Any]]]]]:
        with self._lock:
            state = self._pages.get(session_id)
        if state is None:
            raise juggler_conn.TargetClosedError(
                f"Target page has been closed ({method})"
            )

        raw_latency = p.get("latency")
        if raw_latency is None:
            raw_latency = p.get("latencyMs")
        latency_ms = float(raw_latency or 0.0)
        ts = time.time() - (latency_ms / 1000.0 if latency_ms > 0 else 0.0)

        if method == "Page.dispatchMouseEvent":
            j_ev_type = str(p.get("type") or "mousemove")
            ev_type = {
                "mousemove": "mouseMoved",
                "mousedown": "mousePressed",
                "mouseup": "mouseReleased",
            }.get(j_ev_type, "mouseMoved")
            btn = _MOUSE_BUTTON_NAMES.get(p.get("button", 0), "left")
            if ev_type == "mouseMoved":
                btn = "none"
            mp: Dict[str, Any] = {
                "type": ev_type,
                "x": float(p["x"]),
                "y": float(p["y"]),
                "button": btn,
                "buttons": int(p.get("buttons") or 0),
                "modifiers": _cdp_modifiers(p.get("modifiers") or 0),
                "timestamp": ts,
            }
            if p.get("clickCount") is not None:
                mp["clickCount"] = int(p["clickCount"])
            lands_spec = p.get("landsOn")
            armed_info = (
                self._arm_mouse_landing(state, j_ev_type, lands_spec)
                if lands_spec
                else None
            )

            def _mouse_result(_raw: Dict[str, Any]) -> Dict[str, Any]:
                if lands_spec:
                    verdict = self._read_mouse_landing(state, armed_info)
                    return {"landing": verdict}
                return {}

            return ("Input.dispatchMouseEvent", mp, _mouse_result)

        if method == "Page.dispatchWheelEvent":
            wp: Dict[str, Any] = {
                "type": "mouseWheel",
                "x": float(p["x"]),
                "y": float(p["y"]),
                "deltaX": float(p.get("deltaX") or 0.0),
                "deltaY": float(p.get("deltaY") or 0.0),
                "modifiers": _cdp_modifiers(p.get("modifiers") or 0),
                "timestamp": ts,
            }
            return ("Input.dispatchMouseEvent", wp, None)

        if method == "Page.dispatchKeyEvent":
            ev_type = p.get("type")
            if ev_type not in ("keydown", "keyup"):
                raise juggler_conn.ProtocolError(
                    f"Page.dispatchKeyEvent: Unknown type {ev_type}"
                )
            is_down = ev_type == "keydown"
            key = p.get("key") or ""
            code = p.get("code") or ""
            vk = int(p.get("keyCode") or 0)
            loc = int(p.get("location") or 0)
            repeat = bool(p.get("repeat", False))
            text = p.get("text") or ""

            mod_bit = (
                _CDP_KEY_MODIFIER_BITS.get(key)
                or _CDP_CODE_MODIFIER_BITS.get(code)
                or 0
            )
            slot = code or key
            with self._lock:
                if mod_bit and slot:
                    if is_down:
                        state.held_key_modifiers[slot] = mod_bit
                    else:
                        state.held_key_modifiers.pop(slot, None)
                cdp_mods = _cdp_modifiers(p.get("modifiers") or 0)
                for bit in state.held_key_modifiers.values():
                    cdp_mods |= bit

            # Non-Shift modifiers (Alt=1, Control=2, Meta=4) never emit printable text.
            if cdp_mods & ~8:
                text = ""
            elif is_down and key == "Enter" and not text:
                text = "\r"

            # DOM location 3 (numpad) maps to isKeypad=True in CDP Input.dispatchKeyEvent;
            # CDP location only encodes 1=Left / 2=Right. Omit nativeVirtualKeyCode: on
            # Linux X11/Ozone, native_key_code is interpreted as an X11 hardware keycode
            # (e.g. VK_V=86 == X11 KP_Add, turning Ctrl+V into browser zoom-in Ctrl++).
            is_keypad = loc == 3
            cdp_loc = loc if loc in (1, 2) else 0
            kp: Dict[str, Any] = {
                "type": ("keyDown" if text else "rawKeyDown") if is_down else "keyUp",
                "key": key,
                "code": code,
                "windowsVirtualKeyCode": vk,
                "location": cdp_loc,
                "modifiers": cdp_mods,
                "autoRepeat": repeat,
                "timestamp": ts,
            }
            if is_keypad:
                kp["isKeypad"] = True
            if is_down:
                if text:
                    kp["text"] = text
                    kp["unmodifiedText"] = text
                elif len(key) == 1:
                    kp["unmodifiedText"] = key
            return ("Input.dispatchKeyEvent", kp, None)

        if method == "Page.insertText":
            return ("Input.insertText", {"text": p.get("text") or ""}, None)

        return None

    def send_async(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        session: Optional[str] = None,
    ) -> _CDPPendingReply:
        p = params or {}
        if session is not None:
            direct = self._build_cdp_input_command(session, method, p)
            if direct is not None:
                cdp_method, cdp_params, transform = direct
                raw_handle = self._raw.send_async(
                    cdp_method, cdp_params, session=session
                )
                return _CDPPendingReply(
                    self, raw_pending=raw_handle, transform=transform
                )

        done = threading.Event()
        box: List[Any] = []

        def _run() -> None:
            try:
                res = self.send(method, p, session=session, timeout=None)
                box.append((True, res if isinstance(res, dict) else {}))
            except BaseException as exc:
                box.append((False, exc))
            finally:
                done.set()

        threading.Thread(target=_run, daemon=True).start()
        return _CDPPendingReply(self, done=done, box=box)

    def _enqueue_cdp_event(
        self, session_id: Optional[str], method: str, params: Dict[str, Any]
    ) -> None:
        self._ev_queue.put(("event", session_id, method, params))

    def _drain_event_barrier(self, timeout: Optional[float]) -> None:
        if self._closed or threading.current_thread() is self._ev_worker:
            return
        barrier = threading.Event()
        self._ev_queue.put(("barrier", barrier))
        barrier.wait(timeout=timeout if timeout is not None else 5.0)

    def _on_pipe_closed(self) -> None:
        self._closed = True
        if self._process.poll() is not None and hasattr(self._process, "close_guardian"):
            self._process.close_guardian()
        self._ev_queue.put(None)
        self.announce_closed()

    def _note_request_completed(
        self, state: _PageSessionState, req_id: Optional[str]
    ) -> None:
        if not req_id:
            return
        state.completed_req_order.append(req_id)
        if len(state.completed_req_order) > 4096:
            while len(state.completed_req_order) > 2048:
                old_id = state.completed_req_order.popleft()
                if old_id in state.paused_requests:
                    continue
                state.req_frames.pop(old_id, None)
                state.req_types.pop(old_id, None)
                state.req_sessions.pop(old_id, None)
                state.unpaused_requests.pop(old_id, None)
                state.deferred_paused.pop(old_id, None)
                state.sent_requests.discard(old_id)
                state.responded_requests.discard(old_id)
                state.finished_requests.discard(old_id)

    def _record_bridge_error(self, context: str, exc: BaseException) -> None:
        formatted = f"{context}: {type(exc).__name__}: {exc}"
        with self._lock:
            self._event_errors.append(formatted)
        if os.environ.get("INVPW_DEBUG_CDP"):
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)

    def _event_worker_loop(self) -> None:
        while True:
            item = self._ev_queue.get()
            if item is None:
                break
            kind = item[0]
            if kind == "barrier":
                item[1].set()
                continue
            if kind == "flush_paused":
                _, p_sid, net_id = item
                with self._lock:
                    st = self._pages.get(p_sid)
                if st is not None:
                    j_req = st.deferred_paused.pop(net_id, None)
                    if j_req is not None and net_id not in st.sent_requests:
                        if st.req_types.get(net_id):
                            j_req["resourceType"] = st.req_types[net_id]
                        if st.req_frames.get(net_id):
                            j_req["frameId"] = st.req_frames[net_id][0]
                        st.sent_requests.add(net_id)
                        self.dispatch_event("Network.requestWillBeSent", j_req, p_sid)
                continue
            _, session_id, method, params = item
            try:
                self._handle_cdp_event(session_id, method, params)
            except juggler_conn.TargetClosedError:
                pass
            except Exception as exc:
                self._record_bridge_error(f"event({method})", exc)

    # -- Context & Page Initialization ---------------------------------------
    def _get_ctx(self, context_id: Optional[str]) -> _ContextState:
        with self._lock:
            state = self._contexts.get(context_id)
            if state is None:
                state = _ContextState(context_id=context_id, cdp_context_id=context_id)
                self._contexts[context_id] = state
            return state

    def _cdp_cid(self, context_id: Optional[str]) -> Optional[str]:
        if context_id is None:
            return None
        with self._lock:
            ctx = self._contexts.get(context_id)
            return ctx.cdp_context_id if ctx is not None else context_id

    def _juggler_cid(self, raw_cdp_cid: Optional[str]) -> Optional[str]:
        if not raw_cdp_cid:
            return None
        with self._lock:
            if raw_cdp_cid in self._cdp_to_juggler_cid:
                return self._cdp_to_juggler_cid[raw_cdp_cid]
            if raw_cdp_cid in self._contexts:
                return raw_cdp_cid
        return None

    def _effective_proxy_auth(self, ctx: _ContextState) -> Optional[Dict[str, str]]:
        if ctx.proxy_server is not None:
            return ctx.proxy_auth
        return ctx.proxy_auth or self._proxy_auth

    def _find_page_for_frame(
        self, session_id: Optional[str], frame_id: Optional[str]
    ) -> Optional[_PageSessionState]:
        with self._lock:
            if session_id:
                if session_id in self._pages:
                    return self._pages[session_id]
                if session_id in self._child_to_page:
                    return self._child_to_page[session_id]
            if frame_id:
                for st in self._pages.values():
                    if (
                        st.main_frame_id == frame_id
                        or frame_id in st.nav_loaders
                        or frame_id in st.utility_contexts
                        or frame_id in st.frame_sessions
                    ):
                        return st
            if len(self._pages) == 1:
                return next(iter(self._pages.values()))
        return None

    def _init_attached_page(
        self, session_id: str, target_info: Dict[str, Any]
    ) -> None:
        target_id = target_info["targetId"]
        raw_cid = target_info.get("browserContextId")
        with self._lock:
            cid = self._juggler_cid(raw_cid)
            if target_id in self._initial_blank_targets:
                return
            state = _PageSessionState(
                session_id=session_id, target_id=target_id, context_id=cid
            )
            self._pages[session_id] = state
            self._target_to_session[target_id] = session_id

        try:
            # In Chromium DevTools, a page target's targetId is identical to its
            # initial root frameId (devtools_frame_token_). A popup created by
            # `<a target="_blank" href="...">` (noopener) attaches at
            # NavigationThrottle::WillStartRequest before a renderer RenderFrame
            # exists, so any blocking self._raw.send(...) here on invpw-cdp-events
            # deadlocks behind Runtime.runIfWaitingForDebugger. Pipeline every
            # setup frame via non-blocking post().
            state.main_frame_id = target_id
            state.frame_sessions[target_id] = session_id

            self._raw.post("Page.enable", session=session_id)
            self._raw.post(
                "Page.setLifecycleEventsEnabled", {"enabled": True}, session=session_id
            )
            self._raw.post("DOM.enable", session=session_id)
            self._raw.post("Network.enable", session=session_id)
            self._raw.post("Runtime.enable", {}, session=session_id)
            state.runtime_enabled_sessions.add(session_id)
            self._ensure_utility_world_script(state, session_id, use_post=True)
            self._raw.post("Inspector.enable", session=session_id)
            self._raw.post(
                "Target.setAutoAttach",
                {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
                session=session_id,
            )

            ctx = self._get_ctx(cid)
            self._apply_ctx_to_session(
                state, ctx, session_id, is_main=True, use_post=True
            )

            raw_url = target_info.get("url") or ""
            is_inflight_popup = bool(target_info.get("openerId")) and not raw_url

            juggler_info = {
                "targetId": target_id,
                "type": "page",
                "url": raw_url or "about:blank",
                "browserContextId": cid,
            }
            if target_info.get("openerId"):
                juggler_info["openerId"] = target_info["openerId"]

            self.dispatch_event(
                "Browser.attachedToTarget",
                {"sessionId": session_id, "targetInfo": juggler_info},
                None,
            )

            init_nav_id = f"init-{target_id}"
            state.nav_loaders[target_id] = init_nav_id
            self.dispatch_event(
                "Page.frameAttached",
                {"frameId": target_id, "parentFrameId": None},
                session_id,
            )
            if not is_inflight_popup:
                self.dispatch_event(
                    "Page.navigationCommitted",
                    {
                        "frameId": target_id,
                        "navigationId": init_nav_id,
                        "url": raw_url or "about:blank",
                        "name": "",
                    },
                    session_id,
                )
                for ev_name in ("DOMContentLoaded", "load"):
                    self.dispatch_event(
                        "Page.eventFired",
                        {"frameId": target_id, "name": ev_name},
                        session_id,
                    )

            def _on_renderer_ready(_res: Dict[str, Any]) -> None:
                if not is_inflight_popup and not state.ready_emitted:
                    self._ev_queue.put(("ev", session_id, "__invpw.pageReady", {}))

            self._raw.post("Runtime.runIfWaitingForDebugger", session=session_id)
            self._raw.post(
                "Runtime.evaluate",
                {"expression": self._intrinsics_bootstrap, "returnByValue": True},
                session=session_id,
            )
            for _native, (wrapped, _ev) in list(ctx.bindings.items()):
                if wrapped:
                    self._raw.post(
                        "Runtime.evaluate",
                        {"expression": wrapped, "returnByValue": True},
                        session=session_id,
                    )
            self._request_utility_world(state, target_id, sid=session_id)
            self._raw.post(
                "Page.getFrameTree",
                session=session_id,
                on_result=_on_renderer_ready,
            )
        except juggler_conn.TargetClosedError:
            with self._lock:
                self._pages.pop(session_id, None)
                self._target_to_session.pop(target_id, None)
        except Exception as exc:
            with self._lock:
                self._target_init_errors[target_id] = exc
                self._pages.pop(session_id, None)
                self._target_to_session.pop(target_id, None)
            self._record_bridge_error(f"_init_attached_page({target_id})", exc)
            self._raw.post("Target.closeTarget", {"targetId": target_id})

    def _init_oopif_session(
        self,
        parent_session_id: Optional[str],
        child_sid: str,
        target_info: Dict[str, Any],
    ) -> None:
        fid = target_info["targetId"]
        state = self._find_page_for_frame(parent_session_id, fid)
        if state is None:
            self._raw.post("Runtime.runIfWaitingForDebugger", session=child_sid)
            return
        with self._lock:
            self._child_to_page[child_sid] = state
            state.child_sessions.add(child_sid)
            state.session_root_frame[child_sid] = fid
            state.frame_sessions[fid] = child_sid

        try:
            # NEVER call blocking self._raw.send(...) inside _init_oopif_session on
            # invpw-cdp-events: a newly attached OOPIF is paused in waitingForDebugger
            # with a provisional LocalFrame, so synchronous renderer RPCs (especially
            # Page.createIsolatedWorld) queue in Blink's pending_isolated_worlds_ until
            # Runtime.runIfWaitingForDebugger and any queued Fetch.authRequired /
            # Fetch.requestPaused events are processed. Pipelining all setup frames via
            # post() preserves strict FIFO session order on the CDP pipe with zero
            # head-of-line event-thread blocking.
            self._raw.post("Page.enable", session=child_sid)
            self._raw.post(
                "Page.setLifecycleEventsEnabled", {"enabled": True}, session=child_sid
            )
            self._raw.post("DOM.enable", session=child_sid)
            self._raw.post("Network.enable", session=child_sid)
            self._raw.post("Runtime.enable", {}, session=child_sid)
            state.runtime_enabled_sessions.add(child_sid)
            self._ensure_utility_world_script(state, child_sid, use_post=True)
            self._raw.post(
                "Target.setAutoAttach",
                {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
                session=child_sid,
            )
            ctx = self._get_ctx(state.context_id)
            self._apply_ctx_to_session(
                state, ctx, child_sid, is_main=False, use_post=True
            )
            self._raw.post("Runtime.runIfWaitingForDebugger", session=child_sid)
            self._request_utility_world(state, fid, sid=child_sid)
        except juggler_conn.TargetClosedError:
            pass
        except Exception as exc:
            self._record_bridge_error(f"_init_oopif_session({fid})", exc)
            self._raw.post("Runtime.runIfWaitingForDebugger", session=child_sid)

    def _init_worker_session(
        self,
        parent_session_id: Optional[str],
        child_sid: str,
        target_info: Dict[str, Any],
    ) -> None:
        state: Optional[_PageSessionState] = None
        parent_fid = str(target_info.get("parentFrameId") or "")
        with self._lock:
            if parent_session_id:
                state = self._pages.get(parent_session_id) or self._child_to_page.get(
                    parent_session_id
                )
        if state is None and parent_fid:
            state = self._find_page_for_frame(parent_session_id, parent_fid)
        if state is None:
            self._raw.post("Runtime.runIfWaitingForDebugger", session=child_sid)
            return

        worker_fid = (
            parent_fid
            or (
                state.worker_frames.get(parent_session_id)
                if parent_session_id
                else None
            )
            or (
                state.session_root_frame.get(parent_session_id)
                if parent_session_id
                else None
            )
            or state.main_frame_id
        )
        with self._lock:
            self._child_to_page[child_sid] = state
            state.worker_sessions.add(child_sid)
            state.worker_frames[child_sid] = worker_fid

        try:
            self._raw.post("Network.enable", session=child_sid)
            ctx = self._get_ctx(state.context_id)
            if ctx.cache_disabled or state.intercepting or ctx.intercepting:
                self._raw.post(
                    "Network.setCacheDisabled",
                    {"cacheDisabled": True},
                    session=child_sid,
                )
            if ctx.extra_headers:
                state.had_extra_headers.add(child_sid)
                self._raw.post(
                    "Network.setExtraHTTPHeaders",
                    {"headers": dict(ctx.extra_headers)},
                    session=child_sid,
                )
            self._raw.post(
                "Target.setAutoAttach",
                {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
                session=child_sid,
            )
            self._raw.post("Runtime.runIfWaitingForDebugger", session=child_sid)
        except juggler_conn.TargetClosedError:
            pass
        except Exception as exc:
            self._record_bridge_error(f"_init_worker_session({child_sid})", exc)
            self._raw.post("Runtime.runIfWaitingForDebugger", session=child_sid)

    def _announce_frame_tree(
        self,
        state: _PageSessionState,
        node: Dict[str, Any],
        parent_id: Optional[str],
        *,
        collected_fids: Optional[List[str]] = None,
    ) -> None:
        frame = node["frame"]
        fid = frame["id"]
        if collected_fids is not None:
            collected_fids.append(fid)
        nav_id = frame.get("loaderId") or f"init-{fid}"
        state.nav_loaders[fid] = nav_id
        state.frame_sessions.setdefault(fid, state.session_id)

        self.dispatch_event(
            "Page.frameAttached",
            {"frameId": fid, "parentFrameId": parent_id},
            state.session_id,
        )
        self.dispatch_event(
            "Page.navigationCommitted",
            {
                "frameId": fid,
                "navigationId": nav_id,
                "url": frame.get("url") or "about:blank",
                "name": frame.get("name") or "",
            },
            state.session_id,
        )
        for ev_name in ("DOMContentLoaded", "load"):
            self.dispatch_event(
                "Page.eventFired",
                {"frameId": fid, "name": ev_name},
                state.session_id,
            )
        for child in node.get("childFrames") or ():
            self._announce_frame_tree(
                state, child, parent_id=fid, collected_fids=collected_fids
            )

    def _ensure_utility_world_script(
        self, state: _PageSessionState, sid: str, *, use_post: bool = False
    ) -> None:
        if sid in state.utility_world_sessions:
            return
        state.utility_world_sessions.add(sid)
        for doc_params in (
            {"source": "", "worldName": UTILITY_WORLD},
            {"source": self._intrinsics_bootstrap},
        ):
            if use_post:
                self._raw.post(
                    "Page.addScriptToEvaluateOnNewDocument", doc_params, session=sid
                )
            else:
                self._raw.send(
                    "Page.addScriptToEvaluateOnNewDocument", doc_params, session=sid
                )

    def _request_utility_world(
        self,
        state: _PageSessionState,
        frame_id: str,
        *,
        sid: Optional[str] = None,
    ) -> None:
        frame_sid = sid or self._session_for_frame(state, frame_id)
        self._raw.post(
            "Page.createIsolatedWorld",
            {
                "frameId": frame_id,
                "worldName": UTILITY_WORLD,
                "grantUniveralAccess": True,
            },
            session=frame_sid,
        )

    def _intrinsics_extract_expression(self) -> str:
        return (
            "(() => {"
            " let captured = null;"
            " try {"
            f"   dispatchEvent(new CustomEvent({json.dumps(self._intrinsics_ev)},"
            "     { detail: (v) => { captured = v; } }));"
            " } catch {}"
            " return captured;"
            "})()"
        )

    def _capture_caller_intrinsics_async(
        self,
        state: _PageSessionState,
        caller_id: str,
        raw_sid: str,
        v8_ctx_id: int,
    ) -> None:
        def _on_res(res: Dict[str, Any]) -> None:
            robj = res.get("result") or {}
            oid = robj.get("objectId")
            if oid and state.caller_v8_contexts.get(caller_id) == (raw_sid, v8_ctx_id):
                state.caller_intrinsics[caller_id] = oid
                state.obj_sessions[oid] = raw_sid

        self._raw.post(
            "Runtime.evaluate",
            {
                "expression": self._intrinsics_extract_expression(),
                "contextId": v8_ctx_id,
                "returnByValue": False,
            },
            session=raw_sid,
            on_result=_on_res,
        )

    def _ensure_caller_intrinsics(
        self,
        state: _PageSessionState,
        caller_id: str,
        eval_sid: str,
        v8_ctx_id: Optional[int],
        *,
        timeout: Optional[float],
    ) -> Optional[str]:
        existing = state.caller_intrinsics.get(caller_id)
        if existing:
            return existing
        if v8_ctx_id is None:
            return None
        try:
            res = self._raw.send(
                "Runtime.evaluate",
                {
                    "expression": self._intrinsics_extract_expression(),
                    "contextId": v8_ctx_id,
                    "returnByValue": False,
                },
                session=eval_sid,
                timeout=timeout,
            )
            robj = res.get("result") or {}
            oid = robj.get("objectId")
            if oid:
                state.caller_intrinsics[caller_id] = oid
                state.obj_sessions[oid] = eval_sid
                return oid
        except (juggler_conn.TargetClosedError, juggler_conn.ProtocolError):
            pass
        return None

    def _ensure_raw_binding(
        self,
        state: _PageSessionState,
        native: str,
        sid: Optional[str] = None,
        *,
        use_post: bool = False,
    ) -> None:
        sessions = [sid] if sid else [state.session_id, *list(state.child_sessions)]
        for s in sessions:
            if s not in state.runtime_enabled_sessions:
                if use_post:
                    self._raw.post("Runtime.enable", {}, session=s)
                else:
                    self._raw.send("Runtime.enable", {}, session=s)
                state.runtime_enabled_sessions.add(s)
            installed = state.installed_raw_bindings.setdefault(s, set())
            if native in installed:
                continue
            if use_post:
                self._raw.post("Runtime.addBinding", {"name": native}, session=s)
            else:
                self._raw.send("Runtime.addBinding", {"name": native}, session=s)
            installed.add(native)

    def _eval_script_in_live_frames(
        self,
        state: _PageSessionState,
        script: str,
        *,
        timeout: Optional[float],
    ) -> None:
        for fid, caller_ctx in list(state.caller_contexts.items()):
            try:
                frame_sid = self._session_for_frame(state, fid)
                if fid == state.main_frame_id or frame_sid != state.session_id:
                    self._raw.send(
                        "Runtime.evaluate",
                        {"expression": script, "returnByValue": True},
                        session=frame_sid,
                        timeout=timeout,
                    )
                else:
                    win_oid = self._resolve_caller_window(
                        state, caller_ctx, timeout=timeout
                    )
                    win_sid = self._session_for_object(state, win_oid)
                    self._raw.send(
                        "Runtime.callFunctionOn",
                        {
                            "objectId": win_oid,
                            "functionDeclaration": (
                                f"function() {{ return (0, eval)({json.dumps(script)}); }}"
                            ),
                            "returnByValue": True,
                        },
                        session=win_sid,
                        timeout=timeout,
                    )
            except (juggler_conn.TargetClosedError, juggler_conn.ProtocolError):
                pass

    def _apply_download_options(
        self, cid: Optional[str], opts: Dict[str, Any], *, timeout: Optional[float] = 10.0
    ) -> None:
        behavior = opts.get("behavior")
        dl_dir = opts.get("downloadsDir")
        cdp_params: Dict[str, Any] = {
            "behavior": "deny" if behavior == "cancel" else "allowAndName",
            "eventsEnabled": True,
        }
        if behavior != "cancel" and dl_dir:
            cdp_params["downloadPath"] = str(dl_dir)
        cdp_cid = self._cdp_cid(cid)
        if cdp_cid:
            cdp_params["browserContextId"] = cdp_cid
        try:
            self._raw.send("Browser.setDownloadBehavior", cdp_params, timeout=timeout)
        except juggler_conn.ProtocolError:
            pass

    def _apply_ctx_to_page(
        self, state: _PageSessionState, ctx: _ContextState
    ) -> None:
        self._apply_ctx_to_session(state, ctx, state.session_id, is_main=True)
        for child_sid in list(state.child_sessions):
            try:
                self._apply_ctx_to_session(state, ctx, child_sid, is_main=False)
            except juggler_conn.TargetClosedError:
                pass
        for w_sid in list(state.worker_sessions):
            try:
                if ctx.extra_headers or w_sid in state.had_extra_headers:
                    if ctx.extra_headers:
                        state.had_extra_headers.add(w_sid)
                    else:
                        state.had_extra_headers.discard(w_sid)
                    self._raw.post(
                        "Network.setExtraHTTPHeaders",
                        {"headers": dict(ctx.extra_headers)},
                        session=w_sid,
                    )
                if ctx.offline != state.applied_offline.get(w_sid, False):
                    state.applied_offline[w_sid] = ctx.offline
                    self._raw.post(
                        "Network.emulateNetworkConditions",
                        {
                            "offline": bool(ctx.offline),
                            "latency": 0,
                            "downloadThroughput": -1,
                            "uploadThroughput": -1,
                        },
                        session=w_sid,
                    )
            except juggler_conn.TargetClosedError:
                pass

    def _apply_ctx_to_session(
        self,
        state: _PageSessionState,
        ctx: _ContextState,
        sid: str,
        *,
        is_main: bool,
        use_post: bool = False,
    ) -> None:
        def _emit(m: str, p: Optional[Dict[str, Any]] = None) -> None:
            if use_post:
                self._raw.post(m, p, session=sid)
            else:
                self._raw.send(m, p, session=sid)

        if is_main and ctx.viewport:
            vp = ctx.viewport.get("viewportSize") or {}
            sc = ctx.viewport.get("screenSize") or {}
            dpr = float(ctx.viewport.get("deviceScaleFactor") or 1.0)
            is_mobile = bool(ctx.viewport.get("isMobile", False))
            state.dpr = dpr
            if vp.get("width") and vp.get("height"):
                metrics: Dict[str, Any] = {
                    "width": int(vp["width"]),
                    "height": int(vp["height"]),
                    "deviceScaleFactor": dpr,
                    "mobile": is_mobile,
                }
                if is_mobile and sc.get("width") and sc.get("height"):
                    metrics["screenWidth"] = int(sc["width"])
                    metrics["screenHeight"] = int(sc["height"])
                _emit("Emulation.setDeviceMetricsOverride", metrics)
        if ctx.timezone_id:
            if use_post:
                self._raw.post(
                    "Emulation.setTimezoneOverride",
                    {"timezoneId": ctx.timezone_id},
                    session=sid,
                )
            else:
                try:
                    self._raw.send(
                        "Emulation.setTimezoneOverride",
                        {"timezoneId": ctx.timezone_id},
                        session=sid,
                    )
                except juggler_conn.ProtocolError as exc:
                    if "Timezone override is already in effect" not in str(exc):
                        raise
        if ctx.locale:
            primary_locale = ctx.locale.split(",")[0].strip()
            if primary_locale:
                if use_post:
                    self._raw.post(
                        "Emulation.setLocaleOverride",
                        {"locale": primary_locale},
                        session=sid,
                    )
                else:
                    try:
                        self._raw.send(
                            "Emulation.setLocaleOverride",
                            {"locale": primary_locale},
                            session=sid,
                        )
                    except juggler_conn.ProtocolError:
                        pass
        if ctx.user_agent or ctx.accept_languages:
            ua_params: Dict[str, Any] = {
                "userAgent": ctx.user_agent or self._user_agent
            }
            if ctx.accept_languages:
                ua_params["acceptLanguage"] = ctx.accept_languages
            _emit("Emulation.setUserAgentOverride", ua_params)
        if any(
            v is not None
            for v in (ctx.color_scheme, ctx.reduced_motion, ctx.forced_colors)
        ):
            features = []
            if ctx.color_scheme is not None:
                features.append(
                    {"name": "prefers-color-scheme", "value": ctx.color_scheme}
                )
            if ctx.reduced_motion is not None:
                features.append(
                    {"name": "prefers-reduced-motion", "value": ctx.reduced_motion}
                )
            if ctx.forced_colors is not None:
                features.append(
                    {"name": "forced-colors", "value": ctx.forced_colors}
                )
            _emit("Emulation.setEmulatedMedia", {"features": features})
        if ctx.extra_headers or sid in state.had_extra_headers:
            if ctx.extra_headers:
                state.had_extra_headers.add(sid)
            else:
                state.had_extra_headers.discard(sid)
            _emit("Network.setExtraHTTPHeaders", {"headers": dict(ctx.extra_headers)})
        if ctx.offline != state.applied_offline.get(sid, False):
            state.applied_offline[sid] = ctx.offline
            _emit(
                "Network.emulateNetworkConditions",
                {
                    "offline": bool(ctx.offline),
                    "latency": 0,
                    "downloadThroughput": -1,
                    "uploadThroughput": -1,
                },
            )
        if ctx.geolocation:
            _emit(
                "Emulation.setGeolocationOverride",
                {
                    "latitude": ctx.geolocation.get("latitude"),
                    "longitude": ctx.geolocation.get("longitude"),
                    "accuracy": ctx.geolocation.get("accuracy", 100),
                },
            )
        if ctx.ignore_https_errors != state.applied_ignore_https.get(sid, False):
            state.applied_ignore_https[sid] = ctx.ignore_https_errors
            _emit(
                "Security.setIgnoreCertificateErrors",
                {"ignore": bool(ctx.ignore_https_errors)},
            )
        if ctx.bypass_csp:
            _emit("Page.setBypassCSP", {"enabled": True})
        if ctx.js_disabled != state.applied_js_disabled.get(sid, False):
            state.applied_js_disabled[sid] = ctx.js_disabled
            _emit(
                "Emulation.setScriptExecutionDisabled",
                {"value": bool(ctx.js_disabled)},
            )
        if ctx.service_workers_blocked != state.applied_sw_bypass.get(sid, False):
            state.applied_sw_bypass[sid] = ctx.service_workers_blocked
            _emit(
                "Network.setBypassServiceWorker",
                {"bypass": bool(ctx.service_workers_blocked)},
            )
        if ctx.has_touch:
            _emit("Emulation.setTouchEmulationEnabled", {"enabled": True})
        if ctx.cache_disabled != state.applied_cache_disabled.get(sid, False):
            state.applied_cache_disabled[sid] = ctx.cache_disabled
            _emit(
                "Network.setCacheDisabled",
                {"cacheDisabled": bool(ctx.cache_disabled)},
            )
        for native in list(ctx.bindings):
            self._ensure_raw_binding(state, native, sid=sid, use_post=use_post)
        for native in list(state.page_bindings):
            self._ensure_raw_binding(state, native, sid=sid, use_post=use_post)
        self._sync_doc_scripts(state, ctx, sid=sid, use_post=use_post)
        self._sync_fetch_domain(state, ctx, sid=sid, use_post=use_post)

    def _sync_doc_scripts(
        self,
        state: _PageSessionState,
        ctx: _ContextState,
        sid: Optional[str] = None,
        *,
        use_post: bool = False,
    ) -> None:
        sessions = [sid] if sid else [state.session_id, *list(state.child_sessions)]
        for s in sessions:
            self._ensure_utility_world_script(state, s, use_post=use_post)
            installed_list = state.installed_doc_scripts.setdefault(s, [])
            for script_id in list(installed_list):
                if use_post:
                    self._raw.post(
                        "Page.removeScriptToEvaluateOnNewDocument",
                        {"identifier": script_id},
                        session=s,
                    )
                else:
                    try:
                        self._raw.send(
                            "Page.removeScriptToEvaluateOnNewDocument",
                            {"identifier": script_id},
                            session=s,
                        )
                    except juggler_conn.ProtocolError:
                        pass
            installed_list.clear()

            def _add_doc_script(
                target_sid: str,
                target_list: List[str],
                script_params: Dict[str, Any],
            ) -> None:
                if use_post:
                    def _record(res: Dict[str, Any]) -> None:
                        ident = res.get("identifier")
                        if ident:
                            target_list.append(ident)

                    self._raw.post(
                        "Page.addScriptToEvaluateOnNewDocument",
                        script_params,
                        session=target_sid,
                        on_result=_record,
                    )
                else:
                    res = self._raw.send(
                        "Page.addScriptToEvaluateOnNewDocument",
                        script_params,
                        session=target_sid,
                    )
                    if res.get("identifier"):
                        target_list.append(res["identifier"])

            if ctx.service_workers_blocked:
                _add_doc_script(s, installed_list, {"source": _SW_BLOCK_SCRIPT})

            # Bindings are registered first so exposed functions exist before caller init scripts run.
            combined_bindings: Dict[str, tuple[str, str]] = dict(ctx.bindings)
            combined_bindings.update(state.page_bindings)
            for _native, (wrapped_src, _ev_name) in combined_bindings.items():
                if wrapped_src:
                    _add_doc_script(s, installed_list, {"source": wrapped_src})

            for src in ctx.init_scripts:
                if src:
                    _add_doc_script(s, installed_list, {"source": src})
            for entry in state.page_init_scripts:
                src = entry.get("script") or ""
                w_name = entry.get("worldName")
                if not src:
                    continue
                params: Dict[str, Any] = {"source": src}
                if w_name and entry.get("worldKind") != "caller":
                    params["worldName"] = w_name
                _add_doc_script(s, installed_list, params)

    def _sync_fetch_domain(
        self,
        state: _PageSessionState,
        ctx: _ContextState,
        sid: Optional[str] = None,
        *,
        use_post: bool = False,
    ) -> None:
        effective_proxy_auth = self._effective_proxy_auth(ctx)
        needs_auth = bool(effective_proxy_auth or ctx.http_credentials)
        needs_intercept = bool(state.intercepting or ctx.intercepting)
        sessions = [sid] if sid else [state.session_id, *list(state.child_sessions)]
        for s in sessions:
            if needs_intercept or needs_auth:
                fetch_params = {
                    "handleAuthRequests": needs_auth,
                    "patterns": [{"urlPattern": "*", "requestStage": "Request"}],
                }
                if use_post:
                    self._raw.post("Fetch.enable", fetch_params, session=s)
                else:
                    self._raw.send("Fetch.enable", fetch_params, session=s)
                state.fetch_enabled_sessions.add(s)
            elif s in state.fetch_enabled_sessions:
                state.fetch_enabled_sessions.discard(s)
                if use_post:
                    self._raw.post("Fetch.disable", session=s)
                else:
                    try:
                        self._raw.send("Fetch.disable", session=s)
                    except juggler_conn.ProtocolError:
                        pass

    def _resolve_main_world_frame(
        self, state: _PageSessionState, sid: str, v8_ctx_id: int
    ) -> str:
        cached = state.main_world_to_frame.get((sid, v8_ctx_id))
        if cached:
            return cached
        try:
            doc_res = self._raw.send(
                "Runtime.evaluate",
                {
                    "expression": "document",
                    "contextId": v8_ctx_id,
                    "returnByValue": False,
                },
                session=sid,
                timeout=5.0,
            )
            doc_oid = (doc_res.get("result") or {}).get("objectId")
            if doc_oid:
                desc = self._raw.send(
                    "DOM.describeNode",
                    {"objectId": doc_oid},
                    session=sid,
                    timeout=5.0,
                )
                self._raw.post(
                    "Runtime.releaseObject",
                    {"objectId": doc_oid},
                    session=sid,
                )
                fid = (desc.get("node") or {}).get("frameId")
                if fid:
                    state.main_world_to_frame[(sid, v8_ctx_id)] = fid
                    return str(fid)
        except (juggler_conn.TargetClosedError, juggler_conn.ProtocolError):
            pass
        if sid != state.session_id:
            for fid, f_sid in state.frame_sessions.items():
                if f_sid == sid:
                    return fid
        return state.main_frame_id

    # -- CDP Event Translation -----------------------------------------------
    def _handle_cdp_event(
        self, session_id: Optional[str], method: str, params: Dict[str, Any]
    ) -> None:
        if method == "Target.attachedToTarget":
            info = params.get("targetInfo") or {}
            child_sid = params.get("sessionId")
            t_type = info.get("type")
            if t_type == "page" and child_sid:
                if not self._adopt_started:
                    self._initial_blank_targets.add(info["targetId"])
                    if params.get("waitingForDebugger"):
                        self._raw.post("Runtime.runIfWaitingForDebugger", session=child_sid)
                    return
                self._init_attached_page(child_sid, info)
            elif t_type == "iframe" and child_sid:
                self._init_oopif_session(session_id, child_sid, info)
            elif t_type == "worker" and child_sid:
                self._init_worker_session(session_id, child_sid, info)
            elif child_sid and params.get("waitingForDebugger"):
                self._raw.post("Runtime.runIfWaitingForDebugger", session=child_sid)
            return

        if method == "Target.detachedFromTarget":
            child_sid = params.get("sessionId")
            aborted_worker_reqs: List[tuple[str, str]] = []
            flushed_paused: List[tuple[str, Dict[str, Any]]] = []
            if child_sid:
                with self._lock:
                    st = self._pages.pop(child_sid, None)
                    if st:
                        self._target_to_session.pop(st.target_id, None)
                        for sub_sid in [
                            *list(st.child_sessions),
                            *list(st.worker_sessions),
                        ]:
                            self._child_to_page.pop(sub_sid, None)
                    else:
                        parent_st = self._child_to_page.pop(child_sid, None)
                        if parent_st is not None:
                            was_worker = child_sid in parent_st.worker_sessions
                            was_child = child_sid in parent_st.child_sessions
                            parent_st.worker_sessions.discard(child_sid)
                            parent_st.worker_frames.pop(child_sid, None)
                            parent_st.had_extra_headers.discard(child_sid)
                            parent_st.applied_offline.pop(child_sid, None)
                            if was_worker or was_child:
                                for d_id, d_req in list(parent_st.deferred_paused.items()):
                                    if d_id not in parent_st.sent_requests:
                                        parent_st.deferred_paused.pop(d_id, None)
                                        parent_st.sent_requests.add(d_id)
                                        flushed_paused.append((parent_st.session_id, d_req))
                            if was_worker:
                                for r_id in list(parent_st.sent_requests):
                                    if (
                                        r_id not in parent_st.finished_requests
                                        and parent_st.req_sessions.get(r_id) == child_sid
                                    ):
                                        parent_st.finished_requests.add(r_id)
                                        self._note_request_completed(parent_st, r_id)
                                        aborted_worker_reqs.append(
                                            (parent_st.session_id, r_id)
                                        )
                            parent_st.child_sessions.discard(child_sid)
                            parent_st.session_root_frame.pop(child_sid, None)
                            parent_st.fetch_enabled_sessions.discard(child_sid)
                            parent_st.runtime_enabled_sessions.discard(child_sid)
                            parent_st.utility_world_sessions.discard(child_sid)
                            parent_st.installed_doc_scripts.pop(child_sid, None)
                            parent_st.installed_raw_bindings.pop(child_sid, None)
                            for k in [
                                key
                                for key in parent_st.main_world_to_frame
                                if key[0] == child_sid
                            ]:
                                parent_st.main_world_to_frame.pop(k, None)
                            for k in [
                                key
                                for key in parent_st.utility_context_keys
                                if key[0] == child_sid
                            ]:
                                parent_st.utility_context_keys.discard(k)
                            for k in [
                                key
                                for key in parent_st.session_v8_to_juggler
                                if key[0] == child_sid
                            ]:
                                parent_st.session_v8_to_juggler.pop(k, None)
                            for fid, f_sid in list(parent_st.frame_sessions.items()):
                                if f_sid == child_sid:
                                    parent_st.frame_sessions[fid] = parent_st.session_id
                for p_sid, d_req in flushed_paused:
                    self.dispatch_event("Network.requestWillBeSent", d_req, p_sid)
                for p_sid, r_id in aborted_worker_reqs:
                    self.dispatch_event(
                        "Network.requestFailed",
                        {
                            "requestId": r_id,
                            "errorCode": "net::ERR_ABORTED",
                            "responseEndTime": 0,
                        },
                        p_sid,
                    )
                if st:
                    self.dispatch_event(
                        "Browser.detachedFromTarget",
                        {"sessionId": child_sid, "targetId": st.target_id},
                        None,
                    )
            return

        if method in ("Browser.downloadWillBegin", "Page.downloadWillBegin"):
            guid = params.get("guid")
            if not guid:
                return
            with self._lock:
                if guid in self._seen_downloads:
                    return
                self._seen_downloads.add(guid)
            fid = params.get("frameId") or ""
            st = self._find_page_for_frame(session_id, fid)
            if st is None:
                return
            with self._lock:
                self._download_ctx_by_guid[guid] = st.context_id
            self.dispatch_event(
                "Browser.downloadCreated",
                {
                    "uuid": guid,
                    "url": params.get("url") or "",
                    "suggestedFileName": params.get("suggestedFilename") or "",
                    "frameId": fid or st.main_frame_id,
                    "pageTargetId": st.target_id,
                    "browserContextId": st.context_id,
                },
                None,
            )
            return

        if method in ("Browser.downloadProgress", "Page.downloadProgress"):
            guid = params.get("guid")
            dl_state = params.get("state")
            if not guid or dl_state not in ("completed", "canceled"):
                return
            with self._lock:
                if guid in self._finished_downloads:
                    return
                self._finished_downloads.add(guid)
            if dl_state == "completed":
                self.dispatch_event(
                    "Browser.downloadFinished", {"uuid": guid}, None
                )
            else:
                self.dispatch_event(
                    "Browser.downloadFinished",
                    {"uuid": guid, "canceled": True, "error": "canceled"},
                    None,
                )
            return

        if not session_id:
            return
        with self._lock:
            state = self._pages.get(session_id) or self._child_to_page.get(session_id)
        if state is None:
            return
        raw_sid = session_id
        page_sid = state.session_id

        if method == "__invpw.pageReady":
            if not state.ready_emitted:
                state.ready_emitted = True
                self.dispatch_event("Page.ready", {}, page_sid)
            return

        if method == "Inspector.targetCrashed":
            if raw_sid == page_sid:
                with self._lock:
                    state.held_key_modifiers.clear()
                self.dispatch_event("Page.crashed", {}, page_sid)
            return

        if method == "Page.screencastFrame":
            meta = params.get("metadata") or {}
            ack_sid = params.get("sessionId")
            if isinstance(ack_sid, int):
                with self._lock:
                    state.screencast_ack_ids.append(ack_sid)
            self.dispatch_event(
                "Page.screencastFrame",
                {
                    "data": params.get("data") or "",
                    "timestamp": meta.get("timestamp") or time.time(),
                    "deviceWidth": int(meta.get("deviceWidth") or 0),
                    "deviceHeight": int(meta.get("deviceHeight") or 0),
                },
                page_sid,
            )
            return

        if method == "Page.fileChooserOpened":
            fid = params.get("frameId") or state.main_frame_id
            backend_id = params.get("backendNodeId")
            util_ctx = state.utility_contexts.get(fid)
            if backend_id and util_ctx:
                util_sid, util_int = self._parse_util_ctx(state, util_ctx)

                def _resolve_chooser() -> None:
                    try:
                        resolved = self._raw.send(
                            "DOM.resolveNode",
                            {
                                "backendNodeId": backend_id,
                                "executionContextId": util_int,
                            },
                            session=util_sid,
                            timeout=10.0,
                        )
                        obj = resolved.get("object") or {}
                        obj_id = obj.get("objectId")
                        if obj_id:
                            self._record_result_object(state, util_sid, obj)
                            self.dispatch_event(
                                "Page.fileChooserOpened",
                                {
                                    "executionContextId": util_ctx,
                                    "element": {"objectId": obj_id},
                                },
                                page_sid,
                            )
                    except (
                        juggler_conn.TargetClosedError,
                        juggler_conn.ProtocolError,
                    ):
                        pass

                threading.Thread(
                    target=_resolve_chooser,
                    name="invpw-cdp-filechooser",
                    daemon=True,
                ).start()
            return

        if method == "Runtime.executionContextCreated":
            cinfo = params.get("context") or {}
            aux = cinfo.get("auxData") or {}
            fid_raw = aux.get("frameId")
            ctx_id_raw = cinfo.get("id")
            if not fid_raw or ctx_id_raw is None:
                return
            fid_str = str(fid_raw)
            new_id = int(ctx_id_raw)
            cname = str(cinfo.get("name") or "")
            if aux.get("isDefault"):
                for k, v in list(state.main_world_to_frame.items()):
                    if v == fid_str and k != (raw_sid, new_id):
                        state.main_world_to_frame.pop(k, None)
                state.main_world_to_frame[(raw_sid, new_id)] = fid_str
                old_caller = state.caller_contexts.get(fid_str)
                state.epoch += 1
                caller_id = f"caller:{fid_str}:{state.epoch}"
                state.caller_contexts[fid_str] = caller_id
                state.caller_v8_contexts[caller_id] = (raw_sid, new_id)
                state.session_v8_to_juggler[(raw_sid, new_id)] = caller_id
                if old_caller and old_caller != caller_id:
                    state.caller_windows.pop(old_caller, None)
                    state.caller_v8_contexts.pop(old_caller, None)
                    state.caller_intrinsics.pop(old_caller, None)
                    for k, jv in list(state.session_v8_to_juggler.items()):
                        if jv == old_caller and k != (raw_sid, new_id):
                            state.session_v8_to_juggler.pop(k, None)
                    self.dispatch_event(
                        "Runtime.executionContextDestroyed",
                        {"executionContextId": old_caller},
                        page_sid,
                    )
                self._capture_caller_intrinsics_async(
                    state, caller_id, raw_sid, new_id
                )
                self.dispatch_event(
                    "Runtime.executionContextCreated",
                    {
                        "executionContextId": caller_id,
                        "auxData": {"frameId": fid_str, "name": CALLER_WORLD},
                    },
                    page_sid,
                )
            elif cname == UTILITY_WORLD:
                util_id = (
                    str(new_id) if raw_sid == page_sid else f"{raw_sid}:{new_id}"
                )
                old_util = state.utility_contexts.get(fid_str)
                state.utility_contexts[fid_str] = util_id
                state.ctx_sessions[util_id] = raw_sid
                state.utility_context_keys.add((raw_sid, new_id))
                state.session_v8_to_juggler[(raw_sid, new_id)] = util_id
                if old_util and old_util != util_id:
                    old_sid, old_int = self._parse_util_ctx(state, old_util)
                    if (old_sid, old_int) != (raw_sid, new_id):
                        state.ctx_sessions.pop(old_util, None)
                        state.landing_ready.discard((old_sid, old_util))
                        state.utility_context_keys.discard((old_sid, old_int))
                        state.session_v8_to_juggler.pop((old_sid, old_int), None)
                    self.dispatch_event(
                        "Runtime.executionContextDestroyed",
                        {"executionContextId": old_util},
                        page_sid,
                    )
                self.dispatch_event(
                    "Runtime.executionContextCreated",
                    {
                        "executionContextId": util_id,
                        "auxData": {"frameId": fid_str, "name": UTILITY_WORLD},
                    },
                    page_sid,
                )
            return

        if method == "Runtime.executionContextDestroyed":
            ctx_raw = params.get("executionContextId")
            if ctx_raw is not None:
                v8_id = int(ctx_raw)
                state.main_world_to_frame.pop((raw_sid, v8_id), None)
                state.utility_context_keys.discard((raw_sid, v8_id))
                j_id = state.session_v8_to_juggler.pop((raw_sid, v8_id), None)
                if j_id:
                    if j_id.startswith("caller:"):
                        state.caller_windows.pop(j_id, None)
                        state.caller_v8_contexts.pop(j_id, None)
                        state.caller_intrinsics.pop(j_id, None)
                        for fk, fv in list(state.caller_contexts.items()):
                            if fv == j_id:
                                state.caller_contexts.pop(fk, None)
                    else:
                        state.ctx_sessions.pop(j_id, None)
                        state.landing_ready.discard((raw_sid, j_id))
                        for fk, fv in list(state.utility_contexts.items()):
                            if fv == j_id:
                                state.utility_contexts.pop(fk, None)
                    self.dispatch_event(
                        "Runtime.executionContextDestroyed",
                        {"executionContextId": j_id},
                        page_sid,
                    )
            return

        if method == "Runtime.executionContextsCleared":
            for k in [
                key for key in state.main_world_to_frame if key[0] == raw_sid
            ]:
                state.main_world_to_frame.pop(k, None)
            for k in [
                key for key in state.utility_context_keys if key[0] == raw_sid
            ]:
                state.utility_context_keys.discard(k)
            for k in [
                key for key in state.session_v8_to_juggler if key[0] == raw_sid
            ]:
                j_id = state.session_v8_to_juggler.pop(k, None)
                if not j_id:
                    continue
                if j_id.startswith("caller:"):
                    state.caller_windows.pop(j_id, None)
                    state.caller_v8_contexts.pop(j_id, None)
                    state.caller_intrinsics.pop(j_id, None)
                    for fk, fv in list(state.caller_contexts.items()):
                        if fv == j_id:
                            state.caller_contexts.pop(fk, None)
                else:
                    state.ctx_sessions.pop(j_id, None)
                    state.landing_ready.discard((raw_sid, j_id))
                    for fk, fv in list(state.utility_contexts.items()):
                        if fv == j_id:
                            state.utility_contexts.pop(fk, None)
                self.dispatch_event(
                    "Runtime.executionContextDestroyed",
                    {"executionContextId": j_id},
                    page_sid,
                )
            return

        if method == "Runtime.consoleAPICalled":
            ctx_raw = params.get("executionContextId")
            if ctx_raw is None or int(ctx_raw) == 0:
                return
            if (raw_sid, int(ctx_raw)) in state.utility_context_keys:
                return
            c_type = str(params.get("type") or "log")
            c_args = [_format_console_arg(a) for a in (params.get("args") or ())]
            loc = _extract_cdp_location(params.get("stackTrace"))
            self.dispatch_event(
                "Runtime.console",
                {"type": c_type, "args": c_args, "location": loc},
                page_sid,
            )
            return

        if method == "Runtime.exceptionThrown":
            exc_details = params.get("exceptionDetails") or {}
            ctx_raw = exc_details.get("executionContextId")
            if ctx_raw is not None and (raw_sid, int(ctx_raw)) in state.utility_context_keys:
                return
            exc_obj = exc_details.get("exception") or {}
            desc = exc_obj.get("description")
            if isinstance(desc, str) and desc:
                lines = desc.splitlines()
                msg_line = lines[0] if lines else "Error"
                stack_str = "\n".join(lines[1:]) if len(lines) > 1 else ""
            elif "value" in exc_obj:
                msg_line = str(exc_obj["value"])
                stack_str = ""
            else:
                msg_line = str(exc_details.get("text") or "Error")
                stack_str = ""
            loc = _extract_cdp_location(
                exc_details.get("stackTrace"),
                fallback_url=str(exc_details.get("url") or ""),
                fallback_line=int(exc_details.get("lineNumber") or 0),
                fallback_col=int(exc_details.get("columnNumber") or 0),
            )
            self.dispatch_event(
                "Page.uncaughtError",
                {"message": msg_line, "stack": stack_str, "location": loc},
                page_sid,
            )
            return

        if method == "Runtime.bindingCalled":
            native = params.get("name") or ""
            payload = params.get("payload")
            v8_ctx_raw = params.get("executionContextId")
            if not native or v8_ctx_raw is None:
                return
            v8_ctx_id = int(v8_ctx_raw)
            fid = state.main_world_to_frame.get((raw_sid, v8_ctx_id))
            caller_ctx = state.caller_contexts.get(fid) if fid else None
            ctx = self._get_ctx(state.context_id)
            entry = state.page_bindings.get(native) or ctx.bindings.get(native)
            if payload == _BIND_END_SENTINEL:
                if not entry:
                    return
                _wrapped, ev_name = entry

                def _resolve_binding_end() -> None:
                    try:
                        target_fid = fid or self._resolve_main_world_frame(
                            state, raw_sid, v8_ctx_id
                        )
                        target_caller = state.caller_contexts.get(target_fid)
                        if not target_caller:
                            return
                        extract_expr = (
                            "(() => {"
                            " let captured = null;"
                            f" dispatchEvent(new CustomEvent({json.dumps(ev_name)},"
                            "   { detail: (fn) => { captured = fn; } }));"
                            " return captured;"
                            " })()"
                        )
                        res = self._raw.send(
                            "Runtime.evaluate",
                            {
                                "expression": extract_expr,
                                "contextId": v8_ctx_id,
                                "returnByValue": False,
                            },
                            session=raw_sid,
                            timeout=5.0,
                        )
                        res_obj = res.get("result") or {}
                        end_oid = res_obj.get("objectId")
                        if end_oid:
                            self._record_result_object(state, raw_sid, res_obj)
                            self.dispatch_event(
                                "Page.bindingCalled",
                                {
                                    "executionContextId": target_caller,
                                    "name": native,
                                    "payload": {"bindingEnd": end_oid},
                                },
                                page_sid,
                            )
                    except (
                        juggler_conn.TargetClosedError,
                        juggler_conn.ProtocolError,
                    ):
                        pass

                threading.Thread(
                    target=_resolve_binding_end,
                    name="invpw-cdp-binding",
                    daemon=True,
                ).start()
                return

            if not caller_ctx:
                return
            self.dispatch_event(
                "Page.bindingCalled",
                {
                    "executionContextId": caller_ctx,
                    "name": native,
                    "payload": str(payload) if payload is not None else "",
                },
                page_sid,
            )
            return

        if method == "Page.frameAttached":
            fid = params["frameId"]
            parent_id = params.get("parentFrameId")
            state.frame_sessions.setdefault(fid, raw_sid)
            self.dispatch_event(
                "Page.frameAttached",
                {"frameId": fid, "parentFrameId": parent_id},
                page_sid,
            )
            self._request_utility_world(state, fid, sid=raw_sid)
            return

        if method == "Page.frameDetached":
            fid = params["frameId"]
            if params.get("reason") == "swap":
                return
            old_util = state.utility_contexts.pop(fid, None)
            if old_util:
                old_sid, old_int = self._parse_util_ctx(state, old_util)
                state.ctx_sessions.pop(old_util, None)
                state.landing_ready.discard((old_sid, old_util))
                state.utility_context_keys.discard((old_sid, old_int))
                state.session_v8_to_juggler.pop((old_sid, old_int), None)
            caller_id = state.caller_contexts.pop(fid, None)
            if caller_id:
                state.caller_windows.pop(caller_id, None)
                state.caller_v8_contexts.pop(caller_id, None)
                state.caller_intrinsics.pop(caller_id, None)
                for k, jv in list(state.session_v8_to_juggler.items()):
                    if jv == caller_id:
                        state.session_v8_to_juggler.pop(k, None)
            state.frame_sessions.pop(fid, None)
            for k, v in list(state.main_world_to_frame.items()):
                if v == fid:
                    state.main_world_to_frame.pop(k, None)
            self.dispatch_event(
                "Page.frameDetached", {"frameId": fid}, page_sid
            )
            return

        if method == "Page.frameNavigated":
            frame = params.get("frame") or {}
            fid = frame.get("id") or state.main_frame_id
            state.frame_sessions[fid] = raw_sid
            nav_id = frame.get("loaderId") or f"nav-{fid}-{state.epoch}"
            state.nav_loaders[fid] = nav_id
            if not frame.get("parentId") and raw_sid == page_sid:
                state.main_frame_id = fid
            self.dispatch_event(
                "Page.navigationCommitted",
                {
                    "frameId": fid,
                    "navigationId": nav_id,
                    "url": frame.get("url") or "",
                    "name": frame.get("name") or "",
                },
                page_sid,
            )
            if not state.ready_emitted and fid == state.main_frame_id:
                state.ready_emitted = True
                self.dispatch_event("Page.ready", {}, page_sid)
            if params.get("type") == "BackForwardCacheRestore":
                self._request_utility_world(state, fid, sid=raw_sid)
                for ev_name in ("DOMContentLoaded", "load"):
                    self.dispatch_event(
                        "Page.eventFired",
                        {"frameId": fid, "name": ev_name},
                        page_sid,
                    )
            return

        if method == "Page.navigatedWithinDocument":
            fid = params.get("frameId") or state.main_frame_id
            self.dispatch_event(
                "Page.sameDocumentNavigation",
                {"frameId": fid, "url": params.get("url") or ""},
                page_sid,
            )
            return

        if method == "Page.lifecycleEvent":
            fid = params.get("frameId") or state.main_frame_id
            name = params.get("name") or ""
            if name in ("DOMContentLoaded", "load"):
                self.dispatch_event(
                    "Page.eventFired",
                    {"frameId": fid, "name": name},
                    page_sid,
                )
            return

        if method == "Page.windowOpen":
            state.window_opened_seq += 1
            self._raw.resolve_pending_inputs(page_sid)
            return

        if method == "Page.javascriptDialogOpening":
            state.dialog_open = True
            self._raw.resolve_pending_inputs(page_sid)
            dialog_id = f"dlg-{int(time.monotonic() * 1000000)}"
            ev_params = {
                "dialogId": dialog_id,
                "type": params.get("type") or "alert",
                "message": params.get("message") or "",
                "defaultValue": params.get("defaultPrompt") or "",
            }
            self.dispatch_event("Page.dialogOpened", ev_params, page_sid)
            return

        if method == "Page.javascriptDialogClosed":
            state.dialog_open = False
            return

        if method == "Fetch.authRequired":
            req_id = params.get("requestId")
            net_id = params.get("networkId") or req_id or ""
            ctx = self._get_ctx(state.context_id)
            challenge = params.get("authChallenge") or {}
            source = str(challenge.get("source") or "Server")
            creds = (
                self._effective_proxy_auth(ctx)
                if source == "Proxy"
                else ctx.http_credentials
            )
            if req_id:
                auth_key = f"{net_id}:{source}"
                if creds:
                    if auth_key in state.attempted_auth:
                        self._raw.post(
                            "Fetch.continueWithAuth",
                            {
                                "requestId": req_id,
                                "authChallengeResponse": {"response": "CancelAuth"},
                            },
                            session=raw_sid,
                        )
                    else:
                        state.attempted_auth.add(auth_key)
                        self._raw.post(
                            "Fetch.continueWithAuth",
                            {
                                "requestId": req_id,
                                "authChallengeResponse": {
                                    "response": "ProvideCredentials",
                                    "username": creds.get("username") or "",
                                    "password": creds.get("password") or "",
                                },
                            },
                            session=raw_sid,
                        )
                else:
                    self._raw.post(
                        "Fetch.continueWithAuth",
                        {
                            "requestId": req_id,
                            "authChallengeResponse": {"response": "Default"},
                        },
                        session=raw_sid,
                    )
            return

        if method == "Fetch.requestPaused":
            fetch_id = params.get("requestId")
            net_id = params.get("networkId") or fetch_id
            ctx = self._get_ctx(state.context_id)
            if not (state.intercepting or ctx.intercepting):
                if fetch_id:
                    self._raw.post(
                        "Fetch.continueRequest",
                        {"requestId": fetch_id},
                        session=raw_sid,
                    )
                return
            state.paused_requests[net_id] = fetch_id
            state.paused_sessions[net_id] = raw_sid
            if raw_sid != page_sid or net_id not in state.req_sessions:
                state.req_sessions[net_id] = raw_sid
            req_obj = params.get("request") or {}
            fid = (
                params.get("frameId")
                or (state.req_frames.get(net_id) or ("", False))[0]
                or state.worker_frames.get(raw_sid)
                or state.session_root_frame.get(raw_sid)
                or state.main_frame_id
            )
            is_nav = params.get("resourceType") == "Document"
            if net_id:
                state.unpaused_requests.pop(net_id, None)
                state.req_frames[net_id] = (fid, is_nav)
            nav_id = f"nav-{net_id}" if is_nav else None
            if is_nav and nav_id:
                state.nav_loaders[fid] = nav_id
                self.dispatch_event(
                    "Page.navigationStarted",
                    {"frameId": fid, "navigationId": nav_id},
                    page_sid,
                )
            post_b64 = None
            if req_obj.get("postData") is not None:
                post_b64 = base64.b64encode(
                    str(req_obj["postData"]).encode("utf-8")
                ).decode("ascii")
            j_req = {
                "requestId": net_id,
                "url": req_obj.get("url") or "",
                "method": req_obj.get("method") or "GET",
                "headers": [
                    {"name": k, "value": str(v)}
                    for k, v in (req_obj.get("headers") or {}).items()
                ],
                "frameId": fid,
                "resourceType": (
                    state.req_types.get(net_id)
                    or params.get("resourceType")
                    or "Other"
                ),
                "isIntercepted": True,
                **({"navigationId": nav_id} if nav_id else {}),
                **({"postData": post_b64} if post_b64 is not None else {}),
            }
            # Browser-process Fetch.requestPaused collapses both fetch() and XHR into
            # resourceType="XHR" and can win the IPC race against an OOPIF or Worker
            # renderer's Network.requestWillBeSent (which carries exact "Fetch" vs "XHR").
            if (
                net_id
                and net_id not in state.req_types
                and (
                    params.get("resourceType") == "XHR"
                    or (not params.get("frameId") and state.worker_sessions)
                )
            ):
                state.deferred_paused[net_id] = j_req
                t = threading.Timer(
                    0.05,
                    lambda p=page_sid, n=net_id: self._ev_queue.put(
                        ("flush_paused", p, n)
                    ),
                )
                t.daemon = True
                t.start()
                return
            if net_id:
                state.sent_requests.add(net_id)
            self.dispatch_event("Network.requestWillBeSent", j_req, page_sid)
            return

        if method == "Network.requestWillBeSent":
            ctx = self._get_ctx(state.context_id)
            req_id = params.get("requestId")
            fid = (
                params.get("frameId")
                or state.worker_frames.get(raw_sid)
                or state.session_root_frame.get(raw_sid)
                or state.main_frame_id
            )
            is_nav = params.get("type") == "Document"
            if req_id:
                if raw_sid != page_sid or req_id not in state.req_sessions:
                    state.req_sessions[req_id] = raw_sid
                state.req_frames[req_id] = (fid, is_nav)
                if params.get("type"):
                    state.req_types[req_id] = str(params["type"])
                deferred = state.deferred_paused.pop(req_id, None)
                if deferred is not None:
                    deferred["frameId"] = fid
                    if params.get("type"):
                        deferred["resourceType"] = str(params["type"])
                    state.sent_requests.add(req_id)
                    self.dispatch_event("Network.requestWillBeSent", deferred, page_sid)
                    return
            req_obj = params.get("request") or {}
            nav_id = params.get("loaderId") if is_nav else None
            post_b64 = None
            if req_obj.get("postData") is not None:
                post_b64 = base64.b64encode(
                    str(req_obj["postData"]).encode("utf-8")
                ).decode("ascii")
            j_req = {
                "requestId": req_id,
                "url": req_obj.get("url") or "",
                "method": req_obj.get("method") or "GET",
                "headers": [
                    {"name": k, "value": str(v)}
                    for k, v in (req_obj.get("headers") or {}).items()
                ],
                "frameId": fid,
                "resourceType": params.get("type") or "Other",
                "isIntercepted": False,
                **({"navigationId": nav_id} if nav_id else {}),
                **({"postData": post_b64} if post_b64 is not None else {}),
            }
            if state.intercepting or ctx.intercepting:
                if req_id and req_id not in state.sent_requests:
                    state.unpaused_requests[req_id] = j_req
                return
            if req_id:
                if req_id in state.sent_requests and not params.get("redirectResponse"):
                    return
                state.sent_requests.add(req_id)
            if is_nav and nav_id:
                state.nav_loaders[fid] = nav_id
                self.dispatch_event(
                    "Page.navigationStarted",
                    {"frameId": fid, "navigationId": nav_id},
                    page_sid,
                )
            self.dispatch_event("Network.requestWillBeSent", j_req, page_sid)
            return

        if method == "Network.responseReceived":
            resp = params.get("response") or {}
            req_id = params.get("requestId")
            if req_id:
                if raw_sid != page_sid or req_id not in state.req_sessions:
                    state.req_sessions[req_id] = raw_sid
                with self._ctx_cv:
                    self._ctx_cv.notify_all()
                if req_id in state.responded_requests:
                    return
                state.responded_requests.add(req_id)
                unpaused = state.unpaused_requests.pop(req_id, None)
                if unpaused is not None and req_id not in state.sent_requests:
                    state.sent_requests.add(req_id)
                    self.dispatch_event("Network.requestWillBeSent", unpaused, page_sid)
            raw_headers = resp.get("headers") or {}
            for hk, hv in raw_headers.items():
                if (
                    str(hk).lower() == "content-disposition"
                    and str(hv).strip().lower().startswith("attachment")
                    and req_id
                ):
                    state.attachment_requests.add(req_id)
            headers = [
                {"name": k, "value": str(v)} for k, v in raw_headers.items()
            ]
            j_resp = {
                "requestId": req_id,
                "status": int(resp.get("status") or 0),
                "statusText": resp.get("statusText") or "",
                "headers": headers,
                "fromServiceWorker": bool(resp.get("fromServiceWorker")),
                "remoteIPAddress": resp.get("remoteIPAddress") or "",
                "remotePort": int(resp.get("remotePort") or 0),
                "protocolVersion": resp.get("protocol") or "",
            }
            self.dispatch_event("Network.responseReceived", j_resp, page_sid)
            return

        if method == "Network.loadingFinished":
            req_id = params.get("requestId")
            state.paused_requests.pop(req_id, None)
            if req_id:
                if raw_sid != page_sid:
                    state.req_sessions[req_id] = raw_sid
                state.attachment_requests.discard(req_id)
                state.attempted_auth.discard(f"{req_id}:Proxy")
                state.attempted_auth.discard(f"{req_id}:Server")
                with self._ctx_cv:
                    self._ctx_cv.notify_all()
                if req_id in state.finished_requests:
                    return
                state.finished_requests.add(req_id)
                self._note_request_completed(state, req_id)
                deferred = state.deferred_paused.pop(req_id, None)
                if deferred is not None and req_id not in state.sent_requests:
                    state.sent_requests.add(req_id)
                    self.dispatch_event("Network.requestWillBeSent", deferred, page_sid)
                unpaused = state.unpaused_requests.pop(req_id, None)
                if unpaused is not None and req_id not in state.sent_requests:
                    state.sent_requests.add(req_id)
                    self.dispatch_event("Network.requestWillBeSent", unpaused, page_sid)
            self.dispatch_event(
                "Network.requestFinished",
                {"requestId": req_id, "responseEndTime": params.get("timestamp") or 0},
                page_sid,
            )
            return

        if method == "Network.loadingFailed":
            req_id = params.get("requestId")
            state.paused_requests.pop(req_id, None)
            was_attachment = bool(req_id and req_id in state.attachment_requests)
            if req_id:
                if raw_sid != page_sid:
                    state.req_sessions[req_id] = raw_sid
                state.attachment_requests.discard(req_id)
                state.attempted_auth.discard(f"{req_id}:Proxy")
                state.attempted_auth.discard(f"{req_id}:Server")
                with self._ctx_cv:
                    self._ctx_cv.notify_all()
                if req_id in state.finished_requests:
                    return
                state.finished_requests.add(req_id)
                self._note_request_completed(state, req_id)
                deferred = state.deferred_paused.pop(req_id, None)
                if deferred is not None and req_id not in state.sent_requests:
                    state.sent_requests.add(req_id)
                    self.dispatch_event("Network.requestWillBeSent", deferred, page_sid)
                unpaused = state.unpaused_requests.pop(req_id, None)
                if unpaused is not None and req_id not in state.sent_requests:
                    state.sent_requests.add(req_id)
                    self.dispatch_event("Network.requestWillBeSent", unpaused, page_sid)
            err_text = params.get("errorText") or "net::ERR_FAILED"
            if params.get("type") == "Document":
                fid = params.get("frameId")
                if not fid:
                    if raw_sid != page_sid:
                        for cand_fid, cand_sid in state.frame_sessions.items():
                            if cand_sid == raw_sid:
                                fid = cand_fid
                                break
                    fid = fid or state.main_frame_id
                nav_id = state.nav_loaders.get(fid) or req_id
                nav_err = (
                    "Download is starting"
                    if (was_attachment and "ERR_ABORTED" in err_text)
                    else err_text
                )
                self.dispatch_event(
                    "Page.navigationAborted",
                    {"frameId": fid, "navigationId": nav_id, "errorText": nav_err},
                    page_sid,
                )
            self.dispatch_event(
                "Network.requestFailed",
                {
                    "requestId": req_id,
                    "errorCode": err_text,
                    "responseEndTime": params.get("timestamp") or 0,
                },
                page_sid,
            )
            return

    # -- Juggler Command Translation -----------------------------------------
    def send(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session: Optional[str] = None,
        timeout: Optional[float] = 30.0,
    ) -> Any:
        p = params or {}
        if session is None:
            return self._send_browser_command(method, p, timeout=timeout)
        return self._send_page_command(session, method, p, timeout=timeout)

    def _send_browser_command(
        self, method: str, p: Dict[str, Any], *, timeout: Optional[float]
    ) -> Any:
        if method == "Browser.enable":
            targets = self._raw.send("Target.getTargets", timeout=timeout).get(
                "targetInfos", []
            )
            for t in targets:
                if t.get("type") == "page":
                    self._initial_blank_targets.add(t["targetId"])
            self._raw.send(
                "Target.setDiscoverTargets", {"discover": True}, timeout=timeout
            )
            self._raw.send(
                "Target.setAutoAttach",
                {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
                timeout=timeout,
            )
            self._adopt_started = True
            return {}

        if method == "Browser.getInfo":
            ver = self._raw.send("Browser.getVersion", timeout=timeout)
            self._version_product = ver.get("product") or self._version_product
            self._user_agent = ver.get("userAgent") or self._user_agent
            self._drain_event_barrier(timeout=timeout)
            return {"version": self._version_product, "userAgent": self._user_agent}

        if method == "Browser.close":
            try:
                self._raw.send("Browser.close", timeout=3.0)
            except Exception:
                pass
            return {}

        if method == "Browser.createBrowserContext":
            dispose = bool(p.get("removeOnDetach", True))
            res = self._raw.send(
                "Target.createBrowserContext",
                {"disposeOnDetach": dispose},
                timeout=timeout,
            )
            cid = res["browserContextId"]
            with self._lock:
                self._contexts[cid] = _ContextState(
                    context_id=cid,
                    cdp_context_id=cid,
                    dispose_on_detach=dispose,
                )
                self._cdp_to_juggler_cid[cid] = cid
            return {"browserContextId": cid}

        if method == "Browser.removeBrowserContext":
            cid = p.get("browserContextId")
            cdp_cid: Optional[str] = None
            with self._lock:
                ctx_removed = self._contexts.pop(cid, None)
                if ctx_removed is not None:
                    cdp_cid = ctx_removed.cdp_context_id
                    if cdp_cid:
                        self._cdp_to_juggler_cid.pop(cdp_cid, None)
                elif cid:
                    cdp_cid = cid
                    self._cdp_to_juggler_cid.pop(cid, None)
            if cdp_cid:
                self._raw.send(
                    "Target.disposeBrowserContext",
                    {"browserContextId": cdp_cid},
                    timeout=timeout,
                )
            return {}

        if method == "Browser.newPage":
            cid = p.get("browserContextId")
            ctx = self._get_ctx(cid)
            ctx.has_created_pages = True
            create_params: Dict[str, Any] = {"url": "about:blank"}
            cdp_cid = self._cdp_cid(cid)
            if cdp_cid:
                create_params["browserContextId"] = cdp_cid
            res = self._raw.send("Target.createTarget", create_params, timeout=timeout)
            tid = res["targetId"]
            self._drain_event_barrier(timeout=timeout)
            with self._lock:
                init_err = self._target_init_errors.pop(tid, None)
            if init_err is not None:
                raise init_err
            while self._initial_blank_targets:
                stale_tid = self._initial_blank_targets.pop()
                if stale_tid != tid:
                    try:
                        self._raw.send(
                            "Target.closeTarget", {"targetId": stale_tid}, timeout=2.0
                        )
                    except Exception:
                        pass
            return {"targetId": tid}

        if method == "Browser.getCookies":
            cdp_cid = self._cdp_cid(p.get("browserContextId"))
            sp = {"browserContextId": cdp_cid} if cdp_cid else {}
            res = self._raw.send("Storage.getCookies", sp, timeout=timeout)
            return {"cookies": res.get("cookies") or []}

        if method == "Browser.setCookies":
            cdp_cid = self._cdp_cid(p.get("browserContextId"))
            cookies = []
            for c in p.get("cookies") or ():
                entry = dict(c)
                if cdp_cid:
                    entry["browserContextId"] = cdp_cid
                cookies.append(entry)
            if cookies:
                self._raw.send(
                    "Storage.setCookies", {"cookies": cookies}, timeout=timeout
                )
            return {}

        if method == "Browser.clearCookies":
            cdp_cid = self._cdp_cid(p.get("browserContextId"))
            sp = {"browserContextId": cdp_cid} if cdp_cid else {}
            self._raw.send("Storage.clearCookies", sp, timeout=timeout)
            return {}

        if method == "Browser.grantPermissions":
            cdp_cid = self._cdp_cid(p.get("browserContextId"))
            mapped_perms = [
                _JUGGLER_TO_CDP_PERMISSIONS.get(str(perm), str(perm))
                for perm in (p.get("permissions") or ())
            ]
            gp: Dict[str, Any] = {"permissions": mapped_perms}
            origin = p.get("origin")
            if origin and origin != "*":
                gp["origin"] = origin
            if cdp_cid:
                gp["browserContextId"] = cdp_cid
            self._raw.send("Browser.grantPermissions", gp, timeout=timeout)
            return {}

        if method == "Browser.resetPermissions":
            cdp_cid = self._cdp_cid(p.get("browserContextId"))
            rp = {"browserContextId": cdp_cid} if cdp_cid else {}
            self._raw.send("Browser.resetPermissions", rp, timeout=timeout)
            return {}

        if method == "Browser.cancelDownload":
            uuid = p.get("uuid")
            if uuid:
                with self._lock:
                    dl_cid = self._download_ctx_by_guid.get(uuid)
                cdp_dl_cid = self._cdp_cid(dl_cid)
                cp_dl: Dict[str, Any] = {"guid": uuid}
                if cdp_dl_cid:
                    cp_dl["browserContextId"] = cdp_dl_cid
                try:
                    self._raw.send("Browser.cancelDownload", cp_dl, timeout=timeout)
                except juggler_conn.ProtocolError:
                    pass
            return {}

        if method == "Browser.addBinding":
            cid = p.get("browserContextId")
            native = str(p["name"])
            script = p.get("script") or ""
            ev_name = f"__invpw_be_{secrets.token_hex(8)}"
            wrapped = _wrap_binding_script(native, ev_name, script)
            ctx = self._get_ctx(cid)
            ctx.bindings[native] = (wrapped, ev_name)
            with self._lock:
                matching_pages = [
                    st for st in self._pages.values() if st.context_id == cid
                ]
            for st in matching_pages:
                try:
                    self._ensure_raw_binding(st, native)
                    self._sync_doc_scripts(st, ctx)
                    self._eval_script_in_live_frames(st, wrapped, timeout=timeout)
                except juggler_conn.TargetClosedError:
                    pass
            return {}

        if method in ("Browser.setBrowserProxy", "Browser.setNoBrowserProxy"):
            raise juggler_conn.ProtocolError(
                f"Chromium backend does not support dynamic {method}; "
                "configure proxy at launch or in browser.new_context(proxy=...)"
            )

        if method == "Heap.collectGarbage":
            for st in list(self._pages.values()):
                for s in [st.session_id, *list(st.child_sessions)]:
                    try:
                        self._raw.send(
                            "HeapProfiler.collectGarbage",
                            session=s,
                            timeout=5.0,
                        )
                    except (juggler_conn.TargetClosedError, juggler_conn.ProtocolError):
                        pass
            return {}

        # Context-level stateful configuration commands
        cid = p.get("browserContextId")
        ctx = self._get_ctx(cid)
        if method == "Browser.setContextProxy":
            server_arg, bypass_str, proxy_auth = _format_proxy_spec(
                p, where="new_context(proxy=...)"
            )
            if (
                cid is None
                and self._persistent
                and not ctx.has_created_pages
                and server_arg == self._launch_proxy_server
            ):
                ctx.proxy_auth = proxy_auth
                return {}
            if cid is None or ctx.has_created_pages:
                raise juggler_conn.ProtocolError(
                    "Chromium backend does not support changing proxy on an active "
                    "BrowserContext; pass proxy=... at launch or to browser.new_context(proxy=...)"
                )
            create_ctx_params: Dict[str, Any] = {
                "disposeOnDetach": ctx.dispose_on_detach,
                "proxyServer": server_arg,
            }
            if bypass_str:
                create_ctx_params["proxyBypassList"] = bypass_str
            new_res = self._raw.send(
                "Target.createBrowserContext", create_ctx_params, timeout=timeout
            )
            new_cdp_cid = new_res["browserContextId"]
            old_cdp_cid = ctx.cdp_context_id
            with self._lock:
                if old_cdp_cid:
                    self._cdp_to_juggler_cid.pop(old_cdp_cid, None)
                ctx.cdp_context_id = new_cdp_cid
                ctx.proxy_server = server_arg
                ctx.proxy_bypass = bypass_str
                ctx.proxy_auth = proxy_auth
                self._cdp_to_juggler_cid[new_cdp_cid] = cid
            if old_cdp_cid and old_cdp_cid != new_cdp_cid:
                try:
                    self._raw.send(
                        "Target.disposeBrowserContext",
                        {"browserContextId": old_cdp_cid},
                        timeout=timeout,
                    )
                except juggler_conn.ProtocolError:
                    pass
            if ctx.download_options is not None:
                self._apply_download_options(cid, ctx.download_options, timeout=timeout)
            return {}
        elif method == "Browser.setDefaultViewport":
            ctx.viewport = p.get("viewport")
        elif method == "Browser.setTimezoneOverride":
            ctx.timezone_id = p.get("timezoneId")
        elif method == "Browser.setLocaleOverride":
            raw_loc = p.get("locale") or ""
            ctx.accept_languages = raw_loc
            ctx.locale = raw_loc.split(",")[0].strip()
        elif method == "Browser.setColorScheme":
            ctx.color_scheme = p.get("colorScheme")
        elif method == "Browser.setReducedMotion":
            ctx.reduced_motion = p.get("reducedMotion")
        elif method == "Browser.setForcedColors":
            ctx.forced_colors = p.get("forcedColors")
        elif method == "Browser.setUserAgentOverride":
            ctx.user_agent = p.get("userAgent")
        elif method == "Browser.setExtraHTTPHeaders":
            hdrs = p.get("headers") or []
            if isinstance(hdrs, list):
                ctx.extra_headers = {
                    h["name"]: h["value"] for h in hdrs if "name" in h
                }
            elif isinstance(hdrs, dict):
                ctx.extra_headers = dict(hdrs)
        elif method == "Browser.setOnlineOverride":
            ctx.offline = p.get("override") == "offline"
        elif method == "Browser.setGeolocationOverride":
            ctx.geolocation = p.get("geolocation")
        elif method == "Browser.setHTTPCredentials":
            ctx.http_credentials = p.get("credentials")
        elif method == "Browser.setIgnoreHTTPSErrors":
            ctx.ignore_https_errors = bool(p.get("ignoreHTTPSErrors"))
            if cid is None:
                try:
                    self._raw.send(
                        "Security.setIgnoreCertificateErrors",
                        {"ignore": ctx.ignore_https_errors},
                        timeout=timeout,
                    )
                except juggler_conn.ProtocolError:
                    pass
        elif method == "Browser.setBypassCSP":
            ctx.bypass_csp = bool(p.get("bypassCSP"))
        elif method == "Browser.setJavaScriptDisabled":
            ctx.js_disabled = bool(p.get("javaScriptDisabled"))
        elif method == "Browser.setServiceWorkersBlocked":
            ctx.service_workers_blocked = bool(p.get("blocked"))
        elif method == "Browser.setTouchOverride":
            ctx.has_touch = bool(p.get("hasTouch"))
        elif method == "Browser.setCacheDisabled":
            ctx.cache_disabled = bool(p.get("cacheDisabled"))
        elif method == "Browser.setInitScripts":
            ctx.init_scripts = [
                s.get("script") or "" for s in (p.get("scripts") or ())
            ]
        elif method == "Browser.setRequestInterception":
            ctx.intercepting = bool(p.get("enabled"))
        elif method == "Browser.setDownloadOptions":
            opts = dict(p.get("downloadOptions") or {})
            ctx.download_options = opts
            self._apply_download_options(cid, opts, timeout=timeout)
            return {}
        else:
            raise juggler_conn.ProtocolError(f"Unsupported browser command: {method}")

        with self._lock:
            matching_pages = [
                st for st in self._pages.values() if st.context_id == cid
            ]
        for st in matching_pages:
            try:
                self._apply_ctx_to_page(st, ctx)
            except juggler_conn.TargetClosedError:
                pass
        return {}

    def _resolve_caller_window(
        self, state: _PageSessionState, caller_ctx: str, *, timeout: Optional[float]
    ) -> str:
        cached = state.caller_windows.get(caller_ctx)
        if cached:
            return cached
        parts = caller_ctx.split(":")
        fid = parts[1] if len(parts) >= 2 else state.main_frame_id
        cur_caller = state.caller_contexts.get(fid)
        if cur_caller is not None and cur_caller != caller_ctx:
            raise juggler_conn.ProtocolError("Execution context was destroyed!")
        v8_pair = state.caller_v8_contexts.get(caller_ctx)
        frame_sid = (
            v8_pair[0] if v8_pair is not None else self._session_for_frame(state, fid)
        )
        oid: Optional[str] = None
        main_v8_ctx: Optional[int] = v8_pair[1] if v8_pair is not None else None
        if main_v8_ctx is None and cur_caller is None:
            for (cand_sid, cand_v8), cand_fid in list(state.main_world_to_frame.items()):
                if cand_fid == fid and cand_sid == frame_sid:
                    if main_v8_ctx is None or cand_v8 > main_v8_ctx:
                        main_v8_ctx = cand_v8

        if main_v8_ctx is not None:
            try:
                res = self._raw.send(
                    "Runtime.evaluate",
                    {
                        "expression": "window",
                        "contextId": main_v8_ctx,
                        "returnByValue": False,
                    },
                    session=frame_sid,
                    timeout=timeout,
                )
                res_obj = res.get("result") or {}
                oid = res_obj.get("objectId")
                self._record_result_object(state, frame_sid, res_obj)
            except juggler_conn.ProtocolError:
                state.main_world_to_frame.pop((frame_sid, main_v8_ctx), None)
                if v8_pair is not None:
                    raise juggler_conn.ProtocolError("Execution context was destroyed!")
                oid = None

        if not oid and (fid == state.main_frame_id or frame_sid != state.session_id):
            res = self._raw.send(
                "Runtime.evaluate",
                {"expression": "window", "returnByValue": False},
                session=frame_sid,
                timeout=timeout,
            )
            res_obj = res.get("result") or {}
            oid = res_obj.get("objectId")
            self._record_result_object(state, frame_sid, res_obj)
        elif not oid:
            owner = self._raw.send(
                "DOM.getFrameOwner",
                {"frameId": fid},
                session=state.session_id,
                timeout=timeout,
            )
            desc = self._raw.send(
                "DOM.describeNode",
                {"backendNodeId": owner["backendNodeId"], "pierce": True},
                session=state.session_id,
                timeout=timeout,
            )
            content_doc = (desc.get("node") or {}).get("contentDocument") or {}
            if "backendNodeId" in content_doc:
                doc_resolved = self._raw.send(
                    "DOM.resolveNode",
                    {"backendNodeId": content_doc["backendNodeId"]},
                    session=state.session_id,
                    timeout=timeout,
                )
                doc_oid = (doc_resolved.get("object") or {}).get("objectId")
                win_res = self._raw.send(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": doc_oid,
                        "functionDeclaration": "function() { return this.defaultView; }",
                        "returnByValue": False,
                    },
                    session=state.session_id,
                    timeout=timeout,
                )
                res_obj = win_res.get("result") or {}
                oid = res_obj.get("objectId")
                self._record_result_object(state, state.session_id, res_obj)
        if not oid:
            raise juggler_conn.ProtocolError(
                "Execution context was destroyed while resolving frame window"
            )
        state.caller_windows[caller_ctx] = oid
        return oid

    def _normalize_eval_response(
        self, state: _PageSessionState, sid: str, res: Dict[str, Any]
    ) -> Dict[str, Any]:
        if "exceptionDetails" in res:
            exc = res["exceptionDetails"] or {}
            exc_obj = exc.get("exception") or {}
            desc = (
                exc_obj.get("description") or exc.get("text") or "Evaluation failed"
            )
            return {"exceptionDetails": {"text": desc, "stack": ""}}
        res_obj = res.get("result") or {}
        self._record_result_object(state, sid, res_obj)
        return {"result": res_obj}

    def _send_page_command(
        self,
        session_id: str,
        method: str,
        p: Dict[str, Any],
        *,
        timeout: Optional[float],
    ) -> Any:
        with self._lock:
            state = self._pages.get(session_id)
        if state is None:
            raise juggler_conn.TargetClosedError(
                f"Target page has been closed ({method})"
            )

        if method == "Heap.collectGarbage":
            for s in [session_id, *list(state.child_sessions)]:
                try:
                    self._raw.send(
                        "HeapProfiler.collectGarbage",
                        {},
                        session=s,
                        timeout=timeout,
                    )
                except (juggler_conn.TargetClosedError, juggler_conn.ProtocolError):
                    pass
            return {}

        if method == "Page.setInitScripts":
            state.page_init_scripts = list(p.get("scripts") or ())
            ctx = self._get_ctx(state.context_id)
            self._sync_doc_scripts(state, ctx)
            return {}

        if method == "Page.addBinding":
            native = str(p["name"])
            script = p.get("script") or ""
            ev_name = f"__invpw_be_{secrets.token_hex(8)}"
            wrapped = _wrap_binding_script(native, ev_name, script)
            state.page_bindings[native] = (wrapped, ev_name)
            ctx = self._get_ctx(state.context_id)
            self._ensure_raw_binding(state, native)
            self._sync_doc_scripts(state, ctx)
            self._eval_script_in_live_frames(state, wrapped, timeout=timeout)
            return {}

        if method == "Page.navigate":
            fid = p.get("frameId") or state.main_frame_id
            frame_sid = self._session_for_frame(state, fid)
            nav_params: Dict[str, Any] = {"url": p["url"], "frameId": fid}
            if p.get("referer"):
                nav_params["referrer"] = p["referer"]
            try:
                res = self._raw.send(
                    "Page.navigate", nav_params, session=frame_sid, timeout=timeout
                )
            except juggler_conn.ProtocolError:
                if frame_sid != session_id:
                    res = self._raw.send(
                        "Page.navigate", nav_params, session=session_id, timeout=timeout
                    )
                else:
                    raise
            if res.get("errorText"):
                raise juggler_conn.ProtocolError(res["errorText"])
            return {"navigationId": res.get("loaderId")}

        if method == "Page.reload":
            self._raw.send("Page.reload", {}, session=session_id, timeout=timeout)
            return {}

        if method in ("Page.goBack", "Page.goForward"):
            hist = self._raw.send(
                "Page.getNavigationHistory", {}, session=session_id, timeout=timeout
            )
            idx = int(hist.get("currentIndex", 0))
            entries = hist.get("entries") or []
            target_idx = idx - 1 if method == "Page.goBack" else idx + 1
            if target_idx < 0 or target_idx >= len(entries):
                return {"success": False}
            self._raw.send(
                "Page.navigateToHistoryEntry",
                {"entryId": entries[target_idx]["id"]},
                session=session_id,
                timeout=timeout,
            )
            return {"success": True}

        if method == "Page.close":
            if p.get("runBeforeUnload"):
                self._raw.send("Page.close", {}, session=session_id, timeout=timeout)
            else:
                self._raw.send(
                    "Target.closeTarget",
                    {"targetId": state.target_id},
                    timeout=timeout,
                )
            return {}

        if method == "Page.bringToFront":
            self._raw.send("Page.bringToFront", {}, session=session_id, timeout=timeout)
            return {}

        if method == "Page.setViewportSize":
            vp = p.get("viewportSize")
            if vp:
                self._raw.send(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": int(vp["width"]),
                        "height": int(vp["height"]),
                        "deviceScaleFactor": state.dpr,
                        "mobile": False,
                    },
                    session=session_id,
                    timeout=timeout,
                )
            return {}

        if method == "Page.setEmulatedMedia":
            media_params: Dict[str, Any] = {}
            if "type" in p and p["type"] is not None:
                media_params["media"] = p["type"]
            features = []
            for key, css_name in (
                ("colorScheme", "prefers-color-scheme"),
                ("reducedMotion", "prefers-reduced-motion"),
                ("forcedColors", "forced-colors"),
            ):
                if key in p and p[key] is not None:
                    val = "" if p[key] in ("null", "no-override") else str(p[key])
                    features.append({"name": css_name, "value": val})
            if features:
                media_params["features"] = features
            for s in [session_id, *list(state.child_sessions)]:
                try:
                    self._raw.send(
                        "Emulation.setEmulatedMedia",
                        media_params,
                        session=s,
                        timeout=timeout,
                    )
                except juggler_conn.TargetClosedError:
                    pass
            return {}

        if method == "Page.screenshot":
            fmt = "jpeg" if p.get("mimeType") == "image/jpeg" else "png"
            clip = dict(p["clip"])
            scale = (1.0 / state.dpr) if p.get("omitDeviceScaleFactor") else 1.0
            clip["scale"] = scale
            cap_params: Dict[str, Any] = {
                "format": fmt,
                "clip": clip,
                "captureBeyondViewport": True,
            }
            if p.get("quality") is not None and fmt == "jpeg":
                cap_params["quality"] = int(p["quality"])
            res = self._raw.send(
                "Page.captureScreenshot",
                cap_params,
                session=session_id,
                timeout=timeout,
            )
            return {"data": res.get("data") or ""}

        if method == "Page.startScreencast":
            sc_params: Dict[str, Any] = {
                "format": "jpeg",
                "quality": int(p.get("quality", 80)),
                "maxWidth": int(p.get("width", 1280)),
                "maxHeight": int(p.get("height", 800)),
                "everyNthFrame": 1,
            }
            self._raw.send(
                "Page.startScreencast", sc_params, session=session_id, timeout=timeout
            )
            return {"screencastId": f"sc-{session_id}-{int(time.monotonic() * 1000)}"}

        if method == "Page.stopScreencast":
            self._raw.send(
                "Page.stopScreencast", {}, session=session_id, timeout=timeout
            )
            return {}

        if method == "Page.handleDialog":
            accept = bool(p.get("accept", True))
            dlg_params: Dict[str, Any] = {"accept": accept}
            if p.get("promptText") is not None:
                dlg_params["promptText"] = str(p["promptText"])
            self._raw.send(
                "Page.handleJavaScriptDialog",
                dlg_params,
                session=session_id,
                timeout=timeout,
            )
            return {}

        if method == "Page.setInterceptFileChooserDialog":
            for s in [session_id, *list(state.child_sessions)]:
                try:
                    self._raw.send(
                        "Page.setInterceptFileChooserDialog",
                        {"enabled": bool(p.get("enabled"))},
                        session=s,
                        timeout=timeout,
                    )
                except juggler_conn.TargetClosedError:
                    pass
            return {}

        if method == "Page.scrollIntoViewIfNeeded":
            oid = p["objectId"]
            obj_sid = self._session_for_object(state, oid)
            s_params: Dict[str, Any] = {"objectId": oid}
            if p.get("rect") is not None:
                s_params["rect"] = p["rect"]
            self._raw.send(
                "DOM.scrollIntoViewIfNeeded",
                s_params,
                session=obj_sid,
                timeout=timeout,
            )
            return {}

        if method == "Page.getFrameOrigin":
            fid = p.get("frameId") or state.main_frame_id
            if fid == state.main_frame_id:
                return {
                    "x": 0.0,
                    "y": 0.0,
                    "ex": {"x": 1.0, "y": 0.0},
                    "ey": {"x": 0.0, "y": 1.0},
                }
            try:
                owner = None
                owner_sid = session_id
                for cand_sid in [session_id, *list(state.child_sessions)]:
                    try:
                        owner = self._raw.send(
                            "DOM.getFrameOwner",
                            {"frameId": fid},
                            session=cand_sid,
                            timeout=timeout,
                        )
                        owner_sid = cand_sid
                        break
                    except juggler_conn.ProtocolError:
                        continue
                if owner is not None:
                    backend_id = owner["backendNodeId"]
                    box = self._raw.send(
                        "DOM.getBoxModel",
                        {"backendNodeId": backend_id},
                        session=owner_sid,
                        timeout=timeout,
                    )
                    content = (box.get("model") or {}).get("content") or []
                    if len(content) >= 8:
                        x1, y1, x2, y2, _x3, _y3, x4, y4 = (
                            float(v) for v in content[:8]
                        )
                        # DOM.getBoxModel returns document coordinates in Blink; subtract owner-frame scroll
                        # and recursively add parent frame origin if nested inside a parent iframe.
                        parent_off_x, parent_off_y = 0.0, 0.0
                        if owner_sid != session_id:
                            parent_fid = next(
                                (
                                    pfid
                                    for pfid, psid in state.frame_sessions.items()
                                    if psid == owner_sid
                                ),
                                None,
                            )
                            if parent_fid and parent_fid != state.main_frame_id:
                                p_orig = self._send_page_command(
                                    session_id,
                                    "Page.getFrameOrigin",
                                    {"frameId": parent_fid},
                                    timeout=timeout,
                                )
                                parent_off_x = float(p_orig.get("x") or 0.0)
                                parent_off_y = float(p_orig.get("y") or 0.0)

                        owner_fid = (
                            state.main_frame_id
                            if owner_sid == session_id
                            else next(
                                (
                                    pfid
                                    for pfid, psid in state.frame_sessions.items()
                                    if psid == owner_sid
                                ),
                                state.main_frame_id,
                            )
                        )
                        owner_util = state.utility_contexts.get(owner_fid)
                        sx, sy = 0.0, 0.0
                        if owner_util:
                            o_sid, o_int = self._parse_util_ctx(state, owner_util)
                            s_res = self._raw.send(
                                "Runtime.evaluate",
                                {
                                    "expression": "({x: window.scrollX, y: window.scrollY})",
                                    "contextId": o_int,
                                    "returnByValue": True,
                                },
                                session=o_sid,
                                timeout=timeout,
                            )
                            s_val = ((s_res.get("result") or {}).get("value")) or {}
                            sx = float(s_val.get("x") or 0.0)
                            sy = float(s_val.get("y") or 0.0)

                        util_ctx = state.utility_contexts.get(fid)
                        cw, ch = 0.0, 0.0
                        if util_ctx:
                            u_sid, u_int = self._parse_util_ctx(state, util_ctx)
                            d_res = self._raw.send(
                                "Runtime.evaluate",
                                {
                                    "expression": "({w: window.innerWidth, h: window.innerHeight})",
                                    "contextId": u_int,
                                    "returnByValue": True,
                                },
                                session=u_sid,
                                timeout=timeout,
                            )
                            d_val = ((d_res.get("result") or {}).get("value")) or {}
                            cw = float(d_val.get("w") or 0.0)
                            ch = float(d_val.get("h") or 0.0)
                        if cw <= 0:
                            cw = max(math.hypot(x2 - x1, y2 - y1), 1.0)
                        if ch <= 0:
                            ch = max(math.hypot(x4 - x1, y4 - y1), 1.0)
                        return {
                            "x": parent_off_x + x1 - sx,
                            "y": parent_off_y + y1 - sy,
                            "ex": {"x": (x2 - x1) / cw, "y": (y2 - y1) / cw},
                            "ey": {"x": (x4 - x1) / ch, "y": (y4 - y1) / ch},
                        }
            except (juggler_conn.TargetClosedError, juggler_conn.ProtocolError):
                pass
            util_ctx = state.utility_contexts.get(fid)
            if util_ctx:
                u_sid, u_int = self._parse_util_ctx(state, util_ctx)
                fallback_expr = (
                    "(() => {"
                    "  let x = 0, y = 0, w = window;"
                    "  while (w.frameElement) {"
                    "    const fe = w.frameElement;"
                    "    const r = fe.getBoundingClientRect();"
                    "    const s = w.parent.getComputedStyle(fe);"
                    "    x += r.left + fe.clientLeft + (parseFloat(s.paddingLeft) || 0);"
                    "    y += r.top + fe.clientTop + (parseFloat(s.paddingTop) || 0);"
                    "    w = w.parent;"
                    "  }"
                    "  return {x, y};"
                    "})()"
                )
                f_res = self._raw.send(
                    "Runtime.evaluate",
                    {
                        "expression": fallback_expr,
                        "contextId": u_int,
                        "returnByValue": True,
                    },
                    session=u_sid,
                    timeout=timeout,
                )
                f_val = ((f_res.get("result") or {}).get("value")) or {}
                return {
                    "x": float(f_val.get("x") or 0.0),
                    "y": float(f_val.get("y") or 0.0),
                    "ex": {"x": 1.0, "y": 0.0},
                    "ey": {"x": 0.0, "y": 1.0},
                }
            return {
                "x": 0.0,
                "y": 0.0,
                "ex": {"x": 1.0, "y": 0.0},
                "ey": {"x": 0.0, "y": 1.0},
            }

        if method == "Runtime.evaluate":
            ctx_id = str(p["executionContextId"])
            expr = p["expression"]
            by_value = bool(p.get("returnByValue", True))
            if '"browserName": "firefox"' in expr:
                expr = expr.replace(
                    '"browserName": "firefox"', '"browserName": "chromium"'
                )
            elif expr == "({x: window.mozInnerScreenX, y: window.mozInnerScreenY})":
                expr = (
                    "(() => {"
                    "  let x = 0, y = 0, w = window;"
                    "  while (w.frameElement) {"
                    "    const fe = w.frameElement;"
                    "    const r = fe.getBoundingClientRect();"
                    "    const s = w.parent.getComputedStyle(fe);"
                    "    x += r.left + fe.clientLeft + (parseFloat(s.paddingLeft) || 0);"
                    "    y += r.top + fe.clientTop + (parseFloat(s.paddingTop) || 0);"
                    "    w = w.parent;"
                    "  }"
                    "  return {x, y};"
                    "})()"
                )
            eval_sid = session_id
            try:
                if ctx_id.startswith("caller:"):
                    parts = ctx_id.split(":")
                    fid = parts[1] if len(parts) >= 2 else state.main_frame_id
                    cur_caller = state.caller_contexts.get(fid)
                    if cur_caller is not None and cur_caller != ctx_id:
                        raise juggler_conn.ProtocolError("Execution context was destroyed!")
                    v8_pair = state.caller_v8_contexts.get(ctx_id)
                    v8_ctx_id = v8_pair[1] if v8_pair else None
                    frame_sid = (
                        v8_pair[0] if v8_pair else self._session_for_frame(state, fid)
                    )
                    eval_sid = frame_sid
                    intr_oid = self._ensure_caller_intrinsics(
                        state, ctx_id, eval_sid, v8_ctx_id, timeout=timeout
                    )
                    res = None
                    if intr_oid:
                        decl = (
                            "function() {\n"
                            "  return (function(__invpw_i) {\n"
                            f"    const {{ {_INTRINSICS_DESTRUCTURE} }} = __invpw_i;\n"
                            f"    return (\n{expr}\n    );\n"
                            "  }).call(globalThis, this);\n"
                            "}"
                        )
                        try:
                            cand = self._raw.send(
                                "Runtime.callFunctionOn",
                                {
                                    "objectId": intr_oid,
                                    "functionDeclaration": decl,
                                    "returnByValue": by_value,
                                    "awaitPromise": True,
                                },
                                session=eval_sid,
                                timeout=timeout,
                            )
                            exc_det = cand.get("exceptionDetails") or {}
                            exc_desc = str(
                                (exc_det.get("exception") or {}).get("description")
                                or exc_det.get("text")
                                or ""
                            )
                            if "SyntaxError" not in exc_desc:
                                res = cand
                            else:
                                comp = self._raw.send(
                                    "Runtime.compileScript",
                                    {
                                        "expression": f"(\n{expr}\n)",
                                        "sourceURL": "",
                                        "persistScript": False,
                                        **(
                                            {"executionContextId": v8_ctx_id}
                                            if v8_ctx_id is not None
                                            else {}
                                        ),
                                    },
                                    session=eval_sid,
                                    timeout=timeout,
                                )
                                if not comp.get("exceptionDetails"):
                                    res = cand
                        except juggler_conn.ProtocolError as i_exc:
                            if "Cannot find object" in str(i_exc):
                                state.caller_intrinsics.pop(ctx_id, None)
                            elif any(
                                m in str(i_exc)
                                for m in (
                                    "Cannot find context",
                                    "Execution context was destroyed",
                                    "Inspected target navigated or closed",
                                )
                            ):
                                state.caller_intrinsics.pop(ctx_id, None)
                                raise
                            else:
                                raise
                    if res is None:
                        if v8_ctx_id is not None:
                            res = self._raw.send(
                                "Runtime.evaluate",
                                {
                                    "expression": expr,
                                    "contextId": v8_ctx_id,
                                    "returnByValue": by_value,
                                    "awaitPromise": True,
                                },
                                session=eval_sid,
                                timeout=timeout,
                            )
                        elif (
                            cur_caller is None
                            and (fid == state.main_frame_id or frame_sid != session_id)
                        ):
                            res = self._raw.send(
                                "Runtime.evaluate",
                                {
                                    "expression": expr,
                                    "returnByValue": by_value,
                                    "awaitPromise": True,
                                },
                                session=eval_sid,
                                timeout=timeout,
                            )
                        else:
                            win_oid = self._resolve_caller_window(
                                state, ctx_id, timeout=timeout
                            )
                            eval_sid = self._session_for_object(state, win_oid)
                            res = self._raw.send(
                                "Runtime.callFunctionOn",
                                {
                                    "objectId": win_oid,
                                    "functionDeclaration": (
                                        f"function() {{ return (0, eval)({json.dumps(expr)}); }}"
                                    ),
                                    "returnByValue": by_value,
                                    "awaitPromise": True,
                                },
                                session=eval_sid,
                                timeout=timeout,
                            )
                else:
                    eval_sid, eval_int = self._parse_util_ctx(state, ctx_id)
                    res = self._raw.send(
                        "Runtime.evaluate",
                        {
                            "expression": expr,
                            "contextId": eval_int,
                            "returnByValue": by_value,
                            "awaitPromise": True,
                        },
                        session=eval_sid,
                        timeout=timeout,
                    )
            except juggler_conn.ProtocolError as exc:
                if any(
                    msg in str(exc)
                    for msg in (
                        "Cannot find context",
                        "Execution context was destroyed",
                        "Cannot find default execution context",
                        "Inspected target navigated or closed",
                    )
                ):
                    raise juggler_conn.ProtocolError(
                        "Execution context was destroyed!"
                    ) from exc
                raise
            return self._normalize_eval_response(state, eval_sid, res)

        if method == "Runtime.callFunction":
            ctx_id = str(p["executionContextId"])
            decl = p["functionDeclaration"]
            args = list(p.get("args") or ())
            by_value = bool(p.get("returnByValue", True))
            call_sid = session_id
            try:
                if ctx_id.startswith("caller:"):
                    first_oid = (
                        args[0].get("objectId")
                        if (args and isinstance(args[0], dict))
                        else None
                    )
                    v8_pair = state.caller_v8_contexts.get(ctx_id)
                    if first_oid:
                        call_sid = self._session_for_object(state, first_oid)
                        res = self._raw.send(
                            "Runtime.callFunctionOn",
                            {
                                "objectId": first_oid,
                                "functionDeclaration": (
                                    f"function(...__a) {{ return ({decl}).call(globalThis, this, ...__a); }}"
                                ),
                                "arguments": args[1:],
                                "returnByValue": by_value,
                                "awaitPromise": True,
                            },
                            session=call_sid,
                            timeout=timeout,
                        )
                    elif v8_pair is not None:
                        call_sid, call_int = v8_pair
                        res = self._raw.send(
                            "Runtime.callFunctionOn",
                            {
                                "executionContextId": call_int,
                                "functionDeclaration": decl,
                                "arguments": args,
                                "returnByValue": by_value,
                                "awaitPromise": True,
                            },
                            session=call_sid,
                            timeout=timeout,
                        )
                    else:
                        win_oid = self._resolve_caller_window(
                            state, ctx_id, timeout=timeout
                        )
                        call_sid = self._session_for_object(state, win_oid)
                        res = self._raw.send(
                            "Runtime.callFunctionOn",
                            {
                                "objectId": win_oid,
                                "functionDeclaration": decl,
                                "arguments": args,
                                "returnByValue": by_value,
                                "awaitPromise": True,
                            },
                            session=call_sid,
                            timeout=timeout,
                        )
                else:
                    call_sid, call_int = self._parse_util_ctx(state, ctx_id)
                    res = self._raw.send(
                        "Runtime.callFunctionOn",
                        {
                            "executionContextId": call_int,
                            "functionDeclaration": decl,
                            "arguments": args,
                            "returnByValue": by_value,
                            "awaitPromise": True,
                        },
                        session=call_sid,
                        timeout=timeout,
                    )
            except juggler_conn.ProtocolError as exc:
                if any(
                    msg in str(exc)
                    for msg in (
                        "Cannot find context",
                        "Execution context was destroyed",
                        "Cannot find object with id",
                        "Argument should belong to the same JavaScript world",
                        "Inspected target navigated or closed",
                    )
                ):
                    raise juggler_conn.ProtocolError(
                        "Execution context was destroyed!"
                    ) from exc
                raise
            return self._normalize_eval_response(state, call_sid, res)

        if method == "Runtime.getObjectProperties":
            oid = p["objectId"]
            obj_sid = self._session_for_object(state, oid)
            res = self._raw.send(
                "Runtime.getProperties",
                {"objectId": oid, "ownProperties": True},
                session=obj_sid,
                timeout=timeout,
            )
            props = []
            for item in res.get("result") or ():
                if not item.get("enumerable", True):
                    continue
                if "value" in item:
                    self._record_result_object(state, obj_sid, item["value"])
                    props.append({"name": item["name"], "value": item["value"]})
            return {"properties": props}

        if method == "Runtime.disposeObject":
            oid = p.get("objectId")
            if oid:
                obj_sid = state.obj_sessions.pop(oid, session_id)
                try:
                    self._raw.send(
                        "Runtime.releaseObject",
                        {"objectId": oid},
                        session=obj_sid,
                        timeout=timeout,
                    )
                except (juggler_conn.TargetClosedError, juggler_conn.ProtocolError):
                    pass
            return {}

        if method == "Page.getContentQuads":
            oid = p["objectId"]
            obj_sid = self._session_for_object(state, oid)
            try:
                res = self._raw.send(
                    "DOM.getContentQuads",
                    {"objectId": oid},
                    session=obj_sid,
                    timeout=timeout,
                )
            except juggler_conn.ProtocolError:
                return {"quads": []}
            ox, oy = 0.0, 0.0
            ex_x, ex_y = 1.0, 0.0
            ey_x, ey_y = 0.0, 1.0
            if obj_sid != session_id:
                root_fid = state.session_root_frame.get(obj_sid) or next(
                    (
                        pfid
                        for pfid, psid in state.frame_sessions.items()
                        if psid == obj_sid
                    ),
                    None,
                )
                if root_fid and root_fid != state.main_frame_id:
                    orig = self._send_page_command(
                        session_id,
                        "Page.getFrameOrigin",
                        {"frameId": root_fid},
                        timeout=timeout,
                    )
                    ox = float(orig.get("x") or 0.0)
                    oy = float(orig.get("y") or 0.0)
                    ex = orig.get("ex") or {}
                    ey = orig.get("ey") or {}
                    ex_x = float(ex.get("x", 1.0))
                    ex_y = float(ex.get("y", 0.0))
                    ey_x = float(ey.get("x", 0.0))
                    ey_y = float(ey.get("y", 1.0))

            def _map_pt(px: float, py: float) -> Dict[str, float]:
                return {
                    "x": ox + px * ex_x + py * ey_x,
                    "y": oy + px * ex_y + py * ey_y,
                }

            out_quads = []
            for q in res.get("quads") or ():
                if len(q) >= 8:
                    out_quads.append(
                        {
                            "p1": _map_pt(float(q[0]), float(q[1])),
                            "p2": _map_pt(float(q[2]), float(q[3])),
                            "p3": _map_pt(float(q[4]), float(q[5])),
                            "p4": _map_pt(float(q[6]), float(q[7])),
                        }
                    )
            return {"quads": out_quads}

        if method == "Page.describeNode":
            oid = p["objectId"]
            obj_sid = self._session_for_object(state, oid)
            desc = self._raw.send(
                "DOM.describeNode",
                {"objectId": oid, "pierce": True},
                session=obj_sid,
                timeout=timeout,
            )
            node = desc.get("node") or {}
            cdoc = node.get("contentDocument") or {}
            cfid = cdoc.get("frameId") or node.get("frameId")
            return {"contentFrameId": cfid} if cfid else {}

        if method == "Page.adoptNode":
            target_sid, target_ctx = self._parse_util_ctx(
                state, str(p["executionContextId"])
            )
            backend_id = None
            if p.get("objectId"):
                oid = p["objectId"]
                obj_sid = self._session_for_object(state, oid)
                desc = self._raw.send(
                    "DOM.describeNode",
                    {"objectId": oid},
                    session=obj_sid,
                    timeout=timeout,
                )
                backend_id = (desc.get("node") or {}).get("backendNodeId")
            else:
                fid = p["frameId"]
                for cand_sid in [session_id, *list(state.child_sessions)]:
                    try:
                        owner = self._raw.send(
                            "DOM.getFrameOwner",
                            {"frameId": fid},
                            session=cand_sid,
                            timeout=timeout,
                        )
                        backend_id = owner.get("backendNodeId")
                        if backend_id:
                            break
                    except juggler_conn.ProtocolError:
                        continue
            if not backend_id:
                return {"remoteObject": {}}
            resolved = self._raw.send(
                "DOM.resolveNode",
                {"backendNodeId": backend_id, "executionContextId": target_ctx},
                session=target_sid,
                timeout=timeout,
            )
            robj = resolved.get("object") or {}
            self._record_result_object(state, target_sid, robj)
            return {"remoteObject": robj}

        direct = self._build_cdp_input_command(session_id, method, p)
        if direct is not None:
            cdp_method, cdp_params, transform = direct
            raw_res = self._raw.send(
                cdp_method, cdp_params, session=session_id, timeout=timeout
            )
            return transform(raw_res) if transform is not None else {}

        if method == "Page.dispatchTapEvent":
            x, y = float(p["x"]), float(p["y"])
            mods = _cdp_modifiers(p.get("modifiers") or 0)
            for t_type, pts in (
                ("touchStart", [{"x": x, "y": y}]),
                ("touchEnd", []),
            ):
                self._raw.send(
                    "Input.dispatchTouchEvent",
                    {"type": t_type, "touchPoints": pts, "modifiers": mods},
                    session=session_id,
                    timeout=timeout,
                )
            return {}

        if method == "Page.selectOptions":
            oid = p["objectId"]
            obj_sid = self._session_for_object(state, oid)
            indices = list(p.get("indices") or ())
            self._raw.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": oid,
                    "functionDeclaration": """function(indices) {
                        const set = new Set(indices);
                        for (let i = 0; i < this.options.length; i++) {
                            this.options[i].selected = set.has(i);
                        }
                        this.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
                        this.dispatchEvent(new Event('change', { bubbles: true }));
                    }""",
                    "arguments": [{"value": indices}],
                },
                session=obj_sid,
                timeout=timeout,
            )
            return {}

        if method == "Page.setUserInput":
            oid = p["objectId"]
            obj_sid = self._session_for_object(state, oid)
            self._raw.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": oid,
                    "functionDeclaration": """function(val) {
                        const proto = Object.getPrototypeOf(this);
                        const desc = Object.getOwnPropertyDescriptor(proto, 'value')
                            || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                        if (desc && desc.set) desc.set.call(this, val);
                        else this.value = val;
                        this.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
                        this.dispatchEvent(new Event('change', { bubbles: true }));
                    }""",
                    "arguments": [{"value": p.get("value") or ""}],
                },
                session=obj_sid,
                timeout=timeout,
            )
            return {}

        if method == "Page.setFileInputFiles":
            oid = p["objectId"]
            obj_sid = self._session_for_object(state, oid)
            self._raw.send(
                "DOM.setFileInputFiles",
                {"objectId": oid, "files": list(p.get("files") or ())},
                session=obj_sid,
                timeout=timeout,
            )
            return {}

        if method == "Network.setRequestInterception":
            state.intercepting = bool(p.get("enabled"))
            ctx = self._get_ctx(state.context_id)
            self._sync_fetch_domain(state, ctx)
            return {}

        if method == "Network.getResponseBody":
            req_id = p["requestId"]
            req_sid = state.req_sessions.get(req_id, session_id)
            res: Optional[Dict[str, Any]] = None
            try:
                res = self._raw.send(
                    "Network.getResponseBody",
                    {"requestId": req_id},
                    session=req_sid,
                    timeout=timeout,
                )
            except juggler_conn.ProtocolError as b_exc:
                req_fid, is_nav = state.req_frames.get(req_id, ("", False))
                if "No resource with given identifier found" not in str(b_exc) or (
                    not is_nav and not state.child_sessions and not state.worker_sessions
                ):
                    raise
                last_err: Exception = b_exc
                wait_cap = min(float(timeout) if timeout else 2.5, 2.5)
                deadline = time.monotonic() + wait_cap
                while not self._closed and session_id in self._pages:
                    cand_sids: List[str] = []
                    cur_rsid = state.req_sessions.get(req_id)
                    if cur_rsid and cur_rsid != req_sid:
                        cand_sids.append(cur_rsid)
                    fsid = state.frame_sessions.get(req_fid) if req_fid else None
                    if fsid and fsid not in cand_sids and fsid != req_sid:
                        cand_sids.append(fsid)
                    for csid in [*list(state.child_sessions), *list(state.worker_sessions)]:
                        if csid not in cand_sids and csid != req_sid:
                            cand_sids.append(csid)
                    for csid in cand_sids:
                        try:
                            res = self._raw.send(
                                "Network.getResponseBody",
                                {"requestId": req_id},
                                session=csid,
                                timeout=min(1.5, max(0.2, deadline - time.monotonic())),
                            )
                            state.req_sessions[req_id] = csid
                            break
                        except juggler_conn.ProtocolError as c_exc:
                            last_err = c_exc
                    if res is not None:
                        break
                    rem = deadline - time.monotonic()
                    if rem <= 0:
                        break
                    with self._ctx_cv:
                        self._ctx_cv.wait(timeout=min(0.025, rem))
                if res is None:
                    raise last_err
            body = res.get("body") or ""
            if not res.get("base64Encoded"):
                body = base64.b64encode(body.encode("utf-8")).decode("ascii")
            return {"base64body": body}

        if method == "Network.resumeInterceptedRequest":
            net_id = p["requestId"]
            req_sid = state.paused_sessions.pop(
                net_id, state.req_sessions.get(net_id, session_id)
            )
            fetch_id = state.paused_requests.pop(net_id, net_id)
            cp: Dict[str, Any] = {"requestId": fetch_id}
            if p.get("url"):
                cp["url"] = p["url"]
            if p.get("method"):
                cp["method"] = p["method"]
            if p.get("postData") is not None:
                cp["postData"] = p["postData"]
            if p.get("headers") is not None:
                cp["headers"] = p["headers"]
            self._raw.send(
                "Fetch.continueRequest", cp, session=req_sid, timeout=timeout
            )
            return {}

        if method == "Network.fulfillInterceptedRequest":
            net_id = p["requestId"]
            req_sid = state.paused_sessions.pop(
                net_id, state.req_sessions.get(net_id, session_id)
            )
            fetch_id = state.paused_requests.pop(net_id, net_id)
            fp: Dict[str, Any] = {
                "requestId": fetch_id,
                "responseCode": int(p.get("status") or 200),
                "responsePhrase": p.get("statusText") or "OK",
                "responseHeaders": list(p.get("headers") or ()),
            }
            if p.get("base64body") is not None:
                fp["body"] = p["base64body"]
            self._raw.send(
                "Fetch.fulfillRequest", fp, session=req_sid, timeout=timeout
            )
            return {}

        if method == "Network.abortInterceptedRequest":
            net_id = p["requestId"]
            req_sid = state.paused_sessions.pop(
                net_id, state.req_sessions.get(net_id, session_id)
            )
            fetch_id = state.paused_requests.pop(net_id, net_id)
            reason = _ABORT_REASON_MAP.get(
                str(p.get("errorCode") or "failed").lower(), "Failed"
            )
            self._raw.send(
                "Fetch.failRequest",
                {"requestId": fetch_id, "errorReason": reason},
                session=req_sid,
                timeout=timeout,
            )
            return {}

        raise juggler_conn.ProtocolError(f"Unsupported page command: {method}")

    def close(self) -> None:
        if self._close_called:
            return
        self._close_called = True
        already_closed = self._closed
        self._closed = True
        with self._ctx_cv:
            self._ctx_cv.notify_all()
        if not already_closed:
            try:
                self._raw.send("Browser.close", timeout=2.5)
            except Exception:
                pass
        self._raw.close()
        self._ev_queue.put(None)
        if self._process.poll() is None:
            try:
                self._process.wait(timeout=3.5)
            except subprocess.TimeoutExpired:
                self._process.terminate()
                try:
                    self._process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    try:
                        self._process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        pass
        if hasattr(self._process, "close_guardian"):
            self._process.close_guardian()
        if self._process.stderr:
            try:
                self._process.stderr.close()
            except Exception:
                pass
        self.announce_closed()
