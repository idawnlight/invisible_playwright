"""The fake web, profile P4: a user journey over a small multi-page site, driven by the stock Playwright client.

17 steps a person would make (click, hover, scroll, back, forward, a popstate filter, a new tab, a download, a keyboard
search, a sign-in, sign out, "load more") over two origins, with a deterministic ad noise on every page. Every step must
succeed, and `networkidle` must come where stock Firefox has it: on every step except the ones whose page holds a request open
on purpose (declared in tests/fakeweb/pages.json under journey_p4; one of them holds a keepalive fetch, which the client must
count as in flight). Stock Firefox itself is held to that declaration by scripts/run_fakeweb.py --profile p4, which also
compares our engine with it field by field, step by step.
"""
from __future__ import annotations

import importlib
import json
import pathlib

import pytest

from _evidence_on_failure import FailureEvidence
from fakeweb import journey_site, profile_p4

pytestmark = pytest.mark.e2e

HERE = pathlib.Path(__file__).resolve().parent / "fakeweb"


def _stock_client():
    """The stock Playwright client (a pinned dev dependency), imported by name: the fork's guard forbids a static import."""
    try:
        return importlib.import_module("playwright.sync_api").sync_playwright
    except ImportError as exc:      # a broken environment, not a reason to skip: a skipped gate checks nothing
        pytest.fail("the stock Playwright client is missing (%s): install the wrapper with its dev extra" % exc,
                    pytrace=False)


def test_the_journey_succeeds_on_every_step_and_idles_where_stock_does(firefox_binary):
    """When it fails it saves, under `invpw-evidence/fakeweb-journey-idles-where-stock-does/`, what answers whether the browser sent the
    request a step holds, when the site received and answered it, and what the client heard of it before `networkidle` returned:
    `fakeweb-server-record.jsonl` (the site's own record), `client-request-log.jsonl` (every request event the client heard, with the step),
    `cells.json` (each step's record with `in_flight_at_idle_end`), `juggler-wire.jsonl` and `juggler-engine-trace/`."""
    not_idle = json.loads((HERE / "pages.json").read_text(encoding="utf-8"))["journey_p4"]["not_idle_steps"]
    with FailureEvidence("fakeweb-journey-idles-where-stock-does") as evidence:
        origin_a, origin_o, stop = journey_site.start((0, 0))
        evidence.watch_server(stop)
        step_log = []
        evidence.note_cells(step_log)
        try:
            evidence.arm()
            steps = profile_p4.run_on_candidate(_stock_client(), firefox_binary, origin_a, origin_o, 1,
                                                client_log=evidence.client_log, step_log=step_log)
        finally:
            stop()
        failed = ["step %d (%s): %s" % (s["n"], s["name"], s["error"]) for s in steps if not s["ok"]]
        assert not failed, "steps that did not succeed:\n  " + "\n  ".join(failed)
        wrong = ["step %d (%s): %s" % (s["n"], s["name"], "idle, stock holds a request here" if s["idle"] else
                 "not idle, open: %s" % s["open_at_idle_end"]) for s in steps if s["idle"] != (s["n"] not in not_idle)]
        assert not wrong, "networkidle differs from the declared outcome of stock Firefox:\n  " + "\n  ".join(wrong)
