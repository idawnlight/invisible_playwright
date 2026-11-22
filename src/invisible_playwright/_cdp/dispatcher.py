"""Playwright `BrowserType` dispatcher for the Chromium CDP pipe backend."""
from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any, Dict, Optional

from invisible_core import parse_proxy
from invisible_core.juggler import SessionFiles
from invisible_core.juggler.keyboard import KEYBOARD_LAYOUT_PREF, profiles_pref
from invisible_core.process import alive, psutil

from .._juggler._marshal import _caller_timeout
from .._juggler._reaped import SessionRemoval
from .._juggler.dispatcher import Dispatcher, ProtocolException
from .bridge import CDPBridgeConnection
from .engine import detect_needs_no_sandbox

_SHORT_TMP_PREFIX = "ipw_t_"
# Chromium's ProcessSingletonPosix creates `<TMPDIR>/org.chromium.Chromium.XXXXXX/SingletonSocket`
# (+45 bytes) and calls LOG(FATAL) if `sun_path` exceeds 107 bytes.
_POSIX_TMP_MAX_BYTES = 52


def _pid_alive(pid: int) -> bool:
    if psutil is not None:
        try:
            return alive(psutil.Process(pid))
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _sweep_short_tmps(root: str = "/tmp") -> None:
    try:
        names = os.listdir(root)
    except OSError:
        return
    my_pid = os.getpid()
    for name in names:
        if not name.startswith(_SHORT_TMP_PREFIX):
            continue
        head = name[len(_SHORT_TMP_PREFIX) :].split("_", 1)[0]
        if not head.isdigit():
            continue
        pid = int(head)
        if pid == my_pid or _pid_alive(pid):
            continue
        path = os.path.join(root, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)


class ChromiumBrowserTypeDispatcher(Dispatcher):
    TYPE = "BrowserType"
    browser_class: Any = None
    METHODS = {
        "launch": "op_launch",
        "launchPersistentContext": "op_launch_persistent",
    }

    def __init__(self, server: Any, executable_path: str = "") -> None:
        super().__init__(
            server,
            None,
            {"executablePath": executable_path, "name": "chromium"},
        )

    def op_launch_persistent(self, params: Dict[str, Any]) -> Any:
        directory = params.get("userDataDir")
        if not directory:
            raise ProtocolException(
                "launch_persistent_context() needs a userDataDir: without one "
                "it is just launch(), and the profile it is supposed to keep "
                "would be a temporary directory"
            )
        browser_channel = self.op_launch(
            dict(params, userDataDir=directory, _persistent=True)
        )
        browser = self.server.object(browser_channel["browser"]["guid"])
        try:
            context = browser.op_default_context(params)["context"]
        except BaseException:
            browser.op_close({})
            raise
        return {"browser": browser_channel["browser"], "context": context}

    def op_launch(self, params: Dict[str, Any]) -> Any:
        from .._juggler.server import (
            BrowserDispatcher,
            _launch_environment,
            session_draws_pointer,
            take_session_motion,
        )
        from .engine import resolve_chromium_executable

        executable = params.get("executablePath")
        if not executable:
            executable = str(resolve_chromium_executable(None))

        files = SessionFiles(
            params.get("userDataDir"),
            _launch_environment(params.get("env")),
        )
        env, profile = files.env, files.profile
        short_tmp: Optional[str] = None
        if os.name == "posix" and os.path.isdir("/tmp"):
            _sweep_short_tmps("/tmp")
            cur_tmp = env.get("TMPDIR") or ""
            if len(cur_tmp.encode("utf-8")) > _POSIX_TMP_MAX_BYTES:
                short_tmp = tempfile.mkdtemp(
                    prefix=f"{_SHORT_TMP_PREFIX}{os.getpid()}_", dir="/tmp"
                )
                env = dict(env, TMP=short_tmp, TEMP=short_tmp, TMPDIR=short_tmp)

        def _remove_session_files() -> None:
            files.remove()
            if short_tmp:
                shutil.rmtree(short_tmp, ignore_errors=True)

        session_token = getattr(getattr(files, "token", None), "value", "")
        removal = SessionRemoval(_remove_session_files, session_token)
        self.server.note_session_token(session_token)
        self.server.after_shutdown(removal.after_shutdown)

        launch_prefs = dict(params.get("firefoxUserPrefs") or {})
        pointer_draws = session_draws_pointer(launch_prefs)
        prefs, session_seed, motion_budget_s = take_session_motion(launch_prefs)
        keyboard_layout = prefs.pop(KEYBOARD_LAYOUT_PREF, None) or profiles_pref(
            prefs.get("intl.accept_languages") or ""
        )
        ready = _caller_timeout(params, default_ms=60000)

        proxy = None
        if params.get("proxy"):
            try:
                proxy = parse_proxy(params["proxy"]).as_engine_command()
            except ValueError as exc:
                raise ProtocolException(
                    "launch(proxy=...) cannot be applied: %s" % exc
                ) from exc

        extra_args = list(params.get("args") or [])
        chromium_sandbox = params.get("chromiumSandbox")
        if chromium_sandbox is False:
            no_sandbox = True
        else:
            no_sandbox = detect_needs_no_sandbox()

        conn = CDPBridgeConnection.launch(
            executable,
            profile,
            env=env,
            argv_extra=extra_args,
            ready_timeout=ready,
            proxy=proxy,
            persistent=bool(params.get("_persistent")),
            no_sandbox=no_sandbox,
        )
        self.server.on_shutdown(lambda: removal.close(conn))
        try:
            browser = (self.browser_class or BrowserDispatcher)(
                self.server,
                self,
                conn,
                conn.version_number,
                session_seed=session_seed,
                motion_budget_s=motion_budget_s,
                service_workers_enabled=True,
                layout=keyboard_layout,
                pointer_draws=pointer_draws,
                browser_name="chromium",
            )
        except BaseException:
            conn.close()
            raise
        browser.engine_dir = os.path.dirname(str(executable))
        return {"browser": browser.channel}
