"""What was taken out of the wrapper does not come back.

Each entry below was a second way to reach something that has one place now: a
module that only re-exported the core, a name that only forwarded to it, a switch
for a path that no longer exists. They are asserted by name, because "it still
imports" is exactly how they survived: nothing broke while they sat there, so
nothing ever asked for them to go.

The import paths of the core are the ones to use: `invisible_core.<module>`.
"""
from __future__ import annotations

import importlib
import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.unit

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "invisible_playwright"

#: Modules of this package that only aliased, or re-exported, a module of the core.
_GONE_MODULES = (
    "_geo", "_headless", "_proxy", "_webgl_personas", "config", "constants",
    "download", "prefs", "_fpforge", "_reaper",
)


@pytest.mark.parametrize("name", _GONE_MODULES)
def test_a_module_that_only_forwarded_to_the_core_is_not_back(name):
    assert importlib.util.find_spec("invisible_playwright." + name) is None, (
        "invisible_playwright.%s exists again: the code is in the core "
        "(`_reaper` is `invisible_core.process`)" % name)
    assert not (_SRC / (name + ".py")).exists()
    assert not (_SRC / name).exists()


def test_no_module_of_the_package_hands_its_name_to_another_module():
    """The shape of every one of them: `sys.modules[__name__] = <the core's module>`."""
    offenders = [str(p.relative_to(_SRC)) for p in sorted(_SRC.rglob("*.py"))
                 if "_pw" not in p.relative_to(_SRC).parts
                 and "sys.modules[__name__]" in p.read_text(encoding="utf-8")]
    assert not offenders, offenders


def test_the_cursor_module_has_one_generator_and_two_engines():
    from invisible_playwright import _cursor

    for name in ("ENGINE_BINARY", "MIN_EVENT_INTERVAL_MS", "_build_profile",
                 "_MOTION_FACTORY_NAMES", "motion_available"):
        assert not hasattr(_cursor, name), "_cursor.%s is back" % name
    assert sorted(n for n in dir(_cursor) if n.startswith("ENGINE_") and n != "ENGINE_ENV") == [
        "ENGINE_OFF", "ENGINE_PYTHON"]
    assert "MIN_EVENT_INTERVAL_MS" not in _cursor.__all__
    assert "ENGINE_BINARY" not in _cursor.__all__


def test_the_generator_is_imported_directly_and_has_no_fallback():
    """The core is pinned with `==`: an install without the generator fails at import."""
    import ast

    from invisible_playwright import _cursor

    source = pathlib.Path(_cursor.__file__).read_text(encoding="utf-8")
    assert "_MOTION_IMPORT_ERROR" not in source
    assert "no-motion" not in source
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Try):
            text = ast.get_source_segment(source, node) or ""
            assert "invisible_core" not in text, "the import of the generator is guarded again"
    for name in ("MotionProfile", "new_motion"):
        assert name not in source
    import invisible_core.juggler as core_juggler
    assert _cursor._motion_mod is core_juggler
    assert _cursor._motion_factory() is core_juggler.CursorMotion


@pytest.mark.parametrize("value", ["binary", "juggler", "none", "false", "0"])
def test_the_engine_variable_takes_no_alias(monkeypatch, value):
    from invisible_playwright import _cursor

    monkeypatch.setenv(_cursor.ENGINE_ENV, value)
    with pytest.raises(ValueError, match="python"):
        _cursor.resolve_cursor_engine(True)


def test_the_session_module_does_not_know_a_third_engine():
    from invisible_playwright import _session

    assert not hasattr(_session, "ENGINE_BINARY")


def test_the_transport_module_has_no_driver_switch():
    from invisible_playwright._juggler import transport

    for name in ("CHOICE_ENV", "DRIVER", "JUGGLER", "chosen"):
        assert not hasattr(transport, name), "transport.%s is back" % name


def test_the_package_root_does_not_re_export_what_the_core_dropped():
    import invisible_playwright as root

    assert not hasattr(root, "resolve_session_timezone")
    assert "resolve_session_timezone" not in root.__all__


def test_no_script_reads_the_driver_tree_switch():
    scripts = pathlib.Path(__file__).resolve().parents[1] / "scripts"
    text = (scripts / "capture_protocol.py").read_text(encoding="utf-8")
    assert "INVPW_TRANSPORT" not in text
    assert "INVPW_DRIVER_TREE" not in text


def test_the_wrapper_names_the_core_by_the_names_it_has():
    """The three names the wrapper takes from the core under their final address."""
    core = importlib.import_module("invisible_core")
    assert callable(core.ensure_geoip_mmdb)
    assert callable(importlib.import_module("invisible_core.download").cache_dir_for_seal)
    assert callable(importlib.import_module("invisible_core.process").guard_for)


#: Names the core 66.76.0 took out. A wrapper file that still imports one of them
#: imports fine against the core it is pinned to and breaks on the next one: this
#: is how `tests/conftest.py` kept `BINARY_ENTRY_REL` and turned the core's
#: `wrapper-on-this-core` job red on every e2e fixture.
_NAMES_THE_CORE_REMOVED = frozenset({
    "BINARY_ENTRY_REL", "tz_env", "BROKEN_VERSIONS", "cache_dir_for_version",
    "resolve_session_timezone", "_parse_checksums",
})
#: Names that are only gone from one module of the core (the root still has them).
_GONE_FROM_DOWNLOAD = frozenset({"geoip_mmdb_path", "ensure_geoip_mmdb"})


def _imports_of_removed_core_names(tree):
    """Every import, and every `module.name` access, of a name the core removed."""
    import ast

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                if alias.name in _NAMES_THE_CORE_REMOVED:
                    found.append((node.lineno, "from %s import %s" % (module, alias.name)))
                if module == "invisible_core.download" and alias.name in _GONE_FROM_DOWNLOAD:
                    found.append((node.lineno, "from invisible_core.download import %s" % alias.name))
                if module == "invisible_core" and alias.name == "_pin":
                    found.append((node.lineno, "from invisible_core import _pin"))
            if module == "invisible_core._pin":
                found.append((node.lineno, "from invisible_core._pin import ..."))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "invisible_core._pin":
                    found.append((node.lineno, "import invisible_core._pin"))
        elif isinstance(node, ast.Attribute):
            if node.attr in _NAMES_THE_CORE_REMOVED:
                found.append((node.lineno, "." + node.attr))
            elif (node.attr in _GONE_FROM_DOWNLOAD and isinstance(node.value, ast.Name)
                  and node.value.id == "download"):
                found.append((node.lineno, "download." + node.attr))
    return found


def test_no_file_of_the_wrapper_imports_a_name_the_core_removed():
    """Source, tests, scripts and CI helpers alike: the tests are where it hid."""
    import ast

    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for top in ("src", "tests", "scripts", "ci", "examples"):
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            offenders += ["%s:%d %s" % (path.relative_to(root), line, what)
                          for line, what in _imports_of_removed_core_names(tree)]
    assert not offenders, (
        "these names are not in invisible-core 66.76.0; import them from where "
        "the core keeps them now: %s" % offenders)


@pytest.mark.parametrize("source", [
    "from invisible_core.constants import BINARY_ENTRY_REL",
    "from invisible_core import tz_env",
    "from invisible_core.constants import BROKEN_VERSIONS",
    "from invisible_core.download import cache_dir_for_version",
    "from invisible_core import resolve_session_timezone",
    "import invisible_core._pin",
    "from invisible_core import _pin",
    "from invisible_core._pin import report",
    "from invisible_core.download import geoip_mmdb_path",
    "import invisible_core.constants as c\nc.BINARY_ENTRY_REL",
    "download.geoip_mmdb_path()",
    "x._parse_checksums(text)",
])
def test_the_scan_for_removed_core_names_catches_each_known_bad_import(source):
    import ast

    assert _imports_of_removed_core_names(ast.parse(source)), source


@pytest.mark.parametrize("source", [
    "from invisible_core.seal import DEFAULT_ENTRY_REL",
    "from invisible_core import ensure_geoip_mmdb",
    "from invisible_core.download import cache_dir_for_seal",
    "from invisible_core import pin",
])
def test_the_scan_for_removed_core_names_leaves_the_names_the_core_has(source):
    import ast

    assert not _imports_of_removed_core_names(ast.parse(source)), source


def test_no_stub_script_of_the_wrapper_imports_the_removed_pin_alias():
    """The AST scan does not read inside a string, and `tests/test_seal_floor.py`
    runs a stub core in a subprocess whose code is one: it imported the `_pin`
    alias the core 66.76.0 removed, and only the pre-push run of the release
    branch said so. A line of source text that imports it is refused too."""
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    here = pathlib.Path(__file__).resolve()
    pattern = re.compile(r"(?m)^\s*(?:from\s+invisible_core\s+import\s+_pin\b|"
                         r"(?:from|import)\s+invisible_core\._pin\b)")
    offenders = []
    for top in ("src", "tests", "scripts", "ci", "examples"):
        for path in sorted((root / top).rglob("*.py")) if (root / top).is_dir() else []:
            if path.resolve() == here or "__pycache__" in path.parts:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.match(line):
                    offenders.append("%s:%d" % (path.relative_to(root), number))
    assert not offenders, offenders
