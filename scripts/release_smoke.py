"""Download the selected engine and exercise the installed public API."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

from invisible_playwright import InvisiblePlaywright


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"""<!doctype html><title>Release installation</title>
<input id="value"><button onclick="document.querySelector('#result').textContent=document.querySelector('#value').value">Submit</button>
<output id="result"></output>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with InvisiblePlaywright(headless=True, seed=42, locale="en-US", timezone="UTC") as browser:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            assert page.title() == "Release installation"
            assert page.evaluate("6 * 7") == 42
            page.locator("#value").fill("installed from GitHub")
            page.get_by_role("button", name="Submit").click()
            assert page.locator("#result").inner_text() == "installed from GitHub"
            print(json.dumps({"status": "passed", "browser": browser.version,
                              "checks": ["HTTP", "title", "evaluate", "fill", "click"]}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
