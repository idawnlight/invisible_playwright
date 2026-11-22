"""A popup is reported on Page.ready, as Playwright's own is: with no limit, and with the destination's address already.

Measured against stock Playwright 1.62 and its Firefox (in-process, 2 runs of each, never in disagreement):
  * a destination that answers 204, or never answers: NO `page` event in 30 s (nothing commits, the engine never says Page.ready);
  * a destination that answers after 4 s: the event at 4.08 s, with `url` already the destination and `load` in the same millisecond.
Here: the same two, in-process and through a served browser driven by a stock client, and a destination that answers after 25 s (the
wrapper dropped a popup that was not ready in 20 s, with a warning); the address read at the `page` event is the destination.

Red on b09d620: the popup of the slow destination reads `about:blank` at the event (the commit comes in the same turn as the ready and
was applied after the page was announced), and the one that answers after 25 s was dropped at 20 s.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace

import pytest

from _evidence_on_failure import WIRE_LIMIT, FailureEvidence
from _servers import LocalServer

pytestmark = pytest.mark.e2e


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        # The destination's own record, for a test that saves evidence when it fails (`server.records`): it only listens.
        rec = {"ev": "request", "t_arrive": round(time.time(), 4), "m_arrive": round(time.perf_counter(), 6), "peer": self.client_address[1],
               "path": self.path, "status": None, "m_slept": None, "m_answer": None, "m_done": None, "error": None}
        records = getattr(self.server, "records", None)
        if records is not None:
            with self.server.records_lock:
                records.append(rec)
        try:
            self._get(rec)
        finally:
            rec["m_done"] = round(time.perf_counter(), 6)

    def _get(self, rec):
        path, _, query = self.path.partition("?")
        if path == "/nocontent":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            rec["status"] = 204
            return
        if path == "/slow":
            ms = int(query.split("ms=")[1].split("&")[0]) if "ms=" in query else 0
            time.sleep(ms / 1000.0)
            rec["m_slept"] = round(time.perf_counter(), 6)
            body = b"<!doctype html><html><head><title>slow</title></head><body>slow</body></html>"
        else:
            body = (b"<!doctype html><html><head><title>opener</title></head><body>"
                    b"<button id=open204 onclick=\"window.open('/nocontent','_blank')\">open</button>"
                    b"<a id=link204 href=/nocontent target=_blank>link</a>"
                    b"<button id=open1500 onclick=\"window.open('/slow?ms=1500','_blank')\">open</button>"
                    b"<a id=link1500 href='/slow?ms=1500' target=_blank>link</a>"
                    b"<button id=open25000 onclick=\"window.open('/slow?ms=25000','_blank')\">open</button>"
                    b"</body></html>")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            rec["status"] = 200
            rec["m_answer"] = round(time.perf_counter(), 6)
        except OSError as exc:       # the browser left before a slow answer
            rec["error"] = type(exc).__name__


@pytest.fixture
def site_server():
    srv = LocalServer(("127.0.0.1", 0), _Handler)
    srv.records, srv.records_lock = [], threading.Lock()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def site(site_server):
    return "http://127.0.0.1:%d/" % site_server.server_address[1]


def _stock_client(module):
    import importlib

    try:
        return importlib.import_module(module)
    except ImportError as exc:       # pragma: no cover - only in a broken environment
        pytest.fail("the stock Playwright client is missing (%s): install the wrapper with its dev extra" % exc, pytrace=False)


def _drive(mode, firefox_binary, body):
    """Run body(context, page) in-process or through a served browser driven by a stock client."""
    if mode == "in-process":
        from invisible_playwright import InvisiblePlaywright

        with InvisiblePlaywright(seed=42, binary_path=firefox_binary, headless=True, humanize=False) as b:
            ctx = b.new_context()
            body(ctx, ctx.new_page())
            ctx.close()
        return
    from invisible_playwright._juggler.remote import launch_server
    sync_playwright = _stock_client("playwright.sync_api").sync_playwright

    server = launch_server(headless=True, binary_path=firefox_binary, locale="en-US", timezone="Europe/Rome")
    try:
        with sync_playwright() as p:
            browser = p.firefox.connect(server.ws_endpoint)
            ctx = browser.new_context()
            body(ctx, ctx.new_page())
            ctx.close()
            browser.close()
    finally:
        server.close()


MODES = ["in-process", "served"]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("opener", ["#open204", "#link204"])
def test_a_destination_that_commits_nothing_gives_no_popup_event(firefox_binary, site, mode, opener):
    def body(ctx, page):
        page.goto(site, wait_until="load", timeout=30000)
        seen = []
        ctx.on("page", lambda p: seen.append(p))
        page.click(opener)
        time.sleep(6)
        assert not seen, "a popup whose destination commits nothing was reported (Playwright's is not)"
        assert len(ctx.pages) == 1

    _drive(mode, firefox_binary, body)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("opener", ["#open1500", "#link1500"])
def test_a_slow_popup_is_reported_with_the_address_of_its_destination(firefox_binary, site, mode, opener):
    def body(ctx, page):
        page.goto(site, wait_until="load", timeout=30000)
        with page.expect_popup(timeout=30000) as info:
            page.click(opener)
        assert info.value.url == site + "slow?ms=1500", "the popup was reported with %r" % info.value.url
        info.value.close()

    _drive(mode, firefox_binary, body)


class _ServerSideLog:
    """What OUR server says to the client and how it decides to, recorded from the test, with nothing changed in `src`: the methods are
    wrapped at their class and put back at the end. `messages`: every protocol message the server hands to the client (a served browser:
    `RemoteClient.post`, the text sent on the WebSocket; in-process: `InProcessTransport.emit_message`, the dict). `steps`: the adoption of
    each engine target (`_Adoption`: attached, every note it makes, with the engine's own event names) and the two calls that decide when
    the client hears of a page (`PageDispatcher.wait_until_ready`, `PageDispatcher.announce`). Both carry `t` and `m` like the engine's wire."""

    LIMIT = 20000

    def __init__(self):
        self.messages, self.steps, self.problems = [], [], []
        self._lock = threading.Lock()
        self._patch = pytest.MonkeyPatch()

    def _add(self, where, record):
        try:
            record["t"], record["m"] = round(time.time(), 4), round(time.perf_counter(), 6)
            with self._lock:
                if len(where) < self.LIMIT:
                    where.append(record)
        except Exception:
            pass

    def _sent(self, via, message):
        try:
            text = message if isinstance(message, str) else json.dumps(message, default=str)
            parsed = json.loads(text) if isinstance(message, str) else message
            params = parsed.get("params") or {}
            self._add(self.messages, {"via": via, "len": len(text), "cut": len(text) > WIRE_LIMIT, "guid": parsed.get("guid"),
                                      "method": parsed.get("method"), "id": parsed.get("id"),
                                      "create_type": params.get("type") if parsed.get("method") == "__create__" else None,
                                      "msg": text[:WIRE_LIMIT]})
        except Exception:
            pass

    def _step(self, event, /, **more):
        # `event` is positional-only: a wrapped call records a field called `what` (the adoption's note) and must not collide with it.
        record = {"ev": event}
        record.update(more)
        self._add(self.steps, record)

    def _record(self, make):
        """Run `make`, which records something about a call the server makes; a failure of the recording is noted in the evidence and
        never reaches the server."""
        try:
            make()
        except BaseException as exc:
            with self._lock:
                if len(self.problems) < 20:
                    self.problems.append("recording failed: %r" % (exc,))

    def install(self):
        log = self

        def wrap(owner, name, make):
            try:
                self._patch.setattr(owner, name, make(getattr(owner, name)))
            except Exception as exc:        # the rest is still recorded; the problem is saved with it
                self.problems.append("%s.%s not wrapped: %r" % (getattr(owner, "__name__", owner), name, exc))

        try:
            from invisible_playwright._juggler import remote, server, transport
        except Exception as exc:
            self.problems.append("the server modules could not be imported: %r" % (exc,))
            return

        # Every wrapper takes (*args, **kwargs) and passes them on unchanged: whatever the real code passes, positional or keyword, reaches
        # the wrapped method as it was. What a wrapper reads from the call is read inside `_record`, which never raises.
        def post(original):
            def wrapper(*args, **kwargs):
                log._record(lambda: log._sent("RemoteClient.post", args[1] if len(args) > 1 else kwargs.get("text")))
                return original(*args, **kwargs)
            return wrapper

        def emit_message(original):
            def wrapper(*args, **kwargs):
                log._record(lambda: log._sent("InProcessTransport.emit_message", args[1] if len(args) > 1 else kwargs.get("message")))
                return original(*args, **kwargs)
            return wrapper

        def adoption_init(original):
            def wrapper(*args, **kwargs):
                result = original(*args, **kwargs)
                adoption = args[0]
                log._record(lambda: log._step("adoption_attached", target=adoption.target_id, opener=adoption.opener is not None,
                                              context=adoption.context_id))
                return result
            return wrapper

        def adoption_note(original):
            def wrapper(*args, **kwargs):
                adoption = args[0]
                log._record(lambda: log._step("adoption_note", target=adoption.target_id, state=adoption.state,
                                              what=args[1] if len(args) > 1 else kwargs.get("what")))
                return original(*args, **kwargs)
            return wrapper

        def wait_until_ready(original):
            def wrapper(*args, **kwargs):
                page = args[0]
                log._record(lambda: log._step("wait_until_ready_begin", page=page.guid))
                try:
                    result = original(*args, **kwargs)
                except BaseException as exc:
                    error = repr(exc)[:300]
                    log._record(lambda: log._step("wait_until_ready_failed", page=page.guid, error=error))
                    raise
                log._record(lambda: log._step("wait_until_ready_end", page=page.guid))
                return result
            return wrapper

        def announce(original):
            def wrapper(*args, **kwargs):
                page = args[0]
                log._record(lambda: log._step("announce_begin", page=page.guid))
                try:
                    return original(*args, **kwargs)
                finally:
                    log._record(lambda: log._step("announce_end", page=page.guid))
            return wrapper

        wrap(remote.RemoteClient, "post", post)
        wrap(transport.InProcessTransport, "emit_message", emit_message)
        wrap(server._Adoption, "__init__", adoption_init)
        wrap(server._Adoption, "note", adoption_note)
        wrap(server.PageDispatcher, "wait_until_ready", wait_until_ready)
        wrap(server.PageDispatcher, "announce", announce)

    def uninstall(self):
        self._patch.undo()


#: What each extra file of the evidence of the test below holds, field by field (written into its meta.json).
_EXTRA_FILES = {
    "server-to-client.jsonl": "every protocol message OUR server handed to the client, in order: t (time.time()), m (time.perf_counter(), the scale "
                              "of every other file), via (RemoteClient.post for a served browser, InProcessTransport.emit_message in-process), "
                              "guid and method (the event or the __create__ it carries; id for a reply), create_type (the type a __create__ makes: "
                              "Page, Frame, ...), len, cut (msg was cut at %d characters), msg (the message as JSON text)" % WIRE_LIMIT,
    "server-adoption-steps.jsonl": "what the server did with each engine target and when it told the client: t, m, ev = adoption_attached "
                                   "(target, opener, context), adoption_note (target, state, what = the engine's event names Page.frameAttached/"
                                   "navigationCommitted/ready/... without the 'Page.', and the adoption's own steps building/waiting/announced), "
                                   "wait_until_ready_begin / _end / _failed (page = the Page guid), announce_begin / announce_end (page = the Page "
                                   "guid; the `page` event is sent between them), and `problems` lines if a wrapper could not be installed",
    "client-events.jsonl": "what the stock client heard, in order: t, m, ev = click, page_event (url = popup.url read in the handler of the context's "
                           "page event), then for that popup framenavigated (url, main = the main frame), domcontentloaded, load, close (url), and "
                           "expect_popup_returned (url, took_s, the time since the click)",
}


def _save_extra(folder, files, problems):
    """Add the files of the server's side to the evidence folder `FailureEvidence` saved, and say in its meta.json what each holds."""
    for name, records in files.items():
        with open(folder / name, "w", encoding="utf-8") as out:
            for record in records:
                out.write(json.dumps(record, default=str) + "\n")
    meta_path = folder / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["files"] = sorted(set(meta.get("files", [])) | set(files))
    meta["extra_files"] = _EXTRA_FILES
    meta["extra_problems"] = problems
    meta["note"] = ("client-request-log.jsonl is empty here (the destination has one request; the client's own events are client-events.jsonl); "
                    "fakeweb-server-record.jsonl is the destination's record: t_arrive/m_arrive when the request came, m_slept when its wait "
                    "ended, m_answer when the answer was written, status, error")
    meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")


@pytest.mark.parametrize("mode", MODES)
def test_a_popup_whose_destination_answers_after_25_seconds_is_reported_then(firefox_binary, site, site_server, mode):
    """When it fails it saves, under `invpw-evidence/popup-25s-<mode>/`, the order in which the engine and then our server told the popup's
    story: the engine's Juggler wire and trace (`juggler-wire.jsonl`, `juggler-engine-trace/`), what our server sent to the client
    (`server-to-client.jsonl`), what it did with the target (`server-adoption-steps.jsonl`), what the client heard (`client-events.jsonl`),
    the destination's record (`fakeweb-server-record.jsonl`) and a summary (`cells.json`)."""
    client_events = []
    summary = {"mode": mode}

    def mark(ev, **more):
        client_events.append(dict({"ev": ev, "t": round(time.time(), 4), "m": round(time.perf_counter(), 6)}, **more))

    def watch_popups(ctx):
        def on_page(popup):
            mark("page_event", url=popup.url)
            summary["url_at_page_event"] = popup.url
            popup.on("framenavigated", lambda frame: mark("framenavigated", url=frame.url, main=frame.parent_frame is None))
            popup.on("domcontentloaded", lambda p: mark("domcontentloaded", url=p.url))
            popup.on("load", lambda p: mark("load", url=p.url))
            popup.on("close", lambda p: mark("close", url=p.url))

        ctx.on("page", on_page)

    def body(ctx, page):
        page.goto(site, wait_until="load", timeout=30000)
        watch_popups(ctx)
        t0 = time.monotonic()
        mark("click")
        with page.expect_popup(timeout=45000) as info:
            page.click("#open25000")
        took = time.monotonic() - t0
        mark("expect_popup_returned", url=info.value.url, took_s=round(took, 3))
        summary["took_s"] = round(took, 3)
        # The lower bound is the server's: it answers after 25 s, so a popup announced before 24 s was announced
        # before it was ready. There is no upper bound of ours: the limit above is expect_popup's 45 s (a popup
        # dropped at 20 s, the defect this test is red on, ends in its timeout).
        assert took > 24.0, "reported after %.1f s, before the destination had answered" % took
        assert info.value.url == site + "slow?ms=25000"
        info.value.close()

    evidence = FailureEvidence("popup-25s-" + mode)
    evidence.watch_server(SimpleNamespace(state=SimpleNamespace(lock=site_server.records_lock, records=site_server.records)))
    evidence.note_cells(summary)
    side = _ServerSideLog()
    try:
        with evidence:
            side.install()
            evidence.arm()
            _drive(mode, firefox_binary, body)
    finally:
        side.uninstall()
        if evidence.saved_to is not None:
            try:
                _save_extra(evidence.saved_to, {"server-to-client.jsonl": side.messages,
                                                "server-adoption-steps.jsonl": side.steps + [{"ev": "problem", "what": w} for w in side.problems],
                                                "client-events.jsonl": client_events}, side.problems)
            except Exception as problem:         # the evidence never hides the failure it is about
                import sys
                sys.stderr.write("popup evidence: the server's side could not be saved: %r\n" % (problem,))


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("opener", ["#open1500", "#link1500"])
def test_a_listener_registered_in_the_page_handler_hears_the_load_of_the_popup(firefox_binary, site, mode, opener):
    """Stock Playwright: `context.on("page", lambda p: p.on("load", ...))` fires for a popup that loads after the event (4 s and 25 s
    destinations); the popup is reported when its ready comes, which is when its document commits, so the load may be said before the
    client has the page. `wait_for_load_state` in the same handler returns at once."""
    def body(ctx, page):
        page.goto(site, wait_until="load", timeout=30000)
        heard, waited = [], []

        def on_page(popup):
            popup.on("load", lambda p: heard.append(time.monotonic()))
            t = time.monotonic()
            popup.wait_for_load_state("load", timeout=30000)
            waited.append(time.monotonic() - t)

        ctx.on("page", on_page)
        with page.expect_popup(timeout=30000) as info:
            page.click(opener)
        deadline = time.monotonic() + 30
        while not heard and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert heard, "the listener registered in the handler of the page event never heard the load of the popup"
        # wait_for_load_state in the handler has its own deadline (10 s, above): a wait that never returns raises
        # there and `waited` stays empty, which is red here. How long it took is the machine's, not a verdict.
        assert waited, "wait_for_load_state in the handler never returned"
        info.value.close()

    _drive(mode, firefox_binary, body)
