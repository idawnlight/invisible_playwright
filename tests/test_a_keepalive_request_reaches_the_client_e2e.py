"""A `fetch(..., {keepalive: true})` is a request of the page: the client hears `request` and its end.

Stock Playwright's Firefox reports the keepalive fetch of a page like any other request: `page.on("request")` fires, and
`requestfinished` follows. Here the server saw the request and the client heard nothing, so a client that follows the page's
network by those events (tf-fetch's network recorder does) never knew the request existed.

The page asks for the same address four ways. `plain` is the control (a fetch without keepalive), `keepalive` and
`beacon` are the two that the engine runs in the parent process, and each must give exactly one `request` and one end.

The test waits for what the product promises, not for a time: until every request that began has its end (and the four
have begun), with a deadline of 15 s that no healthy run comes near. A product that loses an end stays red, because the
deadline passes; a slow machine only takes longer. The message gives every event with the time it was heard, from the
goto, so a red run says whether the end came late or never.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace

import pytest

from _servers import LocalServer
from _waits import Timeline, wait_until, without_end

pytestmark = pytest.mark.e2e

EXPECTED = ["/seen/beacon", "/seen/keepalive", "/seen/keepalive-nocors", "/seen/plain"]

#: Far beyond what a healthy run needs (a few hundred ms); it is the only clock the verdict has.
DEADLINE = 15.0

PAGE = b"""<!doctype html><title>keepalive</title><script>
addEventListener('load', () => {
  fetch('/seen/plain', {method: 'POST', body: 'x'});
  fetch('/seen/keepalive', {method: 'POST', body: 'x', keepalive: true});
  fetch('/seen/keepalive-nocors', {keepalive: true, mode: 'no-cors'});
  navigator.sendBeacon('/seen/beacon', 'x');
});
</script>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _answer(self, body, ctype="text/plain"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _heard(self):
        self.server.timeline.add("server", "%s %s" % (self.command, self.path))

    def do_GET(self):
        self._heard()
        if self.path == "/":
            self._answer(PAGE, "text/html; charset=utf-8")
        else:
            self._answer(b"ok")

    def do_POST(self):
        self._heard()
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._answer(b"ok")


@pytest.fixture
def site():
    srv = LocalServer(("127.0.0.1", 0), _Handler)
    srv.timeline = Timeline()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield SimpleNamespace(url="http://127.0.0.1:%d" % srv.server_address[1], timeline=srv.timeline)
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("route", [False, True])
def test_every_request_of_the_page_reaches_the_client_with_its_end(firefox_binary, site, route):
    from invisible_playwright import InvisiblePlaywright

    timeline = site.timeline

    def short(url):
        return url.replace(site.url, "")

    with InvisiblePlaywright(seed=4261, binary_path=firefox_binary, headless=True) as browser:
        context = browser.new_context()
        if route:
            def handler(r):
                timeline.add("route", short(r.request.url))
                r.continue_()
            context.route("**/*", handler)
        page = context.new_page()
        page.on("request", lambda r: timeline.add("request", short(r.url)))
        page.on("requestfinished", lambda r: timeline.add("finished", short(r.url)))
        page.on("requestfailed", lambda r: timeline.add("failed", short(r.url)))

        def of_the_page(kinds):
            return [u for u in timeline.of(*kinds) if u.startswith("/seen/")]

        timeline.begin()
        page.goto(site.url + "/", wait_until="load")
        # The client is a sync one: events are delivered while it calls the browser, so the wait is a call.
        # What is awaited: the four requests have begun, and every request that began has its end.
        wait_until(lambda: sorted(set(of_the_page(["request"]))) == EXPECTED
                   and not without_end(of_the_page(["request"]), of_the_page(["finished", "failed"])),
                   DEADLINE, lambda: page.wait_for_timeout(50))
    started, ended = of_the_page(["request"]), of_the_page(["finished", "failed"])
    events = "\nevents, from the goto:\n" + timeline.render()
    assert sorted(started) == EXPECTED, "the client heard these requests: %r (after %.0f s)%s" % (
        started, DEADLINE, events)
    assert sorted(ended) == sorted(started), "requests without an end for the client: %r%s" % (
        without_end(started, ended), events)
