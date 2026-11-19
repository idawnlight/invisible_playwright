"""scripts/run_real_sites.py imports the package of its own mode and no other (no browser: the packages are stubs in ``sys.modules``).

The venv of ``--browser ours`` holds ``invisible-playwright`` and not ``playwright`` (the first run of the round: ``ModuleNotFoundError: No module named 'playwright'``), and the
venv of ``--browser stock`` holds ``playwright`` and not ``invisible_playwright``. So, in each mode, the other package is made unimportable (a ``None`` entry in
``sys.modules`` raises ImportError) and the script runs end to end over a fake browser:

* the cells are written, ``complete`` is true, and the stub of the mode is the one that was used;
* the ``TimeoutError`` it catches is the one of its own package (the one ``invisible_playwright.sync_api`` exports is the one the client raises: see the note in the
  script), so a timeout is written ``timeout`` and not ``error:<class>``;
* the diagnosis of a cell without its marker has the status of the document and the first 40 ids, none of them empty.
"""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
SITES = [{"id": "fine", "url": "https://fine.example/", "marker": "#main", "class": "calma"},
         {"id": "slow", "url": "https://slow.example/", "marker": "#main", "class": "calma"},
         {"id": "nomarker", "url": "https://nomarker.example/", "marker": "#absent", "class": "calma"}]


def _runner():
    spec = importlib.util.spec_from_file_location("run_real_sites_under_test", ROOT / "scripts" / "run_real_sites.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Package:
    """The stub of one package: its own Error and TimeoutError, and a fake browser whose page raises ITS TimeoutError on the slow site."""

    def __init__(self, label):
        self.label = label
        self.Error = type("Error", (Exception,), {})
        self.TimeoutError = type("TimeoutError", (self.Error,), {})
        self.launched = 0
        package = self

        class Page:
            main_frame = object()

            def on(self, *_a):
                pass

            def goto(self, url, wait_until=None, timeout=None):
                if "slow" in url:
                    raise package.TimeoutError("Timeout %sms exceeded." % timeout)
                return types.SimpleNamespace(status=403 if "nomarker" in url else 200)

            def wait_for_load_state(self, *_a, **_k):
                pass

            def locator(self, selector):
                return types.SimpleNamespace(count=lambda: 0 if selector == "#absent" else 1)

            def title(self):
                return "a title"

            def evaluate(self, _script):
                return ["a", "", "b", ""] + ["id%d" % i for i in range(80)]    # what a page has: empty ids too, and many

        class Context:
            def new_page(self):
                return Page()

            def close(self):
                pass

        class Browser:
            version = "fake-1"

            def new_context(self, viewport=None):
                return Context()

            def close(self):
                pass

        self.browser = Browser()

    def launched_browser(self):
        self.launched += 1
        return self.browser


def _install(monkeypatch, mode):
    """The package of the mode is a stub, the other is unimportable. Returns the stub."""
    package = _Package(mode)
    if mode == "ours":
        top = types.ModuleType("invisible_playwright")
        api = types.ModuleType("invisible_playwright.sync_api")

        class InvisiblePlaywright:
            def __init__(self, **_kw):
                pass

            def __enter__(self):
                return package.launched_browser()

            def __exit__(self, *_a):
                return False

        top.InvisiblePlaywright = api.InvisiblePlaywright = InvisiblePlaywright
        api.Error, api.TimeoutError = package.Error, package.TimeoutError
        top.sync_api = api
        monkeypatch.setitem(sys.modules, "invisible_playwright", top)
        monkeypatch.setitem(sys.modules, "invisible_playwright.sync_api", api)
        blocked = ("playwright", "playwright.sync_api")
    else:
        top = types.ModuleType("playwright")
        api = types.ModuleType("playwright.sync_api")

        class Playwright:
            firefox = types.SimpleNamespace(launch=lambda **_kw: package.launched_browser())

        class Manager:
            def __enter__(self):
                return Playwright()

            def __exit__(self, *_a):
                return False

        api.sync_playwright = Manager
        api.Error, api.TimeoutError = package.Error, package.TimeoutError
        top.sync_api = api
        monkeypatch.setitem(sys.modules, "playwright", top)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", api)
        blocked = ("invisible_playwright", "invisible_playwright.sync_api")
    for name in blocked:
        monkeypatch.setitem(sys.modules, name, None)       # an import of it raises ImportError
    return package


def _run(tmp_path, monkeypatch, mode, extra=()):
    runner = _runner()
    package = _install(monkeypatch, mode)

    def version(name):      # the distribution of the other package is not there either
        if name == ("playwright" if mode == "stock" else "invisible-playwright"):
            return "0.0-test"
        raise runner.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(runner.metadata, "version", version)
    sites = tmp_path / "sites.json"
    sites.write_text(json.dumps({"sites": SITES}), encoding="utf-8")
    out = tmp_path / "cells.json"
    assert runner.main(["--browser", mode, "--sites", str(sites), "--out", str(out), *extra]) == 0
    return package, json.loads(out.read_text(encoding="utf-8"))


@pytest.mark.parametrize("mode", ["ours", "stock"])
def test_a_mode_imports_only_its_own_package_and_writes_its_cells(tmp_path, monkeypatch, mode):
    package, data = _run(tmp_path, monkeypatch, mode)
    assert package.launched == 1 and data["complete"] is True and data["browser"] == mode
    assert list(data["cells"]) == ["fine", "slow", "nomarker"]
    fine = data["cells"]["fine"]
    assert (fine["load"], fine["networkidle"], fine["status"], fine["marker"], fine["canary"]) == ("ok", "ok", "2xx", True, "ok")


@pytest.mark.parametrize("mode", ["ours", "stock"])
def test_a_timeout_is_written_timeout_because_the_class_is_the_one_of_the_mode(tmp_path, monkeypatch, mode):
    _package, data = _run(tmp_path, monkeypatch, mode)
    assert data["cells"]["slow"]["load"] == "timeout", data["cells"]["slow"]


@pytest.mark.parametrize("mode", ["ours", "stock"])
def test_the_diagnosis_has_the_status_of_the_document_and_at_most_forty_non_empty_ids(tmp_path, monkeypatch, mode):
    _package, data = _run(tmp_path, monkeypatch, mode, ["--diagnose"])
    diagnosis = data["cells"]["nomarker"]["diagnosis"]
    assert data["cells"]["nomarker"]["marker"] is False and data["cells"]["nomarker"]["status"] == "4xx"
    assert "http" not in diagnosis and diagnosis["http_status"] == 403 and diagnosis["title"] == "a title"
    assert len(diagnosis["ids"]) == 40 and all(diagnosis["ids"]) and diagnosis["ids"][:3] == ["a", "b", "id0"]
    assert "diagnosis" not in data["cells"]["fine"], "only a cell without its marker is diagnosed"
    _package, plain = _run(tmp_path, monkeypatch, mode)
    assert "diagnosis" not in plain["cells"]["nomarker"], "no diagnosis unless asked for"
