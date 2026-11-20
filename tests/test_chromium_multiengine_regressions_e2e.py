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
