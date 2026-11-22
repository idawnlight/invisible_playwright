"""The fake web is offline: its servers listen on the loopback, and no page, script or declaration of tests/fakeweb names a host of the internet.

A fake web that fetched one font, one script or one image from a real host would make the verdict "the same outcome as stock" depend on a third party
(the host is down, slow or blocked: red for no fault of the product) and would send a request out of the runner. What this holds, reading the files as text:

* every ``http(s)://`` written in tests/fakeweb (pages, scripts, the declaration of what stock does) is the loopback, ``localhost``, or the XML namespace of SVG
  (an identifier, never fetched);
* the servers of the two sites listen on ``127.0.0.1`` unless the caller says otherwise (a container is the caller that does);
* the four e2e tests of the fake web are in a row of ci/required-suites.json that runs on both systems, so the candidate cannot leave them out.

Known-bad inputs: a page that loads a script from a real host, a script that fetches a real URL, a server that listens on all interfaces by default.
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
FAKEWEB = ROOT / "tests" / "fakeweb"

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "www.w3.org"}      # www.w3.org: the namespace of SVG, an identifier


def foreign_hosts(text):
    return sorted({m.group(1) for m in re.finditer(r"https?://([A-Za-z0-9._-]+)", text) if m.group(1).lower() not in ALLOWED_HOSTS})


def listens_on_all_interfaces_by_default(start):
    default = inspect.signature(start).parameters["bind"].default
    return default not in ("127.0.0.1", "localhost")


def test_a_page_that_names_a_real_host_is_caught():
    assert foreign_hosts('<script src="https://cdn.example.com/a.js"></script>') == ["cdn.example.com"]
    assert foreign_hosts("fetch('http://example.org/x')") == ["example.org"]
    assert foreign_hosts('<svg xmlns="http://www.w3.org/2000/svg"></svg> fetch("http://127.0.0.1:8311/x")') == []


def test_a_server_that_listens_on_all_interfaces_by_default_is_caught():
    def start(ports=(0, 0), bind="0.0.0.0"):
        return None

    assert listens_on_all_interfaces_by_default(start)


def test_no_file_of_the_fake_web_names_a_host_of_the_internet():
    problems = {}
    for path in sorted(FAKEWEB.iterdir()):
        if path.suffix in (".py", ".json", ".md"):
            hosts = foreign_hosts(path.read_text(encoding="utf-8"))
            if hosts:
                problems[path.name] = hosts
    assert not problems, "files of tests/fakeweb that name a host outside the loopback: %s" % problems


def test_the_servers_of_the_fake_web_listen_on_the_loopback_by_default():
    from fakeweb import journey_site, sites

    assert not listens_on_all_interfaces_by_default(sites.start)
    assert not listens_on_all_interfaces_by_default(journey_site.start)


def test_the_e2e_tests_of_the_fake_web_are_in_a_row_of_the_required_suites_on_both_systems():
    rows = json.loads((ROOT / "ci" / "required-suites.json").read_text(encoding="utf-8"))
    row = next((r for r in rows if r["id"] == "wrapper.e2e.fakeweb"), None)
    assert row is not None, "the suite wrapper.e2e.fakeweb is not in ci/required-suites.json"
    assert sorted(row["os"]) == ["linux", "windows"] and row["marker"] == "e2e" and row["exact"] is True
    wanted = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").glob("test_fakeweb_*_e2e.py"))
    assert sorted(row["tests"]) == wanted and len(wanted) == 2
