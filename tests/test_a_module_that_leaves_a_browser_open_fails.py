"""The check of tests/_browser_census.py is red on a module that leaks a browser and green on one that closes it.

Each case is a module of its own in a fresh pytest (the check is module level and the leak would otherwise count against this file).
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

TESTS = str(Path(__file__).resolve().parent)

_BUILD = """
    from invisible_playwright._juggler._threads import ThreadStarter
    def build():
        return ThreadStarter(on_error=lambda message: None)
"""


def _run(tmp_path, body):
    (tmp_path / "conftest.py").write_text("from _browser_census import _a_module_leaves_no_browser_open  # noqa\n")
    (tmp_path / "test_the_module.py").write_text(textwrap.dedent(_BUILD) + textwrap.dedent(body))
    env = dict(os.environ, PYTHONPATH=TESTS + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", str(tmp_path / "test_the_module.py")],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120,
    )


def test_a_module_that_never_closes_what_it_built_fails(tmp_path):
    done = _run(tmp_path, """
        def test_one():
            build()

        def test_two():
            build()
    """)
    out = done.stdout + done.stderr
    assert done.returncode != 0, out
    assert "left 2 browser(s) open: close their connection" in out, out
    assert "2 passed" in out and "1 error" in out, out  # the tests pass; the module is the one that fails


def test_a_module_that_closes_what_it_built_passes(tmp_path):
    done = _run(tmp_path, """
        import pytest

        @pytest.fixture
        def starter():
            built = build()
            yield built
            built.stop()

        def test_one(starter):
            pass

        def test_two(starter):
            pass
    """)
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert "2 passed" in out and "error" not in out, out


def test_a_browser_shared_by_the_tests_of_a_module_may_stay_open_until_the_module_ends(tmp_path):
    done = _run(tmp_path, """
        import pytest

        @pytest.fixture(scope="module")
        def shared():
            built = build()
            yield built
            built.stop()

        def test_one(shared):
            pass

        def test_two(shared):
            pass
    """)
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert "2 passed" in out, out


def test_the_census_counts_a_starter_whose_thread_has_not_begun(monkeypatch):
    """The starter's thread is begun without waiting for it, so right after the constructor it can be in no `threading.enumerate()`:
    the census must still count it (it missed both leaks of a module on a Linux runner, 3 of 3 jobs)."""
    import threading
    import time

    sys.path.insert(0, TESTS)
    import _browser_census as census
    from invisible_playwright._juggler._threads import ThreadStarter

    original = threading.Thread.start

    def slow_start(self):
        time.sleep(0.3)
        return original(self)
    monkeypatch.setattr(threading.Thread, "start", slow_start)

    before = len(census.starters_alive())
    starter = ThreadStarter(on_error=lambda message: None)
    try:
        assert len(census.starters_alive()) == before + 1
    finally:
        starter.stop()
    assert census.wait_until_back_to(before) == 0
