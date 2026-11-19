"""Playwright `BrowserType` dispatcher for the Chromium CDP pipe backend."""
from __future__ import annotations

from typing import Any, Dict

from invisible_core import parse_proxy
from invisible_core.juggler import SessionFiles

from .._juggler._marshal import _caller_timeout
from .._juggler.dispatcher import Dispatcher, ProtocolException
from .bridge import CDPBridgeConnection
from .engine import detect_needs_no_sandbox


class ChromiumBrowserTypeDispatcher(Dispatcher):
    TYPE = "BrowserType"
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
        self.server.after_shutdown(files.remove)
        env, profile = files.env, files.profile

        _, session_seed, motion_budget_s = take_session_motion(
            params.get("firefoxUserPrefs") or {}
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
        self.server.on_shutdown(conn.close)
        try:
            browser = BrowserDispatcher(
                self.server,
                self,
                conn,
                conn.version_number,
                session_seed=session_seed,
                motion_budget_s=motion_budget_s,
                service_workers_enabled=True,
                browser_name="chromium",
            )
        except BaseException:
            conn.close()
            raise
        return {"browser": browser.channel}
