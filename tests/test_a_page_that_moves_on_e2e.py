"""Pages that replace themselves right after they load, on a real engine.

⛔ THE KNOWN-BAD INPUT, from invisible_dots (pages behind JavaScript challenges) and
reproduced here on 2026-10-12 against firefox-41 and 0.34.0:

- a read right after `goto(..., "domcontentloaded")` on a page that calls
  `location.replace` from its `<head>`, or 0 ms after `load`, was refused with
  the engine's raw `Failed to find execution context`, 5 times in 5;
- `goto(..., "networkidle")` on a page that replaces itself after `load`
  timed out, 12 times in 12: the move to another content process re-announces
  the main frame, and the lifecycle built a new Frame that had forgotten our
  navigation ([B228]). In the invisible_dots VMs, where the events arrive
  together, the same happened to `domcontentloaded` (45 s);
- `page.frames` grew by a stale iframe and a repeated main frame at every
  cross-process navigation.

[B228]'s own e2e did not see any of it: its page called `location.replace`
from the `<head>` and was waited for `load`, a case where no second process
is involved. These pages navigate AFTER `load`, same-site and cross-site
(127.0.0.1 to localhost, which Fission puts in another process), and each
case is repeated, because a race that is missed once in five is still a race.

The rounds run back to back, so a `goto` can be sent while the previous page's
own `location.replace` is in flight, and Firefox lets that one win: ours is
never heard of again, no commit and no abort. Until 41.36.0 that `goto` waited
for its timeout (2 and 3 rounds in 80, measured 2026-10-12); now the first
document committed after the command decides, and when it is not ours `goto`
raises at once with upstream Playwright's "is interrupted by another
navigation to".

And the page's `location.replace` can come while OUR goto is on its way, and
stop it: from firefox-46 the engine reports that stop ([B302]; it dropped every
abort of the main frame until then), so goto raises NS_BINDING_ABORTED and the
page goes to /landed. Upstream Playwright on its Firefox answers these rounds
the same way, measured four at once (`C:/tmp/b230/moves_load_upstream.sh`,
2026-10-14): 41 rows of 48 all landed, the rest one "interrupted" and eight
NS_BINDING_ABORTED, every one with the page then on /landed; firefox-46 under
the same load, 39 of 48, ten "interrupted" and four NS_BINDING_ABORTED, the
page on /landed every time and never on our address. `_goto` accepts those
three answers, fast, and nothing else.
"""
from __future__ import annotations

import http.server
import threading

import pytest

pytestmark = pytest.mark.e2e

ROUNDS = 5


def _goto(page, url, **kwargs):
    """`page.goto`, which may answer that the previous page's own redirect won
    (module docstring). The detector of a navigation that never answers is the timeout of the `goto` itself (each caller passes
    one): a bound of the test stricter than it only judged the speed of the machine."""
    from invisible_playwright.sync_api import Error

    try:
        page.goto(url, **kwargs)
    except Error as e:
        if "NS_BINDING_ABORTED" in str(e):
            # The page's own replace stopped ours: it is where it sent itself.
            page.wait_for_url("**/landed", timeout=30_000)
        else:
            assert "is interrupted by another navigation to" in str(e), e
            assert "/landed" in str(e).split("another navigation to")[1], e


def _replace_after_load(ms: int, cross_site: bool) -> bytes:
    target = ("location.origin.replace('127.0.0.1','localhost')+'/landed'"
              if cross_site else "'/landed'")
    return (("<!doctype html><html><body><p id=p>first</p><script>"
             "addEventListener('load', () => setTimeout(() => "
             "location.replace(%s), %d))</script></body></html>")
            % (target, ms)).encode()


def _replace_without_a_timer(held: bool, cross_site: bool) -> bytes:
    """The page of a `networkidle` case: it replaces itself with no timer in it, so how fast the machine is cannot
    decide whether the move comes before the half second of quiet that `networkidle` waits for.

    - ``held`` False: from inside the `load` handler itself, before `load` is even reported;
    - ``held`` True: when the first bytes of a response the server holds open arrive. That request is in flight until
      the navigation aborts it, so the network is not quiet for as long as the page has not moved, whatever the load
      on the machine."""
    target = ("location.origin.replace('127.0.0.1','localhost')+'/landed'"
              if cross_site else "'/landed'")
    if held:
        script = ("addEventListener('load', () => fetch('/hold').then(r => r.body.getReader().read())"
                  ".then(() => location.replace(%s)))" % target)
    else:
        script = "addEventListener('load', () => location.replace(%s))" % target
    return ("<!doctype html><html><body><p id=p>first</p><script>%s</script></body></html>" % script).encode()


PAGES = {
    "/head": (b"<!doctype html><html><head><script>location.replace('/landed')"
              b"</script></head><body><p id=p>head</p></body></html>"),
    "/landed": b"<!doctype html><html><body><p id=p>landed</p></body></html>",
    "/withframe": (b"<!doctype html><html><body><iframe src='/landed'></iframe>"
                   b"</body></html>"),
}
for _ms in (0, 50, 300):
    PAGES["/r%d" % _ms] = _replace_after_load(_ms, False)
    PAGES["/x%d" % _ms] = _replace_after_load(_ms, True)
# the networkidle cases: "/ir0" and "/ix0" move inside the load handler, "/ir50" and "/ix50" when the held response
# starts to arrive (the 0 and the 50 of the names are the cases of the old timers)
for _name, _held in (("0", False), ("50", True)):
    PAGES["/ir" + _name] = _replace_without_a_timer(_held, False)
    PAGES["/ix" + _name] = _replace_without_a_timer(_held, True)


@pytest.fixture(scope="module")
def site():
    release = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] == "/hold":
                # a response that has begun and does not end: 16 KiB so the first chunk is delivered at once,
                # then it stays open until the page that asked for it moves away (or the module's teardown)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(b"x" * 16384)
                    self.wfile.flush()
                except OSError:
                    return
                release.wait(60)
                return
            body = PAGES.get(self.path.split("?")[0], PAGES["/landed"])
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    # Threading: Firefox's speculative connections starve a single-threaded
    # server (test_navigation_response.py).
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    release.set()
    srv.shutdown()


@pytest.fixture(scope="module")
def page(firefox_binary):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=4321, binary_path=firefox_binary,
                             headless=True) as browser:
        yield browser.new_page()


@pytest.mark.parametrize("path", ["/head", "/r0", "/r50", "/r300",
                                  "/x0", "/x50", "/x300"])
def test_a_read_right_after_goto_answers(page, site, path):
    for i in range(ROUNDS):
        _goto(page, "%s%s?i=%d" % (site, path, i), wait_until="domcontentloaded",
              timeout=30_000)
        # Each read is made at once, while the page may still be moving: none
        # of them may surface the engine's refusal of a vanished world. A count
        # is of the page as it is NOW, and that can be the replacing document
        # before its parser reached <body> (measured under -n 4 on Windows):
        # 0 is a true answer there, as in Playwright, an error is not.
        assert page.locator("body").count() in (0, 1)
        # The library's reads of every match go the way `count` does: made
        # again on the page that replaced the one they went to ([B282]), not
        # through the door that sends the caller's code once ([B285]). The
        # MCP's `read_text` is `all_inner_texts`.
        texts = ([], ["first"], ["head"], ["landed"])
        assert page.locator("#p").all_inner_texts() in texts
        assert page.locator("#p").all_text_contents() in texts
        assert page.inner_text("#p", timeout=30_000) in ("first", "head",
                                                         "landed")
        # The caller's code is NOT a read: it is sent once ([B285]). On a page
        # still moving - `/x50` replaces itself after the read above - it
        # answers, or raises Playwright's own navigation error, as upstream
        # does; sending it again ran a script twice until core 41.38.0
        # (`tests/test_the_callers_code_runs_once_e2e.py`).
        from invisible_playwright.sync_api import Error
        try:
            assert page.evaluate("1 + 1") == 2
        except Error as e:
            assert "Execution context was destroyed" in str(e), e


@pytest.mark.parametrize("path", ["/r0", "/r50", "/x0", "/x50"])
def test_networkidle_is_reached_on_the_page_that_replaced_ours(page, site, path):
    """The page moves with no timer (`_replace_without_a_timer`): `networkidle` promises half a second without
    network, and that cannot pass before the move however slow the machine is. A `networkidle` that comes back on the
    first page, or never comes back because the new frame forgot the navigation, is still red."""
    for i in range(ROUNDS):
        _goto(page, "%s/i%s?n=%d" % (site, path[1:], i), wait_until="networkidle",
              timeout=30_000)
        assert page.url.endswith("/landed"), page.url


def test_the_frames_follow_the_documents(page, site):
    """Every navigation here moves the main frame to another process, and the
    engine reports no detach for the iframe left behind.

    Not asserted: that the iframe is always listed. When the engine puts it in
    another process it reports it detached (measured on firefox-41, once in
    three), and the list says what the engine can drive. What is asserted is
    that nothing of a document already left is listed, and the main frame
    once: the defect listed eight frames for two after six navigations."""
    for i in range(4):
        page.goto("%s/withframe?f=%d" % (site, i), wait_until="load")
        frames = page.frames
        assert frames[0] is page.main_frame
        assert [f for f in frames if f is page.main_frame] == [page.main_frame]
        assert all(f.parent_frame is page.main_frame for f in frames[1:])
        assert len(frames) <= 2, [f.url for f in frames]
        page.goto("%s/landed?f=%d" % (site.replace("127.0.0.1", "localhost"), i),
                  wait_until="load")
        assert page.frames == [page.main_frame], [f.url for f in page.frames]


def test_reads_wait_as_playwright_s_do(page, site):
    page.goto(site + "/landed")
    assert page.is_visible("#missing") is False
    assert page.is_hidden("#missing") is True
    page.evaluate("setTimeout(() => { const b = document.createElement('b');"
                  " b.id = 'late'; b.textContent = 'late';"
                  " document.body.appendChild(b); }, 500)")
    assert page.inner_text("#late", timeout=30_000) == "late"
    from invisible_playwright.sync_api import TimeoutError as PlaywrightTimeout
    with pytest.raises(PlaywrightTimeout, match="Timeout 300ms exceeded"):
        page.inner_text("#never", timeout=300)


def test_expect_reads_the_state_it_names(page, site):
    from invisible_playwright._pw.sync_api import expect

    page.goto(site + "/landed")
    page.set_content("<input type=checkbox id=c><p id=h hidden>x</p>")
    with pytest.raises(AssertionError):
        expect(page.locator("#c")).to_be_checked(timeout=500)
    expect(page.locator("#c")).not_to_be_checked(timeout=30_000)
    with pytest.raises(AssertionError):
        expect(page.locator("#h")).to_be_visible(timeout=500)
    expect(page.locator("#h")).to_be_hidden(timeout=30_000)
    expect(page.locator("#missing")).to_be_hidden(timeout=30_000)
