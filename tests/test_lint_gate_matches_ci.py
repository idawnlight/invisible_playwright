"""The pre-push lint gate checks the exact ruff rules enforced by CI tests.yml."""
from __future__ import annotations

import pathlib
import re
import shlex
import tomllib

import pytest

pytestmark = pytest.mark.unit

_REPO = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO / ".github" / "workflows" / "tests.yml"
_THE_REST = ["--no-cache", "--output-format", "concise", "."]


def _declared() -> str:
    data = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    value = data["tool"]["invisible"]["hooks"].get("lint")
    assert value, (
        "[tool.invisible.hooks] declares no `lint`, so the pre-push hook does "
        "not run the ruff rules enforced by tests.yml"
    )
    return ",".join(value) if isinstance(value, list) else value.replace(" ", "")


def _ci_commands() -> list[list[str]]:
    text = _WORKFLOW.read_text(encoding="utf-8")
    return [
        shlex.split(m.group(0))
        for m in re.finditer(r"python -m ruff check [^\r\n]*", text)
    ]


def test_the_hook_lints_the_rules_the_ci_job_lints() -> None:
    if not _WORKFLOW.is_file():
        pytest.skip("an installed copy has no workflow")
    commands = _ci_commands()
    assert len(commands) == 1, (
        f"expected exactly one `ruff check` step in tests.yml, found {len(commands)}"
    )
    cmd = commands[0]
    select = cmd[cmd.index("--select") + 1]
    assert select == _declared(), (
        f"tests.yml selects {select!r} but pyproject.toml declares lint = {_declared()!r}"
    )
    rest = cmd[cmd.index("--select") + 2:]
    assert rest == _THE_REST, (
        f"tests.yml runs ruff with {rest} after --select; the hook runs {_THE_REST}"
    )
