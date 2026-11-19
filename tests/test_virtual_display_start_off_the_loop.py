"""Starting the hidden display must not block the event loop of an async user.

`headless=True` starts a virtual display (an Xvfb on Linux) before the browser
launches. The start is a blocking call (it spawns the server and waits for it
to report ready, ~75 ms measured, far more on a loaded host), and `__aenter__`
made it ON the user's loop: four browsers in one process each froze the other
three for that long. The async path awaits it in a worker thread; the sync path
has no other browser on its thread and stays as it was.

The display is still started - the hidden-headed surface is what the product
promises (`core-10-superficie-nascosta-headed`) - only the thread changes.
"""
from __future__ import annotations

import asyncio
import threading
import time
import types

import pytest
from invisible_core import SessionLocale
from invisible_core.launch import FontManifestMismatch

from invisible_playwright import _session, async_api

START_SECONDS = 0.5


class SlowDisplay:
    """Stand-in for a display whose start takes 500 ms."""

    def __init__(self) -> None:
        self.started_on = None
        self.stopped = 0
        self.release = None

    def start(self) -> None:
        self.started_on = threading.get_ident()
        time.sleep(START_SECONDS)

    def stop(self) -> None:
        self.stopped += 1

    def launch_env(self):
        return {}


@pytest.fixture
def slow(monkeypatch):
    vd = SlowDisplay()
    geo = types.SimpleNamespace(timezone="Europe/Rome",
                                locale=SessionLocale.of("it-IT"),
                                egress_ip=None,
                                geolocation_override=lambda: None)

    def refuse(**_kw):
        raise FontManifestMismatch("stop here: the launch is not the subject")

    monkeypatch.delenv(_session.TRUE_HEADLESS_ENV, raising=False)
    monkeypatch.setattr(_session, "make_virtual_display", lambda: vd)
    monkeypatch.setattr(_session, "build_env", refuse)
    # Composing the prefs is its own ~100 ms of CPU on a cold process and is not
    # the subject: the test measures the display start alone.
    monkeypatch.setattr(async_api.InvisiblePlaywright, "_build_prefs", lambda _s: {})
    # Nor is the teardown's process scan (~100 ms on a host with many
    # processes): a stand-in that does only what this test checks, stop the display.
    async def teardown(self):
        vd, self._virtual_display = self._virtual_display, None
        if vd is not None:
            vd.stop()

    monkeypatch.setattr(async_api.InvisiblePlaywright, "_teardown", teardown)
    monkeypatch.setattr(async_api, "prepare_session_geo", lambda *_a, **_k: geo)
    monkeypatch.setattr(async_api, "resolve_executable", lambda _p: "firefox")
    return vd


def test_a_second_browser_is_served_while_the_first_starts_its_display(slow):
    gaps = []

    async def second_browser():
        # Another browser on this loop asks for a reply every 10 ms while the
        # first one launches. A reply later than 100 ms means the loop was held.
        last = time.perf_counter()
        while True:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    async def run():
        first = async_api.InvisiblePlaywright(seed=7, headless=True)
        other = asyncio.ensure_future(second_browser())
        await asyncio.sleep(0.05)
        launch = asyncio.ensure_future(first.__aenter__())
        with pytest.raises(FontManifestMismatch):
            await launch
        await asyncio.sleep(0.05)               # let the ticker record the last gap
        other.cancel()
        return first

    first = asyncio.run(run())
    assert len(gaps) > 5 and max(gaps) < 0.1, (
        "the second browser waited %.0f ms for a reply: the display start ran "
        "on the loop" % (max(gaps) * 1000))
    assert slow.started_on is not None
    assert slow.started_on != threading.get_ident(), "the start ran on the loop thread"
    # The surface is still created, and still stopped by a failed launch.
    assert slow.stopped == 1
    assert first._virtual_display is None


def test_a_launch_cancelled_during_the_display_start_leaves_no_display(slow):
    async def run():
        first = async_api.InvisiblePlaywright(seed=7, headless=True)
        launch = asyncio.ensure_future(first.__aenter__())
        await asyncio.sleep(0.1)                # the start is in its worker
        launch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await launch
        await asyncio.sleep(START_SECONDS + 0.3)    # the worker finishes
        return first

    asyncio.run(run())
    assert slow.stopped == 1, "a display started for a cancelled launch was left running"


# -- one decision for both APIs ----------------------------------------------

class QuickDisplay:
    def __init__(self) -> None:
        self.started = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        pass


@pytest.mark.parametrize("headless", [False, True])
@pytest.mark.parametrize("true_headless", [False, True])
@pytest.mark.parametrize("platform_has_display", [True, False])
def test_sync_and_async_make_the_same_decision(monkeypatch, headless, true_headless,
                                               platform_has_display):
    """The same inputs give the same answer, the same display and the same
    started/recorded facts on both paths: the decision lives in one function."""
    from invisible_playwright import launcher

    if true_headless:
        monkeypatch.setenv(_session.TRUE_HEADLESS_ENV, "1")
    else:
        monkeypatch.delenv(_session.TRUE_HEADLESS_ENV, raising=False)
    monkeypatch.setattr(
        _session, "make_virtual_display",
        lambda: QuickDisplay() if platform_has_display else None)

    def outcome(session, result):
        vd = session._virtual_display
        return (result, vd is not None, vd.started if vd is not None else None)

    sync = launcher.InvisiblePlaywright(seed=7, headless=headless)
    got_sync = outcome(sync, sync._resolve_headless())

    use = async_api.InvisiblePlaywright(seed=7, headless=headless)
    got_async = outcome(use, asyncio.run(use._resolve_headless_async()))

    assert got_sync == got_async
    # And the decision itself, for the record.
    if not headless:
        assert got_sync == (False, False, None)
    elif true_headless:
        assert got_sync == (True, False, None)
    elif platform_has_display:
        assert got_sync == (False, True, 1)
    else:
        assert got_sync == (False, False, None)
