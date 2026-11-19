"""The removal of a session's directories is the launch's reaper's, when the core gives the launch one.

⛔ THE KNOWN-BAD INPUT: invisible-core's `Connection.close` returns when the browser has exited, and a reaper of the
launch (`conn.reaper`) waits for the sender of the shutdown pings, ends what is left and runs the steps handed to
`reaper.then`. The server ran the removal itself right after the shutdown (`SessionFiles.remove` waits for the sender
and the tree), so the close of the wrapper took as long as before the core stopped waiting. With a reaper the removal is
registered with `reaper.then` and does not run inside the shutdown; with a core that has none, it runs there, as before.
"""
from __future__ import annotations

import os
import shutil
import threading

import pytest

from invisible_core.juggler.connection import EventListeners
from invisible_playwright._juggler import _reaped
from invisible_playwright._juggler import server as module
from invisible_playwright._juggler.server import BrowserTypeDispatcher, JugglerServer
from _browser_census import closes_with_the_test

TOKEN = "fake-session-token"


class _Files:
    """The session's directories as the launch sees them: what it removes is recorded. `base` is the test's own directory."""

    base = None

    def __init__(self, profile_dir, env):
        self.profile = profile_dir or str(self.base / "profile")
        os.makedirs(self.profile, exist_ok=True)
        self.tmp = str(self.base / "tmp")
        self.env = dict(env or {})
        self.token = type("T", (), {"value": TOKEN})()
        self.removed = 0
        _Files.last = self

    def remove(self):
        self.removed += 1


class _Reaper:
    """The launch's reaper as the core's: `then` keeps the step for the thread that ends the tree."""

    def __init__(self):
        self.steps = []
        self.finished = False
        self.joined = 0

    def then(self, step):
        self.steps.append(step)

    def join(self, timeout=None):
        self.joined += 1
        self.finish()

    def finish(self):
        steps, self.steps = self.steps, []
        for step in steps:
            step()
        self.finished = True


class _Engine(EventListeners):
    """A connection that closes without waiting for anything; `reaper` is what the core gives it at the close."""

    def __init__(self, reaper_class=None):
        EventListeners.__init__(self)
        closes_with_the_test(self)
        self.reaper_class = reaper_class
        self.closes = 0

    def send(self, method, params=None, session=None, timeout=30.0):
        return {"browserContextId": "ctx-1", "targetId": "t-1"}

    def startup_send(self, method, params=None):
        return self.send(method, params, timeout=None)

    def close(self, timeout=5.0):
        self.closes += 1
        if self.reaper_class is not None and not hasattr(self, "reaper"):
            self.reaper = self.reaper_class()


def _launched(monkeypatch, engine, base):
    monkeypatch.setattr(_Files, "base", base)
    monkeypatch.setattr(module, "SessionFiles", _Files)
    monkeypatch.setattr(module.juggler, "launch", lambda *a, **k: engine)
    server = JugglerServer()
    server.attach(type("R", (), {"emit_message": lambda self, m: None})())
    BrowserTypeDispatcher(server).op_launch({"executablePath": "x", "firefoxUserPrefs": {}})
    return server, _Files.last


@pytest.mark.unit
def test_with_a_reaper_the_removal_waits_for_it_and_does_not_run_in_the_shutdown(monkeypatch, tmp_path):
    engine = _Engine(_Reaper)
    server, files = _launched(monkeypatch, engine, tmp_path)
    server.shutdown()
    assert engine.closes >= 1
    assert files.removed == 0, "the shutdown removed the directories itself and waited for the sender and the tree"
    assert len(engine.reaper.steps) == 1, "the removal was not handed to `reaper.then`"
    engine.reaper.finish()
    assert files.removed == 1


@pytest.mark.unit
def test_the_removal_runs_once_however_it_is_reached(monkeypatch, tmp_path):
    engine = _Engine(_Reaper)
    server, files = _launched(monkeypatch, engine, tmp_path)
    server.shutdown()
    step = engine.reaper.steps[0]
    step()
    step()
    server.shutdown()
    assert files.removed == 1


@pytest.mark.unit
def test_without_a_reaper_the_removal_runs_in_the_shutdown_as_before(monkeypatch, tmp_path):
    engine = _Engine(None)
    assert not hasattr(engine, "reaper")
    server, files = _launched(monkeypatch, engine, tmp_path)
    assert files.removed == 0
    server.shutdown()
    assert files.removed == 1, "a core with no reaper must have its directories removed by the shutdown"


@pytest.mark.unit
def test_a_close_that_raised_leaves_the_removal_to_the_shutdown(monkeypatch, tmp_path):
    engine = _Engine(_Reaper)

    def failing(timeout=5.0):
        raise RuntimeError("the pipe is gone")

    engine.close = failing
    server, files = _launched(monkeypatch, engine, tmp_path)
    server.shutdown()
    assert files.removed == 1, "no reaper was ever made, and nobody removed the directories"


@pytest.mark.unit
def test_wait_reaped_waits_for_the_reaper_of_the_session(monkeypatch, tmp_path):
    engine = _Engine(_Reaper)
    server, files = _launched(monkeypatch, engine, tmp_path)
    server.shutdown()
    assert _reaped.wait_reaped(TOKEN, timeout=5) is True
    assert engine.reaper.joined == 1
    assert files.removed == 1
    # Nothing is kept of a finished launch, and a session with none has nothing to wait for.
    assert _reaped.wait_reaped(TOKEN, timeout=0) is True
    assert engine.reaper.joined == 1
    assert _reaped.wait_reaped("never-launched", timeout=0) is True


@pytest.mark.unit
def test_wait_reaped_says_false_at_its_timeout():
    release = threading.Event()

    class Slow:
        finished = False

        def join(self, timeout=None):
            release.wait(timeout)

    slow = Slow()
    _reaped._reapers["slow-session"] = [slow]
    try:
        assert _reaped.wait_reaped("slow-session", timeout=0.05) is False
    finally:
        _reaped._reapers.pop("slow-session", None)
        release.set()


def _the_session_folders(server):
    """Makes the two folders a session keeps beside its profile, the way a context and a file upload make them: the
    downloads root (`BrowserDispatcher._downloads_dir`) and the upload root (`stage_upload`). Returns both roots."""
    holder = type("Holder", (), {"_downloads_root": None, "server": server})()
    module.BrowserDispatcher._downloads_dir(holder, "ctx-1")
    uploaded = server.stage_upload({"name": "a.txt", "buffer": ""})
    downloads = holder._downloads_root
    uploads = server._upload_root
    assert os.path.isdir(downloads) and os.path.isdir(uploads) and os.path.isfile(uploaded)
    return downloads, uploads


@pytest.mark.unit
def test_with_a_reaper_alive_the_shutdown_leaves_the_downloads_and_upload_folders_to_it(monkeypatch, tmp_path):
    engine = _Engine(_Reaper)
    server, files = _launched(monkeypatch, engine, tmp_path)
    downloads, uploads = _the_session_folders(server)
    try:
        server.shutdown()
        assert os.path.isdir(downloads), "the shutdown thread removed the downloads root while the reaper was alive"
        assert os.path.isdir(uploads), "the shutdown thread removed the upload root while the reaper was alive"
        assert files.removed == 0
    finally:
        engine.reaper.finish()
        _reaped.wait_reaped(TOKEN, timeout=5)
        for root in (downloads, uploads):
            shutil.rmtree(root, ignore_errors=True)


@pytest.mark.unit
def test_once_the_reaper_is_released_the_two_folders_are_gone_when_wait_reaped_returns(monkeypatch, tmp_path):
    engine = _Engine(_Reaper)
    server, files = _launched(monkeypatch, engine, tmp_path)
    downloads, uploads = _the_session_folders(server)
    try:
        server.shutdown()
        assert len(engine.reaper.steps) == 3, "the removal and the two folders are the reaper's steps"
        assert _reaped.wait_reaped(TOKEN, timeout=5) is True
        assert not os.path.exists(downloads)
        assert not os.path.exists(uploads)
        assert files.removed == 1
    finally:
        for root in (downloads, uploads):
            shutil.rmtree(root, ignore_errors=True)


@pytest.mark.unit
def test_with_no_reaper_the_two_folders_are_gone_when_the_shutdown_returns(monkeypatch, tmp_path):
    engine = _Engine(None)
    server, files = _launched(monkeypatch, engine, tmp_path)
    downloads, uploads = _the_session_folders(server)
    try:
        server.shutdown()
        assert not os.path.exists(downloads), "with no reaper nobody else removes the downloads root"
        assert not os.path.exists(uploads), "with no reaper nobody else removes the upload root"
    finally:
        for root in (downloads, uploads):
            shutil.rmtree(root, ignore_errors=True)
