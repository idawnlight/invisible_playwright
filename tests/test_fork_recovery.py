import ast
import json
from email.message import Message
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def command(monkeypatch, *, url=None, repository=None, commit="a" * 40,
            version="0.43.1.dev0", pin="159.49.1", installer="pip"):
    metadata = Message()
    if repository:
        metadata["Project-URL"] = f"Repository, {repository}"
    direct = {"url": url, "vcs_info": {"vcs": "git", "commit_id": commit}} if url else None
    record = SimpleNamespace(
        version=version, metadata=metadata,
        requires=[f"invisible-core=={pin}"] if pin else [],
        read_text=lambda name: installer if name == "INSTALLER" else json.dumps(direct) if direct else None)
    monkeypatch.setattr("importlib.metadata.distribution", lambda name: record)
    source = Path(__file__).parents[1] / "src/invisible_playwright/_pin.py"
    tree = ast.parse(source.read_text())
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "reinstall_command")
    namespace = {"DIST_NAME": "invisible-playwright", "_sys": sys}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    return shlex.split(namespace["reinstall_command"]())


@pytest.mark.parametrize("installer", ["pip", "uv"])
def test_missing_core_recovery_includes_both_pinned_git_sources(monkeypatch, installer):
    result = command(monkeypatch, url="https://github.com/idawnlight/invisible_playwright.git",
                     installer=installer)
    assert result[-2:] == [
        "invisible-core @ git+https://github.com/idawnlight/invisible_core.git@v159.49.1",
        "invisible-playwright @ git+https://github.com/idawnlight/invisible_playwright.git@" + "a" * 40,
    ]
    assert result[:2] == (["uv", "pip"] if installer == "uv" else [sys.executable, "-m"])


def test_release_metadata_fixes_the_wrapper_to_its_own_version_tag(monkeypatch):
    result = command(monkeypatch, repository="https://github.com/idawnlight/invisible_playwright")
    assert result[-1].endswith("invisible_playwright.git@v0.43.1.dev0")
    assert result[-2].endswith("invisible_core.git@v159.49.1")


@pytest.mark.parametrize("url", [None, "https://github.com/feder-cr/invisible_playwright.git",
                               "https://github.com/idawnlight/invisible_playwright.git.evil.test",
                               "https://evil.test/idawnlight/invisible_playwright.git"])
def test_index_author_and_untrusted_sources_keep_the_legacy_command(monkeypatch, url):
    result = command(monkeypatch, url=url)
    assert result[-1] == "invisible-playwright"
    assert all("git+" not in part for part in result)


def test_fork_with_incomplete_pin_does_not_recommend_an_unpublished_index_package(monkeypatch):
    result = command(monkeypatch, url="https://github.com/idawnlight/invisible_playwright.git", pin=None)
    assert "README.md;" in result
    assert "install" not in result


def test_untrusted_requested_revision_cannot_become_shell_text(monkeypatch):
    result = command(monkeypatch, url="https://github.com/idawnlight/invisible_playwright.git",
                     commit="$(echo unexpected)")
    assert result[-1].endswith("invisible_playwright.git@v0.43.1.dev0")
    assert "unexpected" not in " ".join(result)
