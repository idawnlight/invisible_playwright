"""End-to-end regressions for Chromium multi-engine parity and cancellation safety.

Covers every blocker and limitation surfaced by downstream multi-engine validation:
1. Authenticated HTTP/CONNECT proxy + real end-to-end TLS tunnel (including cross-site OOPIFs)
2. Fast failure (no 60s hang, no infinite 407 retry loop) on wrong proxy credentials
3. Coexistence of authenticated proxy with context/page route() interception
4. Per-context proxy isolation via browser.new_context(proxy=...)
5. Explicit rejection of unsupported SOCKS5 auth and live proxy hot-swap on Chromium
6. Runtime.consoleAPICalled -> page.on("console") & Runtime.exceptionThrown -> page.on("pageerror")
   with V8 Runtime.enable stealth preserved and zero utility-world noise
7. Zero-delay trusted iframe clicks (same-origin, cross-port, and cross-site OOPIF) under humanize=False
8. Genuine landsOn hit-target verification in isolated utility world (zero main-world footprint)
9. Self-signed TLS enforcement vs ignore_https_errors=True
10. java_script_enabled=False and service_workers="block"
11. WebRTC disable_non_proxied_udp enforcement when launched behind a proxy
12. Closing the last open tab followed by opening a new page
13. Zero "Future exception was never retrieved: TargetClosedError" warnings on async cancellation
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import ExitStack
import gc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import json
from pathlib import Path
import select
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import pytest

from invisible_playwright.async_api import InvisiblePlaywright
from invisible_playwright._pw._impl._errors import Error as PlaywrightError


pytestmark = pytest.mark.e2e


def _generate_san_cert(directory: Path, dns_names: List[str]) -> Tuple[Path, Path]:
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    san_entries = ",".join(
        [*(f"DNS:{name}" for name in dns_names), "IP:127.0.0.1"]
    )
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            f"subjectAltName={san_entries}",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return cert_path, key_path


class _RecordingTLSServer:
    """Loopback HTTPS server that records SNI hostnames and serves HTML by Host."""

    def __init__(self, cert_path: Path, key_path: Path, routes: Dict[str, bytes]) -> None:
        self.routes = routes
        self.sni_seen: List[str] = []
        self.requests: List[Tuple[str, str]] = []
        self._lock = threading.Lock()

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

        def _on_sni(ssl_sock: ssl.SSLSocket, server_name: Optional[str], _ctx: ssl.SSLContext) -> None:
            if server_name:
                with self._lock:
                    self.sni_seen.append(server_name)

        ctx.sni_callback = _on_sni
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def do_GET(self) -> None:
                host = (self.headers.get("Host") or "").split(":", 1)[0]
                path = urlsplit(self.path).path
                with parent._lock:
                    parent.requests.append((host, path))
                body = parent.routes.get(f"{host}{path}") or parent.routes.get(path) or b"<!doctype html><p>ok</p>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.socket = ctx.wrap_socket(self._httpd.socket, server_side=True)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class _AuthenticatingHTTPProxy:
    """Minimal HTTP/1.1 forward + CONNECT tunnel proxy with Basic authentication."""

    def __init__(
        self,
        *,
        expected_user: Optional[str],
        expected_pass: Optional[str],
        host_map: Optional[Dict[str, Tuple[str, int]]] = None,
    ) -> None:
        self.expected_user = expected_user
        self.expected_pass = expected_pass
        self.host_map = host_map or {}
        self.challenges_sent = 0
        self.authorized_connects: List[str] = []
        self.authorized_http: List[str] = []
        self.rejected_attempts = 0
        self._lock = threading.Lock()
        parent = self

        class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
            daemon_threads = True
            allow_reuse_address = True

        class ProxyHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                parent._handle_client(self.request)

        self._srv = ThreadedTCPServer(("127.0.0.1", 0), ProxyHandler)
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()

    def _check_auth(self, headers: Dict[str, str]) -> bool:
        if self.expected_user is None:
            return True
        raw = headers.get("proxy-authorization") or ""
        if not raw.lower().startswith("basic "):
            with self._lock:
                self.challenges_sent += 1
            return False
        try:
            decoded = base64.b64decode(raw.split(" ", 1)[1].strip()).decode("utf-8")
        except Exception:
            decoded = ""
        if decoded == f"{self.expected_user}:{self.expected_pass}":
            return True
        with self._lock:
            self.rejected_attempts += 1
            self.challenges_sent += 1
        return False

    def _resolve_dest(self, host: str, port: int) -> Optional[Tuple[str, int]]:
        if host in self.host_map:
            return self.host_map[host]
        if host in ("127.0.0.1", "localhost"):
            return ("127.0.0.1", port)
        return None

    def _handle_client(self, client: socket.socket) -> None:
        client.settimeout(10.0)
        try:
            while True:
                buf = bytearray()
                while b"\r\n\r\n" not in buf:
                    chunk = client.recv(4096)
                    if not chunk:
                        return
                    buf.extend(chunk)
                    if len(buf) > 65536:
                        return
                head_end = buf.index(b"\r\n\r\n") + 4
                head_bytes = bytes(buf[:head_end])
                leftover = bytes(buf[head_end:])
                lines = head_bytes.decode("iso-8859-1").split("\r\n")
                req_line = lines[0]
                parts = req_line.split(" ")
                if len(parts) < 3:
                    return
                method, target, _proto = parts[0], parts[1], parts[2]
                headers: Dict[str, str] = {}
                for line in lines[1:]:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip().lower()] = v.strip()

                content_len = int(headers.get("content-length") or 0)
                while len(leftover) < content_len:
                    more = client.recv(4096)
                    if not more:
                        break
                    leftover += more

                if not self._check_auth(headers):
                    resp = (
                        b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                        b'Proxy-Authenticate: Basic realm="invpw-test"\r\n'
                        b"Content-Length: 0\r\n"
                        b"Connection: keep-alive\r\n\r\n"
                    )
                    client.sendall(resp)
                    continue

                if method.upper() == "CONNECT":
                    host_part, _, port_str = target.rpartition(":")
                    port = int(port_str or 443)
                    dest = self._resolve_dest(host_part, port)
                    if dest is None:
                        client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                        return
                    with self._lock:
                        self.authorized_connects.append(f"{host_part}:{port}")
                    upstream = socket.create_connection(dest, timeout=30.0)
                    try:
                        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                        if leftover:
                            upstream.sendall(leftover)
                        self._relay_bidirectional(client, upstream)
                    finally:
                        upstream.close()
                    return

                parsed = urlsplit(target)
                host = parsed.hostname or ""
                port = parsed.port or 80
                dest = self._resolve_dest(host, port)
                if dest is None:
                    client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                    return
                with self._lock:
                    self.authorized_http.append(target)
                rel_path = parsed.path or "/"
                if parsed.query:
                    rel_path += "?" + parsed.query
                out_lines = [f"{method} {rel_path} HTTP/1.1"]
                for k, v in headers.items():
                    if k in ("proxy-authorization", "proxy-connection", "connection"):
                        continue
                    out_lines.append(f"{k}: {v}")
                out_lines.append("Connection: close")
                out_raw = ("\r\n".join(out_lines) + "\r\n\r\n").encode("iso-8859-1") + leftover
                with socket.create_connection(dest, timeout=30.0) as upstream:
                    upstream.sendall(out_raw)
                    while True:
                        data = upstream.recv(16384)
                        if not data:
                            break
                        client.sendall(data)
                return
        except OSError:
            return
        finally:
            try:
                client.close()
            except OSError:
                pass

    @staticmethod
    def _relay_bidirectional(a: socket.socket, b: socket.socket) -> None:
        a.setblocking(False)
        b.setblocking(False)
        sockets = [a, b]
        while True:
            readable, _, exceptional = select.select(sockets, [], sockets, 10.0)
            if exceptional or not readable:
                break
            for s in readable:
                other = b if s is a else a
                try:
                    data = s.recv(16384)
                except OSError:
                    return
                if not data:
                    return
                other.setblocking(True)
                other.sendall(data)
                other.setblocking(False)


def test_chromium_authenticated_http_connect_tls_and_cross_site_oopif() -> None:
    asyncio.run(_run_authenticated_http_connect_tls_and_cross_site_oopif())


async def _run_authenticated_http_connect_tls_and_cross_site_oopif() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cert_path, key_path = _generate_san_cert(
            Path(tmp), ["site-a.test", "site-b.test"]
        )
        anchor_html = (
            b'<!doctype html><body><div id="anchor" style="width:44px;height:44px;border:1px solid #000"></div>'
            b"<script>window.clicks=[];document.getElementById('anchor').onclick=e=>{"
            b"window.clicks.push({trusted:e.isTrusted,origin:location.origin});};"
            b"console.log('oopif-ready', 99);</script></body>"
        )
        tls_origin = _RecordingTLSServer(
            cert_path,
            key_path,
            routes={},
        )
        top_html = (
            f'<!doctype html><body style="margin:80px 100px">'
            f'<h1 id="top">Top A</h1>'
            f'<iframe id="xframe" width="500" height="720" '
            f'src="https://site-b.test:{tls_origin.port}/anchor"></iframe>'
            f"<script>console.log('top-ready', true);</script></body>"
        ).encode("utf-8")
        tls_origin.routes[f"site-a.test/"] = top_html
        tls_origin.routes[f"site-b.test/anchor"] = anchor_html

        proxy = _AuthenticatingHTTPProxy(
            expected_user="svc-user",
            expected_pass="svc-secret",
            host_map={
                "site-a.test": ("127.0.0.1", tls_origin.port),
                "site-b.test": ("127.0.0.1", tls_origin.port),
            },
        )
        try:
            async with InvisiblePlaywright(
                backend="chromium",
                headless=True,
                seed=42,
                humanize=False,
                locale="en-US",
                timezone="UTC",
                proxy={
                    "server": f"http://127.0.0.1:{proxy.port}",
                    "username": "svc-user",
                    "password": "svc-secret",
                },
            ) as browser:
                context = await browser.new_context(ignore_https_errors=True)
                page = await context.new_page()
                seen_console: List[Tuple[str, str]] = []
                page.on("console", lambda m: seen_console.append((m.type, m.text)))

                await page.goto(
                    f"https://site-a.test:{tls_origin.port}/",
                    wait_until="load",
                )
                assert await page.locator("#top").text_content() == "Top A"

                child = next(f for f in page.frames if "site-b.test" in f.url)
                await child.locator("#anchor").click()
                clicks = await child.evaluate("window.clicks")
                assert clicks == [
                    {
                        "trusted": True,
                        "origin": f"https://site-b.test:{tls_origin.port}",
                    }
                ]

                # Both top-level and cross-site OOPIF negotiated end-to-end TLS over HTTP CONNECT
                assert f"site-a.test:{tls_origin.port}" in proxy.authorized_connects
                assert f"site-b.test:{tls_origin.port}" in proxy.authorized_connects
                assert proxy.challenges_sent >= 1
                assert "site-a.test" in tls_origin.sni_seen
                assert "site-b.test" in tls_origin.sni_seen
                assert ("log", "top-ready true") in seen_console
                assert ("log", "oopif-ready 99") in seen_console
                await context.close()
        finally:
            proxy.close()
            tls_origin.close()


def test_chromium_authenticated_proxy_wrong_password_fails_fast() -> None:
    asyncio.run(_run_authenticated_proxy_wrong_password_fails_fast())


async def _run_authenticated_proxy_wrong_password_fails_fast() -> None:
    class EchoHandler(BaseHTTPRequestHandler):
        hit = False

        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:
            EchoHandler.hit = True
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"should-not-reach")

    origin = ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    proxy = _AuthenticatingHTTPProxy(
        expected_user="right-user",
        expected_pass="right-pass",
        host_map={"target.example.test": ("127.0.0.1", origin.server_port)},
    )
    try:
        async with InvisiblePlaywright(
            backend="chromium",
            headless=True,
            seed=42,
            humanize=False,
            locale="en-US",
            timezone="UTC",
            proxy={
                "server": f"http://127.0.0.1:{proxy.port}",
                "username": "right-user",
                "password": "WRONG-PASSWORD",
            },
        ) as browser:
            # Blank page creation must succeed immediately (no 60s hang)
            page = await browser.new_page()
            # Navigation with wrong credentials must fail fast (CancelAuth breaks 407 loop)
            resp = None
            err = None
            try:
                resp = await page.goto(
                    f"http://target.example.test:{origin.server_port}/"
                )
            except PlaywrightError as exc:
                err = exc
            assert not EchoHandler.hit
            assert proxy.rejected_attempts >= 1
            assert err is not None or (resp is not None and resp.status == 407)
    finally:
        proxy.close()
        origin.shutdown()
        origin.server_close()


def test_chromium_authenticated_proxy_with_route_and_per_context_proxy() -> None:
    asyncio.run(_run_authenticated_proxy_with_route_and_per_context_proxy())


async def _run_authenticated_proxy_with_route_and_per_context_proxy() -> None:
    class OriginHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:
            body = f"<!doctype html><div id='path'>{self.path}</div>".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    origin = ThreadingHTTPServer(("127.0.0.1", 0), OriginHandler)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    proxy_a = _AuthenticatingHTTPProxy(
        expected_user="user-a",
        expected_pass="pass-a",
        host_map={"svc.example.test": ("127.0.0.1", origin.server_port)},
    )
    proxy_b = _AuthenticatingHTTPProxy(
        expected_user="user-b",
        expected_pass="pass-b",
        host_map={"svc.example.test": ("127.0.0.1", origin.server_port)},
    )
    try:
        async with InvisiblePlaywright(
            backend="chromium",
            headless=True,
            seed=42,
            humanize=False,
            locale="en-US",
            timezone="UTC",
        ) as browser:
            ctx_a = await browser.new_context(
                proxy={
                    "server": f"http://127.0.0.1:{proxy_a.port}",
                    "username": "user-a",
                    "password": "pass-a",
                }
            )
            ctx_b = await browser.new_context(
                proxy={
                    "server": f"http://127.0.0.1:{proxy_b.port}",
                    "username": "user-b",
                    "password": "pass-b",
                }
            )

            async def mock_api(route: Any) -> None:
                if "/mocked" in route.request.url:
                    await route.fulfill(
                        status=200,
                        content_type="text/html",
                        body="<!doctype html><div id='path'>/intercepted</div>",
                    )
                else:
                    await route.continue_()

            await ctx_a.route("**/*", mock_api)
            pa = await ctx_a.new_page()
            pb = await ctx_b.new_page()

            await pa.goto(f"http://svc.example.test:{origin.server_port}/from-a")
            assert await pa.locator("#path").text_content() == "/from-a"
            await pa.goto(f"http://svc.example.test:{origin.server_port}/mocked")
            assert await pa.locator("#path").text_content() == "/intercepted"

            # Unrouting must keep proxy authentication enabled!
            await ctx_a.unroute_all()
            await pa.goto(f"http://svc.example.test:{origin.server_port}/after-unroute")
            assert await pa.locator("#path").text_content() == "/after-unroute"

            await pb.goto(f"http://svc.example.test:{origin.server_port}/from-b")
            assert await pb.locator("#path").text_content() == "/from-b"

            assert any("/from-a" in u for u in proxy_a.authorized_http)
            assert any("/after-unroute" in u for u in proxy_a.authorized_http)
            assert not any("/from-b" in u for u in proxy_a.authorized_http)
            assert any("/from-b" in u for u in proxy_b.authorized_http)

            await ctx_a.close()
            await ctx_b.close()
    finally:
        proxy_a.close()
        proxy_b.close()
        origin.shutdown()
        origin.server_close()


def test_chromium_explicit_errors_for_socks_auth_and_live_proxy_swap() -> None:
    asyncio.run(_run_explicit_errors_for_socks_auth_and_live_proxy_swap())


async def _run_explicit_errors_for_socks_auth_and_live_proxy_swap() -> None:
    with pytest.raises(Exception, match="SOCKS proxy username/password"):
        async with InvisiblePlaywright(
            backend="chromium",
            headless=True,
            seed=42,
            humanize=False,
            locale="en-US",
            timezone="UTC",
            proxy={
                "server": "socks5://127.0.0.1:1080",
                "username": "u",
                "password": "p",
            },
        ):
            pass

    async with InvisiblePlaywright(
        backend="chromium",
        headless=True,
        seed=42,
        humanize=False,
        locale="en-US",
        timezone="UTC",
    ) as browser:
        with pytest.raises(PlaywrightError, match="SOCKS proxy username/password"):
            await browser.new_context(
                proxy={
                    "server": "socks5://127.0.0.1:1080",
                    "username": "u",
                    "password": "p",
                }
            )

        ctx = await browser.new_context()
        _page = await ctx.new_page()
        # Attempting private Browser.setContextProxy on an active Chromium context must fail explicitly
        server = browser._impl_obj._connection._transport._server
        ctx_disp = server.object(ctx._impl_obj._guid)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            ephemeral_port = sock.getsockname()[1]

        with pytest.raises(Exception, match="does not support changing proxy on an active"):
            ctx_disp.browser.conn.send(
                "Browser.setContextProxy",
                {
                    "browserContextId": ctx_disp.context_id,
                    "type": "http",
                    "host": "127.0.0.1",
                    "port": ephemeral_port,
                },
            )
        await ctx.close()


def test_chromium_console_pageerror_and_runtime_enable_stealth() -> None:
    asyncio.run(_run_console_pageerror_and_runtime_enable_stealth())


async def _run_console_pageerror_and_runtime_enable_stealth() -> None:
    html = b"""<!doctype html><body>
    <script>
      console.log('alpha', 42, true, false, null, undefined);
      console.warn('beta-warn');
      console.error('gamma-err');
      queueMicrotask(() => { throw new Error('uncaught-fixture-boom'); });
    </script></body>"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        async with InvisiblePlaywright(
            backend="chromium",
            headless=True,
            seed=42,
            humanize=False,
            locale="en-US",
            timezone="UTC",
        ) as browser:
            page = await browser.new_page()
            consoles: List[Tuple[str, str, str]] = []
            errors: List[str] = []
            got_error_ev = asyncio.Event()
            page.on(
                "console",
                lambda m: consoles.append(
                    (m.type, m.text, m.location.get("url", ""))
                ),
            )

            def _on_page_err(exc: Any) -> None:
                errors.append(str(exc))
                got_error_ev.set()

            page.on("pageerror", _on_page_err)

            await page.goto(f"http://127.0.0.1:{srv.server_port}/", wait_until="load")
            await got_error_ev.wait()

            # Verify console messages and pageerror
            assert ("log", "alpha 42 true false null undefined", f"http://127.0.0.1:{srv.server_port}/") in consoles or any(
                t == "log" and txt == "alpha 42 true false null undefined" for t, txt, _ in consoles
            ), consoles
            assert any(t == "warning" and txt == "beta-warn" for t, txt, _ in consoles), consoles
            assert any(t == "error" and txt == "gamma-err" for t, txt, _ in consoles), consoles
            assert any("uncaught-fixture-boom" in e for e in errors), errors

            # Verify Runtime.enable stealth probes stay completely clean
            before_count = len(consoles)
            probe = await page.evaluate("""() => {
                let errStackGetter = false;
                const err = new Error('probe');
                Object.defineProperty(err, 'stack', { get() { errStackGetter = true; return 'leak'; } });
                console.log(err);

                let proxyTrapped = false;
                const px = new Proxy({}, {
                    get() { proxyTrapped = true; return 1; },
                    ownKeys() { proxyTrapped = true; return []; },
                    getOwnPropertyDescriptor() { proxyTrapped = true; return undefined; },
                });
                console.dir(px);
                return {
                    webdriver: navigator.webdriver,
                    errStackGetter,
                    proxyTrapped,
                    landingLeak: '__invpwLanding' in window,
                };
            }""")
            assert probe == {
                "webdriver": False,
                "errStackGetter": False,
                "proxyTrapped": False,
                "landingLeak": False,
            }
            # Locator queries in utility world must NOT emit spurious console messages
            count_after_probe = len(consoles)
            assert count_after_probe >= before_count + 2
            await page.locator("body").is_visible()
            assert len(consoles) == count_after_probe
    finally:
        srv.shutdown()
        srv.server_close()


def test_chromium_landson_detects_overlay_interception_and_iframe_click_matrix() -> None:
    asyncio.run(_run_landson_and_iframe_click_matrix())


async def _run_landson_and_iframe_click_matrix() -> None:
    html = """<!doctype html><body>
    <button id="target" style="position:absolute;left:40px;top:40px;width:100px;height:50px">Go</button>
    <a id="nav-link" href="about:blank#landed" style="position:absolute;left:40px;top:120px">Jump</a>
    <script>
      window.targetClicked = false;
      document.getElementById('target').addEventListener('click', () => { window.targetClicked = true; });
    </script></body>"""

    async with InvisiblePlaywright(
        backend="chromium",
        headless=True,
        seed=42,
        humanize=False,
        locale="en-US",
        timezone="UTC",
    ) as browser:
        page = await browser.new_page()
        await page.set_content(html)

        # 1. Normal click lands cleanly and leaves zero main-world globals
        await page.locator("#target").click()
        assert await page.evaluate("window.targetClicked") is True
        assert await page.evaluate("'__invpwLanding' in window") is False

        # 2. Same-document hash/navigation click lands cleanly
        await page.locator("#nav-link").click()
        assert page.url.endswith("#landed")

        # 3. Direct protocol landsOn check when an overlay covers the target at dispatch time:
        # must report landed=False with the covering element's selector (never lie landed=True!)
        await page.evaluate("""() => {
            const d = document.createElement('div');
            d.id = 'covering-overlay';
            d.className = 'blocker';
            d.style.cssText = 'position:fixed;inset:0;z-index:9999';
            document.body.appendChild(d);
        }""")
        server = browser._impl_obj._connection._transport._server
        page_disp = server.object(page._impl_obj._guid)
        fid = page_disp.lifecycle.main_frame
        el_oid = page_disp.injected.query_selector(fid, "#target")
        res = page_disp.conn.send(
            "Page.dispatchMouseEvent",
            {
                "type": "mousedown",
                "x": 60.0,
                "y": 60.0,
                "button": 0,
                "buttons": 1,
                "clickCount": 1,
                "modifiers": 0,
                "landsOn": {"frameId": fid, "objectId": el_oid},
            },
            session=page_disp.session,
        )
        page_disp.conn.send(
            "Page.dispatchMouseEvent",
            {
                "type": "mouseup",
                "x": 60.0,
                "y": 60.0,
                "button": 0,
                "buttons": 0,
                "clickCount": 1,
                "modifiers": 0,
            },
            session=page_disp.session,
        )
        assert res["landing"] == {
            "landed": False,
            "on": "div#covering-overlay.blocker",
        }

    # 4. Full 8-mode zero-delay iframe click matrix (exact downstream reproducer coverage)
    events_js = (
        "<script>window.events=[];"
        "for (const type of ['pointermove','pointerdown','pointerup','mousedown','mouseup','click']) {"
        " document.addEventListener(type, e => window.events.push({type, trusted:e.isTrusted}), true);"
        "}</script>"
    )
    anchor_doc = (
        '<!doctype html><body><div class="rc-anchor"><div id="recaptcha-anchor" role="checkbox" '
        'style="width:40px;height:40px;border:2px solid black"></div>'
        "<script>window.nativeClicks=[];document.querySelector('#recaptcha-anchor').onclick=e=>{"
        "window.nativeClicks.push({operation:'checkbox',trusted:e.isTrusted})};</script></div>"
        + events_js
    )
    child_origin_holder: List[str] = [""]

    class MatrixHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path in ("/", "/same-origin", "/without-aria"):
                c_orig = (
                    f"http://127.0.0.1:{self.server.server_port}"
                    if path == "/same-origin"
                    else child_origin_holder[0]
                )
                body = (
                    '<!doctype html><body style="margin:90px 120px"><div aria-hidden="true">'
                    '<iframe id="frame" style="border:0" width="620" height="760" src="'
                    + c_orig
                    + '/recaptcha/api2/anchor"></iframe></div>'
                    + events_js
                )
                if path == "/without-aria":
                    body = body.replace('aria-hidden="true"', "")
            else:
                body = anchor_doc
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    with ExitStack() as stack:
        srvs = [ThreadingHTTPServer(("127.0.0.1", 0), MatrixHandler) for _ in range(2)]
        for s in srvs:
            threading.Thread(target=s.serve_forever, daemon=True).start()
            stack.callback(s.server_close)
            stack.callback(s.shutdown)
        top_orig = f"http://127.0.0.1:{srvs[0].server_port}"
        child_origin_holder[0] = f"http://127.0.0.1:{srvs[1].server_port}"

        async with InvisiblePlaywright(
            backend="chromium",
            seed=42,
            headless=True,
            humanize=False,
            locale="en-US",
            timezone="UTC",
        ) as browser:
            ctx = await browser.new_context()
            await ctx.route("**/*", lambda r: r.continue_())
            page = await ctx.new_page()
            for mode in (
                "locator",
                "locator-explicit-position",
                "mouse-from-bbox",
                "mouse-from-dom",
                "same-origin",
                "without-aria",
                "direct-locator",
                "direct-mouse",
            ):
                sub = (
                    "/direct"
                    if mode.startswith("direct")
                    else ("/" + mode if mode in ("same-origin", "without-aria") else "")
                )
                await page.goto(top_orig + sub, wait_until="load")
                frame = (
                    page.main_frame
                    if mode.startswith("direct")
                    else next(f for f in page.frames if "/recaptcha/" in f.url)
                )
                loc = frame.locator("#recaptcha-anchor")
                bbox = await loc.bounding_box()
                assert bbox is not None
                if mode in ("locator", "direct-locator", "same-origin", "without-aria"):
                    await loc.click()
                elif mode == "locator-explicit-position":
                    await loc.click(position={"x": 20, "y": 20})
                elif mode in ("mouse-from-bbox", "direct-mouse"):
                    await page.mouse.click(
                        bbox["x"] + bbox["width"] / 2,
                        bbox["y"] + bbox["height"] / 2,
                    )
                else:
                    outer = await page.evaluate(
                        "document.querySelector('iframe').getBoundingClientRect().toJSON()"
                    )
                    inner = await frame.evaluate(
                        "document.querySelector('#recaptcha-anchor').getBoundingClientRect().toJSON()"
                    )
                    await page.mouse.click(
                        outer["x"] + inner["x"] + inner["width"] / 2,
                        outer["y"] + inner["y"] + inner["height"] / 2,
                    )
                clicks = await frame.evaluate("window.nativeClicks")
                assert clicks == [{"operation": "checkbox", "trusted": True}], mode
            await ctx.close()


def test_chromium_self_signed_tls_js_disabled_sw_blocked_and_last_tab_close() -> None:
    asyncio.run(_run_self_signed_tls_js_disabled_sw_blocked_and_last_tab_close())


async def _run_self_signed_tls_js_disabled_sw_blocked_and_last_tab_close() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cert_path, key_path = _generate_san_cert(Path(tmp), ["localhost"])
        tls_srv = _RecordingTLSServer(
            cert_path,
            key_path,
            routes={
                "/": (
                    b"<!doctype html><body><div id='static'>Safe</div>"
                    b"<script>document.getElementById('static').textContent='Executed';</script></body>"
                )
            },
        )
        try:
            async with InvisiblePlaywright(
                backend="chromium",
                headless=True,
                seed=42,
                humanize=False,
                locale="en-US",
                timezone="UTC",
            ) as browser:
                # 1. Default context rejects self-signed TLS
                p1 = await browser.new_page()
                with pytest.raises(PlaywrightError):
                    await p1.goto(f"https://127.0.0.1:{tls_srv.port}/")

                # 2. Closing the ONLY open tab then opening a new page works cleanly
                await p1.close()
                p2 = await browser.new_page()
                await p2.set_content("<h1>Reopened</h1>")
                assert await p2.locator("h1").text_content() == "Reopened"
                await p2.close()

                # 3. ignore_https_errors=True + java_script_enabled=False + service_workers="block"
                ctx = await browser.new_context(
                    ignore_https_errors=True,
                    java_script_enabled=False,
                    service_workers="block",
                )
                p3 = await ctx.new_page()
                await p3.goto(f"https://127.0.0.1:{tls_srv.port}/")
                # Page script was NOT executed because java_script_enabled=False
                assert await p3.locator("#static").text_content() == "Safe"
                sw_check = await p3.evaluate("""async () => {
                    const src = navigator.serviceWorker ? String(navigator.serviceWorker.register) : '';
                    let rejected = false;
                    if (navigator.serviceWorker) {
                        try { await navigator.serviceWorker.register('/sw.js'); }
                        catch (e) { rejected = true; }
                    }
                    return { rejected, leaksPlaywright: src.includes('Playwright') };
                }""")
                assert sw_check == {"rejected": True, "leaksPlaywright": False}
                await ctx.close()
        finally:
            tls_srv.close()


def test_chromium_webrtc_non_proxied_udp_policy_enabled_with_proxy() -> None:
    asyncio.run(_run_webrtc_non_proxied_udp_policy_enabled_with_proxy())


async def _run_webrtc_non_proxied_udp_policy_enabled_with_proxy() -> None:
    proxy = _AuthenticatingHTTPProxy(expected_user=None, expected_pass=None)
    try:
        async with InvisiblePlaywright(
            backend="chromium",
            headless=True,
            seed=42,
            humanize=False,
            locale="en-US",
            timezone="UTC",
            proxy={"server": f"http://127.0.0.1:{proxy.port}"},
        ) as browser:
            page = await browser.new_page()
            await page.set_content("<!doctype html><p>webrtc</p>")
            candidates = await page.evaluate("""() => new Promise((resolve) => {
                const list = [];
                let settled = false;
                const finish = () => {
                    if (settled) return;
                    settled = true;
                    try { pc.close(); } catch {}
                    resolve(list);
                };
                const pc = new RTCPeerConnection({ iceServers: [] });
                pc.createDataChannel('probe');
                pc.onicecandidate = (e) => {
                    if (e.candidate && e.candidate.candidate) {
                        list.push(e.candidate.candidate);
                    } else {
                        finish();
                    }
                };
                pc.onicegatheringstatechange = () => {
                    if (pc.iceGatheringState === 'complete') finish();
                };
                pc.createOffer().then((o) => pc.setLocalDescription(o));
            })""")
            # With disable_non_proxied_udp active and an HTTP proxy (no UDP relay), zero raw UDP host candidates leak
            udp_candidates = [c for c in candidates if " udp " in c.lower()]
            assert udp_candidates == [], f"Unexpected non-proxied UDP candidates: {candidates}"
    finally:
        proxy.close()


@pytest.mark.parametrize("backend", ["firefox", "chromium"])
def test_async_route_cancellation_emits_zero_unretrieved_future_warnings(
    backend: str,
) -> None:
    asyncio.run(_run_async_route_cancellation(backend))


async def _run_async_route_cancellation(
    backend: str,
) -> None:
    class SlowHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:
            if self.path == "/":
                body = (
                    b"<!doctype html><body><img src='/slow.png'>"
                    b"<script src='/slow.js'></script></body>"
                )
            else:
                body = b"console.log(1);"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    loop = asyncio.get_running_loop()
    unhandled: List[Dict[str, Any]] = []
    prev_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _l, ctx: unhandled.append(ctx))

    try:
        async with InvisiblePlaywright(
            backend=backend,
            headless=True,
            seed=42,
            humanize=False,
            locale="en-US",
            timezone="UTC",
        ) as browser:
            for _ in range(3):
                context = await browser.new_context()
                route_entered = asyncio.Event()
                hold_route = asyncio.Event()

                async def slow_route(route: Any) -> None:
                    if "slow" in route.request.url:
                        route_entered.set()
                        await hold_route.wait()
                    await route.continue_()

                await context.route("**/*", slow_route)
                page = await context.new_page()

                async def worker() -> None:
                    try:
                        await page.goto(
                            f"http://127.0.0.1:{srv.server_port}/",
                            wait_until="load",
                        )
                    finally:
                        hold_route.set()
                        await context.unroute_all(behavior="ignoreErrors")
                        await context.close()

                task = asyncio.create_task(worker())
                await route_entered.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

                for _turn in range(3):
                    yield_fut = loop.create_future()
                    loop.call_soon(yield_fut.set_result, None)
                    await yield_fut
                gc.collect()

        gc.collect()
        yield_fut = loop.create_future()
        loop.call_soon(yield_fut.set_result, None)
        await yield_fut
        assert unhandled == [], f"Unhandled asyncio loop exceptions: {unhandled}"
    finally:
        loop.set_exception_handler(prev_handler)
        srv.shutdown()
        srv.server_close()


def test_chromium_invalid_auth_immediate_sequential_navigations() -> None:
    asyncio.run(_run_invalid_auth_immediate_sequential_navigations())


async def _run_invalid_auth_immediate_sequential_navigations() -> None:
    class NeverHitOrigin(BaseHTTPRequestHandler):
        hit = False

        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:
            NeverHitOrigin.hit = True
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"unreachable")

    http_origin = ThreadingHTTPServer(("127.0.0.1", 0), NeverHitOrigin)
    threading.Thread(target=http_origin.serve_forever, daemon=True).start()
    proxy = _AuthenticatingHTTPProxy(
        expected_user="valid-user",
        expected_pass="valid-pass",
        host_map={
            "auth-http.example.test": ("127.0.0.1", http_origin.server_port),
            "auth-https.example.test": ("127.0.0.1", http_origin.server_port),
        },
    )
    try:
        async with InvisiblePlaywright(
            backend="chromium",
            headless=True,
            seed=42,
            humanize=False,
            locale="en-US",
            timezone="UTC",
            proxy={
                "server": f"http://127.0.0.1:{proxy.port}",
                "username": "valid-user",
                "password": "WRONG-PASSWORD",
            },
        ) as browser:
            for round_idx in range(3):
                context = await browser.new_context(ignore_https_errors=True)
                page = await context.new_page()
                err1 = None
                try:
                    await page.goto(
                        f"http://auth-http.example.test:{http_origin.server_port}/r{round_idx}",
                    )
                except PlaywrightError as exc:
                    err1 = exc
                # Immediate second navigation on the exact same page after HTTP 407 failure
                err2 = None
                try:
                    await page.goto(
                        f"https://auth-https.example.test:{http_origin.server_port}/r{round_idx}",
                    )
                except PlaywrightError as exc:
                    err2 = exc
                assert not NeverHitOrigin.hit
                assert err1 is not None
                assert err2 is not None
                await page.close()
                reopened = await context.new_page()
                assert await reopened.evaluate("1 + 1") == 2
                await context.close()
    finally:
        proxy.close()
        http_origin.shutdown()
        http_origin.server_close()


@pytest.mark.parametrize("with_route", [False, True])
def test_chromium_true_cross_site_oopif_127_to_localhost(with_route: bool) -> None:
    asyncio.run(_run_true_cross_site_oopif_127_to_localhost(with_route))


async def _run_true_cross_site_oopif_127_to_localhost(with_route: bool) -> None:
    class CrossSiteHandler(BaseHTTPRequestHandler):
        port_ref = 0

        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:
            p = CrossSiteHandler.port_ref
            if self.path == "/":
                body = (
                    "<!doctype html><html><body>"
                    f"<iframe id='same' src='http://127.0.0.1:{p}/same'></iframe>"
                    f"<iframe id='cross' src='http://localhost:{p}/cross'></iframe>"
                    "<script>"
                    "console.log('main-log', 1);"
                    "queueMicrotask(() => { throw new Error('main-boom'); });"
                    "</script></body></html>"
                ).encode("utf-8")
            elif self.path == "/same":
                body = (
                    "<!doctype html><html><body>"
                    "<script>"
                    "console.warn('same-warn', 2);"
                    "queueMicrotask(() => { throw new Error('same-boom'); });"
                    "</script></body></html>"
                ).encode("utf-8")
            else:
                body = (
                    "<!doctype html><html><body style='margin:0'>"
                    "<button id='xbtn' style='width:120px;height:48px'"
                    " onclick='window.__clicked=(window.__clicked||0)+1;this.textContent=String(window.__clicked)'>0</button>"
                    "<script>"
                    "console.error('cross-err', 3);"
                    "queueMicrotask(() => { throw new Error('cross-boom'); });"
                    "</script></body></html>"
                ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), CrossSiteHandler)
    CrossSiteHandler.port_ref = srv.server_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        async with InvisiblePlaywright(
            backend="chromium",
            headless=True,
            seed=42,
            humanize=False,
            locale="en-US",
            timezone="UTC",
        ) as browser:
            context = await browser.new_context()
            routed_urls: List[str] = []
            if with_route:
                async def pass_route(route: Any) -> None:
                    routed_urls.append(route.request.url)
                    await route.continue_()

                await context.route("**/*", pass_route)

            page = await context.new_page()
            consoles: List[tuple[str, str]] = []
            errors: List[str] = []
            all_three_errors = asyncio.Event()

            def on_console(msg: Any) -> None:
                consoles.append((msg.type, msg.text))

            def on_err(exc: Any) -> None:
                errors.append(str(exc))
                if len(errors) >= 3:
                    all_three_errors.set()

            page.on("console", on_console)
            page.on("pageerror", on_err)

            await page.goto(
                f"http://127.0.0.1:{srv.server_port}/",
                wait_until="load",
            )
            await all_three_errors.wait()

            assert ("log", "main-log 1") in consoles
            assert ("warning", "same-warn 2") in consoles
            assert ("error", "cross-err 3") in consoles
            assert len(consoles) == 3, f"Unexpected duplicate/internal console events: {consoles}"
            assert any("main-boom" in e for e in errors)
            assert any("same-boom" in e for e in errors)
            assert any("cross-boom" in e for e in errors)
            assert len(errors) == 3, f"Unexpected pageerror count: {errors}"

            # Also verify zero-delay trusted click inside the true cross-site localhost OOPIF
            cross_btn = page.frame_locator("#cross").locator("#xbtn")
            await cross_btn.click()
            assert await cross_btn.text_content() == "1"

            if with_route:
                assert any("localhost" in u for u in routed_urls)

            await context.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_chromium_long_tmpdir_singleton_socket_launch_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deep_tmp = (
        tmp_path
        / "google-browser-worker-nested-directory-exceeding-unix-sun-path-limit"
    )
    deep_tmp.mkdir(parents=True, exist_ok=True)
    assert len(str(deep_tmp).encode("utf-8")) > 65
    monkeypatch.setenv("TMPDIR", str(deep_tmp))
    monkeypatch.setenv("TMP", str(deep_tmp))
    monkeypatch.setenv("TEMP", str(deep_tmp))
    tempfile.tempdir = None
    try:
        asyncio.run(_run_long_tmpdir_singleton_socket_launch())
    finally:
        tempfile.tempdir = None


async def _run_long_tmpdir_singleton_socket_launch() -> None:
    before_short = {
        n for n in os.listdir("/tmp") if n.startswith(f"ipw_t_{os.getpid()}_")
    }
    async with InvisiblePlaywright(
        backend="chromium",
        headless=True,
        seed=42,
        humanize=False,
        locale="en-US",
        timezone="UTC",
    ) as browser:
        page = await browser.new_page()
        assert await page.evaluate("navigator.webdriver") is False
        assert await page.evaluate("21 * 2") == 42
    after_short = {
        n for n in os.listdir("/tmp") if n.startswith(f"ipw_t_{os.getpid()}_")
    }
    assert after_short <= before_short


def test_chromium_target_blank_noopener_popup_and_window_open() -> None:
    asyncio.run(_run_target_blank_noopener_popup_and_window_open())


async def _run_target_blank_noopener_popup_and_window_open() -> None:
    class PopupHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:
            if self.path == "/":
                body = (
                    b"<!doctype html><html><body>"
                    b"<a id='immediate' target='_blank' href='/popup?immediate'>Immediate</a>"
                    b"<button id='wopen' onclick=\"window.open('/popup?wopen','_blank')\">Open</button>"
                    b"</body></html>"
                )
            else:
                body = (
                    f"<!doctype html><html><head><title>Popup {self.path}</title></head>"
                    f"<body><div id='p'>{self.path}</div></body></html>"
                ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), PopupHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        async with InvisiblePlaywright(
            backend="chromium",
            headless=True,
            seed=42,
            humanize=0.2,
            locale="en-US",
            timezone="UTC",
        ) as browser:
            context = await browser.new_context()
            page = await context.new_page()
            seen_popups: List[Any] = []
            page.on("popup", lambda p: seen_popups.append(p))

            await page.goto(
                f"http://127.0.0.1:{srv.server_port}/", wait_until="load"
            )

            async with context.expect_page() as p1_info:
                await page.locator("#immediate").click()
            pop1 = await p1_info.value
            await pop1.wait_for_load_state("load")
            assert pop1.url.endswith("/popup?immediate")
            assert pop1 in context.pages
            assert pop1 in seen_popups
            await pop1.close()
            assert len(context.pages) == 1

            async with page.expect_popup() as p2_info:
                await page.locator("#wopen").click()
            pop2 = await p2_info.value
            await pop2.wait_for_load_state("load")
            assert pop2.url.endswith("/popup?wopen")
            await pop2.close()
            assert len(context.pages) == 1
            await context.close()
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("backend", ["firefox", "chromium"])
def test_dual_backend_caller_observer_isolation_and_isolated_handles(
    backend: str,
) -> None:
    asyncio.run(_run_caller_observer_isolation_and_isolated_handles(backend))


async def _run_caller_observer_isolation_and_isolated_handles(
    backend: str,
) -> None:
    class ObserverHandler(BaseHTTPRequestHandler):
        port_ref = 0

        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:
            p = ObserverHandler.port_ref
            if self.path == "/":
                body = (
                    "<!doctype html><html><body>"
                    f"<iframe id='xf' width='400' height='300' src='http://localhost:{p}/frame'></iframe>"
                    "</body></html>"
                ).encode("utf-8")
            else:
                body = (
                    "<!doctype html><html><body data-obs-calls='0' data-qs-calls='0'>"
                    "<div id='status' data-step='init'>ready</div>"
                    "<button id='mut' onclick=\"document.getElementById('status').setAttribute('data-step','clicked')\">Mutate</button>"
                    "<script>"
                    "window.pageCustomValue = 99;"
                    "const NativeMO = window.MutationObserver;"
                    "window.MutationObserver = class extends NativeMO {"
                    "  constructor(cb) {"
                    "    super(cb);"
                    "    document.body.dataset.obsCalls = String(1 + Number(document.body.dataset.obsCalls));"
                    "  }"
                    "};"
                    "const nativeQS = Document.prototype.querySelector;"
                    "Document.prototype.querySelector = function(sel) {"
                    "  document.body.dataset.qsCalls = String(1 + Number(document.body.dataset.qsCalls));"
                    "  return nativeQS.call(this, sel);"
                    "};"
                    "</script></body></html>"
                ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), ObserverHandler)
    ObserverHandler.port_ref = srv.server_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        async with InvisiblePlaywright(
            backend=backend,
            headless=True,
            seed=42,
            humanize=False,
            locale="en-US",
            timezone="UTC",
        ) as browser:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(
                f"http://127.0.0.1:{srv.server_port}/", wait_until="load"
            )
            frame = next(f for f in page.frames if "localhost" in f.url)

            # 1. Default caller evaluate_handle sees page globals AND uses pristine MutationObserver
            #    without adding any own keys on window.
            has_invpw_keys = await frame.evaluate(
                "Reflect.ownKeys(window).some(k => String(k).includes('invpw'))"
            )
            assert has_invpw_keys is False
            assert await frame.evaluate("window.pageCustomValue") == 99

            obs_handle = await frame.evaluate_handle(
                """() => {
                    const seen = [];
                    const obs = new MutationObserver(recs => {
                        for (const r of recs) seen.push(r.attributeName);
                    });
                    obs.observe(document.getElementById('status'), { attributes: true });
                    return {
                        drain: () => seen.splice(0),
                        close: () => { obs.disconnect(); return seen.splice(0); }
                    };
                }"""
            )
            assert await frame.locator("body").get_attribute("data-obs-calls") == "0"
            await frame.locator("#mut").click()
            drained = await obs_handle.evaluate("h => h.drain()")
            assert "data-step" in drained
            await obs_handle.evaluate("h => h.close()")
            await obs_handle.dispose()
            assert await frame.locator("body").get_attribute("data-obs-calls") == "0"

            # 2. Public isolated=True handle & Frame.evaluate_read bypass even prototype monkey-patching
            iso_handle = await frame.evaluate_handle(
                """() => {
                    const el = document.querySelector('#status');
                    return { step: el.getAttribute('data-step'), hasPageCustom: 'pageCustomValue' in window };
                }""",
                isolated=True,
            )
            assert await frame.locator("body").get_attribute("data-qs-calls") == "0"
            assert await iso_handle.json_value() == {
                "step": "clicked",
                "hasPageCustom": False,
            }
            assert await iso_handle.evaluate("o => o.step") == "clicked"
            await iso_handle.dispose()

            read_val = await frame.evaluate_read(
                "() => document.querySelector('#status').textContent",
                isolated=True,
            )
            assert read_val == "ready"
            assert await frame.locator("body").get_attribute("data-qs-calls") == "0"
            await context.close()
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("backend", ["firefox", "chromium"])
@pytest.mark.parametrize("with_route", [False, True])
def test_dual_backend_oopif_resource_types_and_document_response_body(
    backend: str, with_route: bool
) -> None:
    asyncio.run(_run_oopif_resource_types_and_document_response_body(backend, with_route))


async def _run_oopif_resource_types_and_document_response_body(
    backend: str, with_route: bool
) -> None:
    png_1x1 = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )

    class AssetHandler(BaseHTTPRequestHandler):
        frame_port = 0

        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:
            if self.path == "/":
                body = (
                    "<!doctype html><html><body>"
                    f"<iframe id='bframe' src='http://localhost:{AssetHandler.frame_port}/bframe'></iframe>"
                    "</body></html>"
                ).encode("utf-8")
                mime = "text/html; charset=utf-8"
            elif self.path == "/bframe":
                body = (
                    "<!doctype html><html><head>"
                    "<link rel='stylesheet' href='/style.css'>"
                    "</head><body>"
                    "<img id='tile' src='/tile.png'>"
                    "<script src='/app.js'></script>"
                    "</body></html>"
                ).encode("utf-8")
                mime = "text/html; charset=utf-8"
            elif self.path == "/style.css":
                body = b"body { margin: 0; }"
                mime = "text/css"
            elif self.path == "/tile.png":
                body = png_1x1
                mime = "image/png"
            elif self.path == "/app.js":
                body = (
                    b"Promise.all(["
                    b"  fetch('/api-fetch').then(r => r.text()),"
                    b"  new Promise(res => { const x = new XMLHttpRequest(); x.onload = () => res(x.responseText); x.open('GET', '/api-xhr'); x.send(); })"
                    b"]).then(() => { document.body.dataset.ready = 'yes'; });"
                )
                mime = "application/javascript"
            elif self.path == "/api-fetch":
                body = b'{"ok":"fetch"}'
                mime = "application/json"
            elif self.path == "/api-xhr":
                body = b'{"ok":"xhr"}'
                mime = "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    s_main = ThreadingHTTPServer(("127.0.0.1", 0), AssetHandler)
    s_frame = ThreadingHTTPServer(("127.0.0.1", 0), AssetHandler)
    AssetHandler.frame_port = s_frame.server_port
    for s in (s_main, s_frame):
        threading.Thread(target=s.serve_forever, daemon=True).start()

    try:
        async with InvisiblePlaywright(
            backend=backend,
            headless=True,
            seed=42,
            humanize=False,
            locale="en-US",
            timezone="UTC",
        ) as browser:
            context = await browser.new_context()
            if with_route:
                await context.route("**/*", lambda route: route.continue_())
            page = await context.new_page()

            seen_types: Dict[str, str] = {}
            body_results: Dict[str, bytes] = {}
            body_errors: List[tuple[str, str]] = []
            body_tasks: List[asyncio.Task[None]] = []

            async def _read_finished(req: Any) -> None:
                path = "/" + req.url.split("/", 3)[-1]
                seen_types[path] = req.resource_type
                resp = await req.response()
                assert resp is not None
                try:
                    body_results[path] = await resp.body()
                except Exception as exc:
                    body_errors.append((path, repr(exc)))

            page.on(
                "requestfinished",
                lambda r: body_tasks.append(asyncio.create_task(_read_finished(r))),
            )

            await page.goto(
                f"http://127.0.0.1:{s_main.server_port}/", wait_until="load"
            )
            frame = next(f for f in page.frames if "/bframe" in f.url)
            await frame.locator("body[data-ready='yes']").wait_for(timeout=30000)
            if body_tasks:
                await asyncio.gather(*body_tasks)

            assert not body_errors, f"Unexpected response.body() errors: {body_errors}"
            assert seen_types["/"] == "document"
            assert seen_types["/bframe"] == "document"
            assert seen_types["/style.css"] == "stylesheet"
            assert seen_types["/tile.png"] == "image"
            assert seen_types["/app.js"] == "script"
            assert seen_types["/api-fetch"] == "fetch"
            assert seen_types["/api-xhr"] == "xhr"
            assert b"<img id='tile'" in body_results["/bframe"]
            assert body_results["/tile.png"] == png_1x1
            assert body_results["/api-fetch"] == b'{"ok":"fetch"}'
            assert body_results["/api-xhr"] == b'{"ok":"xhr"}'
            await context.close()
    finally:
        for s in (s_main, s_frame):
            s.shutdown()
            s.server_close()


def test_chromium_sigkill_launcher_reaps_process_group_for_immediate_rmtree(
    tmp_path: Path,
) -> None:
    import psutil
    import shutil
    import signal
    import subprocess
    import sys

    worker_tmp = tmp_path / "google-browser-worker-sigkill-rmtree-check"
    worker_tmp.mkdir(parents=True, exist_ok=True)
    token = f"sigkill_tok_{os.urandom(8).hex()}"
    child_env = dict(
        os.environ,
        TMPDIR=str(worker_tmp),
        TMP=str(worker_tmp),
        TEMP=str(worker_tmp),
        INVPW_TEST_WORKER_TOKEN=token,
    )
    script = (
        "import asyncio, sys\n"
        "from invisible_playwright.async_api import InvisiblePlaywright\n"
        "async def main():\n"
        "    async with InvisiblePlaywright(backend='chromium', headless=True, seed=42, humanize=False, locale='en-US', timezone='UTC') as b:\n"
        "        p = await b.new_page()\n"
        "        await p.goto('data:text/html,<h1>ready</h1>')\n"
        "        sys.stdout.write('READY\\n')\n"
        "        sys.stdout.flush()\n"
        "        await asyncio.Future()\n"
        "asyncio.run(main())\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline().strip()
        assert line == "READY"

        # Record Chromium descendants before SIGKILL (their /proc/environ is overwritten by setproctitle)
        chrome_identities = []
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmd = " ".join(p.info["cmdline"] or [])
                if str(worker_tmp) in cmd:
                    chrome_identities.append((p.pid, p.create_time()))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        assert chrome_identities

        # Send SIGKILL to worker, reap token-tagged guardians just like worker.py, then immediately rmtree(worker_tmp)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=30)

        tagged = []
        for p in psutil.process_iter(["pid"]):
            try:
                if p.environ().get("INVPW_TEST_WORKER_TOKEN") == token:
                    p.terminate()
                    tagged.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        psutil.wait_procs(tagged, timeout=30)

        # Strict non-ignoring rmtree must succeed with zero ENOTEMPTY
        shutil.rmtree(worker_tmp, ignore_errors=False)
        assert not worker_tmp.exists()

        surviving = []
        for pid, ctime in chrome_identities:
            try:
                p = psutil.Process(pid)
                if p.is_running() and p.status() != psutil.STATUS_ZOMBIE and abs(p.create_time() - ctime) < 1.0:
                    surviving.append(pid)
            except psutil.NoSuchProcess:
                pass
        assert surviving == []
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)


@pytest.mark.parametrize("backend", ["firefox", "chromium"])
@pytest.mark.parametrize("with_route", [False, True])
def test_dual_backend_dedicated_worker_lifecycle_oopif_subrequests_and_failure(
    backend: str, with_route: bool
) -> None:
    asyncio.run(
        _run_dedicated_worker_lifecycle_oopif_subrequests_and_failure(
            backend, with_route
        )
    )


async def _run_dedicated_worker_lifecycle_oopif_subrequests_and_failure(
    backend: str, with_route: bool
) -> None:
    worker_js = (
        b"importScripts('/recaptcha/worker-import.js');"
        b"self.onmessage = async (ev) => {"
        b"  if (ev.data === 'ping') {"
        b"    const sub = await fetch('/recaptcha/worker-sub').then(r => r.text());"
        b"    const nw = new Worker('/recaptcha/nested-worker.js');"
        b"    const nested = await new Promise(res => { nw.onmessage = e => res(e.data); nw.postMessage('hi'); });"
        b"    nw.terminate();"
        b"    self.postMessage({ reply: 'pong:' + self.IMPORTED_TAG + ':' + sub + ':' + nested });"
        b"  }"
        b"};"
    )

    class WorkerHandler(BaseHTTPRequestHandler):
        frame_port = 0

        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:
            if self.path == "/recaptcha/reset-worker.js":
                self.close_connection = True
                return
            if self.path == "/":
                body = (
                    "<!doctype html><html><body>"
                    "<script>"
                    "window.topWorkerReply = null;"
                    "const tw = new Worker('/top-worker.js');"
                    "tw.onmessage = e => { window.topWorkerReply = e.data; document.documentElement.dataset.topDone = 'yes'; };"
                    "tw.postMessage('go');"
                    "</script>"
                    f"<iframe id='anchor' src='http://localhost:{WorkerHandler.frame_port}/recaptcha/anchor'></iframe>"
                    "</body></html>"
                ).encode("utf-8")
                mime = "text/html; charset=utf-8"
            elif self.path == "/top-worker.js":
                body = b"self.onmessage = e => self.postMessage('top:' + e.data);"
                mime = "application/javascript"
            elif self.path == "/recaptcha/anchor":
                body = (
                    "<!doctype html><html><body><div>anchor</div>"
                    "<script>"
                    "window.oopifWorkerResult = null;"
                    "window.deadWorkerFailed = false;"
                    "const w = new Worker('/recaptcha/enterprise/webworker.js');"
                    "w.onmessage = e => {"
                    "  window.oopifWorkerResult = e.data;"
                    "  const bad = new Worker('/recaptcha/reset-worker.js');"
                    "  bad.onerror = () => { window.deadWorkerFailed = true; document.body.dataset.allDone = 'yes'; };"
                    "};"
                    "w.postMessage('ping');"
                    "</script></body></html>"
                ).encode("utf-8")
                mime = "text/html; charset=utf-8"
            elif self.path == "/recaptcha/enterprise/webworker.js":
                body = worker_js
                mime = "application/javascript"
            elif self.path == "/recaptcha/worker-import.js":
                body = b"self.IMPORTED_TAG = 'imp';"
                mime = "application/javascript"
            elif self.path == "/recaptcha/worker-sub":
                body = b"subok"
                mime = "text/plain"
            elif self.path == "/recaptcha/nested-worker.js":
                body = b"self.onmessage = e => self.postMessage('nested:' + e.data);"
                mime = "application/javascript"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    s_main = ThreadingHTTPServer(("127.0.0.1", 0), WorkerHandler)
    s_frame = ThreadingHTTPServer(("127.0.0.1", 0), WorkerHandler)
    WorkerHandler.frame_port = s_frame.server_port
    for s in (s_main, s_frame):
        threading.Thread(target=s.serve_forever, daemon=True).start()

    try:
        async with InvisiblePlaywright(
            backend=backend,
            headless=True,
            seed=42,
            humanize=False,
            locale="en-US",
            timezone="UTC",
        ) as browser:
            context = await browser.new_context()
            if with_route:
                await context.route("**/*", lambda route: route.continue_())
            page = await context.new_page()

            pending: set[Any] = set()
            started_counts: Dict[str, int] = {}
            finished_urls: Dict[str, Any] = {}
            failed_urls: Dict[str, Optional[str]] = {}
            all_settled = asyncio.Event()

            def _check_settled() -> None:
                if (
                    not pending
                    and any("/recaptcha/worker-sub" in u for u in finished_urls)
                    and any("/recaptcha/reset-worker.js" in u for u in failed_urls)
                ):
                    all_settled.set()

            def on_req(req: Any) -> None:
                pending.add(req)
                started_counts[req.url] = started_counts.get(req.url, 0) + 1

            def on_fin(req: Any) -> None:
                pending.discard(req)
                finished_urls[req.url] = req
                _check_settled()

            def on_fail(req: Any) -> None:
                pending.discard(req)
                failed_urls[req.url] = req.failure
                _check_settled()

            page.on("request", on_req)
            page.on("requestfinished", on_fin)
            page.on("requestfailed", on_fail)

            await page.goto(
                f"http://127.0.0.1:{s_main.server_port}/", wait_until="load"
            )
            await page.locator("html[data-top-done='yes']").wait_for(
                state="attached", timeout=30000
            )
            frame = next(f for f in page.frames if "/recaptcha/anchor" in f.url)
            await frame.locator("body[data-all-done='yes']").wait_for(
                state="attached", timeout=30000
            )
            await all_settled.wait()

            assert pending == set(), f"Unsettled pending requests: {[r.url for r in pending]}"
            assert await page.evaluate("window.topWorkerReply") == "top:go"
            assert await frame.evaluate("window.oopifWorkerResult") == {
                "reply": "pong:imp:subok:nested:hi"
            }
            assert await frame.evaluate("window.deadWorkerFailed") is True

            top_w_url = f"http://127.0.0.1:{s_main.server_port}/top-worker.js"
            oopif_w_url = (
                f"http://localhost:{s_frame.server_port}/recaptcha/enterprise/webworker.js"
            )
            sub_url = f"http://localhost:{s_frame.server_port}/recaptcha/worker-sub"
            imp_url = (
                f"http://localhost:{s_frame.server_port}/recaptcha/worker-import.js"
            )
            nested_url = (
                f"http://localhost:{s_frame.server_port}/recaptcha/nested-worker.js"
            )
            reset_url = (
                f"http://localhost:{s_frame.server_port}/recaptcha/reset-worker.js"
            )

            for u in (top_w_url, oopif_w_url, sub_url, imp_url):
                assert started_counts.get(u) == 1, f"Duplicate or missing request for {u}: {started_counts}"
                assert u in finished_urls, f"Missing requestfinished for {u}"
            if backend == "chromium":
                assert started_counts.get(nested_url) == 1
                assert nested_url in finished_urls

            assert started_counts.get(reset_url) == 1
            assert reset_url in failed_urls and failed_urls[reset_url]

            assert finished_urls[oopif_w_url].resource_type == "script"
            assert finished_urls[oopif_w_url].frame == frame
            assert finished_urls[sub_url].resource_type == "fetch"
            assert finished_urls[sub_url].frame == frame

            w_resp = await finished_urls[oopif_w_url].response()
            assert w_resp is not None and await w_resp.body() == worker_js
            sub_resp = await finished_urls[sub_url].response()
            assert sub_resp is not None and await sub_resp.body() == b"subok"

            await context.close()
    finally:
        for s in (s_main, s_frame):
            s.shutdown()
            s.server_close()


@pytest.mark.parametrize("backend", ["firefox", "chromium"])
def test_dual_backend_mid_evaluate_navigation_reports_context_destroyed(
    backend: str,
) -> None:
    asyncio.run(_run_mid_evaluate_navigation_reports_context_destroyed(backend))


async def _run_mid_evaluate_navigation_reports_context_destroyed(
    backend: str,
) -> None:
    class NavRaceHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:
            if self.path.startswith("/start"):
                body = b"<!doctype html><title>start</title><div id='node'>src</div>"
            else:
                body = b"<!doctype html><title>end</title><div id='node'>dst</div>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), NavRaceHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        async with InvisiblePlaywright(
            backend=backend,
            headless=True,
            seed=42,
            humanize=False,
            locale="en-US",
            timezone="UTC",
        ) as browser:
            page = await browser.new_page()
            for idx in range(5):
                await page.goto(
                    f"http://127.0.0.1:{srv.server_port}/start?i={idx}",
                    wait_until="load",
                )
                # Verify normal locator.evaluate(fn, arg) with both node adoption and argument passing
                tag_res = await page.locator("#node").evaluate(
                    "(el, extra) => ({ text: el.textContent, k: extra.k })",
                    {"k": idx},
                )
                assert tag_res == {"text": "src", "k": idx}

                # Trigger navigation between phase 1 (returning function handle) and phase 2 (invoking function with arg)
                try:
                    val = await page.evaluate(
                        "(() => { location.href = '/end'; return ({capture_html}) => ({ url: location.href, html: capture_html ? document.title : null }); })()",
                        {"capture_html": True},
                    )
                    assert val["html"] in ("start", "end")
                except Exception as exc:
                    msg = str(exc)
                    assert "same JavaScript world" not in msg
                    assert "Execution context was destroyed" in msg
                await page.wait_for_url("**/end", timeout=30000)
                await page.wait_for_load_state("load")
                after = await page.evaluate(
                    "({capture_html}) => ({ title: document.title, ok: capture_html })",
                    {"capture_html": True},
                )
                assert after == {"title": "end", "ok": True}
    finally:
        srv.shutdown()
        srv.server_close()


def test_sweep_short_tmps_preserves_live_sibling_pid(tmp_path: Any) -> None:
    import subprocess as sp
    import sys
    from invisible_playwright._cdp.dispatcher import _sweep_short_tmps

    sleeper = sp.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=sp.PIPE,
    )
    try:
        live_dir = tmp_path / f"ipw_t_{sleeper.pid}_alive"
        dead_dir = tmp_path / "ipw_t_999999999_dead"
        live_dir.mkdir()
        dead_dir.mkdir()
        _sweep_short_tmps(str(tmp_path))
        assert live_dir.is_dir(), "live sibling driver temp dir must not be deleted"
        assert not dead_dir.exists(), "dead driver temp dir should be cleaned up"
    finally:
        if sleeper.stdin:
            sleeper.stdin.close()
        sleeper.wait(timeout=30)


@pytest.mark.parametrize("backend", ["firefox", "chromium"])
def test_dual_backend_eval_syntaxerror_no_double_exec_and_worker_dynamic_headers(
    backend: str,
) -> None:
    asyncio.run(
        _run_eval_syntaxerror_no_double_exec_and_worker_dynamic_headers(backend)
    )


async def _run_eval_syntaxerror_no_double_exec_and_worker_dynamic_headers(
    backend: str,
) -> None:
    seen_worker_headers: list[str] = []

    class EvalWorkerHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:
            if self.path == "/page1":
                body = (
                    b"<!doctype html><script>"
                    b"window.w = new Worker('/w.js');"
                    b"window.askWorker = () => new Promise((res, rej) => { window.w.onmessage = e => e.data.ok ? res(e.data.val) : rej(new Error(e.data.err)); window.w.postMessage('ping'); });"
                    b"</script><div>p1</div>"
                )
                mime = "text/html; charset=utf-8"
            elif self.path == "/page2":
                body = b"<!doctype html><div>p2</div>"
                mime = "text/html; charset=utf-8"
            elif self.path == "/w.js":
                body = (
                    b"self.onmessage = async () => {"
                    b"  try {"
                    b"    const t = await fetch('/echo-hdr').then(r => r.text());"
                    b"    self.postMessage({ ok: true, val: t });"
                    b"  } catch (e) {"
                    b"    self.postMessage({ ok: false, err: String(e) });"
                    b"  }"
                    b"};"
                )
                mime = "application/javascript"
            elif self.path == "/echo-hdr":
                val = self.headers.get("X-Dyn-Token") or "missing"
                seen_worker_headers.append(val)
                body = val.encode("utf-8")
                mime = "text/plain; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), EvalWorkerHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    nav_hits = 0
    try:
        async with InvisiblePlaywright(
            backend=backend,
            headless=True,
            seed=42,
            humanize=False,
            locale="en-US",
            timezone="UTC",
        ) as browser:
            context = await browser.new_context(
                extra_http_headers={"X-Dyn-Token": "v1"}
            )
            page = await context.new_page()
            await page.goto(
                f"http://127.0.0.1:{srv.server_port}/page1", wait_until="load"
            )

            # 1. Runtime SyntaxError (JSON.parse) must execute caller side-effects ONCE, never twice
            with pytest.raises(Exception):
                await page.evaluate(
                    "(() => { window.__seCount = (window.__seCount || 0) + 1; JSON.parse('not-json'); })()"
                )
            assert await page.evaluate("window.__seCount") == 1

            # 2. Multi-statement script expression still works cleanly
            assert await page.evaluate("window.__stmtVal = 40; window.__stmtVal + 2") == 42

            # 3. Context extra_http_headers + dynamic set_offline updates already-running Dedicated Workers
            assert await page.evaluate("window.askWorker()") == "v1"
            await context.set_offline(True)
            offline_failed = await page.evaluate(
                "window.askWorker().then(() => false, () => true)"
            )
            assert offline_failed is True
            await context.set_offline(False)
            assert await page.evaluate("window.askWorker()") == "v1"

            # 4. In-flight promise navigation must fail with destroyed context, NEVER re-execute in /page2
            async def _record_hit() -> None:
                nonlocal nav_hits
                nav_hits += 1

            await page.expose_function("__recordEvalEntry", _record_hit)
            with pytest.raises(Exception) as exc_info:
                await page.evaluate(
                    "(() => { window.__recordEvalEntry(); location.href = '/page2'; return new Promise(() => {}); })()"
                )
            assert "Execution context was destroyed" in str(exc_info.value) or "navigation" in str(exc_info.value).lower()
            await page.wait_for_url("**/page2", timeout=30000)
            assert nav_hits == 1, f"expected expression to run once before navigation, ran {nav_hits} times"
            await context.close()
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("backend", ["chromium", "firefox"])
@pytest.mark.parametrize("entry_mode", ["click", "focus"])
def test_keyboard_modifier_shortcuts_and_native_clipboard_paste_parity(
    backend: str, entry_mode: str
) -> None:
    """Regression for Chromium modifier tracking + Linux X11 nativeVirtualKeyCode bug:
    both click() and focus() -> Control+KeyA -> native clipboard -> Control+KeyV -> Enter
    must select all, fire trusted paste/beforeinput/input with ctrlKey=True, and submit
    the exact replacement query without swallowing KeyV as X11 KP_Add (Ctrl++ zoom)."""
    asyncio.run(_run_keyboard_modifier_and_paste_check(backend, entry_mode))


async def _run_keyboard_modifier_and_paste_check(
    backend: str, entry_mode: str
) -> None:
    from urllib.parse import parse_qs
    import shutil

    query = '\u4e2d\u6587 "quotes" \'single\' & + = ? # % \\ / \U0001f600'
    received: List[Dict[str, Any]] = []
    html = (
        b'<!doctype html><meta charset="utf-8">'
        b'<form method="post" action="/submit">'
        b'<input id="q" name="q" value="replace this old query">'
        b'<input id="src" value="">'
        b'<input id="events" name="events" type="hidden">'
        b'<button>Search</button></form><script>'
        b"const events=[];"
        b"for(const name of ['click','keydown','keyup','paste','beforeinput','input','change','submit'])"
        b" document.addEventListener(name,e=>{events.push({"
        b"  type:e.type,isTrusted:e.isTrusted,key:e.key||null,code:e.code||null,"
        b"  location:typeof e.location==='number'?e.location:null,"
        b"  ctrlKey:!!e.ctrlKey,shiftKey:!!e.shiftKey,altKey:!!e.altKey,metaKey:!!e.metaKey"
        b" });"
        b" if(e.type==='submit')document.querySelector('#events').value=JSON.stringify(events)},true);"
        b"</script>"
    )

    class FormHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def _reply(self, body: bytes, ct: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._reply(html, "text/html; charset=utf-8")

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8")
            fields = parse_qs(raw)
            record = {
                "query": fields["q"][0],
                "events": json.loads(fields["events"][0]),
            }
            received.append(record)
            self._reply(json.dumps(record, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), FormHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    xclip_proc: Optional[asyncio.subprocess.Process] = None
    try:
        launcher = InvisiblePlaywright(
            backend=backend,
            headless=True,
            seed=42,
            humanize=True,
            locale="en-US",
            timezone="UTC",
        )
        async with launcher as browser:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(
                f"http://127.0.0.1:{srv.server_port}/", wait_until="load"
            )

            vdisplay = getattr(launcher, "_virtual_display", None)
            use_xclip = bool(
                vdisplay is not None
                and os.name != "nt"
                and shutil.which("xclip")
            )
            if use_xclip:
                env = os.environ.copy()
                for k, v in vdisplay.launch_env().items():
                    if v is None:
                        env.pop(k, None)
                    else:
                        env[k] = v
                xclip_proc = await asyncio.create_subprocess_exec(
                    "xclip",
                    "-quiet",
                    "-in",
                    "-selection",
                    "clipboard",
                    "-target",
                    "UTF8_STRING",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                assert xclip_proc.stdin is not None
                xclip_proc.stdin.write(query.encode("utf-8"))
                await xclip_proc.stdin.drain()
                xclip_proc.stdin.close()
                await xclip_proc.stdin.wait_closed()
                # Confirm X11 selection ownership is live before pasting.
                probe = await asyncio.create_subprocess_exec(
                    "xclip",
                    "-out",
                    "-selection",
                    "clipboard",
                    "-target",
                    "UTF8_STRING",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=env,
                )
                out_bytes, _ = await probe.communicate()
                assert out_bytes.decode("utf-8") == query
            else:
                # Cross-platform fallback when xclip is absent: populate OS clipboard via Ctrl+C.
                await page.locator("#src").evaluate("(el, val) => { el.value = val; }", query)
                await page.locator("#src").focus()
                await page.keyboard.press("Control+KeyA", delay=0)
                await page.keyboard.press("Control+KeyC", delay=0)

            if entry_mode == "click":
                await page.locator("#q").click()
            else:
                await page.locator("#q").focus()

            await page.keyboard.press("Control+KeyA", delay=0)
            after_sel = await page.locator("#q").evaluate(
                "el => ({start: el.selectionStart, end: el.selectionEnd, active: document.activeElement === el})"
            )
            assert after_sel == {"start": 0, "end": 22, "active": True}

            await page.keyboard.press("Control+KeyV", delay=0)
            after_paste = await page.locator("#q").evaluate("el => el.value")
            assert after_paste == query

            async with page.expect_navigation(wait_until="load"):
                await page.keyboard.press("NumpadEnter", delay=0)

            assert len(received) == 1
            rec = received[0]
            assert rec["query"] == query
            evs = rec["events"]
            assert all(e["isTrusted"] for e in evs)
            ev_types = {e["type"] for e in evs}
            assert {"keydown", "keyup", "paste", "beforeinput", "input", "submit"} <= ev_types

            keydowns_a = [e for e in evs if e["type"] == "keydown" and e["code"] == "KeyA"]
            keydowns_v = [e for e in evs if e["type"] == "keydown" and e["code"] == "KeyV"]
            numpad_enters = [e for e in evs if e["type"] == "keydown" and e["code"] == "NumpadEnter"]
            assert keydowns_a and all(e["ctrlKey"] is True for e in keydowns_a)
            assert keydowns_v and all(e["ctrlKey"] is True for e in keydowns_v)
            assert numpad_enters and all(e["location"] == 3 for e in numpad_enters)

            await context.close()
    finally:
        if xclip_proc is not None and xclip_proc.returncode is None:
            xclip_proc.kill()
            await xclip_proc.wait()
        srv.shutdown()
        srv.server_close()
