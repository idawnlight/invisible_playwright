"""Dual-backend parity E2E suite exercising Firefox 160 and Chromium 157 identically."""
from __future__ import annotations

import asyncio
import http.server
import json
from pathlib import Path
import socketserver
import struct
import threading
from typing import Iterator

import pytest

from invisible_playwright import InvisiblePlaywright
from invisible_playwright._pw.sync_api import expect
from invisible_playwright.async_api import (
    InvisiblePlaywright as AsyncInvisiblePlaywright,
)


@pytest.fixture(scope="module")
def parity_server() -> Iterator[str]:
    pages = {
        "/": b"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Parity Home</title></head>
<body style="margin:0; height:2400px; font-family:sans-serif;">
  <h1 id="heading">Dual Engine Home</h1>
  <a id="nav-link" href="/page2">Go Page 2</a>
  <button id="btn" style="position:absolute; left:360px; top:220px; width:150px; height:44px;">Submit</button>
  <input id="inp" type="text" style="position:absolute; left:100px; top:110px; width:220px; height:30px;" />
  <iframe id="child-frame" src="/child" style="position:absolute; left:100px; top:340px; width:300px; height:120px;"></iframe>
  <script>
    window.__traps = { qsHits: 0, stackHits: 0 };
    const origQS = Document.prototype.querySelector;
    Document.prototype.querySelector = function(...args) {
      window.__traps.qsHits += 1;
      return origQS.apply(this, args);
    };
    const probeErr = new Error('probe');
    Object.defineProperty(probeErr, 'stack', {
      get() {
        window.__traps.stackHits += 1;
        return '';
      }
    });
    console.debug(probeErr);

    window.__events = { moves: [], downs: [], clicks: [] };
    window.addEventListener('pointermove', (e) => {
      window.__events.moves.push({ x: e.clientX, y: e.clientY });
    });
    window.addEventListener('pointerdown', (e) => {
      window.__events.downs.push({ x: e.clientX, y: e.clientY, pressure: e.pressure });
    });
    document.getElementById('btn').addEventListener('click', (e) => {
      window.__events.clicks.push({ x: e.clientX, y: e.clientY, isTrusted: e.isTrusted });
    });
  </script>
</body>
</html>""",
        "/page2": b"""<!doctype html>
<html><head><meta charset="utf-8"><title>Page Two</title></head>
<body><h2 id="p2">Arrived Page 2</h2></body></html>""",
        "/child": b"""<!doctype html>
<html><head><meta charset="utf-8"><title>Child Frame</title></head>
<body>
  <button id="inner-btn" onclick="this.textContent='Clicked Inner'">Inner Ready</button>
</body></html>""",
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/headers":
                raw = json.dumps(dict(self.headers)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            body = pages.get(self.path, pages["/"])
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            pass

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as srv:
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            srv.shutdown()


@pytest.mark.e2e
@pytest.mark.parametrize("backend", ["firefox", "chromium"])
def test_sync_dual_backend_stealth_navigation_and_isolation(
    parity_server: str, backend: str
) -> None:
    with InvisiblePlaywright(
        seed=42,
        backend=backend,
        headless=True,
        humanize=0.35,
        timezone="Europe/Berlin",
        locale="en-US",
        pin={"hardware.concurrency": 8},
    ) as browser:
        assert browser.browser_type.name == backend
        if backend == "firefox":
            assert browser.version == "160.0"
        else:
            assert browser.version == "157.0.8103.78"

        page = browser.new_page()
        page.goto(f"{parity_server}/")

        expect(page.locator("#heading")).to_be_visible()
        expect(page.locator("#btn")).to_have_text("Submit")

        page.fill("#inp", "Dual Engine!")
        page.click("#btn")

        info = page.evaluate(
            """() => {
              const c = document.createElement('canvas');
              const gl = c.getContext('webgl2', { failIfMajorPerformanceCaveat: true });
              return {
                webdriver: navigator.webdriver,
                cores: navigator.hardwareConcurrency,
                tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
                availLess: screen.availHeight < screen.height,
                hasWebgl2: !!gl,
                traps: window.__traps,
                events: window.__events,
                inpVal: document.getElementById('inp').value,
              };
            }"""
        )

        assert info["webdriver"] is False
        assert info["cores"] == 8
        assert info["tz"] == "Europe/Berlin"
        assert info["availLess"] is True
        assert info["hasWebgl2"] is True
        assert info["traps"] == {"qsHits": 0, "stackHits": 0}
        assert info["inpVal"] == "Dual Engine!"
        assert len(info["events"]["moves"]) >= 5
        assert len(info["events"]["clicks"]) == 1
        assert info["events"]["clicks"][0]["isTrusted"] is True
        assert info["events"]["downs"][0]["pressure"] == 0.5

        # Navigation history: link -> back -> forward
        page.click("#nav-link")
        expect(page.locator("#p2")).to_have_text("Arrived Page 2")
        page.go_back()
        expect(page.locator("#heading")).to_have_text("Dual Engine Home")
        page.go_forward()
        expect(page.locator("#p2")).to_have_text("Arrived Page 2")


@pytest.mark.e2e
@pytest.mark.parametrize("backend", ["firefox", "chromium"])
def test_sync_dual_backend_route_iframe_dialog_and_persistence(
    parity_server: str, backend: str, tmp_path: Path
) -> None:
    profile_dir = tmp_path / f"profile-{backend}"

    with InvisiblePlaywright(
        seed=84,
        backend=backend,
        headless=True,
        humanize=0.25,
        timezone="UTC",
        locale="en-US",
        profile_dir=profile_dir,
    ) as ctx:
        page = ctx.new_page()
        page.route(
            "**/intercepted.json",
            lambda route: route.fulfill(
                status=200,
                headers={"Content-Type": "application/json"},
                body='{"ok": true, "source": "route"}',
            ),
        )
        page.goto(f"{parity_server}/")

        # Route fulfill check
        routed = page.evaluate(
            "fetch('/intercepted.json').then(r => r.json())"
        )
        assert routed == {"ok": True, "source": "route"}

        # Child iframe locator & click
        inner = page.frame_locator("#child-frame").locator("#inner-btn")
        expect(inner).to_have_text("Inner Ready")
        inner.click()
        expect(inner).to_have_text("Clicked Inner")

        # Dialog handling
        seen_dialogs: list[str] = []
        page.on("dialog", lambda d: (seen_dialogs.append(d.message), d.accept("yes")))
        prompt_ans = page.evaluate("prompt('Confirm action?')")
        assert seen_dialogs == ["Confirm action?"]
        assert prompt_ans == "yes"

        page.evaluate("localStorage.setItem('persist_check', 'kept')")

    # Second launch against the same persistent profile_dir
    with InvisiblePlaywright(
        seed=84,
        backend=backend,
        headless=True,
        humanize=False,
        timezone="UTC",
        locale="en-US",
        profile_dir=profile_dir,
    ) as ctx2:
        page2 = ctx2.new_page()
        page2.goto(f"{parity_server}/")
        assert page2.evaluate("localStorage.getItem('persist_check')") == "kept"


@pytest.mark.e2e
@pytest.mark.parametrize("backend", ["firefox", "chromium"])
def test_async_dual_backend_and_css_screenshot(
    parity_server: str, backend: str
) -> None:
    async def _run() -> None:
        async with AsyncInvisiblePlaywright(
            seed=99,
            backend=backend,
            headless=True,
            humanize=0.25,
            timezone="Asia/Tokyo",
            locale="en-US",
        ) as browser:
            page = await browser.new_page()
            await page.goto(f"{parity_server}/")
            await page.fill("#inp", f"async-{backend}")
            val = await page.evaluate("document.getElementById('inp').value")
            assert val == f"async-{backend}"

            png = await page.screenshot(
                clip={"x": 10, "y": 10, "width": 160, "height": 90},
                scale="css",
            )
            assert png[:8] == b"\x89PNG\r\n\x1a\n"
            w, h = struct.unpack(">II", png[16:24])
            assert (w, h) == (160, 90)

    asyncio.run(_run())


@pytest.mark.e2e
def test_simultaneous_firefox_and_chromium_in_single_process(
    parity_server: str,
) -> None:
    async def _run_both() -> None:
        async with (
            AsyncInvisiblePlaywright(
                seed=42,
                backend="firefox",
                headless=True,
                humanize=0.25,
                timezone="UTC",
                locale="en-US",
            ) as fx_browser,
            AsyncInvisiblePlaywright(
                seed=42,
                backend="chromium",
                headless=True,
                humanize=0.25,
                timezone="UTC",
                locale="en-US",
            ) as cr_browser,
        ):
            fx_page = await fx_browser.new_page()
            cr_page = await cr_browser.new_page()

            await fx_page.goto(f"{parity_server}/")
            await cr_page.goto(f"{parity_server}/")

            await fx_page.click("#btn")
            await cr_page.click("#btn")

            fx_sig = await fx_page.evaluate(
                "({ua: navigator.userAgent, platform: navigator.platform, wd: navigator.webdriver, traps: window.__traps})"
            )
            cr_sig = await cr_page.evaluate(
                "({ua: navigator.userAgent, platform: navigator.platform, wd: navigator.webdriver, traps: window.__traps})"
            )

            assert "Firefox/160.0" in fx_sig["ua"]
            assert fx_sig["platform"] == "Win32"
            assert fx_sig["wd"] is False
            assert fx_sig["traps"] == {"qsHits": 0, "stackHits": 0}

            assert "Chrome/157." in cr_sig["ua"]
            assert cr_sig["platform"] == "Linux x86_64"
            assert cr_sig["wd"] is False
            assert cr_sig["traps"] == {"qsHits": 0, "stackHits": 0}

    asyncio.run(_run_both())

