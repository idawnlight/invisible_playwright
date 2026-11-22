"""The page of a journey step that asserts "an open keepalive fetch holds networkidle" starts that fetch after `load`.

The engine announces a request at once, but the commit of the new document waits for the engine's handshake; a fetch started while
the page is parsed can be announced before the commit and the client drops it there (the reference Juggler does the same), so the
step would test a race. The served page must start the held keepalive fetch from a later task. No browser: the page is read as it
is served and its script is read as text.
"""
from __future__ import annotations

import urllib.request

import pytest

from fakeweb import journey_site, sites

pytestmark = pytest.mark.unit

HELD = "kapark"     # the id prefix of the held keepalive fetch


def _scripts(page: str) -> list[str]:
    out, pos = [], 0
    while True:
        start = page.find("<script>", pos)
        if start < 0:
            return out
        end = page.index("</script>", start)
        out.append(page[start + len("<script>"):end])
        pos = end


def _without_deferred(js: str) -> str:
    """The script with every `afterLoad(...)` call removed: what is left runs while the page is parsed."""
    out, i = [], 0
    while True:
        j = js.find("afterLoad(", i)
        if j < 0:
            out.append(js[i:])
            return "".join(out)
        out.append(js[i:j])
        depth, k, quote = 0, j + len("afterLoad"), None
        while k < len(js):
            c = js[k]
            if quote:
                if c == "\\":
                    k += 1
                elif c == quote:
                    quote = None
            elif c in "'\"`":
                quote = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        i = k + 1


def held_keepalive_at_parse_time(page: str) -> list[str]:
    """The statements of the page's scripts that start the held keepalive fetch outside any afterLoad callback."""
    found = []
    for js in _scripts(page):
        for line in _without_deferred(js).splitlines():
            if HELD in line:
                found.append(line.strip())
    return found


def _serve(path: str) -> str:
    a, _o, stop = journey_site.start((0, 0))
    try:
        with urllib.request.urlopen(a + path) as r:
            return r.read().decode("utf-8")
    finally:
        stop()


PAGES = ["/" + name for name in journey_site.PARK_KEEPALIVE_PAGES]


@pytest.mark.parametrize("path", PAGES)
def test_a_page_that_holds_a_keepalive_fetch_does_not_start_it_while_it_is_parsed(path):
    page = _serve(path)
    assert any(HELD in js for js in _scripts(page)), "the page of %s no longer holds a keepalive fetch: the step has nothing to test" % path
    assert held_keepalive_at_parse_time(page) == []


@pytest.mark.parametrize("path", PAGES)
def test_the_held_keepalive_fetch_is_a_keepalive_fetch_to_the_hold_route(path):
    page = _serve(path)
    deferred = [js for js in _scripts(page) if HELD in js]
    assert deferred and "keepalive: true" in deferred[0]
    assert "afterLoad(0, () => fetch(A + '/hold?id=%s' + tok" % HELD in deferred[0]


def test_the_search_results_with_a_query_hold_it_too():
    page = _serve("/search?q=harbour")
    assert any(HELD in js for js in _scripts(page))
    assert held_keepalive_at_parse_time(page) == []


def test_afterload_runs_its_callback_from_a_task_after_the_load_event():
    prelude = sites.PRELUDE
    assert "addEventListener('load'" in prelude and "setTimeout(f, ms)" in prelude
    assert "document.readyState === 'complete'" in prelude


def test_the_check_sees_a_fetch_started_while_the_page_is_parsed():
    """On the old page the fetch was a statement of the script, not a callback."""
    page = _serve("/search")
    start = page.index("afterLoad(0, () => fetch(A + '/hold?id=" + HELD)
    end = page.index("));", start) + 2
    old = page[:start] + page[start + len("afterLoad(0, () => "):end - 1] + ";" + page[end + 1:]
    assert held_keepalive_at_parse_time(old) != []
    assert "fetch(A + '/hold?id=" + HELD in held_keepalive_at_parse_time(old)[0]
