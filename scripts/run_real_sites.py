#!/usr/bin/env python3
"""One pass over the list of real sites with ONE browser, and the cells it gave, in a JSON file. No judgement is made here (scripts/judge_real_sites.py does it).

    run_real_sites.py --browser stock --out cells/stock-1.json     # stock Firefox driven by stock Playwright
    run_real_sites.py --browser ours  --out cells/ours-1.json      # the published wrapper (invisible_playwright)

One browser, one context and one page are reused for the whole list: it is how a client uses the package. For every site of ``tests/realsites/sites.json``:

 1. ``goto`` with the wait ``load``;
 2. the wait ``networkidle``;
 3. the marker (a CSS selector that matches only when the real page arrived), and the HTTP status of the document;
 4. the canary: a quiet page served by this script on 127.0.0.1, visited on the same page, which must reach ``networkidle`` (a state that survives the
    navigation, a request that never ends, a route left behind, makes it fail).

The cell of a site:

    load         "ok" | "timeout" | "error:<type>"     the ``goto`` (the type is the engine's NS_ERROR_* name when it gives one, else the exception class)
    networkidle  "ok" | "timeout"                      the wait for ``networkidle`` after the load
    status       "2xx" | "3xx" | "4xx" | "5xx" | "none"  the last response of the main document (a redirect chain is read to its end)
    marker       true | false
    canary       "ok" | "no"

No time is measured or asserted: the waits are Playwright's own, with the same limits for both browsers (``--load-timeout``, ``--idle-timeout``). Both browsers
are launched with a window, at the same viewport, with no proxy and with the language of the runner (no locale is passed to either).

No click and no handling of a consent wall: both browsers see what the page shows. ``--diagnose`` (the admission of a list, stock alone) adds to the cell, where
the marker was not found, a ``diagnosis`` of the document: its HTTP status, its title and the first 40 element ids, to fix the marker. It is a note for the person
who reads the file: the judge ignores it.

``--binary`` (the engine to drive) and ``--core-ref`` (the core the engine was built with) are accepted for the round of a candidate; ``--binary`` is passed to
the launch when given and ``--core-ref`` is only written into the file.
"""
from __future__ import annotations

import argparse
import http.server
import json
import platform
import re
import sys
import threading
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SITES = ROOT / "tests" / "realsites" / "sites.json"
CANARY_PAGE = b"<!doctype html><html><head><meta charset=utf-8><title>canary</title></head><body><p id=canary>quiet</p></body></html>"
ENGINE_ERROR = re.compile(r"\b(NS_[A-Z0-9_]+)\b")


class _Canary(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (the name the server calls)
        if self.path.split("?", 1)[0] == "/canary":
            body, status, kind = CANARY_PAGE, 200, "text/html; charset=utf-8"
        else:
            body, status, kind = b"", 204, "text/plain"
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # quiet
        pass


def serve_canary():
    """The quiet page, on a free port of the loopback: returns the server (to shut down) and the URL of the page."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Canary)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d/canary" % server.server_address[1]


def status_class(code) -> str:
    return "none" if not isinstance(code, int) or not 200 <= code <= 599 else "%dxx" % (code // 100)


def load_error(exc: BaseException) -> str:
    """``error:<type>``: the engine's own name for the failure when the message carries one, else the exception class."""
    found = ENGINE_ERROR.search(str(exc))
    return "error:" + (found.group(1) if found else type(exc).__name__)


def read_sites(path, only=None):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    sites = data.get("sites", [])
    if only:
        wanted = [s for s in only.split(",") if s]
        known = {s["id"] for s in sites}
        unknown = [s for s in wanted if s not in known]
        if unknown:
            raise SystemExit("not in the list: " + ", ".join(unknown))
        sites = [s for s in sites if s["id"] in wanted]
    return sites


def visit(page, state, site, canary_url, timeouts, errors, diagnose=False):
    """One visit of one site on the reused page, then the canary. Returns the cell."""
    PWError, PWTimeout = errors
    load_ms, idle_ms = timeouts
    state["status"] = None
    cell = {"load": "ok", "networkidle": "ok", "status": "none", "marker": False, "canary": "no"}
    response = None
    try:
        response = page.goto(site["url"], wait_until="load", timeout=load_ms)
    except PWTimeout:
        cell["load"] = "timeout"
    except PWError as exc:
        cell["load"] = load_error(exc)
    except Exception as exc:  # a driver that fails is a cell, not the end of the pass
        cell["load"] = load_error(exc)
    try:
        page.wait_for_load_state("networkidle", timeout=idle_ms)
    except Exception:
        cell["networkidle"] = "timeout"
    try:
        cell["marker"] = page.locator(site["marker"]).count() > 0
    except Exception:
        cell["marker"] = False
    code = response.status if response is not None else state["status"]
    cell["status"] = status_class(code)
    if diagnose and not cell["marker"]:
        cell["diagnosis"] = diagnosis(page, code)
    cell["canary"] = canary(page, canary_url, timeouts, errors)
    return cell


def diagnosis(page, code) -> dict:
    """What the document is, for fixing a marker: its HTTP status, its title and the first 40 ids of its elements. Read, never judged."""
    out = {"http_status": code, "title": None, "ids": []}
    try:
        out["title"] = page.title()
    except Exception:
        pass
    try:
        ids = page.evaluate("() => Array.from(document.querySelectorAll('[id]')).map(e => e.id).filter(i => i).slice(0, 40)")
        out["ids"] = [i for i in ids if isinstance(i, str) and i][:40]
    except Exception:
        pass
    return out


def canary(page, canary_url, timeouts, errors):
    load_ms, idle_ms = timeouts
    try:
        page.goto(canary_url, wait_until="load", timeout=load_ms)
        page.wait_for_load_state("networkidle", timeout=idle_ms)
        return "ok" if page.locator("#canary").count() == 1 else "no"
    except Exception:
        return "no"


def run_pass(browser, sites, canary_url, timeouts, errors, viewport, cells, diagnose=False):
    context = browser.new_context(viewport=viewport)
    page = context.new_page()
    state = {"status": None}

    def on_response(response):
        try:
            if response.request.is_navigation_request() and response.request.frame == page.main_frame:
                state["status"] = response.status
        except Exception:
            pass

    page.on("response", on_response)
    try:
        for site in sites:
            cells[site["id"]] = visit(page, state, site, canary_url, timeouts, errors, diagnose)
            print("%s %s" % (site["id"], json.dumps(cells[site["id"]], sort_keys=True)), flush=True)
    finally:
        context.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--browser", choices=("stock", "ours"), required=True)
    parser.add_argument("--out", required=True, help="the JSON file of the cells")
    parser.add_argument("--sites", default=str(DEFAULT_SITES))
    parser.add_argument("--only", help="comma-separated site ids: visit these sites alone (the later passes of the cells that differ)")
    parser.add_argument("--binary", help="the engine (ours) or Firefox (stock) to drive, for the round of a candidate")
    parser.add_argument("--core-ref", help="the core the engine was built with; only written into the file")
    parser.add_argument("--load-timeout", type=int, default=30000, help="ms, the limit of goto with the wait load")
    parser.add_argument("--idle-timeout", type=int, default=15000, help="ms, the limit of the wait for networkidle")
    parser.add_argument("--diagnose", action="store_true", help="where the marker is not found, add the status, the title and the first 40 ids of the document")
    parser.add_argument("--viewport", default="1280x720")
    args = parser.parse_args(argv)

    width, height = (int(n) for n in args.viewport.lower().split("x"))
    viewport = {"width": width, "height": height}
    sites = read_sites(args.sites, args.only)
    timeouts = (args.load_timeout, args.idle_timeout)

    # Each mode imports its own package and no other: the venv of ours holds invisible-playwright (its client is vendored, there is no `playwright` in it), the venv of
    # stock holds playwright. The errors of ours are the ones its client raises: invisible_playwright.sync_api exports the classes of
    # invisible_playwright/_pw/_impl/_errors.py, which the waiter of a navigation raises on a timeout (_pw/_impl/_waiter.py reject_on_timeout) and which the answer of
    # the server is turned into by parse_error (_pw/_impl/_helper.py), so an `except TimeoutError` here catches what goto and wait_for_load_state raise.
    server, canary_url = serve_canary()
    meta = {"browser": args.browser, "os": platform.system().lower(), "python": platform.python_version(),
            "binary": args.binary, "core_ref": args.core_ref, "viewport": args.viewport, "load_timeout_ms": args.load_timeout,
            "idle_timeout_ms": args.idle_timeout, "only": args.only}
    cells, complete = {}, False  # filled site by site: a pass that dies keeps what it had, marked incomplete
    try:
        if args.browser == "stock":
            from playwright.sync_api import Error as PWError, TimeoutError as PWTimeout, sync_playwright
            errors = (PWError, PWTimeout)
            meta["playwright"] = metadata.version("playwright")
            with sync_playwright() as p:
                browser = p.firefox.launch(headless=False, **({"executable_path": args.binary} if args.binary else {}))
                try:
                    meta["engine"] = browser.version
                    run_pass(browser, sites, canary_url, timeouts, errors, viewport, cells, args.diagnose)
                finally:
                    browser.close()
        else:
            from invisible_playwright.sync_api import Error as PWError, InvisiblePlaywright, TimeoutError as PWTimeout
            errors = (PWError, PWTimeout)
            meta["package"] = metadata.version("invisible-playwright")
            with InvisiblePlaywright(**({"binary_path": args.binary} if args.binary else {})) as browser:
                meta["engine"] = browser.version
                run_pass(browser, sites, canary_url, timeouts, errors, viewport, cells, args.diagnose)
        complete = True
    finally:
        server.shutdown()
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({**meta, "complete": complete, "order": [s["id"] for s in sites], "cells": cells}, indent=2) + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
