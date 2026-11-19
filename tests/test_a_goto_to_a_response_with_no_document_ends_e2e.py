"""A goto to a response that carries no document (204 No Content, 205 Reset Content) ENDS, with the error the engine says.

Firefox does not replace the page for such a response: the load of the new document stops where its request ends, and the
page stays where it was. In the network of the parent the request does not fail (it is answered), so the end of that
navigation is said by the process that held the frame, and nothing else says it. A goto whose end nobody says hangs until
its own timeout: that is the defect this file is red on, and the timeout here (30 s) is only its detector.

Known-bad input: an engine whose content process stays silent on the abort of an http load (the frame tree that leaves
every http end to the parent) -> both goto here raise a TimeoutError instead of the engine's error.
"""
from __future__ import annotations

import http.server
import threading

import pytest

pytestmark = pytest.mark.e2e


def _serve():
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/no-content"):
                self.send_response(204)
                self.end_headers()
                return
            if self.path.startswith("/reset-content"):
                self.send_response(205)
                self.send_header("content-length", "0")
                self.end_headers()
                return
            body = b"<!doctype html><title>start</title><p>start"
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/" % srv.server_address[1]


@pytest.fixture
def served():
    srv, url = _serve()
    try:
        yield url
    finally:
        srv.shutdown()


@pytest.mark.parametrize("path", ["no-content", "reset-content"])
def test_a_goto_to_a_response_with_no_document_ends_with_the_engines_error(firefox_binary, served, path):
    from invisible_playwright import InvisiblePlaywright
    from invisible_playwright.sync_api import Error

    with InvisiblePlaywright(seed=4243, binary_path=firefox_binary, headless=True) as browser:
        page = browser.new_page()
        page.goto(served + "start", wait_until="load", timeout=30_000)
        with pytest.raises(Error) as nav:
            page.goto(served + path, timeout=30_000)
        message = nav.value.message
        # it ENDED: an error the engine said, not the goto's own timeout running out
        assert not any(w in message for w in ("Timeout", "exceeded", "not reached")), message
        assert page.url == served + "start", "a response with no document replaced the page: %s" % page.url
