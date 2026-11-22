"""Profile P4 of ci-release-18: a user journey over a small multi-page fake site, driven by the stock Playwright client.

One context, one page, 17 steps a person would make: click, hover, scroll, back, forward, a popstate filter, a
`target=_blank` tab, a download, a keyboard search, a sign-in with a wrong then the right password, sign out, "load more".
After every step the natural wait of the action runs and then `networkidle` is recorded as a boolean (never a failure).
Every page also runs a deterministic ad noise (tests/fakeweb/journey_site.py). The verdict is "same outcome as stock
Firefox, step by step": ok, url, title, marker, cookie names, history length, open pages, idle and the step's own values,
with stock Firefox run by the same client in the same invocation. Chromium is not part of the verdict. Time is never compared.

Two pages stay "not idle" on purpose (a request held until the walk moves on, one of them a keepalive fetch): the client
must count every request that stock counts. Which steps those are is declared in tests/fakeweb/pages.json.
"""
from __future__ import annotations

import os
import re
import tempfile
import time

from fakeweb import journey_site

BLOCKED = ("image", "media")
TIMEOUT_MS = 15000
FIELDS = ["ok", "url", "title", "marker", "cookies", "history_length", "pages", "idle"]


def install_route(context):
    context.route("**/*", lambda route: route.abort() if route.request.resource_type in BLOCKED else route.continue_())


class Tracer:
    """The requests the client has heard of and not yet heard the end of (kept for the report; never compared). With
    `events` (the runner's --trace) it also keeps every request event the client reports, per step, for the request-type check.
    With `log` (a list; a test that saves evidence when it fails gives it) it keeps, apart from that, one entry per event the client
    hears (request, response, requestfinished, requestfailed, and the page's own framenavigated / domcontentloaded / load), each with
    the step the walk is in (`self.step`, set by the walk), its wall time `t` and `time.perf_counter()` as `m`; it only listens."""

    def __init__(self, context, events=False, log=None):
        self.open = {}
        self.events = [] if events else None
        self.t0 = time.monotonic()
        self.log = log
        self.step = 0
        self.detail = {}
        context.on("request", self._started)
        context.on("requestfinished", lambda r: self._ended(r, "finished"))
        context.on("requestfailed", lambda r: self._ended(r, "failed"))
        if log is not None:
            context.on("response", self._answered)
            context.on("page", self._page_opened)

    def _started(self, r):
        self.open[r] = "%s %s" % (r.resource_type, r.url.split("/", 3)[-1][:70])
        self._event(r, "request")
        entry = self._logged(r, "request")
        if entry is not None:
            self.detail[r] = {k: entry[k] for k in ("step", "m", "type", "method", "url", "frame")}

    def _ended(self, r, kind):
        self.open.pop(r, None)
        self.detail.pop(r, None)
        self._event(r, kind)
        more = {}
        if kind == "failed" and self.log is not None:
            try:
                more["failure"] = str(r.failure)[:200]
            except Exception:
                more["failure"] = None
        self._logged(r, "requestfinished" if kind == "finished" else "requestfailed", **more)

    def _event(self, r, kind):
        if self.events is not None:
            self.events.append({"ev": kind, "type": r.resource_type, "method": r.method,
                                "url": "/" + r.url.split("/", 3)[-1][:90], "t": round(time.monotonic() - self.t0, 3)})

    def _logged(self, r, kind, **more):
        """One entry of the log; a failure of the recording is never the client's (the walk goes on, the entry is missing)."""
        if self.log is None:
            return None
        try:
            try:
                frame = r.frame.url[:160]
            except Exception:
                frame = None
            entry = {"ev": kind, "step": self.step, "t": round(time.time(), 4), "m": round(time.perf_counter(), 6), "req": id(r),
                     "type": r.resource_type, "method": r.method, "url": r.url[:200], "frame": frame}
            entry.update(more)
            self.log.append(entry)
            return entry
        except Exception:
            return None

    def _answered(self, response):
        try:
            self._logged(response.request, "response", status=response.status)
        except Exception:
            pass

    def _page_opened(self, page):
        try:
            for name in ("framenavigated", "domcontentloaded", "load"):
                page.on(name, lambda arg, name=name: self._page_event(page, name, arg))
            self._page_event(page, "page", None)
        except Exception:
            pass

    def _page_event(self, page, name, arg):
        try:
            url = (arg.url if name == "framenavigated" else page.url)[:200]
        except Exception:
            url = None
        try:
            self.log.append({"ev": name, "step": self.step, "t": round(time.time(), 4), "m": round(time.perf_counter(), 6), "page": id(page), "url": url})
        except Exception:
            pass

    def snapshot(self):
        return sorted(self.open.values())[:12]

    def in_flight(self):
        """What the client counts as in flight now, with how long (`age_s`) and from which step (`step`); `log` only."""
        now = time.perf_counter()
        return [dict(d, age_s=round(now - d["m"], 3)) for d in sorted(self.detail.values(), key=lambda d: d["m"])]

    def take_events(self):
        taken, self.events = self.events, []
        return taken


class Journey:
    def __init__(self, context, page, a, o):
        self.ctx, self.page, self.a, self.o = context, page, a, o
        self.extra = {}

    def norm(self, url):
        return (url or "").replace(self.a, "A").replace(self.o, "O")

    # helpers -----------------------------------------------------------------
    def nav_click(self, selector, hover_first=False):
        loc = self.page.locator(selector).first
        if hover_first:
            loc.hover()
        with self.page.expect_navigation():
            loc.click()

    def expect_path(self, pattern):
        path = re.sub(r"^[A-Z]", "", self.norm(self.page.url))
        if not re.search(pattern, path):
            raise AssertionError("expected path %s, got %s" % (pattern, self.norm(self.page.url)))

    def keep_mark(self):
        self.page.evaluate("window.__keep = 1")

    def keep_check(self):
        if self.page.evaluate("window.__keep") != 1:
            raise AssertionError("the page navigated but should not have")

    def history_step(self, direction, max_presses=4, required=True):
        """Press back or forward like a person; if the URL did not change, press again (up to 4 times)."""
        p = self.page
        before = p.url
        presses = 0
        for _ in range(max_presses):
            presses += 1
            try:
                (p.go_back if direction == "back" else p.go_forward)(wait_until="commit", timeout=3000)
            except Exception:
                pass  # nothing committed in the main frame
            if p.url != before:
                break
        self.extra["presses"] = presses
        if p.url == before:
            if required:
                raise AssertionError("%s did not change the URL after %d presses" % (direction, presses))
            return False
        p.wait_for_load_state("load")
        return True

    def type_in(self, selector, text):
        self.page.locator(selector).click()
        self.page.keyboard.type(text, delay=30)

    # steps: each returns (page_to_observe, cleanup or None) -----------------
    def s1_open_home(self):
        self.page.goto(self.a + "/")
        self.expect_path(r"^/")
        return self.page, None

    def s2_hover_click_teaser(self):
        self.nav_click("a.teaser >> nth=1", hover_first=True)
        self.expect_path(r"^/article/2")
        return self.page, None

    def s3_scroll_next(self):
        p = self.page
        p.mouse.move(640, 400)
        for _ in range(80):
            if p.evaluate("window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2"):
                break
            p.mouse.wheel(0, 600)
            p.wait_for_timeout(60)
        self.extra["scrolled"] = bool(p.evaluate("window.scrollY > 0"))
        self.nav_click("#next")
        self.expect_path(r"^/article/3")
        return p, None

    def s4_back(self):
        self.history_step("back")
        self.expect_path(r"^/article/2")
        return self.page, None

    def s5_back_home(self):
        self.history_step("back")
        self.expect_path(r"^/($|\?)")
        return self.page, None

    def s6_forward(self):
        if not self.history_step("forward", max_presses=2, required=False):
            self.extra["forward_available"] = False
            self.nav_click("a.teaser >> nth=1")
        self.expect_path(r"^/article/2")
        return self.page, None

    def s7_nav_category(self):
        self.nav_click("#nav-category")
        self.expect_path(r"^/category/world")
        return self.page, None

    def s8_filter_popstate(self):
        p = self.page
        self.keep_mark()
        p.click("#sort-new")
        p.wait_for_selector("#list[data-sort=new]")
        p.click("#sort-top")
        p.wait_for_selector("#list[data-sort=top]")
        self.extra["top_first"] = p.inner_text("#list a.item >> nth=0")
        p.go_back()
        p.wait_for_selector("#list[data-sort=new]")
        self.keep_check()
        self.extra["new_first"] = p.inner_text("#list a.item >> nth=0")
        if "sort=new" not in p.url:
            raise AssertionError("popstate did not restore the URL: " + self.norm(p.url))
        return p, None

    def s9_category_article(self):
        self.nav_click("#list a.item >> nth=0")
        self.expect_path(r"^/article/")
        return self.page, None

    def s10_blank_tab(self):
        with self.ctx.expect_page() as info:
            self.page.click("#partner")
        tab = info.value
        tab.wait_for_load_state("load")
        tab.wait_for_selector("#marker")
        self.extra["pages_when_open"] = len(self.ctx.pages)
        if "Partner" not in tab.inner_text("#marker"):
            raise AssertionError("unexpected marker in the new tab")

        def cleanup():
            tab.close()
            self.extra["pages_after_close"] = len(self.ctx.pages)

        return tab, cleanup

    def s11_download(self):
        with self.page.expect_download() as info:
            self.page.click("#download")
        dl = info.value
        fd, path = tempfile.mkstemp(suffix=".txt", prefix="journey_dl_")
        os.close(fd)
        try:
            dl.save_as(path)
            size = os.path.getsize(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        self.extra["download_name"] = dl.suggested_filename
        self.extra["download_size"] = size
        if size <= 0:
            raise AssertionError("empty download")
        return self.page, None

    def s12_search(self):
        p = self.page
        self.nav_click("#nav-search")
        self.expect_path(r"^/search")
        self.keep_mark()
        p.click("#search-submit")
        p.wait_for_selector("#msg")
        self.keep_check()
        self.type_in("#q", "quiet morning")
        with p.expect_navigation():
            p.keyboard.press("Enter")
        p.wait_for_selector("#qshow")
        self.extra["qshow"] = p.inner_text("#qshow")
        self.extra["results"] = p.locator("a.result").count()
        if self.extra["qshow"] != "quiet morning" or self.extra["results"] < 1:
            raise AssertionError("bad search results page")
        return p, None

    def s13_click_result(self):
        self.nav_click("a.result >> nth=0")
        self.expect_path(r"^/article/")
        return self.page, None

    def s14_signin(self):
        p = self.page
        self.nav_click("#nav-signin")
        self.expect_path(r"^/signin")
        self.type_in("#user", journey_site.USER)
        self.type_in("#pass", "not-the-password")
        with p.expect_navigation():
            p.keyboard.press("Enter")
        p.wait_for_selector("#error")
        self.extra["error"] = p.inner_text("#error")
        self.type_in("#pass", journey_site.PASSWORD)
        with p.expect_navigation(url=re.compile(r"/account")):
            p.keyboard.press("Enter")
        self.expect_path(r"^/account")
        self.extra["user"] = p.inner_text("#user")
        return p, None

    def s15_logo_account(self):
        self.nav_click("#logo")
        self.expect_path(r"^/($|\?)")
        self.nav_click("#nav-account")
        self.expect_path(r"^/account")
        self.extra["user"] = self.page.inner_text("#user")
        return self.page, None

    def s16_signout(self):
        self.nav_click("#signout")
        self.expect_path(r"^/($|\?)")
        self.nav_click("#nav-account")
        self.expect_path(r"^/signin")
        return self.page, None

    def s17_load_more(self):
        p = self.page
        self.nav_click("#logo")
        self.expect_path(r"^/($|\?)")
        before = p.locator("a.teaser").count()
        self.keep_mark()
        p.click("#loadmore")
        p.wait_for_function("document.querySelectorAll('a.teaser').length >= %d" % (before + 3))
        self.keep_check()
        self.extra["teasers"] = [before, p.locator("a.teaser").count()]
        return p, None


STEPS = [
    ("open home", "s1_open_home"),
    ("hover and click a teaser", "s2_hover_click_teaser"),
    ("scroll to the bottom, next article", "s3_scroll_next"),
    ("back", "s4_back"),
    ("back to home", "s5_back_home"),
    ("forward", "s6_forward"),
    ("nav Category", "s7_nav_category"),
    ("filter twice, back (popstate)", "s8_filter_popstate"),
    ("click an article from the category", "s9_category_article"),
    ("target=_blank link, new tab", "s10_blank_tab"),
    ("download summary", "s11_download"),
    ("search: empty, then keyboard + Enter", "s12_search"),
    ("click a result", "s13_click_result"),
    ("sign in: wrong then right password", "s14_signin"),
    ("logo, then Account (cookie kept)", "s15_logo_account"),
    ("sign out, then Account lands on sign in", "s16_signout"),
    ("load more on home", "s17_load_more"),
]


def first_line(exc):
    text = str(exc).strip()
    return (text.splitlines()[0] if text else repr(exc))[:300]


def snapshot(j, pg, rec):
    for key, expr in (
        ("title", "document.title"),
        ("marker", "(document.getElementById('marker') || {}).textContent || null"),
        ("cookies", "document.cookie.split(';').map((s) => s.trim().split('=')[0]).filter(Boolean).sort()"),
        ("history_length", "history.length"),
    ):
        try:
            rec[key] = pg.evaluate(expr)
        except Exception as exc:
            rec[key] = None
            rec.setdefault("snapshot_error", first_line(exc))
    rec["url"] = j.norm(pg.url)


def run_round(j, tracer, round_no, idle_timeout_ms, step_log=None):
    """`step_log`, a list (default None: nothing kept), receives each step's record as soon as it is done, with the moments of the step
    (`m_*`: time.perf_counter(), the scale of the client log and of the site's record) and what the client counted as in flight when
    `networkidle` returned or gave up: for a test that saves evidence when it fails. Never compared."""
    out = []
    walk = journey_site.STATE.begin_walk()      # the site tags what it sees with the walk, stock and ours both use steps 1..N
    for n, (name, method) in enumerate(STEPS, 1):
        j.extra = {}
        tracer.step = n
        m_before_set_step = time.perf_counter()
        journey_site.STATE.set_step(n)      # a held response of the site is released when the walk reaches its step
        rec = {"round": round_no, "n": n, "name": name, "ok": False, "error": None}
        if step_log is not None:
            rec["m_step_start"], rec["t_step_start"] = round(m_before_set_step, 6), round(time.time(), 4)
        t0 = time.monotonic()
        pg, cleanup = j.page, None
        try:
            pg, cleanup = getattr(j, method)()
            rec["ok"] = True
        except Exception as exc:
            rec["error"] = first_line(exc)
        if step_log is not None:
            rec["m_action_end"] = round(time.perf_counter(), 6)
        try:
            pg.wait_for_load_state("networkidle", timeout=idle_timeout_ms)
            rec["idle"] = True
        except Exception as exc:
            rec["idle"] = False
            if step_log is not None:
                rec["idle_error"] = first_line(exc)
        rec["open_at_idle_end"] = tracer.snapshot()      # for the report, not compared
        if step_log is not None:
            rec["m_idle_end"], rec["t_idle_end"] = round(time.perf_counter(), 6), round(time.time(), 4)
            rec["in_flight_at_idle_end"] = tracer.in_flight()
        if tracer.events is not None:
            rec["client_events"] = tracer.take_events()
        snapshot(j, pg, rec)
        rec["pages"] = len(j.ctx.pages)
        rec["extra"] = dict(j.extra)
        if cleanup:
            try:
                cleanup()
            except Exception as exc:
                rec["ok"] = False
                rec["error"] = rec["error"] or ("cleanup: " + first_line(exc))
        rec["seconds"] = round(time.monotonic() - t0, 2)
        out.append(rec)
        if step_log is not None:
            step_log.append(rec)
    if tracer.events is not None:
        # read once, at the end: a request fired on pagehide reaches the server after its step's own work is done
        time.sleep(0.5)
        for rec in out:
            rec["server_events"] = journey_site.STATE.events_of(walk, rec["n"])
    return out


def drive(browser, origin_a, origin_o, rounds=1, idle_timeout_ms=15000, trace=False, client_log=None, step_log=None):
    """`rounds` walks, each in a fresh context and a fresh page. Returns the step records. `trace`: keep, per step, the request
    events the client reported and the hits the server saw (for the report; never compared). `client_log` and `step_log`, two lists
    (default None: nothing kept), are filled as the walk goes with what the client heard (`Tracer`) and with each step's record
    (`run_round`): for a test that saves evidence when it fails."""
    records = []
    for r in range(1, rounds + 1):
        context = browser.new_context()
        try:
            context.set_default_timeout(TIMEOUT_MS)
            context.set_default_navigation_timeout(TIMEOUT_MS)
            install_route(context)
            tracer = Tracer(context, events=trace, log=client_log)
            page = context.new_page()
            records += run_round(Journey(context, page, origin_a, origin_o), tracer, r, idle_timeout_ms, step_log)
        finally:
            try:
                context.close()
            except Exception:
                pass
    return records


def run_on_candidate(sync_playwright, binary, origin_a, origin_o, rounds=1, **kw):
    """Our engine, served over the Playwright WebSocket and driven by the stock client."""
    from invisible_playwright._juggler.remote import launch_server

    server = launch_server(headless=True, binary_path=binary, locale="en-US", timezone="Europe/Rome")
    try:
        with sync_playwright() as p:
            browser = p.firefox.connect(server.ws_endpoint)
            try:
                return drive(browser, origin_a, origin_o, rounds, **kw)
            finally:
                browser.close()
    finally:
        server.close()


def run_on_stock(sync_playwright, origin_a, origin_o, rounds=1, **kw):
    with sync_playwright() as p:
        browser = p.firefox.launch()
        try:
            return drive(browser, origin_a, origin_o, rounds, **kw)
        finally:
            browser.close()


def _flat(rec):
    flat = {f: rec.get(f) for f in FIELDS}
    for k, v in (rec.get("extra") or {}).items():
        flat["extra." + k] = v
    return flat


def synthesize_stock(candidate, not_idle_steps):
    """Stock as pages.json declares it: every step ok, idle everywhere except the declared steps. Only these two fields
    are then compared (the other fields are copied from the candidate)."""
    return [dict(c, ok=True, idle=c["n"] not in not_idle_steps) for c in candidate]


def verdict(candidate, stock, not_idle_steps):
    """Returns (fails, broken). fails: (round, step, field, stock value, candidate value) where the candidate differs from
    stock Firefox. broken: where stock is not what pages.json declares (a step failed, or idle is not as declared)."""
    stock_by = {(s["round"], s["n"]): s for s in stock}
    fails, broken = [], []
    for c in candidate:
        s = stock_by.get((c["round"], c["n"]))
        if s is None:
            fails.append((c["round"], c["n"], "step", "missing in stock", None))
            continue
        cf, sf = _flat(c), _flat(s)
        for k in sorted(set(cf) | set(sf)):
            if cf.get(k) != sf.get(k):
                fails.append((c["round"], c["n"], k, sf.get(k), cf.get(k)))
    for s in stock:
        if not s["ok"]:
            broken.append((s["round"], s["n"], "ok", s["error"]))
        elif s["idle"] != (s["n"] not in not_idle_steps):
            broken.append((s["round"], s["n"], "idle", "idle=%s, declared %s" % (
                s["idle"], "not idle" if s["n"] in not_idle_steps else "idle")))
    return fails, broken
