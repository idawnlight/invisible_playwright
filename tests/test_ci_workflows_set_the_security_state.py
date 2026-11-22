"""The workflows that run the wrapper's tests launch with the ready security state of ci/security-state/, so the core asks Mozilla for nothing and runs no generation.

Without STEALTHFOX_SECURITY_STATE the core downloads Mozilla's security-state collections on a cold cache (`invisible_playwright fetch` and the first launch) and has the
engine generate the state from them. tests.yml, fakeweb.yml and e2e.yml (its Linux run, its Windows parts and the Windows job of Control+A) set the variable
(scripts/ci_lab_security_state.py set) ONCE, before the step that runs pytest and, where the leg has one, before the step that fetches the engine (`fetch` makes the state
present, so it would be the first to download and generate).
user-install.yml proves the installation a user gets and is left as it is.
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
SET = "python scripts/ci_lab_security_state.py set --pin ci/security-state.pin.json"
FETCH = "      - name: Fetch the published firefox binary\n"
RUNS_PYTEST = {
    "tests.yml": "      - name: Run pytest\n",
    "e2e.yml": "      - name: Run the full e2e suite under a virtual display, without the root ones\n",
    "e2e.yml#windows": "      - name: Run this part of the e2e\n",
    "e2e.yml#control-a": "      - name: The keyboard shortcut selects all",
    "fakeweb.yml": "      - name: Profile P2, stock first and ours second",
}


def _scope(name):
    """The text of one workflow, or of one leg of it: `e2e.yml` is its Linux job, `e2e.yml#windows` its Windows parts, `e2e.yml#control-a` the
    Windows job of the keyboard shortcut (it fetches and launches too)."""
    file, _, leg = name.partition("#")
    text = (WORKFLOWS / file).read_text(encoding="utf-8")
    if file != "e2e.yml":
        return text
    windows = text.index("\n  e2e-windows:\n")
    if leg == "windows":
        return text[windows:]
    if leg == "control-a":
        start = text.index("\n  e2e-windows-control-a:\n")
        return text[start:windows] if start < windows else text[start:]
    ends = [i for i in (text.find("\n  e2e-windows-control-a:\n"), windows) if i >= 0]
    return text[:min(ends)]


@pytest.mark.parametrize("name, pytest_step", sorted(RUNS_PYTEST.items()))
def test_the_security_state_is_set_once_before_the_step_that_runs_the_tests(name, pytest_step):
    text = _scope(name)
    assert text.count(SET) == 1, "%s sets the security state %d times" % (name, text.count(SET))
    assert pytest_step in text, "the step that runs the tests of %s moved: update this test with it" % name
    assert text.index(SET) < text.index(pytest_step), "%s sets the security state after the tests started" % name


@pytest.mark.parametrize("name", ["e2e.yml", "e2e.yml#windows", "e2e.yml#control-a", "fakeweb.yml"])
def test_the_security_state_is_set_before_the_engine_is_fetched(name):
    text = _scope(name)
    assert FETCH in text, "the fetch step of %s moved: update this test with it" % name
    assert text.index(SET) < text.index(FETCH), "%s fetches before the security state is set: `fetch` would download Mozilla's and generate" % name


def test_the_workflow_that_proves_the_installation_is_left_alone():
    assert SET not in (WORKFLOWS / "user-install.yml").read_text(encoding="utf-8")
