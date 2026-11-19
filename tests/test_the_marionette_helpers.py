"""The helpers the e2e tests reach Marionette with, without a browser.

A browser's Marionette port is chosen per launch and never fixed: three e2e files shared 28431 and run on different xdist workers at
once. And a connection that fails says what the engine says about its Marionette (e2e Windows part 2, run 43704738537: a refused
connection for 120 s and nothing else to read)."""
import pathlib
import re
import socket

import webfont_probe

TESTS = pathlib.Path(__file__).resolve().parent


def test_a_marionette_port_is_free_when_it_is_given():
    port = webfont_probe.marionette_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))


def test_no_test_gives_a_browser_a_fixed_marionette_port():
    fixed = []
    for path in TESTS.glob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"""["']marionette\.port["']\s*:\s*\d""", line) or re.search(r"^MARIONETTE_PORT\s*=\s*\d", line):
                fixed.append("%s:%d" % (path.name, n))
    assert not fixed, "a fixed Marionette port: %s" % fixed


def test_a_failed_connection_says_what_the_engine_says_about_its_marionette():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    try:
        webfont_probe._Marionette(port, timeout=0)
    except OSError as failure:
        text = str(failure)
    else:
        raise AssertionError("a connection to a port nobody listens on succeeded")
    assert "Marionette did not answer on port %d" % port in text, text
    assert "no engine process below this one" in text, text
    assert "listening on %d: nobody" % port in text, text
