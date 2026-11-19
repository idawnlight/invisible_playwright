"""Juggler-compatible connection bridge over Chromium --remote-debugging-pipe.

Allows the entire Playwright dispatcher graph in `_juggler/server.py` and the
shared engines in `invisible_core.juggler` (`Lifecycle`, `InjectedScript`,
`Actions`, `Keyboard`) to drive patched Linux Desktop Chromium over POSIX fd 3/4
without calling `Runtime.enable`.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from invisible_core.juggler import connection as juggler_conn
from invisible_core.juggler.deadline import Deadline
from invisible_core.juggler.injected import CALLER_WORLD, UTILITY_WORLD

NUL = b"\x00"

# Juggler bitmask: Alt=1, Control=2, Shift=4, Meta=8
# Chromium CDP bitmask: Alt=1, Control=2, Meta=4, Shift=8
_JUGGLER_TO_CDP_MODS = (
    (1, 1),  # Alt
    (2, 2),  # Control
    (4, 8),  # Shift -> 8
    (8, 4),  # Meta -> 4
)

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


def _cdp_modifiers(juggler_mask: int) -> int:
    out = 0
    mask = int(juggler_mask or 0)
    for j_bit, c_bit in _JUGGLER_TO_CDP_MODS:
        if mask & j_bit:
            out |= c_bit
    return out


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
        self._pending: Dict[int, tuple[threading.Event, List[Any], str]] = {}
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
    ) -> None:
        with self._lock:
            if self.closed:
                return
            self._next_id += 1
            msg_id = self._next_id
        self._write_frame(msg_id, method, params, session)

    def send(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session: Optional[str] = None,
        timeout: Optional[float] = 30.0,
    ) -> Dict[str, Any]:
        done = threading.Event()
        box: List[Any] = []
        with self._lock:
            if self.closed:
                raise juggler_conn.TargetClosedError(
                    f"Target page, context or browser has been closed ({method})"
                )
            self._next_id += 1
            msg_id = self._next_id
            self._pending[msg_id] = (done, box, method)

        try:
            self._write_frame(msg_id, method, params, session)
        except OSError as exc:
            with self._lock:
                self._pending.pop(msg_id, None)
            raise juggler_conn.TargetClosedError(
                f"Browser pipe closed while sending {method}: {exc}"
            ) from exc

        if not done.wait(timeout=timeout):
            with self._lock:
                self._pending.pop(msg_id, None)
            raise juggler_conn.ReplyTimeout(
                f"{method} timed out after {timeout:.1f}s"
            )

        kind, payload = box[0]
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
                    "Inspected target navigated or closed",
                )
            ):
                raise juggler_conn.TargetClosedError(f"{method}: {err_msg}")
            raise juggler_conn.ProtocolError(f"{method}: {err_msg}")
        return payload or {}

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
            if waiter is not None:
                done, box, _ = waiter
                if "error" in msg:
                    box.append(("error", msg["error"]))
                else:
                    box.append(("ok", msg.get("result") or {}))
                done.set()
            return
        method = msg.get("method")
        if method:
            self._on_event(msg.get("sessionId"), method, msg.get("params") or {})

    def _fail_pending(self, reason: str) -> None:
        with self._lock:
            self.closed = True
            pending = list(self._pending.values())
            self._pending.clear()
        for done, box, _ in pending:
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
    bypass_csp: bool = False
    has_touch: bool = False
    cache_disabled: bool = False
    intercepting: bool = False
    init_scripts: List[str] = field(default_factory=list)


@dataclass
class _PageSessionState:
    session_id: str
    target_id: str
    context_id: Optional[str]
    main_frame_id: str = ""
    dpr: float = 1.0
    intercepting: bool = False
    page_init_scripts: List[Dict[str, Any]] = field(default_factory=list)
    installed_doc_scripts: List[str] = field(default_factory=list)
    # frame_id -> utility executionContextId (numeric int as str)
    utility_contexts: Dict[str, str] = field(default_factory=dict)
    # frame_id -> caller synthetic executionContextId ("caller:<fid>:<epoch>")
    caller_contexts: Dict[str, str] = field(default_factory=dict)
    # caller_ctx -> cached window objectId in that frame's main world
    caller_windows: Dict[str, str] = field(default_factory=dict)
    # CDP Fetch requestId <-> Network requestId tracking
    paused_requests: Dict[str, str] = field(default_factory=dict)  # networkId -> fetchRequestId
    pending_fetch_events: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    nav_loaders: Dict[str, str] = field(default_factory=dict)  # frameId -> current loaderId
    dialog_open: bool = False
    epoch: int = 0


class CDPBridgeConnection(juggler_conn.EventListeners):
    """Translates Juggler commands/events onto Chromium Pipe CDP."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        write_fd: int,
        read_fd: int,
        *,
        ready_timeout: Optional[float] = 60.0,
        proxy_auth: Optional[Dict[str, str]] = None,
        persistent: bool = False,
    ) -> None:
        super().__init__()
        self._process = process
        self.output = _StderrDrain(process.stderr) if process.stderr else None
        self._startup = Deadline(ready_timeout)
        self._proxy_auth = proxy_auth
        self._persistent = persistent
        self._lock = threading.RLock()
        self._contexts: Dict[Optional[str], _ContextState] = {
            None: _ContextState(context_id=None)
        }
        self._pages: Dict[str, _PageSessionState] = {}  # session_id -> state
        self._target_to_session: Dict[str, str] = {}
        self._initial_blank_targets: set[str] = set()
        self._adopt_started = False
        self._closed = False
        self._version_product = ""
        self._user_agent = ""

        self._ev_queue: "queue.Queue[Optional[tuple[Optional[str], str, Dict[str, Any]]]]" = (
            queue.Queue()
        )
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
        prefs_file.write_text(json.dumps(prefs_data), encoding="utf-8")

        w, h = window_size
        args = [
            str(executable),
            f"--user-data-dir={profile_path}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            "--remote-debugging-pipe",
            f"--window-size={w},{h}",
            "--ozone-platform=x11",
            "--use-gl=angle",
            "--use-angle=gl",
        ]
        if no_sandbox:
            args.append("--no-sandbox")

        proxy_auth: Optional[Dict[str, str]] = None
        if proxy:
            p_type = proxy.get("type") or "http"
            p_host = proxy.get("host") or ""
            p_port = proxy.get("port")
            scheme = "socks5" if p_type == "socks" else p_type
            server_arg = f"{scheme}://{p_host}:{p_port}" if p_port else f"{scheme}://{p_host}"
            args.append(f"--proxy-server={server_arg}")
            bypass = proxy.get("bypass")
            if bypass:
                args.append(f"--proxy-bypass-list={','.join(bypass)}")
            if proxy.get("username"):
                proxy_auth = {
                    "username": str(proxy.get("username") or ""),
                    "password": str(proxy.get("password") or ""),
                }

        args.extend(argv_extra)
        args.append("about:blank")

        launch_env = dict(env)
        if not Path("/dev/dri/renderD128").exists() and "LIBGL_ALWAYS_SOFTWARE" not in launch_env:
            launch_env["LIBGL_ALWAYS_SOFTWARE"] = "1"

        c_read_fd, p_write_fd = os.pipe()
        p_read_fd, c_write_fd = os.pipe()

        def _setup_child_fds() -> None:
            r_fd, w_fd = c_read_fd, c_write_fd
            if w_fd == 3:
                w_fd = os.dup(w_fd)
            if r_fd != 3:
                os.dup2(r_fd, 3)
            if w_fd != 4:
                os.dup2(w_fd, 4)
            for extra in {c_read_fd, c_write_fd, w_fd} - {3, 4}:
                try:
                    os.close(extra)
                except OSError:
                    pass
            os.set_inheritable(3, True)
            os.set_inheritable(4, True)

        try:
            proc = subprocess.Popen(
                args,
                env=launch_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                pass_fds=(c_read_fd, c_write_fd, 3, 4),
                preexec_fn=_setup_child_fds,
            )
        except BaseException:
            os.close(p_write_fd)
            os.close(p_read_fd)
            raise
        finally:
            os.close(c_read_fd)
            os.close(c_write_fd)

        try:
            return cls(
                proc,
                write_fd=p_write_fd,
                read_fd=p_read_fd,
                ready_timeout=ready_timeout,
                proxy_auth=proxy_auth,
                persistent=persistent,
            )
        except BaseException:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
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

    def post(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session: Optional[str] = None,
    ) -> None:
        if method == "Runtime.disposeObject" and params and params.get("objectId"):
            self._raw.post(
                "Runtime.releaseObject",
                {"objectId": params["objectId"]},
                session=session,
            )
            return
        if method == "Page.stopScreencast":
            self._raw.post("Page.stopScreencast", {}, session=session)
            return
        try:
            self.send(method, params, session=session, timeout=5.0)
        except Exception:
            pass

    def _enqueue_cdp_event(
        self, session_id: Optional[str], method: str, params: Dict[str, Any]
    ) -> None:
        self._ev_queue.put((session_id, method, params))

    def _on_pipe_closed(self) -> None:
        self._closed = True
        self._ev_queue.put(None)
        self.announce_closed()

    def _event_worker_loop(self) -> None:
        while True:
            item = self._ev_queue.get()
            if item is None:
                break
            session_id, method, params = item
            try:
                self._handle_cdp_event(session_id, method, params)
            except Exception:
                pass

    # ── Context & Page Initialization ─────────────────────────────────────
    def _get_ctx(self, context_id: Optional[str]) -> _ContextState:
        with self._lock:
            state = self._contexts.get(context_id)
            if state is None:
                state = _ContextState(context_id=context_id)
                self._contexts[context_id] = state
            return state

    def _init_attached_page(
        self, session_id: str, target_info: Dict[str, Any]
    ) -> None:
        target_id = target_info["targetId"]
        raw_cid = target_info.get("browserContextId")
        with self._lock:
            cid = raw_cid if raw_cid in self._contexts else None
            if target_id in self._initial_blank_targets:
                return
            state = _PageSessionState(
                session_id=session_id, target_id=target_id, context_id=cid
            )
            self._pages[session_id] = state
            self._target_to_session[target_id] = session_id

        self._raw.send("Page.enable", session=session_id)
        self._raw.send(
            "Page.setLifecycleEventsEnabled", {"enabled": True}, session=session_id
        )
        self._raw.send("DOM.enable", session=session_id)
        self._raw.send("Network.enable", session=session_id)

        ctx = self._get_ctx(cid)
        self._apply_ctx_to_page(state, ctx)

        tree_resp = self._raw.send("Page.getFrameTree", session=session_id)
        root_tree = tree_resp["frameTree"]
        main_frame = root_tree["frame"]
        state.main_frame_id = main_frame["id"]

        juggler_info = {
            "targetId": target_id,
            "type": "page",
            "url": target_info.get("url") or "about:blank",
            "browserContextId": cid,
        }
        if target_info.get("openerId"):
            juggler_info["openerId"] = target_info["openerId"]

        self.dispatch_event(
            "Browser.attachedToTarget",
            {"sessionId": session_id, "targetInfo": juggler_info},
            None,
        )

        self._announce_frame_tree(state, root_tree, parent_id=None)
        self.dispatch_event("Page.ready", {}, session_id)

    def _announce_frame_tree(
        self,
        state: _PageSessionState,
        node: Dict[str, Any],
        parent_id: Optional[str],
    ) -> None:
        frame = node["frame"]
        fid = frame["id"]
        nav_id = frame.get("loaderId") or f"init-{fid}"
        state.nav_loaders[fid] = nav_id

        self.dispatch_event(
            "Page.frameAttached",
            {"frameId": fid, "parentFrameId": parent_id},
            state.session_id,
        )
        self._mint_worlds_for_frame(state, fid)
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
            self._announce_frame_tree(state, child, parent_id=fid)

    def _mint_worlds_for_frame(self, state: _PageSessionState, frame_id: str) -> None:
        old_util = state.utility_contexts.pop(frame_id, None)
        old_caller = state.caller_contexts.pop(frame_id, None)
        if old_caller:
            state.caller_windows.pop(old_caller, None)
            self.dispatch_event(
                "Runtime.executionContextDestroyed",
                {"executionContextId": old_caller},
                state.session_id,
            )
        if old_util:
            self.dispatch_event(
                "Runtime.executionContextDestroyed",
                {"executionContextId": old_util},
                state.session_id,
            )

        res = self._raw.send(
            "Page.createIsolatedWorld",
            {
                "frameId": frame_id,
                "worldName": UTILITY_WORLD,
                "grantUniveralAccess": True,
            },
            session=state.session_id,
        )
        util_id = str(res["executionContextId"])
        state.epoch += 1
        caller_id = f"caller:{frame_id}:{state.epoch}"
        state.utility_contexts[frame_id] = util_id
        state.caller_contexts[frame_id] = caller_id

        self.dispatch_event(
            "Runtime.executionContextCreated",
            {
                "executionContextId": util_id,
                "auxData": {"frameId": frame_id, "name": UTILITY_WORLD},
            },
            state.session_id,
        )
        self.dispatch_event(
            "Runtime.executionContextCreated",
            {
                "executionContextId": caller_id,
                "auxData": {"frameId": frame_id, "name": CALLER_WORLD},
            },
            state.session_id,
        )

    def _apply_ctx_to_page(
        self, state: _PageSessionState, ctx: _ContextState
    ) -> None:
        sid = state.session_id
        if ctx.viewport:
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
                self._raw.send(
                    "Emulation.setDeviceMetricsOverride", metrics, session=sid
                )
        if ctx.timezone_id:
            try:
                self._raw.send(
                    "Emulation.setTimezoneOverride",
                    {"timezoneId": ctx.timezone_id},
                    session=sid,
                )
            except Exception:
                pass
        if ctx.locale:
            primary_locale = ctx.locale.split(",")[0].strip()
            if primary_locale:
                try:
                    self._raw.send(
                        "Emulation.setLocaleOverride",
                        {"locale": primary_locale},
                        session=sid,
                    )
                except Exception:
                    pass
        if ctx.user_agent or ctx.accept_languages:
            ua_params: Dict[str, Any] = {
                "userAgent": ctx.user_agent or self._user_agent
            }
            if ctx.accept_languages:
                ua_params["acceptLanguage"] = ctx.accept_languages
            self._raw.send("Emulation.setUserAgentOverride", ua_params, session=sid)
        if any(
            v is not None
            for v in (ctx.color_scheme, ctx.reduced_motion, ctx.forced_colors)
        ):
            features = []
            if ctx.color_scheme is not None:
                features.append({"name": "prefers-color-scheme", "value": ctx.color_scheme})
            if ctx.reduced_motion is not None:
                features.append({"name": "prefers-reduced-motion", "value": ctx.reduced_motion})
            if ctx.forced_colors is not None:
                features.append({"name": "forced-colors", "value": ctx.forced_colors})
            self._raw.send(
                "Emulation.setEmulatedMedia", {"features": features}, session=sid
            )
        if ctx.extra_headers:
            self._raw.send(
                "Network.setExtraHTTPHeaders",
                {"headers": dict(ctx.extra_headers)},
                session=sid,
            )
        if ctx.offline:
            self._raw.send(
                "Network.emulateNetworkConditions",
                {
                    "offline": True,
                    "latency": 0,
                    "downloadThroughput": -1,
                    "uploadThroughput": -1,
                },
                session=sid,
            )
        if ctx.geolocation:
            self._raw.send(
                "Emulation.setGeolocationOverride",
                {
                    "latitude": ctx.geolocation.get("latitude"),
                    "longitude": ctx.geolocation.get("longitude"),
                    "accuracy": ctx.geolocation.get("accuracy", 100),
                },
                session=sid,
            )
        if ctx.bypass_csp:
            self._raw.send("Page.setBypassCSP", {"enabled": True}, session=sid)
        if ctx.has_touch:
            self._raw.send(
                "Emulation.setTouchEmulationEnabled", {"enabled": True}, session=sid
            )
        if ctx.cache_disabled:
            self._raw.send(
                "Network.setCacheDisabled", {"cacheDisabled": True}, session=sid
            )
        self._sync_doc_scripts(state, ctx)
        self._sync_fetch_domain(state, ctx)

    def _sync_doc_scripts(
        self, state: _PageSessionState, ctx: _ContextState
    ) -> None:
        sid = state.session_id
        for script_id in state.installed_doc_scripts:
            try:
                self._raw.send(
                    "Page.removeScriptToEvaluateOnNewDocument",
                    {"identifier": script_id},
                    session=sid,
                )
            except Exception:
                pass
        state.installed_doc_scripts.clear()

        for src in ctx.init_scripts:
            if src:
                res = self._raw.send(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": src},
                    session=sid,
                )
                if res.get("identifier"):
                    state.installed_doc_scripts.append(res["identifier"])
        for entry in state.page_init_scripts:
            src = entry.get("script") or ""
            w_name = entry.get("worldName")
            if not src:
                continue
            params: Dict[str, Any] = {"source": src}
            if w_name and entry.get("worldKind") != "caller":
                params["worldName"] = w_name
            res = self._raw.send(
                "Page.addScriptToEvaluateOnNewDocument", params, session=sid
            )
            if res.get("identifier"):
                state.installed_doc_scripts.append(res["identifier"])

    def _sync_fetch_domain(
        self, state: _PageSessionState, ctx: _ContextState
    ) -> None:
        needs_auth = bool(self._proxy_auth or ctx.http_credentials)
        needs_intercept = bool(state.intercepting or ctx.intercepting)
        if needs_intercept or needs_auth:
            params: Dict[str, Any] = {"handleAuthRequests": needs_auth}
            if needs_intercept:
                params["patterns"] = [{"urlPattern": "*", "requestStage": "Request"}]
            else:
                params["patterns"] = []
            self._raw.send("Fetch.enable", params, session=state.session_id)
        else:
            try:
                self._raw.send("Fetch.disable", session=state.session_id)
            except Exception:
                pass

    # ── CDP Event Translation ─────────────────────────────────────────────
    def _handle_cdp_event(
        self, session_id: Optional[str], method: str, params: Dict[str, Any]
    ) -> None:
        if method == "Target.attachedToTarget":
            info = params.get("targetInfo") or {}
            child_sid = params.get("sessionId")
            if info.get("type") == "page" and child_sid:
                if not self._adopt_started:
                    self._initial_blank_targets.add(info["targetId"])
                    return
                self._init_attached_page(child_sid, info)
            return

        if method == "Target.detachedFromTarget":
            child_sid = params.get("sessionId")
            if child_sid:
                with self._lock:
                    st = self._pages.pop(child_sid, None)
                    if st:
                        self._target_to_session.pop(st.target_id, None)
                if st:
                    self.dispatch_event(
                        "Browser.detachedFromTarget",
                        {"sessionId": child_sid, "targetId": st.target_id},
                        None,
                    )
            return

        if not session_id:
            return
        with self._lock:
            state = self._pages.get(session_id)
        if state is None:
            return

        if method == "Page.frameAttached":
            fid = params["frameId"]
            parent_id = params.get("parentFrameId")
            self.dispatch_event(
                "Page.frameAttached",
                {"frameId": fid, "parentFrameId": parent_id},
                session_id,
            )
            try:
                self._mint_worlds_for_frame(state, fid)
            except Exception:
                pass
            return

        if method == "Page.frameDetached":
            fid = params["frameId"]
            state.utility_contexts.pop(fid, None)
            caller_id = state.caller_contexts.pop(fid, None)
            if caller_id:
                state.caller_windows.pop(caller_id, None)
            self.dispatch_event(
                "Page.frameDetached", {"frameId": fid}, session_id
            )
            return

        if method == "Page.frameNavigated":
            frame = params.get("frame") or {}
            fid = frame.get("id") or state.main_frame_id
            nav_id = frame.get("loaderId") or f"nav-{fid}-{state.epoch}"
            state.nav_loaders[fid] = nav_id
            if not frame.get("parentId"):
                state.main_frame_id = fid
            self.dispatch_event(
                "Page.navigationCommitted",
                {
                    "frameId": fid,
                    "navigationId": nav_id,
                    "url": frame.get("url") or "",
                    "name": frame.get("name") or "",
                },
                session_id,
            )
            try:
                self._mint_worlds_for_frame(state, fid)
            except Exception:
                pass
            if params.get("type") == "BackForwardCacheRestore":
                for ev_name in ("DOMContentLoaded", "load"):
                    self.dispatch_event(
                        "Page.eventFired",
                        {"frameId": fid, "name": ev_name},
                        session_id,
                    )
            return

        if method == "Page.navigatedWithinDocument":
            fid = params.get("frameId") or state.main_frame_id
            self.dispatch_event(
                "Page.sameDocumentNavigation",
                {"frameId": fid, "url": params.get("url") or ""},
                session_id,
            )
            return

        if method == "Page.lifecycleEvent":
            fid = params.get("frameId") or state.main_frame_id
            name = params.get("name") or ""
            if name in ("DOMContentLoaded", "load"):
                self.dispatch_event(
                    "Page.eventFired",
                    {"frameId": fid, "name": name},
                    session_id,
                )
            return

        if method == "Page.javascriptDialogOpening":
            state.dialog_open = True
            dialog_id = f"dlg-{int(time.monotonic() * 1000000)}"
            ev_params = {
                "dialogId": dialog_id,
                "type": params.get("type") or "alert",
                "message": params.get("message") or "",
                "defaultValue": params.get("defaultPrompt") or "",
            }
            self.dispatch_event("Page.dialogOpened", ev_params, session_id)
            return

        if method == "Page.javascriptDialogClosed":
            state.dialog_open = False
            return

        if method == "Fetch.authRequired":
            req_id = params.get("requestId")
            ctx = self._get_ctx(state.context_id)
            challenge = params.get("authChallenge") or {}
            creds = (
                self._proxy_auth
                if challenge.get("source") == "Proxy"
                else ctx.http_credentials
            )
            if req_id:
                if creds:
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
                        session=session_id,
                    )
                else:
                    self._raw.post(
                        "Fetch.continueWithAuth",
                        {
                            "requestId": req_id,
                            "authChallengeResponse": {"response": "Default"},
                        },
                        session=session_id,
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
                        session=session_id,
                    )
                return
            state.paused_requests[net_id] = fetch_id
            req_obj = params.get("request") or {}
            fid = params.get("frameId") or state.main_frame_id
            is_nav = params.get("resourceType") == "Document"
            nav_id = f"nav-{net_id}" if is_nav else None
            if is_nav and nav_id:
                self.dispatch_event(
                    "Page.navigationStarted",
                    {"frameId": fid, "navigationId": nav_id},
                    session_id,
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
                "resourceType": (params.get("resourceType") or "Other").lower(),
                "isIntercepted": True,
                **({"navigationId": nav_id} if nav_id else {}),
                **({"postData": post_b64} if post_b64 is not None else {}),
            }
            self.dispatch_event("Network.requestWillBeSent", j_req, session_id)
            return

        if method == "Network.requestWillBeSent":
            ctx = self._get_ctx(state.context_id)
            if state.intercepting or ctx.intercepting:
                # Fetch.requestPaused announces the intercepted request with isIntercepted=True.
                return
            req_id = params.get("requestId")
            req_obj = params.get("request") or {}
            fid = params.get("frameId") or state.main_frame_id
            is_nav = params.get("type") == "Document"
            nav_id = params.get("loaderId") if is_nav else None
            if is_nav and nav_id:
                self.dispatch_event(
                    "Page.navigationStarted",
                    {"frameId": fid, "navigationId": nav_id},
                    session_id,
                )
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
                "resourceType": (params.get("type") or "Other").lower(),
                "isIntercepted": False,
                **({"navigationId": nav_id} if nav_id else {}),
                **({"postData": post_b64} if post_b64 is not None else {}),
            }
            self.dispatch_event("Network.requestWillBeSent", j_req, session_id)
            return

        if method == "Network.responseReceived":
            resp = params.get("response") or {}
            req_id = params.get("requestId")
            headers = [
                {"name": k, "value": str(v)}
                for k, v in (resp.get("headers") or {}).items()
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
            self.dispatch_event("Network.responseReceived", j_resp, session_id)
            return

        if method == "Network.loadingFinished":
            req_id = params.get("requestId")
            state.paused_requests.pop(req_id, None)
            self.dispatch_event(
                "Network.requestFinished",
                {"requestId": req_id, "responseEndTime": params.get("timestamp") or 0},
                session_id,
            )
            return

        if method == "Network.loadingFailed":
            req_id = params.get("requestId")
            state.paused_requests.pop(req_id, None)
            err_text = params.get("errorText") or "net::ERR_FAILED"
            if params.get("type") == "Document":
                fid = state.main_frame_id
                nav_id = state.nav_loaders.get(fid) or req_id
                self.dispatch_event(
                    "Page.navigationAborted",
                    {"frameId": fid, "navigationId": nav_id, "errorText": err_text},
                    session_id,
                )
            self.dispatch_event(
                "Network.requestFailed",
                {
                    "requestId": req_id,
                    "errorCode": err_text,
                    "responseEndTime": params.get("timestamp") or 0,
                },
                session_id,
            )
            return

    # ── Juggler Command Translation ───────────────────────────────────────
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
                {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
                timeout=timeout,
            )
            self._adopt_started = True
            return {}

        if method == "Browser.getInfo":
            return {"version": self._version_product, "userAgent": self._user_agent}

        if method == "Browser.close":
            try:
                self._raw.send("Browser.close", timeout=3.0)
            except Exception:
                pass
            return {}

        if method == "Browser.createBrowserContext":
            res = self._raw.send(
                "Target.createBrowserContext",
                {"disposeOnDetach": bool(p.get("removeOnDetach", True))},
                timeout=timeout,
            )
            cid = res["browserContextId"]
            with self._lock:
                self._contexts[cid] = _ContextState(context_id=cid)
            return {"browserContextId": cid}

        if method == "Browser.removeBrowserContext":
            cid = p.get("browserContextId")
            with self._lock:
                self._contexts.pop(cid, None)
            if cid:
                self._raw.send(
                    "Target.disposeBrowserContext",
                    {"browserContextId": cid},
                    timeout=timeout,
                )
            return {}

        if method == "Browser.newPage":
            cid = p.get("browserContextId")
            create_params: Dict[str, Any] = {"url": "about:blank"}
            if cid:
                create_params["browserContextId"] = cid
            res = self._raw.send("Target.createTarget", create_params, timeout=timeout)
            tid = res["targetId"]
            # Clean up unused startup blank tab once the first real page exists.
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
            cid = p.get("browserContextId")
            sp = {"browserContextId": cid} if cid else {}
            res = self._raw.send("Storage.getCookies", sp, timeout=timeout)
            return {"cookies": res.get("cookies") or []}

        if method == "Browser.setCookies":
            cid = p.get("browserContextId")
            cookies = []
            for c in p.get("cookies") or ():
                entry = dict(c)
                if cid:
                    entry["browserContextId"] = cid
                cookies.append(entry)
            if cookies:
                self._raw.send(
                    "Storage.setCookies", {"cookies": cookies}, timeout=timeout
                )
            return {}

        if method == "Browser.clearCookies":
            cid = p.get("browserContextId")
            sp = {"browserContextId": cid} if cid else {}
            self._raw.send("Storage.clearCookies", sp, timeout=timeout)
            return {}

        if method == "Browser.grantPermissions":
            cid = p.get("browserContextId")
            gp: Dict[str, Any] = {"permissions": list(p.get("permissions") or ())}
            origin = p.get("origin")
            if origin and origin != "*":
                gp["origin"] = origin
            if cid:
                gp["browserContextId"] = cid
            try:
                self._raw.send("Browser.grantPermissions", gp, timeout=timeout)
            except Exception:
                pass
            return {}

        if method == "Browser.resetPermissions":
            cid = p.get("browserContextId")
            rp = {"browserContextId": cid} if cid else {}
            self._raw.send("Browser.resetPermissions", rp, timeout=timeout)
            return {}

        # Context-level stateful configuration commands
        cid = p.get("browserContextId")
        ctx = self._get_ctx(cid)
        if method == "Browser.setDefaultViewport":
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
                ctx.extra_headers = {h["name"]: h["value"] for h in hdrs if "name" in h}
            elif isinstance(hdrs, dict):
                ctx.extra_headers = dict(hdrs)
        elif method == "Browser.setOnlineOverride":
            ctx.offline = p.get("override") == "offline"
        elif method == "Browser.setGeolocationOverride":
            ctx.geolocation = p.get("geolocation")
        elif method == "Browser.setHTTPCredentials":
            ctx.http_credentials = p.get("credentials")
        elif method == "Browser.setBypassCSP":
            ctx.bypass_csp = bool(p.get("bypassCSP"))
        elif method == "Browser.setTouchOverride":
            ctx.has_touch = bool(p.get("hasTouch"))
        elif method == "Browser.setCacheDisabled":
            ctx.cache_disabled = bool(p.get("cacheDisabled"))
        elif method == "Browser.setInitScripts":
            ctx.init_scripts = [s.get("script") or "" for s in (p.get("scripts") or ())]
        elif method == "Browser.setRequestInterception":
            ctx.intercepting = bool(p.get("enabled"))
        elif method in (
            "Browser.setIgnoreHTTPSErrors",
            "Browser.setJavaScriptDisabled",
            "Browser.setServiceWorkersBlocked",
            "Browser.setContextProxy",
            "Heap.collectGarbage",
        ):
            if method == "Heap.collectGarbage":
                for st in list(self._pages.values()):
                    try:
                        self._raw.send(
                            "HeapProfiler.collectGarbage", session=st.session_id, timeout=5.0
                        )
                    except Exception:
                        pass
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
            except Exception:
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
        if fid == state.main_frame_id:
            res = self._raw.send(
                "Runtime.evaluate",
                {"expression": "window", "returnByValue": False},
                session=state.session_id,
                timeout=timeout,
            )
            oid = (res.get("result") or {}).get("objectId")
        else:
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
            oid = (win_res.get("result") or {}).get("objectId")
        if not oid:
            raise juggler_conn.ProtocolError(
                "Execution context was destroyed while resolving frame window"
            )
        state.caller_windows[caller_ctx] = oid
        return oid

    @staticmethod
    def _normalize_eval_response(res: Dict[str, Any]) -> Dict[str, Any]:
        if "exceptionDetails" in res:
            exc = res["exceptionDetails"] or {}
            exc_obj = exc.get("exception") or {}
            desc = exc_obj.get("description") or exc.get("text") or "Evaluation failed"
            return {"exceptionDetails": {"text": desc, "stack": ""}}
        return {"result": res.get("result") or {}}

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

        if method == "Page.setInitScripts":
            state.page_init_scripts = list(p.get("scripts") or ())
            ctx = self._get_ctx(state.context_id)
            self._sync_doc_scripts(state, ctx)
            return {}

        if method == "Page.navigate":
            fid = p.get("frameId") or state.main_frame_id
            nav_params: Dict[str, Any] = {"url": p["url"], "frameId": fid}
            if p.get("referer"):
                nav_params["referrer"] = p["referer"]
            res = self._raw.send(
                "Page.navigate", nav_params, session=session_id, timeout=timeout
            )
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
            self._raw.send(
                "Emulation.setEmulatedMedia",
                media_params,
                session=session_id,
                timeout=timeout,
            )
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
            return {}

        if method == "Runtime.evaluate":
            ctx_id = str(p["executionContextId"])
            expr = p["expression"]
            by_value = bool(p.get("returnByValue", True))
            if '"browserName": "firefox"' in expr:
                expr = expr.replace('"browserName": "firefox"', '"browserName": "chromium"')
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
            try:
                if ctx_id.startswith("caller:"):
                    parts = ctx_id.split(":")
                    fid = parts[1] if len(parts) >= 2 else state.main_frame_id
                    if fid == state.main_frame_id:
                        res = self._raw.send(
                            "Runtime.evaluate",
                            {
                                "expression": expr,
                                "returnByValue": by_value,
                                "awaitPromise": True,
                            },
                            session=session_id,
                            timeout=timeout,
                        )
                    else:
                        win_oid = self._resolve_caller_window(
                            state, ctx_id, timeout=timeout
                        )
                        res = self._raw.send(
                            "Runtime.callFunctionOn",
                            {
                                "objectId": win_oid,
                                "functionDeclaration": f"function() {{ return (0, eval)({json.dumps(expr)}); }}",
                                "returnByValue": by_value,
                                "awaitPromise": True,
                            },
                            session=session_id,
                            timeout=timeout,
                        )
                else:
                    res = self._raw.send(
                        "Runtime.evaluate",
                        {
                            "expression": expr,
                            "contextId": int(ctx_id),
                            "returnByValue": by_value,
                            "awaitPromise": True,
                        },
                        session=session_id,
                        timeout=timeout,
                    )
            except juggler_conn.ProtocolError as exc:
                if any(
                    msg in str(exc)
                    for msg in (
                        "Cannot find context",
                        "Execution context was destroyed",
                        "Cannot find default execution context",
                    )
                ):
                    raise juggler_conn.ProtocolError(
                        "Execution context was destroyed!"
                    ) from exc
                raise
            return self._normalize_eval_response(res)

        if method == "Runtime.callFunction":
            ctx_id = str(p["executionContextId"])
            decl = p["functionDeclaration"]
            args = list(p.get("args") or ())
            by_value = bool(p.get("returnByValue", True))
            try:
                if ctx_id.startswith("caller:"):
                    first_oid = (
                        args[0].get("objectId")
                        if (args and isinstance(args[0], dict))
                        else None
                    )
                    if first_oid and len(args) == 1:
                        res = self._raw.send(
                            "Runtime.callFunctionOn",
                            {
                                "objectId": first_oid,
                                "functionDeclaration": f"function() {{ return ({decl})(this); }}",
                                "returnByValue": by_value,
                                "awaitPromise": True,
                            },
                            session=session_id,
                            timeout=timeout,
                        )
                    else:
                        win_oid = self._resolve_caller_window(
                            state, ctx_id, timeout=timeout
                        )
                        res = self._raw.send(
                            "Runtime.callFunctionOn",
                            {
                                "objectId": win_oid,
                                "functionDeclaration": decl,
                                "arguments": args,
                                "returnByValue": by_value,
                                "awaitPromise": True,
                            },
                            session=session_id,
                            timeout=timeout,
                        )
                else:
                    res = self._raw.send(
                        "Runtime.callFunctionOn",
                        {
                            "executionContextId": int(ctx_id),
                            "functionDeclaration": decl,
                            "arguments": args,
                            "returnByValue": by_value,
                            "awaitPromise": True,
                        },
                        session=session_id,
                        timeout=timeout,
                    )
            except juggler_conn.ProtocolError as exc:
                if any(
                    msg in str(exc)
                    for msg in (
                        "Cannot find context",
                        "Execution context was destroyed",
                        "Cannot find object with id",
                    )
                ):
                    raise juggler_conn.ProtocolError(
                        "Execution context was destroyed!"
                    ) from exc
                raise
            return self._normalize_eval_response(res)

        if method == "Runtime.getObjectProperties":
            res = self._raw.send(
                "Runtime.getProperties",
                {"objectId": p["objectId"], "ownProperties": True},
                session=session_id,
                timeout=timeout,
            )
            props = []
            for item in res.get("result") or ():
                if not item.get("enumerable", True):
                    continue
                if "value" in item:
                    props.append({"name": item["name"], "value": item["value"]})
            return {"properties": props}

        if method == "Runtime.disposeObject":
            if p.get("objectId"):
                try:
                    self._raw.send(
                        "Runtime.releaseObject",
                        {"objectId": p["objectId"]},
                        session=session_id,
                        timeout=timeout,
                    )
                except Exception:
                    pass
            return {}

        if method == "Page.getContentQuads":
            try:
                res = self._raw.send(
                    "DOM.getContentQuads",
                    {"objectId": p["objectId"]},
                    session=session_id,
                    timeout=timeout,
                )
            except juggler_conn.ProtocolError:
                return {"quads": []}
            out_quads = []
            for q in res.get("quads") or ():
                if len(q) >= 8:
                    out_quads.append(
                        {
                            "p1": {"x": q[0], "y": q[1]},
                            "p2": {"x": q[2], "y": q[3]},
                            "p3": {"x": q[4], "y": q[5]},
                            "p4": {"x": q[6], "y": q[7]},
                        }
                    )
            return {"quads": out_quads}

        if method == "Page.describeNode":
            desc = self._raw.send(
                "DOM.describeNode",
                {"objectId": p["objectId"], "pierce": True},
                session=session_id,
                timeout=timeout,
            )
            node = desc.get("node") or {}
            cdoc = node.get("contentDocument") or {}
            cfid = cdoc.get("frameId") or node.get("frameId")
            return {"contentFrameId": cfid} if cfid else {}

        if method == "Page.adoptNode":
            target_ctx = int(p["executionContextId"])
            if p.get("objectId"):
                desc = self._raw.send(
                    "DOM.describeNode",
                    {"objectId": p["objectId"]},
                    session=session_id,
                    timeout=timeout,
                )
                backend_id = (desc.get("node") or {}).get("backendNodeId")
            else:
                owner = self._raw.send(
                    "DOM.getFrameOwner",
                    {"frameId": p["frameId"]},
                    session=session_id,
                    timeout=timeout,
                )
                backend_id = owner.get("backendNodeId")
            if not backend_id:
                return {"remoteObject": {}}
            resolved = self._raw.send(
                "DOM.resolveNode",
                {"backendNodeId": backend_id, "executionContextId": target_ctx},
                session=session_id,
                timeout=timeout,
            )
            return {"remoteObject": resolved.get("object") or {}}

        if method == "Page.dispatchMouseEvent":
            ev_type = {
                "mousemove": "mouseMoved",
                "mousedown": "mousePressed",
                "mouseup": "mouseReleased",
            }.get(p["type"], "mouseMoved")
            btn = _MOUSE_BUTTON_NAMES.get(p.get("button", 0), "left")
            if ev_type == "mouseMoved":
                btn = "none"
            latency_ms = float(p.get("latencyMs") or 0.0)
            ts = time.time() - (latency_ms / 1000.0 if latency_ms > 0 else 0.0)
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
            self._raw.send(
                "Input.dispatchMouseEvent", mp, session=session_id, timeout=timeout
            )
            if p.get("landsOn"):
                if state.dialog_open:
                    return {"landing": None}
                return {"landing": {"landed": True, "on": "element"}}
            return {}

        if method == "Page.dispatchWheelEvent":
            self._raw.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": float(p["x"]),
                    "y": float(p["y"]),
                    "deltaX": float(p.get("deltaX") or 0.0),
                    "deltaY": float(p.get("deltaY") or 0.0),
                    "modifiers": _cdp_modifiers(p.get("modifiers") or 0),
                    "timestamp": time.time(),
                },
                session=session_id,
                timeout=timeout,
            )
            return {}

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

        if method == "Page.dispatchKeyEvent":
            is_down = p["type"] == "keydown"
            key = p.get("key") or ""
            code = p.get("code") or ""
            vk = int(p.get("keyCode") or 0)
            loc = int(p.get("location") or 0)
            repeat = bool(p.get("repeat", False))
            text = p.get("text") or ""
            if is_down and key == "Enter" and not text:
                text = "\r"
            latency_ms = float(p.get("latencyMs") or 0.0)
            ts = time.time() - (latency_ms / 1000.0 if latency_ms > 0 else 0.0)
            kp: Dict[str, Any] = {
                "type": ("keyDown" if text else "rawKeyDown") if is_down else "keyUp",
                "key": key,
                "code": code,
                "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk,
                "location": loc,
                "autoRepeat": repeat,
                "timestamp": ts,
            }
            if is_down and text:
                kp["text"] = text
                kp["unmodifiedText"] = text.lower()
            self._raw.send(
                "Input.dispatchKeyEvent", kp, session=session_id, timeout=timeout
            )
            return {}

        if method == "Page.insertText":
            self._raw.send(
                "Input.insertText",
                {"text": p.get("text") or ""},
                session=session_id,
                timeout=timeout,
            )
            return {}

        if method == "Page.selectOptions":
            indices = list(p.get("indices") or ())
            self._raw.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": p["objectId"],
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
                session=session_id,
                timeout=timeout,
            )
            return {}

        if method == "Page.setUserInput":
            self._raw.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": p["objectId"],
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
                session=session_id,
                timeout=timeout,
            )
            return {}

        if method == "Page.setFileInputFiles":
            self._raw.send(
                "DOM.setFileInputFiles",
                {"objectId": p["objectId"], "files": list(p.get("files") or ())},
                session=session_id,
                timeout=timeout,
            )
            return {}

        if method == "Network.setRequestInterception":
            state.intercepting = bool(p.get("enabled"))
            ctx = self._get_ctx(state.context_id)
            self._sync_fetch_domain(state, ctx)
            return {}

        if method == "Network.getResponseBody":
            res = self._raw.send(
                "Network.getResponseBody",
                {"requestId": p["requestId"]},
                session=session_id,
                timeout=timeout,
            )
            body = res.get("body") or ""
            if not res.get("base64Encoded"):
                body = base64.b64encode(body.encode("utf-8")).decode("ascii")
            return {"base64body": body}

        if method == "Network.resumeInterceptedRequest":
            net_id = p["requestId"]
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
                "Fetch.continueRequest", cp, session=session_id, timeout=timeout
            )
            return {}

        if method == "Network.fulfillInterceptedRequest":
            net_id = p["requestId"]
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
                "Fetch.fulfillRequest", fp, session=session_id, timeout=timeout
            )
            return {}

        if method == "Network.abortInterceptedRequest":
            net_id = p["requestId"]
            fetch_id = state.paused_requests.pop(net_id, net_id)
            reason = _ABORT_REASON_MAP.get(
                str(p.get("errorCode") or "failed").lower(), "Failed"
            )
            self._raw.send(
                "Fetch.failRequest",
                {"requestId": fetch_id, "errorReason": reason},
                session=session_id,
                timeout=timeout,
            )
            return {}

        raise juggler_conn.ProtocolError(f"Unsupported page command: {method}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
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
                    self._process.wait(timeout=2.0)
        if self._process.stderr:
            try:
                self._process.stderr.close()
            except Exception:
                pass
        self.announce_closed()
