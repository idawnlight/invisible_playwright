"""The verdicts of the second goto that starts while the first changes process (``_second_goto_verdicts``), on the outcomes the engine
gives and on the defects, and the order of the e2e scenario: the address of the page is read only once it has landed, after the rest
of the first response was sent.

The green rows are the six kinds of run of the hunt on the candidate of firefox-65 (run 43790642333, 200 runs per OS: the three
outcomes, on Linux and on Windows, the first row of each), as the hunt wrote them. The red row of main dd4c623d (Linux) is the
address read before the first goto's navigation landed: with the scenario of today it is what the page says only if it never lands.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from _second_goto_verdicts import landed_on, second_goto_problem

ASKED = ["/favicon.ico", "/partial", "/plain", "/second"]


def _interrupted(port: int) -> str:
    return ('Page.goto: Navigation to "http://b.test:%d/partial" is interrupted by another navigation to "http://c.test:%d/second"'
            % (port, port))


#: (os, repetition of the hunt): (goto 1 error, goto 2 error, url after, requests at the server), as run 43790642333 wrote them.
HUNT = {
    ("linux", 1): (_interrupted(40925), "Page.goto: NS_BINDING_ABORTED", "http://b.test:40925/partial", ASKED),
    ("linux", 2): (_interrupted(44697), None, "http://c.test:44697/second", ASKED),
    ("linux", 8): (None, "Page.goto: NS_BINDING_ABORTED", "http://b.test:46495/partial", ASKED),
    ("windows", 1): (_interrupted(65494), "Page.goto: NS_BINDING_ABORTED", "http://b.test:65494/partial", ASKED),
    ("windows", 2): (_interrupted(65517), None, "http://c.test:65517/second", ASKED),
    ("windows", 51): (None, "Page.goto: NS_BINDING_ABORTED", "http://b.test:51270/partial", ASKED),
}


@pytest.mark.parametrize("row", sorted(HUNT), ids=lambda row: "%s-%d" % row)
def test_every_outcome_of_the_hunt_is_green(row):
    first, second, url, asked = HUNT[row]
    assert second_goto_problem(first, second, url, asked) is None


def test_the_address_read_before_the_first_goto_landed_is_red():
    """Main dd4c623d, Linux: the first won (interrupted by the client, the second aborted) and the address was read before /partial
    committed. The scenario now waits for the landing; a page still at a.test after that wait landed on neither goto."""
    first = _interrupted(34183)
    problem = second_goto_problem(first, "Page.goto: NS_BINDING_ABORTED", "http://a.test:34183/plain", ASKED)
    assert problem is not None and "landed on neither goto" in problem


@pytest.mark.parametrize("first, second", [
    ("Timeout 30000ms exceeded.", "Page.goto: NS_BINDING_ABORTED"),
    (_interrupted(1), "Page.goto: Timeout 30000ms exceeded."),
    (_interrupted(1), "Page.goto: commit not reached in 30s"),
])
def test_a_goto_that_ended_at_its_timeout_is_red_whoever_won(first, second):
    for url in ("http://b.test:1/partial", "http://c.test:1/second"):
        problem = second_goto_problem(first, second, url, ASKED)
        assert problem is not None and "timeout" in problem


def test_a_second_request_that_never_left_is_red():
    problem = second_goto_problem(_interrupted(1), None, "http://c.test:1/second", ["/favicon.ico", "/partial", "/plain"])
    assert problem is not None and "never reached the server" in problem


def test_the_abort_of_the_replaced_first_taken_for_the_end_of_the_second_is_red():
    """The second won (the page is at c.test) but its goto ended NS_BINDING_ABORTED: the abort of the first, told for the second."""
    problem = second_goto_problem(_interrupted(1), "Page.goto: NS_BINDING_ABORTED", "http://c.test:1/second", ASKED)
    assert problem is not None and "landed on the second goto" in problem


def test_a_first_that_returned_a_commit_while_the_second_won_is_red():
    problem = second_goto_problem(None, None, "http://c.test:1/second", ASKED)
    assert problem is not None and "first did not end interrupted or aborted" in problem


def test_a_second_that_returned_a_commit_while_the_first_won_is_red():
    problem = second_goto_problem(_interrupted(1), None, "http://b.test:1/partial", ASKED)
    assert problem is not None and "returned a commit" in problem


def test_the_abort_of_the_process_left_taken_for_the_end_of_the_winning_first_is_red():
    problem = second_goto_problem("Page.goto: NS_BINDING_ABORTED", "Page.goto: NS_BINDING_ABORTED", "http://b.test:1/partial", ASKED)
    assert problem is not None and "abort of the process it left" in problem


def test_only_the_two_addresses_of_the_gotos_are_a_landing():
    assert landed_on("http://b.test:1/partial") == "first"
    assert landed_on("http://c.test:1/second") == "second"
    for url in ("http://a.test:1/plain", "about:blank", "", "http://b.test:1/held", "http://c.test:1/partial"):
        assert landed_on(url) is None, url


def _scenario() -> ast.AsyncFunctionDef:
    source = (Path(__file__).resolve().parent / "test_navigation_across_processes_e2e.py").read_text(encoding="utf-8")
    test = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
                and node.name == "test_a_second_goto_that_starts_while_the_first_changes_process_ends_both_gotos_and_one_of_them_lands")
    return next(node for node in ast.walk(test) if isinstance(node, ast.AsyncFunctionDef) and node.name == "scenario")


def _lines(scenario: ast.AsyncFunctionDef) -> dict:
    lines = {}
    for node in ast.walk(scenario):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "set" \
                and ast.unparse(node.func.value) == "sites['release']":
            lines.setdefault("release", node.lineno)
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Name) and node.value.id == "first_goto":
            lines.setdefault("first awaited", node.lineno)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "awatch" and "landed_on" in ast.unparse(node):
            lines.setdefault("landing awaited", node.lineno)
        if isinstance(node, ast.Return) and node.value is not None and "page.url" in ast.unparse(node.value):
            lines["address returned"] = node.lineno
    return lines


def test_the_scenario_reads_the_address_only_once_the_page_has_landed_after_the_release():
    """The order of the e2e scenario: the rest of /partial is sent once the second goto has returned (a first goto that wins and
    waits for its commit can return only after it), the first goto is awaited after that, and the address of the page is read
    after a wait for it to land. Red on the scenario of 0.53.0, which awaited both gotos before the release and read the address
    right after it."""
    lines = _lines(_scenario())
    for name in ("release", "first awaited", "landing awaited", "address returned"):
        assert name in lines, "the scenario has no %s: %r" % (name, lines)
    assert lines["release"] < lines["first awaited"] < lines["landing awaited"] < lines["address returned"], lines
