"""A launch of an e2e test is told where it leaves from; no service of the internet is asked (no browser).

The core's ``prepare_session_geo`` asks ``discover_egress_ip`` for the exit of a session, and with no proxy that is an HTTP request to three "what is my
address" services. ``tests/_lab_egress.py`` and the session fixture ``_the_lab_declares_the_exit`` of ``tests/conftest.py`` declare the exit of the lab
instead, so that the 222 launches that name neither ``timezone=`` nor ``proxy=`` depend on nothing outside the machine. This file is the proof, on the
real composition of a session and with every way out of the process closed:

* a session composed with the declared exit asks the core's discovery once, gets the zone and the language of the lab database, and opens no
  connection and resolves no name (the socket and the resolver raise, and ``requests`` is not called);
* the same with a zone named by the caller: the question is still asked, and still answered here;
* a question behind a proxy is not answered here: it goes to what answered it before (the gateways of the tests answer it themselves);
* the test that must prove the discovery itself gets the real function against an echo service on loopback, and the three services are not asked;
* the conftest holds the declaration for the whole session, and a module-scoped browser is launched after it.
"""
from __future__ import annotations

import ast
import socket
from pathlib import Path

import pytest

import _lab_egress

pytestmark = pytest.mark.unit

TESTS = Path(__file__).resolve().parent


class _Closed(AssertionError):
    pass


@pytest.fixture
def no_way_out(monkeypatch):
    """Every way out of the process raises: the connect, the name lookup, the request. What an offline runner does, said loudly."""
    tried = []

    def closed(what):
        def refuse(*args, **kwargs):
            tried.append(what)
            raise _Closed("%s was asked: a launch of an e2e test must not leave the machine" % what)
        return refuse

    monkeypatch.setattr(socket.socket, "connect", closed("socket.connect"))
    monkeypatch.setattr(socket, "create_connection", closed("socket.create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", closed("socket.getaddrinfo"))
    import requests

    monkeypatch.setattr(requests.sessions.Session, "request", closed("requests"))
    return tried


@pytest.fixture
def declared(monkeypatch, tmp_path):
    """What the session fixture of the conftest installs, installed for one test (the same function, with the patch of the test)."""
    monkeypatch.delenv("STEALTHFOX_GEOIP_MMDB", raising=False)
    exit_ = _lab_egress.install(monkeypatch, tmp_path)
    assert exit_ is not None
    return exit_


def test_a_session_composed_with_the_declared_exit_opens_no_connection_and_gets_the_zone_of_the_lab(declared, no_way_out):
    from invisible_core import prepare_session_geo

    geo = prepare_session_geo("", None, "auto", seed=7)
    assert no_way_out == [], "the composition left the machine: %s" % no_way_out
    assert declared.direct_questions == 1 and declared.proxied_questions == []
    assert geo.egress_ip == _lab_egress.LAB_EGRESS_IP
    assert geo.timezone == "America/New_York" and geo.locale.primary == "en-US", (geo.timezone, geo.locale)


def test_a_zone_named_by_the_caller_does_not_make_the_launch_ask_the_internet(declared, no_way_out):
    from invisible_core import prepare_session_geo

    geo = prepare_session_geo("Europe/Rome", None, "auto", seed=7)
    assert no_way_out == []
    assert declared.direct_questions == 1, "the core asks for the exit even when the zone is named: the question must be answered here"
    assert geo.timezone == "Europe/Rome" and geo.egress_ip == _lab_egress.LAB_EGRESS_IP


def test_the_lab_exit_is_a_public_address_the_lab_database_answers_as_new_york(tmp_path):
    import ipaddress

    import maxminddb

    assert ipaddress.ip_address(_lab_egress.LAB_EGRESS_IP).is_global, "a reserved address is refused by the core as not routable"
    path = tmp_path / "lab.mmdb"
    path.write_bytes(_lab_egress.lab_database_bytes())
    with maxminddb.open_database(str(path)) as reader:
        record = reader.get(_lab_egress.LAB_EGRESS_IP)
    assert record["country"]["iso_code"] == "US" and record["location"]["time_zone"] == "America/New_York", record


def test_a_question_behind_a_proxy_goes_to_what_answered_it_before(monkeypatch):
    asked = []

    def before(proxy=None, *args, **kwargs):
        asked.append(proxy)
        return "203.0.113.9"

    exit_ = _lab_egress.DeclaredExit(before)
    proxy = {"server": "socks5://127.0.0.1:9"}
    assert exit_(None) == _lab_egress.LAB_EGRESS_IP and asked == []
    assert exit_({}) == _lab_egress.LAB_EGRESS_IP and asked == []
    assert exit_(proxy, timeout=3) == "203.0.113.9" and asked == [proxy]
    assert exit_.direct_questions == 2 and exit_.proxied_questions == ["socks5://127.0.0.1:9"]


def test_a_test_that_replaces_the_question_goes_back_to_the_declared_exit_not_to_the_network(monkeypatch, tmp_path):
    """The order a session makes: the fixture of the session installs first, the test's own ``monkeypatch`` replaces and then undoes."""
    from invisible_core import _geo

    with pytest.MonkeyPatch.context() as session:
        session.setenv("STEALTHFOX_GEOIP_MMDB", str(tmp_path / "set-by-a-leg"))
        declared = _lab_egress.install(session, tmp_path)
        with pytest.MonkeyPatch.context() as test:
            test.setattr(_geo, "discover_egress_ip", lambda *a, **k: "203.0.113.5")
            assert _geo.discover_egress_ip(None) == "203.0.113.5"
        assert _geo.discover_egress_ip(None) == _lab_egress.LAB_EGRESS_IP and declared.direct_questions == 1


def test_a_database_set_by_a_leg_is_left_alone_and_none_is_made_when_not_set(monkeypatch, tmp_path):
    monkeypatch.setenv("STEALTHFOX_GEOIP_MMDB", str(tmp_path / "pinned.mmdb"))
    assert _lab_egress.use_the_lab_database(monkeypatch, tmp_path / "made") is None and not (tmp_path / "made").exists()
    monkeypatch.delenv("STEALTHFOX_GEOIP_MMDB")
    made = _lab_egress.use_the_lab_database(monkeypatch, tmp_path / "made")
    import os

    assert made is not None and made.read_bytes() == _lab_egress.lab_database_bytes() and os.environ["STEALTHFOX_GEOIP_MMDB"] == str(made)


def test_the_real_discovery_is_proved_against_an_echo_service_on_loopback(local_egress_echo):
    from invisible_core import _geo

    found = _geo.discover_egress_ip(None)
    assert found == _lab_egress.LAB_EGRESS_IP
    assert local_egress_echo.questions == 1, "the real function asked %d time(s) and only the echo of the test may be asked" % local_egress_echo.questions
    assert all(url.startswith("http://127.0.0.1:") for url in _geo._IP_ECHO_ENDPOINTS)


def test_the_conftest_declares_the_exit_for_the_whole_session_and_before_any_module_browser():
    tree = ast.parse((TESTS / "conftest.py").read_text(encoding="utf-8"))
    fixtures = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and getattr(decorator.func, "attr", "") == "fixture":
                    fixtures[node.name] = {kw.arg: getattr(kw.value, "value", None) for kw in decorator.keywords}
    declaration = fixtures.get("_the_lab_declares_the_exit")
    assert declaration == {"scope": "session", "autouse": True}, (
        "the exit of the lab is declared by an autouse fixture of SESSION scope (a module-scoped browser is launched before any function-scoped one): %r"
        % (declaration,))
    assert fixtures["browser"].get("scope") == "module", "the conftest's module browser is the reason for the scope above"
