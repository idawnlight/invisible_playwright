"""engine_at_least (tests/conftest.py): which engines a test runs on. No browser."""
import pytest
from _pytest.outcomes import Failed, Skipped

import conftest

pytestmark = pytest.mark.unit


def _check(tag, minimum="firefox-64"):
    conftest._skip_unless_engine_at_least(minimum, tag)


def test_an_older_engine_is_skipped_with_the_reason():
    with pytest.raises(Skipped, match="needs firefox-64 or later; this engine is firefox-58"):
        _check("firefox-58")


@pytest.mark.parametrize("tag", ["firefox-64", "firefox-64-candidate", "firefox-65"])
def test_the_engine_that_promises_it_runs(tag):
    _check(tag)


def test_a_candidate_counts_as_its_number():
    with pytest.raises(Skipped):
        _check("firefox-59-candidate")


@pytest.mark.parametrize("tag", ["local", "my-build", "", None])
def test_a_seal_with_no_number_fails_and_says_why(tag):
    with pytest.raises(Failed, match="has no number: a candidate must be sealed as firefox-N-candidate"):
        _check(tag)


@pytest.mark.parametrize("tag", ["firefox-61", "firefox-61-candidate", "firefox-62", "firefox-62-candidate",
                                 "firefox-63", "firefox-63-candidate"])
def test_firefox_62_and_63_do_not_run_what_the_64_promises(tag):
    """firefox-62 is the Fission release (one process per site) and firefox-63 the coherent UDP; the shelved
    line with the idwr text, the colours, the maths and the media goals is firefox-64. A test that asks for 64
    must skip on 63 and 62 (and on 61), or they would run goals that line has not shipped."""
    with pytest.raises(Skipped, match="needs firefox-64 or later; this engine is " + tag):
        _check(tag)


# ---------------------------------------------------------------------------
# The gates of the release being made and of the line after it.
#
# A gate (`engine_at_least("firefox-N")`, `engine_reaches("firefox-N")`) says which release carries the property a test waits for. The line
# that piles up is always named after the release AFTER the open one, and when a release is cut the gates have to follow its definition: a gate
# that still names the release being made makes the candidate of that release run a test of a property it does not carry (red for a reason that
# is not the code). `tests/data/engine_gates.json` states, for each release from the open one on, what it carries and which test files wait
# for it, each with the property; this holds the source against it.
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "tests" / "data" / "engine_gates.json"
#: The first release the registry answers for: a gate below it names an engine already published and is not judged here.
FIRST_REGISTERED = 68
_GATES = ("engine_at_least", "engine_reaches")


def gates_in(source):
    """[(line, name, N or None)] for each call of a gate: N is None when the argument is neither a ``"firefox-N"`` literal nor a module constant of one."""
    tree = ast.parse(source)
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name not in _GATES:
            continue
        arg = node.args[0] if node.args else None
        value = arg.value if isinstance(arg, ast.Constant) else constants.get(arg.id) if isinstance(arg, ast.Name) else None
        m = conftest._ENGINE_TAG.match(value) if isinstance(value, str) else None
        found.append((node.lineno, name, int(m.group(1)) if m else None))
    return sorted(found)


def gate_problems(sources, registry):
    """What is wrong between the gates of ``sources`` ({path: text}) and ``registry`` ({"firefox-N": {"files": {path: property}}})."""
    out = []
    seen = {}
    for path, text in sorted(sources.items()):
        for line, name, number in gates_in(text):
            if number is None:
                out.append("%s:%d: %s does not name a literal firefox-N" % (path, line, name))
            elif number >= FIRST_REGISTERED:
                seen.setdefault(path, {}).setdefault(number, line)
    for path, by_number in sorted(seen.items()):
        for number, line in sorted(by_number.items()):
            if path not in registry.get("firefox-%d" % number, {}).get("files", {}):
                where = [r for r, row in sorted(registry.items()) if path in row.get("files", {})]
                out.append("%s:%d: waits for firefox-%d, and the registry %s" % (
                    path, line, number,
                    "puts it in %s (the property it proves is there)" % ", ".join(where) if where
                    else "does not know it: state its property under that release"))
    for release, row in sorted(registry.items()):
        number = int(release.split("-")[1])
        for path in sorted(row.get("files", {})):
            if number not in seen.get(path, {}):
                out.append("%s: the registry has it in %s, and it has no gate on that release" % (path, release))
    return out


def _repo_sources():
    return {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in sorted((ROOT / "tests").glob("test_*.py"))}


def _registry():
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def test_every_test_waits_for_the_release_that_carries_its_property():
    """The state of the repository: every gate from firefox-68 on is registered under its release, and every registered file has its gate."""
    assert gate_problems(_repo_sources(), _registry()) == []


def test_the_release_being_made_carries_only_its_own_properties():
    """firefox-68 holds the tests of the property it carries (networkidle: the held cut of a committed navigation); no later release has a row until
    a test waits for it."""
    registry = _registry()
    assert sorted(registry) == ["firefox-68"]
    assert sorted(registry["firefox-68"]["files"]) == ["tests/test_a_page_that_moves_on_e2e.py", "tests/test_fakeweb_p2_e2e.py"]


def test_a_test_of_the_accumulated_line_gated_on_the_release_being_made_is_named():
    """KNOWN BAD: a test of the line after the open release that still names the open release (the registry puts it under the later one)."""
    registry = {"firefox-68": {"files": {"tests/test_a_page_that_moves_on_e2e.py": "p"}},
                "firefox-69": {"files": {"tests/test_the_line_after.py": "q"}}}
    source = 'def test_a(engine_at_least):\n    engine_at_least("firefox-68")\n'
    problems = gate_problems({"tests/test_the_line_after.py": source}, registry)
    assert any("test_the_line_after.py:2: waits for firefox-68, and the registry puts it in firefox-69" in p for p in problems), problems
    assert any("test_a_page_that_moves_on_e2e.py: the registry has it in firefox-68, and it has no gate" in p for p in problems), problems


def test_a_gate_the_registry_does_not_know_is_named():
    """KNOWN BAD: a new test gated on a release the registry has no row for says what to state."""
    source = 'def test_a(engine_reaches):\n    engine_reaches("firefox-70")\n'
    problems = gate_problems({"tests/test_new.py": source}, _registry())
    assert any("test_new.py:2: waits for firefox-70, and the registry does not know it" in p for p in problems), problems


def test_a_gate_read_through_a_constant_or_that_cannot_be_read_is_judged_too():
    constant = 'GATE = "firefox-69"\n\ndef test_a(engine_at_least):\n    engine_at_least(GATE)\n'
    assert gates_in(constant) == [(4, "engine_at_least", 69)]
    unreadable = 'def test_a(engine_at_least, n):\n    engine_at_least(n)\n'
    assert gate_problems({"tests/test_x.py": unreadable}, {}) == ["tests/test_x.py:2: engine_at_least does not name a literal firefox-N"]
    # an engine already published is not judged here
    assert gate_problems({"tests/test_old.py": 'def test_a(engine_at_least):\n    engine_at_least("firefox-62")\n'}, {}) == []
