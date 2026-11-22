"""The measurement of the fontconfig cache e2e: which opens count, and which cache is the bundle's, on fake strace lines.

The e2e (tests/test_the_parent_reads_the_bundle_from_fontconfig_s_cache_linux_e2e.py) asks that a warm parent reads the cache of the
bundle. Two defects of the measurement showed in the red runs 44701725683 and 44738141432 (the names below are theirs):

* a ``*.cache-*`` file of ANY folder passed as "the warm parent read the cache": the caches the red warm parent "read" were
  ``c855463f...`` (``/usr/share/fonts/X11``) and ``4c599c20...`` (``/usr/local/share/fonts``), not the bundle's ``aeb8dbde...``;
* an ``openat`` that strace left as "<unfinished ...>" had no result on its line and counted as an open that succeeded.
"""
import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

TESTS = Path(__file__).resolve().parents[1]
E2E = TESTS / "test_the_parent_reads_the_bundle_from_fontconfig_s_cache_linux_e2e.py"
BINARY = "/engine/firefox"
BUNDLE = "/opt/invisible-playwright/firefox-67_159.0_20261119174916/fonts"
BUNDLE_MD5 = hashlib.md5(BUNDLE.encode()).hexdigest()
XDG = "/tmp/pytest-of-runner/pytest-0/popen-gw3/test_the_warm_parent_reads_the0/xdg-cache"


def _module():
    sys.path.insert(0, str(TESTS))
    try:
        spec = importlib.util.spec_from_file_location("fontconfig_cache_e2e", E2E)
        module = importlib.util.module_from_spec(spec)
        sys.modules["fontconfig_cache_e2e"] = module
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(TESTS))
    return module


def _lines(*calls):
    """A strace log of a parent (pid 100) with the calls, as the test reads it (the epoch already off)."""
    return ['100 execve("%s", ["%s"], 0x0) = 0' % (BINARY, BINARY)] + list(calls)


def _opens(module, lines, under):
    role, _argv = module._roles(lines, BINARY)
    return module._opens_of_the_parent(lines, [0.0] * len(lines), role, 0, len(lines), under)


def test_an_open_that_strace_left_unfinished_has_the_result_of_its_resumed_line():
    module = _module()
    lines = _lines(
        '100 openat(AT_FDCWD, "%s/a.cache-9", O_RDONLY <unfinished ...>' % XDG,
        '101 getpid() = 101',
        '100 <... openat resumed>) = -1 ENOENT (No such file or directory)',
        '100 openat(AT_FDCWD, "%s/b.cache-9", O_RDONLY <unfinished ...>' % XDG,
        '100 <... openat resumed>) = 7')
    opens = _opens(module, lines, [XDG])
    assert [(o["path"].rsplit("/", 1)[1], o["ok"]) for o in opens] == [("a.cache-9", False), ("b.cache-9", True)]


def test_an_unfinished_open_with_no_resumed_line_is_not_counted_either_way():
    module = _module()
    lines = _lines('100 openat(AT_FDCWD, "%s/a.cache-9", O_RDONLY <unfinished ...>' % XDG,
                   '100 openat(AT_FDCWD, "%s/c.cache-9", O_RDONLY) = 5' % XDG)
    assert [(o["path"].rsplit("/", 1)[1], o["ok"]) for o in _opens(module, lines, [XDG])] == [("c.cache-9", True)]


def test_the_cache_of_the_bundle_is_the_one_named_by_the_md5_of_its_path_and_no_other():
    module = _module()
    prefixes = module._bundle_cache_prefixes([BUNDLE])
    assert prefixes == {hashlib.md5(BUNDLE.encode()).hexdigest()} and BUNDLE_MD5 in prefixes
    # green: the cache the cold launch wrote and the warm one read
    assert module._is_a_cache_of(XDG + "/fontconfig//" + BUNDLE_MD5 + "-le64.cache-9", prefixes)
    # red: the caches of system folders, which the old criterion counted as "the warm parent read the cache"
    for system in ("c855463f699352c367813e37f3f70ea7", "4c599c202bc5c08e2d34565a40eac3b2", "3830d5c3ddfd5cd38a049b759396e72e",
                   "e13b20fdb08344e0e664864cc2ede53d"):
        assert module._is_a_cache_of(XDG + "/fontconfig//%s-le64.cache-9" % system, prefixes) is False
    # what it is written through is not a read of the cache
    assert not module._is_a_cache_of(XDG + "/fontconfig//" + BUNDLE_MD5 + "-le64.cache-9.NEW", prefixes)
    assert not module._is_a_cache_of(XDG + "/fontconfig//" + BUNDLE_MD5 + "-le64.cache-9.TMP-YhscHU", prefixes)


def test_the_name_fontconfig_prints_for_the_folder_is_a_name_of_the_bundle_s_cache_too():
    module = _module()
    printed = "cache: 0123456789abcdef0123456789abcdef-le64.cache-9 (dir: %s)\ncache: 4c599c202bc5c08e2d34565a40eac3b2-le64.cache-9 " \
              "(dir: /usr/local/share/fonts)" % BUNDLE
    prefixes = module._bundle_cache_prefixes([BUNDLE], printed)
    assert "0123456789abcdef0123456789abcdef" in prefixes and "4c599c202bc5c08e2d34565a40eac3b2" not in prefixes


def test_what_fontconfig_prints_is_read_from_the_writes_to_the_descriptor_1():
    module = _module()
    lines = _lines(
        '100 write(1, "cache: aeb8-le64.cache-9 (dir: %s)\\nFcCacheTimeValid dir \\"%s\\" cache checksum 1.0\\n", 120) = 120' % (BUNDLE, BUNDLE),
        '100 write(3, "not the output", 14) = 14')
    role, _argv = module._roles(lines, BINARY)
    text = module._fc_debug_text(lines, role)
    assert text.splitlines() == ["100 parent\tcache: aeb8-le64.cache-9 (dir: %s)" % BUNDLE,
                                 '100 parent\tFcCacheTimeValid dir "%s" cache checksum 1.0' % BUNDLE]
