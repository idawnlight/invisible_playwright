"""The leak census runs from conftest in every job, including the ones that install pytest alone. There it must import nothing of the
package: the `version` job of the release pull request failed with `No module named 'invisible_core'` when the census imported
`invisible_playwright._juggler._threads` (which pulls `invisible_playwright._pin`)."""
import subprocess
import sys
import textwrap
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def test_a_census_in_a_process_that_never_loaded_the_package_imports_nothing_and_counts_nothing():
    script = textwrap.dedent("""
        import sys
        sys.path.insert(0, %r)
        class _NoPackage:
            def find_spec(self, name, path=None, target=None):
                if name == "invisible_core" or name.startswith(("invisible_core.", "invisible_playwright")):
                    raise ImportError("the census imported %%s" %% name)
                return None
        sys.meta_path.insert(0, _NoPackage())
        import _browser_census as census
        assert census.starters_alive() == [], census.starters_alive()
        assert census.wait_until_back_to(0, seconds=0) == 0
        loaded = sorted(m for m in sys.modules if m.startswith(("invisible_core", "invisible_playwright")))
        assert not loaded, loaded
        print("OK")
    """) % str(TESTS)
    done = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout.strip() == "OK", done.stdout + done.stderr
