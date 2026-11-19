"""A navigation that changes the process (a -> b, sites isolated: strategy 1) ends the way one that does not does.

With sites isolated a navigation to another site starts in the process of the page being left and commits in a new
one. The id of the navigation is the load identifier the parent assigns; the process that commits does not know it, and
the old process's own stop of the load (it is cut when the navigation changes process) is not a failure of the
navigation. What the client must still see, whatever the process:

  * a goto stopped by the page it is leaving raises NS_BINDING_ABORTED (the abort of the old process is its own);
  * a second goto that starts while the first one changes process (the first response is half there) ends both
    gotos, each with a commit or with an error the engine tells, never with its timeout; either may win (as in stock
    Firefox) and the page lands on the winner's address (the first's only once the rest of its response is sent); the
    loser is "interrupted by another navigation" or an abort, and the abort of the replaced one is told under its own
    id, not taken for the end of the new one;
  * an iframe whose load is refused ends its load (the events a waiter needs) and its replacement error page is a
    navigation of its own; an iframe removed while it loads is aborted, then detached;
  * an iframe that goes to another site (its src changes) ends with the new address in the frame list: the process
    it leaves cuts its load (NS_BINDING_ABORTED, its docShell being destroyed), which is not the navigation's abort;
  * a frame in another process is committed with the name of its <iframe> (the embedder reads it).

Prefs: strategy 1 (`fission.webContentIsolationStrategy = 1`, `fission.autostart`) for this file only, as the bench
(`test_fission_strategy1_bench.py`); hosts a.test, b.test and c.test resolve to the local server through
`network.dns.localDomains`: two hosts are two sites.
"""
from __future__ import annotations

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

import pytest

from _second_goto_verdicts import landed_on, second_goto_problem
from _servers import LocalServer
from _waits import awatch

pytestmark = pytest.mark.e2e

#: The longest any wait of this file lasts; no healthy run comes near it. It is the only clock the verdicts have.
DEADLINE = 30.0

#: The timeout of the gotos that are stopped or replaced below. A goto that is not stopped waits for its held answer and
#: ends at this timeout with another error: it is the bound of the waits that lead up to the stop, nothing narrower.
GOTO_TIMEOUT_MS = 30000

#: The timeout of the two gotos of the replaced-goto test, which return by themselves: it is not a bound on how long
#: they take, only the point past which one that has not returned is the defect.
COMMIT_TIMEOUT_MS = 30000

FISSION_ON = {
    "fission.autostart": True,
    "fission.webContentIsolationStrategy": 1,
    "dom.ipc.processCount.webIsolated": 4,
    "network.dns.localDomains": "a.test,b.test,c.test",
}


@pytest.fixture
def sites():
    arrived = set()
    # what the server holds until the test lets it go, after the call that must not wait for it has returned
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def log_message(self, *args):
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            arrived.add(path)
            try:
                if path == "/act":
                    # the page asks whether the request it is to stop has arrived
                    body = b"go" if "/held" in arrived else b"wait"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/held":
                    # nothing is answered until the test opens the gate
                    release.wait()
                    path = "/plain"
                if path == "/partial":
                    # the headers and the start of the body arrive, the rest is held: the response is there, the
                    # navigation has changed process, the document is not complete
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", "200")
                    self.end_headers()
                    self.wfile.write(b"<!doctype html><title>partial</title><p>start")
                    self.wfile.flush()
                    # the rest is sent when the test lets it go, after the second goto has returned
                    release.wait()
                    self.wfile.write(b" " * 156)
                    return
                if path == "/slow-frame":
                    time.sleep(3)
                body = ("<!doctype html><title>%s</title><p>%s" % (path, path)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass

    server = LocalServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield {"at": lambda host, path, p=None: "http://%s:%d%s" % (host, p or port, path), "arrived": arrived,
           "release": release}
    release.set()
    server.shutdown()
    server.server_close()
    thread.join()


def _run(firefox_binary, scenario):
    from invisible_playwright.async_api import InvisiblePlaywright

    async def go():
        async with InvisiblePlaywright(seed=42, binary_path=firefox_binary, humanize=False, headless=True,
                                       extra_prefs=FISSION_ON) as browser:
            page = await browser.new_page()
            return await scenario(page)

    return asyncio.run(go())


def test_a_goto_to_another_site_stopped_by_the_page_it_leaves_raises_aborted(firefox_binary, sites):
    async def scenario(page):
        await page.goto(sites["at"]("a.test", "/plain"))
        # the page stops its load when the server says the request of the goto has arrived (and is held): the stop
        # comes while the navigation is on its way, whatever the machine; a timer in the page could come before it
        await page.evaluate(
            "window.__w = setInterval(() => fetch('/act', {cache: 'no-store'}).then(r => r.text())"
            ".then(t => { if (t === 'go') { clearInterval(window.__w); window.stop(); } }), 50)")
        try:
            # the server answers it only once this call has returned: it can end only by the stop (or by its timeout,
            # which carries no NS_BINDING_ABORTED)
            await page.goto(sites["at"]("b.test", "/held"), wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        except Exception as e:
            return str(e), page.url
        finally:
            sites["release"].set()
        return None, page.url

    error, url = _run(firefox_binary, scenario)
    assert error is not None and "NS_BINDING_ABORTED" in error, error
    assert url.startswith("http://a.test:"), url


def test_a_second_goto_that_starts_while_the_first_changes_process_ends_both_gotos_and_one_of_them_lands(
        firefox_binary, sites):
    # The contract: each goto returns, with a commit or with an error the engine tells, and the page lands on the address of the
    # one that won. Which of the two wins is not the contract (stock Firefox 159 with Fission gives the first the win in most runs,
    # the second in the others); a goto that ends only at its timeout is the defect. The verdicts are in _second_goto_verdicts.
    async def scenario(page):
        await page.goto(sites["at"]("a.test", "/plain"))
        # The responses the client heard: the one of the first goto is the moment its headers and the start of its body
        # reached the parent, which is where the navigation is handed to the process of b.test. A document that has its
        # start but not its end does not commit, so the URL of the page is not the condition; the response is.
        responses = []
        page.on("response", lambda r: responses.append(urlsplit(r.url).path))

        async def go(url, after=None):
            if after is not None:
                seen = await awatch(lambda: (after in sites["arrived"], after in responses),
                                    lambda state: state[0] and state[1],
                                    COMMIT_TIMEOUT_MS / 1000, lambda: asyncio.sleep(0.02))
                if not seen.ok:
                    return "the client never heard the response of the first goto; seen: %s" % seen.render()
            try:
                # "commit": the first goto's domcontentloaded would wait for the body that the server holds
                await page.goto(url, wait_until="commit", timeout=COMMIT_TIMEOUT_MS)
                return None
            except Exception as e:
                return str(e)

        first_goto = asyncio.ensure_future(go(sites["at"]("b.test", "/partial")))
        try:
            second = await go(sites["at"]("c.test", "/second"), after="/partial")
        finally:
            # the rest of the first response is sent only now, after the second goto has returned: when the first wins, its
            # navigation commits only with the rest of /partial, and its goto may be waiting for that commit
            sites["release"].set()
        first = await first_goto
        # The page lands on the winner: at once when the second won, only after the rest of /partial when the first did. Read
        # before that, the address is still a.test, the page that was left (main dd4c623d, Linux: red for that, not for the
        # product).
        landed = await awatch(lambda: page.url, lambda url: landed_on(url) is not None, DEADLINE, lambda: asyncio.sleep(0.02))
        return first, second, page.url, landed.render()

    first, second, url, landed = _run(firefox_binary, scenario)
    # what the server had been asked for: whether the second goto's request left at all, or waited behind the held response
    problem = second_goto_problem(first, second, url, sites["arrived"])
    assert problem is None, "%s; the address of the page after the release: %s" % (problem, landed)


def test_an_iframe_refused_its_load_ends_it_and_its_error_page_is_a_navigation(firefox_binary, sites):
    async def scenario(page):
        await page.goto(sites["at"]("a.test", "/plain"))
        events = []
        page.on("frameattached", lambda f: events.append("attached"))
        await page.evaluate(
            "(u) => { const f = document.createElement('iframe'); f.id = 'r'; f.src = u; document.body.appendChild(f); }",
            sites["at"]("b.test", "/x", 9))   # a port Firefox refuses

        def frames():
            return [(f.url, f.name) for f in page.frames]

        # until the error page's frame is there with its address (a condition, not 2.5 s: a loaded machine is slower)
        seen = await awatch(frames, lambda found: len(found) == 2 and urlsplit(found[1][0])[:2] == ("http", "b.test:9"),
                            DEADLINE, lambda: page.wait_for_timeout(100))
        return frames(), events, seen.render()

    frames, events, seen = _run(firefox_binary, scenario)
    assert len(frames) == 2, "%r; the frames over time: %s" % (frames, seen)
    assert frames[1][1] == "r", "%r; the frames over time: %s" % (frames, seen)   # named by its iframe, in whatever process
    assert urlsplit(frames[1][0])[:2] == ("http", "b.test:9"), "%r; the frames over time: %s" % (frames, seen)


def test_an_iframe_removed_while_it_loads_is_aborted_then_gone(firefox_binary, sites):
    async def scenario(page):
        await page.goto(sites["at"]("a.test", "/plain"))
        counts = {"attached": 0, "detached": 0}
        page.on("frameattached", lambda f: counts.__setitem__("attached", counts["attached"] + 1))
        page.on("framedetached", lambda f: counts.__setitem__("detached", counts["detached"] + 1))
        await page.evaluate(
            "(u) => { const f = document.createElement('iframe'); f.id = 's'; f.src = u; document.body.appendChild(f);"
            " setTimeout(() => f.remove(), 1000); }", sites["at"]("b.test", "/slow-frame"))
        # until the iframe has been attached AND detached and only the page is left (the removal comes 1 s after the
        # insertion, whatever the machine: a test that only reads the list could read it before the frame existed)
        seen = await awatch(lambda: (counts["attached"], counts["detached"], len(page.frames)),
                            lambda state: state[0] >= 1 and state[1] >= 1 and state[2] == 1,
                            DEADLINE, lambda: page.wait_for_timeout(100))
        return [f.url for f in page.frames], seen.render()

    urls, seen = _run(firefox_binary, scenario)
    assert len(urls) == 1 and urls[0].startswith("http://a.test:"), (
        "%r; (frames attached, detached, listed) over time: %s" % (urls, seen))


def test_an_iframe_whose_src_goes_to_another_site_shows_the_new_address(firefox_binary, sites):
    async def scenario(page):
        await page.goto(sites["at"]("a.test", "/plain"))
        await page.evaluate(
            "(u) => { const f = document.createElement('iframe'); f.id = 'f'; f.src = u; document.body.appendChild(f); }",
            sites["at"]("b.test", "/first"))
        # until the iframe is at its first address (a condition, not a time: a loaded machine takes longer than 1.5 s)
        first = await awatch(lambda: [f.url for f in page.frames], lambda urls: any(u.endswith("/first") for u in urls),
                             DEADLINE, lambda: page.wait_for_timeout(100))
        before = [f.url for f in page.frames]
        await page.evaluate("(u) => { document.getElementById('f').src = u; }", sites["at"]("c.test", "/second"))
        second = await awatch(lambda: [f.url for f in page.frames], lambda urls: any(u.endswith("/second") for u in urls),
                              DEADLINE, lambda: page.wait_for_timeout(100))
        return before, [(f.url, f.name) for f in page.frames], first.render(), second.render()

    before, after, seen_first, seen_second = _run(firefox_binary, scenario)
    seen = "; the frame urls over time: first %s | second %s" % (seen_first, seen_second)
    assert len(before) == 2 and before[1].startswith("http://b.test:") and before[1].endswith("/first"), (
        "%r%s" % (before, seen))
    assert len(after) == 2, "%r%s" % (after, seen)
    assert after[1][0].startswith("http://c.test:") and after[1][0].endswith("/second"), "%r%s" % (after, seen)
    assert after[1][1] == "f", "%r%s" % (after, seen)            # still the frame of its <iframe>
