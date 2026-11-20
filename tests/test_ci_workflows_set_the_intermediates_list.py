"""The workflows that run the wrapper's tests launch with the intermediates list of ci/intermediates.json, so the core asks Mozilla for nothing.

Without STEALTHFOX_INTERMEDIATES the core downloads Mozilla's intermediates list on a cold cache (`invisible_playwright fetch` and the first launch) and checks
it once a week. tests.yml and e2e.yml (its Linux run, its Windows parts and the Windows job of Control+A) set the variable (scripts/ci_lab_intermediates.py set) ONCE, before the step that
runs pytest and, where the leg has one, before the step that fetches the engine (`fetch` makes the list present, so it would be the first to download).
user-install.yml proves the installation a user gets and is left as it is.
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
SET = "python scripts/ci_lab_intermediates.py set --pin ci/intermediates.pin.json"
FETCH = "      - name: Fetch the published firefox binary\n"
RUNS_PYTEST = {
    "tests.yml": "      - name: Run the default suite\n",
    "e2e.yml": "      - name: Run the full e2e suite under a virtual display\n",
}


def _scope(name):
    """The text of one workflow."""
    file, _, _leg = name.partition("#")
    return (WORKFLOWS / file).read_text(encoding="utf-8")


@pytest.mark.parametrize("name, pytest_step", sorted(RUNS_PYTEST.items()))
def test_the_list_is_set_once_before_the_step_that_runs_the_tests(name, pytest_step):
    text = _scope(name)
    assert text.count(SET) == 1, "%s sets the intermediates list %d times" % (name, text.count(SET))
    assert pytest_step in text, "the step that runs the tests of %s moved: update this test with it" % name
    assert text.index(SET) < text.index(pytest_step), "%s sets the list after the tests started" % name


@pytest.mark.parametrize("name", ["e2e.yml"])
def test_the_list_is_set_before_the_engine_is_fetched(name):
    text = _scope(name)
    assert FETCH in text, "the fetch step of %s moved: update this test with it" % name
    assert text.index(SET) < text.index(FETCH), "%s fetches before the list is set: `fetch` would download Mozilla's" % name


def test_the_workflow_that_proves_the_installation_is_left_alone():
    assert SET not in (WORKFLOWS / "user-install.yml").read_text(encoding="utf-8")
