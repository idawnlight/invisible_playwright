"""The parent reads the bundle of fonts from fontconfig's cache and does not scan it again (e2e, Linux, from firefox-66).

Why Linux only: fontconfig exists only there. On Windows the parent rebuilds a font from the path a text run carries with DirectWrite,
which opens the file by its path and keeps no index of a folder; there is nothing to scan and nothing to cache, and this test has no
Windows form (the row of ``ci/required-suites.json`` says so: ``linux_only``).

What the engine does. A screenshot sends each font of a text run to the parent as a path and a face index, and the parent turns it into
a font again with fontconfig (``UnscaledFontFontconfig::CreateFromFontDescriptor``). For fontconfig to know the files of the bundle the
parent gives it the folder ``<GRE>/fonts`` with ``FcConfigAppFontAddDir`` (``gfxFcPlatformFontList::ActivateBundledFonts``, which the
shared font list of the parent calls first). On a COLD cache fontconfig opens and scans every file of the folder (about 121) and writes a
cache under ``$XDG_CACHE_HOME/fontconfig``. On a WARM cache it must read that cache (a ``*.cache-*`` file) and NOT open the files again for
the scan: it is the difference between a start that reads 121 fonts and a start that reads one file.

The measurement. Two launches of the same binary with the same ``XDG_CACHE_HOME``, a new folder of the test: the first is cold, the second
warm. Each runs under ``strace -f -ttt`` (the stand-in of ``tests/_strace_stand_in.py``) that traces the opens of the browser. The environment reaches the browser as it does for a user: the test sets
``XDG_CACHE_HOME`` in its own environment, and the core composes the engine's environment from it (``build_launch_env``: it removes the
host's ``MOZ_*``, ``LANG``, ``TZ`` and the toolkit families, and nothing else); that the variable did arrive is shown by the cache that is
written under it.

Scan or drawing: the criterion. FreeType also opens a file of the bundle (``FT_New_Face`` from the path) when a text run is DRAWN, in the
parent for the screenshot and in the content process for the page: those opens are not a scan and they happen in the warm launch too. The
scan is made by the parent while it builds the font list, before there is a page. So the test counts only what the PARENT process opens
between the start of the browser and the moment the wrapper has returned the launched browser, before any page is opened (``new_page``
is not yet called). The point is a position in the strace log (its size when the launch returns), not a time: the log is written line by
line, and everything the parent did to build its font list is before it. The opens after it (the page with every family of the
screenshot test, and the screenshot) are measured as well and written to the evidence, as ``after_startup``, but the assertion does not
read them.

Why "all or none" and not "none that the cold one opened". Even on a warm cache the parent opens a few files of the bundle before the
first page, to USE them (the face blob of a font of its own interface is opened and mapped); those files are also in the cold set, so a
test that asks for an empty intersection would be red for a good engine. What a scan is, is different: fontconfig scans a folder as a
whole (``FcDirCacheScan`` opens every file of it) or reads the cache of the folder and opens none for the scan. So the test asks about
the whole folder, as the disk lists it (no number written by hand):

  * the validity of the measurement: the cold parent opened EVERY font file of ``<GRE>/fonts`` in the window, and fontconfig wrote its
    cache under ``$XDG_CACHE_HOME/fontconfig``; otherwise the window does not see the scan and the test says it cannot judge;
  * the assertion, the only one that judges: in the same window the warm parent does NOT open every font file of the folder, and it
    opens a ``*.cache-*`` file under ``$XDG_CACHE_HOME/fontconfig``.

The files of the folder that are not fonts (a ``.list``, a README) are left out of the list, explicitly: a font is a file whose name ends
in one of ``FONT_EXTENSIONS`` (what fontconfig reads through FreeType); the evidence names the files that were left out. The files the
warm parent opened are in the evidence, by name. No threshold: a set that is not the whole folder, and a file that is read.

Evidence, whatever the verdict: when ``RUNNER_TEMP`` is set the test writes ``result.json`` and, for each launch, the opens of the bundle
by the parent with their time, under ``$RUNNER_TEMP/invpw-evidence/fontconfig-cache/``. For the cold and the warm launch (and for a
third, "before", see below): the interval in seconds between the first and the last open of a file of ``<GRE>/fonts`` by the parent, how
many opens, how many distinct files, the name and size of each file of fontconfig's cache, the ``VmRSS`` of the parent after
``about:blank`` and after a page with every family of the screenshot test (drawn and screenshotted).

The third launch, "before", is evidence only: the engine with ``gfx.bundled-fonts.activate`` = 0 never shows the folder to fontconfig (the
pref is Mozilla's, ``mirror: once``, read at start; ``ActivateBundledFonts`` is under ``!= 0``), which is what the parent did before the
correction. The core does not declare that pref anywhere, so the caller's ``extra_prefs`` (applied last, ``_apply_caller_overlay``) is
the only place that says it. If that launch fails, its error is in the evidence and nothing else changes.

What is a cache "of the bundle", and what is an open that happened. fontconfig names the cache of a folder by the md5 of the folder's
path (``<md5>-le64.cache-9``), so the cache of the bundle is the file whose name starts with the md5 of ``<GRE>/fonts`` (and, in the same
launch, with the name fontconfig itself prints for that folder, ``FC_DEBUG=16``); a ``*.cache-*`` file of a system folder
(``/usr/share/fonts``...) is a different cache and proves nothing about the bundle. An open counts only when its result is on the log:
strace splits a call that another thread interrupts in "PID openat(... <unfinished ...>" and "PID <... openat resumed>) = -1 ENOENT", the
result is on the second line, and an unfinished line with no resumed line has no result and is not counted either way.

The evidence of a launch (when ``RUNNER_TEMP`` is set) also holds what fontconfig says: the browser runs with ``FC_DEBUG=16`` (what it
does with caches: the folders it examines, the caches it finds valid, the writes), strace saves the writes to the descriptors 1 and 2
(``fc-debug-<launch>.txt``), and ``trace-<launch>.tsv`` has every open of a ``*.cache-*`` file or of a ``CACHEDIR.TAG`` by any process, in any
folder, and every line that names the bundle's folder or the cache folder: where fontconfig found a valid cache is on it.

Run: pytest tests/test_the_parent_reads_the_bundle_from_fontconfig_s_cache_linux_e2e.py -m e2e -v
"""
from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from _waits import wait_until
from test_a_screenshot_draws_the_text_of_the_bundled_fonts_as_the_content_does_e2e import EVIDENCE_FOLDER, FAMILIES
from _strace_stand_in import (
    _detached, _group_members, _group_of, _kill_group, _LINE, _require_strace_can_trace, _RESUMED, _roles, _stand_in, _wait_for_exit)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="fontconfig is the Linux font system"),
]

#: Opens, and what is needed to tell the parent from a content process (``_roles`` reads the execve and the clones).
TRACE = "execve,clone,clone3,fork,vfork,openat,openat2,write"

#: What fontconfig prints with ``FC_DEBUG=16`` goes to the descriptor 1 (the core reads it as the browser's output, and keeps 200 lines): the
#: strace log also holds the ``write`` calls, and ``_fc_debug_text`` keeps those to the descriptors 1 and 2. NOT ``--trace-fds=1,2``: it
#: filters EVERY traced call by descriptor, so the execve, clone and openat lines (which the parent's identity and the opens are read
#: from) are gone and the test saw no parent (candidate run 44749623531).
FC_DEBUG = "16"

SEED = 21

#: What counts as a font of the bundle in the folder ``<GRE>/fonts``: the extensions of the files fontconfig reads through FreeType. Any
#: other file of the folder (a ``.list``, a README) is not a font and is left out of the list, and the evidence says which ones.
FONT_EXTENSIONS = (".ttf", ".ttc", ".otf", ".otc", ".pfb", ".pfa", ".pcf", ".pcf.gz", ".bdf", ".woff", ".woff2", ".pfr", ".fon", ".fnt")

#: ``strace -f -ttt``: "PID EPOCH call(...". ``_LINE`` and ``_roles`` read "PID call(...", so the epoch is taken off first and kept apart.
_STAMPED = re.compile(r"^(\d+)\s+(\d+\.\d+)\s+(.*)$")
_QUOTED = re.compile(r'"([^"]*)"')
_RETURN = re.compile(r"\)\s+=\s+(-?\d+)")

#: One line of text for each family, in the writing it draws (MS PGothic and NSimSun are a face of a collection that is not the first).
_SAMPLES = {
    "MS PGothic": "日本語のテキスト ABC abc ｱｲｳｴｵ",
    "NSimSun": "中文汉字测试 ABC abc 0123",
}
_DEFAULT_SAMPLE = "Hamburgefonstiv 0123456789 éèàü €"


def _page_with_every_family() -> str:
    rows = "".join(
        '<div style="font-family:\'%s\';font-size:20px;line-height:30px;white-space:nowrap">%s</div>'
        % (family, _SAMPLES.get(family, _DEFAULT_SAMPLE))
        for family in FAMILIES)
    return '<!doctype html><meta charset="utf-8"><body style="margin:8px;background:#fff;color:#000">' + rows


def _evidence_dir():
    base = os.environ.get("RUNNER_TEMP")
    return Path(base) / EVIDENCE_FOLDER / "fontconfig-cache" if base else None


def _vmrss_kb(pid: int) -> int:
    for line in Path("/proc/%d/status" % pid).read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise AssertionError("no VmRSS in /proc/%d/status" % pid)


def _read(log: Path, real: str, boundary: int = None):
    """``(lines, times, role, argv_of, start)`` of a strace log: the lines without their epoch, the epoch of each, the role of a pid,
    the execve of each, and the index of the first line that is NOT in the window of the start (``boundary`` is the size of the log
    when the launch returned; None: the whole log is the window)."""
    data = log.read_bytes()
    lines, times = [], []
    for raw in data.decode("utf-8", errors="replace").splitlines():
        m = _STAMPED.match(raw)
        if m:
            lines.append("%s %s" % (m.group(1), m.group(3)))
            times.append(float(m.group(2)))
        else:
            lines.append(raw)
            times.append(None)
    start = len(lines)
    if boundary is not None:
        head = data[:boundary]
        start = len(head.decode("utf-8", errors="replace").splitlines())
        if head and not head.endswith(b"\n"):
            start -= 1  # a line the log had not finished writing is not in the window
    role, argv_of = _roles(lines, real)
    return lines, times, role, argv_of, start


def _parent_pid(argv_of: dict, real: str) -> int:
    parents = sorted(pid for pid, argv in argv_of.items() if real in argv and "-contentproc" not in argv)
    assert parents, "strace saw no execve of the browser's parent: the test cannot tell the parent from the rest"
    return parents[0]


def _results_of_unfinished(lines) -> dict:
    """``{index of an unfinished openat/openat2 line: its result}``: strace -f writes a call that another thread interrupts as
    "PID openat(...<unfinished ...>" and gives the result on the next line of that pid, "PID <... openat resumed>) = -1 ENOENT". An
    unfinished line with no resumed line has no entry."""
    pending, results = {}, {}
    for i, line in enumerate(lines):
        m = _LINE.match(line)
        if m and m.group(2) in ("openat", "openat2") and "<unfinished ...>" in m.group(3):
            pending[int(m.group(1))] = i
            continue
        r = _RESUMED.match(line)
        if r and r.group(2) in ("openat", "openat2") and int(r.group(1)) in pending:
            result = _RETURN.search(r.group(3))
            if result:
                results[pending.pop(int(r.group(1)))] = int(result.group(1))
    return results


def _open_result(lines, i, results):
    """The result of the open on ``lines[i]``: on the line itself, or on the resumed one; None when the log has none."""
    m = _LINE.match(lines[i])
    result = _RETURN.search(m.group(3)) if m else None
    return int(result.group(1)) if result else results.get(i)


def _opens_of_the_parent(lines, times, role, begin, end, under, results=None):
    """The opens (openat, openat2) by the parent, in ``lines[begin:end]``, of a path under one of ``under`` (folders):
    ``[{"t", "path", "ok"}]``, ``ok`` = the open succeeded. An open with no result on the log (an unfinished line with no resumed
    line) is left out: it is neither made nor refused as far as the log knows."""
    results = _results_of_unfinished(lines) if results is None else results
    found = []
    for i in range(begin, end):
        m = _LINE.match(lines[i])
        if not m or m.group(2) not in ("openat", "openat2"):
            continue
        quoted = _QUOTED.findall(m.group(3))
        if not quoted or not any(quoted[0].startswith(u.rstrip("/") + "/") for u in under):
            continue
        if role(int(m.group(1))) != "parent":
            continue
        result = _open_result(lines, i, results)
        if result is None:
            continue
        found.append({"t": times[i], "path": quoted[0], "ok": result >= 0})
    return found


def _bundle_cache_prefixes(folders, fc_debug_text: str = "") -> set:
    """The first part of the name of a cache OF THE BUNDLE: fontconfig names the cache of a folder by the md5 of its path; and the name
    fontconfig itself prints for the folder in the same launch (``cache: <name> (dir: <folder>)``), when it printed one."""
    prefixes = {hashlib.md5(f.rstrip("/").encode()).hexdigest() for f in folders}
    for f in folders:
        for name in re.findall(r"cache: (\S+) \(dir: %s\)" % re.escape(f.rstrip("/")), fc_debug_text):
            prefixes.add(name.split("-")[0])
    return prefixes


def _is_a_cache_of(path: str, prefixes) -> bool:
    """A cache file that is read: ``<md5>-<arch>.cache-<n>`` of one of ``prefixes`` (not the ``.NEW`` and ``.TMP-*`` it is written through)."""
    return re.match(r"^[0-9a-f]{32}-[^.]+\.cache-\d+$", os.path.basename(path)) is not None and os.path.basename(path).split("-")[0] in prefixes


def _fc_debug_text(lines, role) -> str:
    """What the browser printed to the descriptors 1 and 2 (``FC_DEBUG``), from the writes strace saved, with the pid and role of each."""
    out = []
    for line in lines:
        m = re.match(r'^(\d+)\s+write\([12], "(.*)"(?:\.\.\.)?, \d+', line)
        if m:
            text = codecs.escape_decode(m.group(2).encode("latin-1", errors="replace"))[0].decode("utf-8", errors="replace")
            out.extend("%s %s\t%s" % (m.group(1), role(int(m.group(1))), t) for t in text.splitlines() if t.strip())
    return "\n".join(out)


def _summary(opens):
    made = [o for o in opens if o["ok"]]
    stamps = [o["t"] for o in made if o["t"] is not None]
    return {
        "opens": len(made),
        "distinct_files": len({o["path"] for o in made}),
        "refused_opens": len(opens) - len(made),
        "first_to_last_s": round(max(stamps) - min(stamps), 6) if stamps else None,
    }


def _environ_of(pid: int, name: str):
    """The value of ``name`` in the environment the process ``pid`` was started with (``/proc/<pid>/environ``); None when it has none or
    the file cannot be read."""
    try:
        for item in Path("/proc/%d/environ" % pid).read_bytes().split(b"\0"):
            key, _eq, value = item.partition(b"=")
            if key == name.encode():
                return value.decode("utf-8", errors="replace")
    except OSError:
        return None
    return None


def _fontconfig_cache(xdg: Path):
    folder = xdg / "fontconfig"
    if not folder.is_dir():
        return []
    return sorted(({"name": f.name, "bytes": f.stat().st_size} for f in folder.iterdir() if f.is_file()), key=lambda f: f["name"])


def _the_folder(gre_fonts: str):
    """``(fonts, left_out)``: the names of the files of the folder as the DISK lists them, split by ``FONT_EXTENSIONS``."""
    names = sorted(e.name for e in os.scandir(gre_fonts) if e.is_file()) if os.path.isdir(gre_fonts) else []
    fonts = [n for n in names if n.lower().endswith(FONT_EXTENSIONS)]
    return fonts, [n for n in names if n not in fonts]


def _launch(firefox_binary, work: Path, xdg: Path, extra_prefs=None):
    """One launch of the binary under strace, with ``XDG_CACHE_HOME`` = ``xdg`` in the environment of the test. Returns what the log and
    /proc say; the browser is gone when it returns."""
    from invisible_playwright import InvisiblePlaywright

    work.mkdir(parents=True)
    log, pidfile = work / "strace.log", work / "stand-in.pid"
    stand_in = _stand_in(firefox_binary, work / "engine", log, pidfile, syscalls=TRACE, timestamps=True)
    real = os.path.realpath(firefox_binary)
    gre_fonts = os.path.join(os.path.dirname(real), "fonts")
    rss, boundary, pid, environ_xdg = {}, None, None, None
    try:
        with InvisiblePlaywright(seed=SEED, binary_path=stand_in, headless=True, extra_prefs=extra_prefs) as browser:
            # The launch has returned: the parent has built its font list. No page has been opened yet.
            boundary = log.stat().st_size
            _lines, _times, _role, argv_of, _start = _read(log, real, boundary)
            pid = _parent_pid(argv_of, real)
            environ_xdg = _environ_of(pid, "XDG_CACHE_HOME")
            page = browser.new_page()
            rss["after_about_blank_kb"] = _vmrss_kb(pid)
            page.set_content(_page_with_every_family())
            assert page.evaluate("() => document.body.getBoundingClientRect().width") > 0
            page.screenshot()
            rss["after_every_family_kb"] = _vmrss_kb(pid)
    finally:
        _kill_group(pidfile)

    lines, times, role, _argv_of, start = _read(log, real, boundary)
    under = sorted({gre_fonts, os.path.realpath(gre_fonts)})
    results = _results_of_unfinished(lines)
    scan = _opens_of_the_parent(lines, times, role, 0, start, under, results)
    later = _opens_of_the_parent(lines, times, role, start, len(lines), under, results)
    fc_debug = _fc_debug_text(lines, role)
    prefixes = _bundle_cache_prefixes(under, fc_debug)
    fonts, left_out = _the_folder(gre_fonts)
    opened = sorted({os.path.basename(o["path"]) for o in scan if o["ok"]})
    cache_dir = str(xdg / "fontconfig")
    # Evidence for a launch whose cache is missing: every line of the log, by ANY process, that names the bundle's folder, the cache
    # folder or a ``.uuid``, with the role of the process and the window it is in (the opens above count only the parent's, and only
    # in the window). The raw lines say who scanned (or did not), and where fontconfig wrote.
    trace = []
    for i, line in enumerate(lines):
        if any(u in line for u in under) or cache_dir in line or "/.uuid" in line:
            m = _LINE.match(line) or re.match(r"^(\d+)\s", line)
            who = role(int(m.group(1))) if m else "?"
            trace.append("%s\t%s\t%s\t%s" % (times[i], "startup" if i < start else "later", who, line[:400]))
    # The reads of the cache of the BUNDLE (not of a system folder), by the parent in the window: under the cache folder, and anywhere.
    cache_reads = [o for o in _opens_of_the_parent(lines, times, role, 0, start, [cache_dir], results)
                   if o["ok"] and _is_a_cache_of(o["path"], prefixes)]
    anywhere = []
    for i, line in enumerate(lines[:start]):
        m = _LINE.match(line)
        if m and m.group(2) in ("openat", "openat2") and role(int(m.group(1))) == "parent":
            quoted = _QUOTED.findall(m.group(3))
            if quoted and _is_a_cache_of(quoted[0], prefixes) and (_open_result(lines, i, results) or -1) >= 0:
                anywhere.append(quoted[0])
    # Every open of a cache file or of a CACHEDIR.TAG by any process, in any folder, with its result: where fontconfig found a valid cache.
    cache_opens = []
    for i, line in enumerate(lines):
        m = _LINE.match(line)
        if m and m.group(2) in ("openat", "openat2"):
            quoted = _QUOTED.findall(m.group(3))
            if quoted and (".cache-" in quoted[0] or "CACHEDIR.TAG" in quoted[0]):
                result = _open_result(lines, i, results)
                cache_opens.append("%s\t%s\t%s\t%s\t%s" % (
                    times[i], "startup" if i < start else "later", role(int(m.group(1))),
                    "no-result" if result is None else ("ok" if result >= 0 else "refused"), quoted[0]))
    trace = cache_opens + trace
    return {
        "parent_pid": pid,
        "parent_environ_xdg_cache_home": environ_xdg,
        "startup": _summary(scan),
        "after_startup": _summary(later),
        "cache_files_read_at_startup": sorted({o["path"] for o in cache_reads}),
        "bundle_cache_read_anywhere_at_startup": sorted(set(anywhere)),
        "bundle_cache_names": sorted(prefixes),
        "bundle_cache_written": [f for f in _fontconfig_cache(xdg) if _is_a_cache_of(f["name"], prefixes)],
        "fc_debug_about_the_bundle": [l for l in fc_debug.splitlines() if any(u in l for u in under)],
        "_fc_debug": fc_debug,
        "fontconfig_cache": _fontconfig_cache(xdg),
        "vmrss_kb": rss,
        "bundle_font_files": len(fonts),
        "files_left_out_as_not_fonts": left_out,
        "font_files_opened_at_startup": len([n for n in opened if n in fonts]),
        "font_files_not_opened_at_startup": [n for n in fonts if n not in opened],
        "files_opened_at_startup": opened,
        "_scan_opens": scan,
        "_later_opens": later,
        "_trace": trace,
    }


def _public(measure: dict) -> dict:
    return {k: v for k, v in measure.items() if not k.startswith("_")}


@pytest.mark.timeout(120, method="thread")
def test_the_stand_in_stays_in_the_engine_s_place_and_its_group_is_killed_whole(tmp_path):
    """The tool, without a browser. A fake engine (a script that starts a child and waits for it) is launched through the stand-in
    as the core launches the engine: the leader of a process group of its own (``_detached``). Once the fake engine says its child
    runs (a signal file, no pause), the process the launcher holds must be alive and be the fake engine's parent, strace: with the
    stand-in that exec'd ``setsid``, setsid forked (its caller led a group), its parent exited 0 at once, and the core saw the engine
    exit during startup (candidate 44161687675, job 134356188701). The group the cleanup kills is the launcher's, it holds strace and
    the fake engine's child, and nothing of it is left afterwards."""
    if shutil.which("strace") is None:
        pytest.fail("needs strace (apt install strace): without it the stand-in cannot be tried", pytrace=False)
    _require_strace_can_trace(tmp_path)
    real = tmp_path / "real" / "firefox"
    real.parent.mkdir()
    # The signal, written whole (a rename) once the child runs: the child's pid and the fake engine's parent, the process strace is.
    signal_file = tmp_path / "started"
    real.write_text('#!/bin/sh\nsleep 300 &\necho "$! $PPID" > %s.tmp && mv %s.tmp %s\nwait\n' % (signal_file, signal_file, signal_file))
    real.chmod(0o755)
    log, pidfile = tmp_path / "strace.log", tmp_path / "stand-in.pid"
    stand_in = _stand_in(str(real), tmp_path / "engine", log, pidfile)
    proc = _detached([stand_in], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        started = wait_until(signal_file.exists, 30, lambda: time.sleep(0.05))
        child, engine_parent = (int(word) for word in signal_file.read_text().split()) if started else (None, None)
        exited_with = proc.poll()
        pgid = _group_of(pidfile)
        members = _group_members(pgid) if pgid is not None else []
        comms = sorted(Path("/proc/%d/comm" % p).read_text().strip() for p in members if Path("/proc/%d/comm" % p).exists())
        killed = _kill_group(pidfile)
        proc.wait(timeout=30)
        left = _wait_for_exit(pgid) if pgid is not None else []
    finally:
        _kill_group(pidfile)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
    assert started, "the fake engine did not start its child under the stand-in"
    assert exited_with is None and engine_parent == proc.pid, (
        "the process the launcher holds (pid %d, exited with %s) is not the one that runs the engine (its parent is %s): the launcher would "
        "see the engine gone" % (proc.pid, exited_with, engine_parent))
    assert pgid == proc.pid and pgid != os.getpgrp(), "the group is not the one the launcher gave (group %s, pid %d)" % (pgid, proc.pid)
    assert "strace" in comms and child in members, "the group holds %s (%s), not strace and the child %s" % (members, comms, child)
    assert killed == pgid and not left, "after the kill of group %s: killed %s, left %s" % (pgid, killed, left)


@pytest.mark.timeout(900, method="thread")
def test_the_warm_parent_reads_the_bundle_from_fontconfig_cache_and_does_not_scan_it(firefox_binary, engine_at_least, tmp_path, monkeypatch):
    engine_at_least("firefox-66")
    if shutil.which("strace") is None:
        pytest.fail("needs strace (apt install strace): without it nothing is measured", pytrace=False)
    _require_strace_can_trace(tmp_path)

    xdg = tmp_path / "xdg-cache"
    xdg.mkdir()
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))
    monkeypatch.setenv("FC_DEBUG", FC_DEBUG)  # it only prints; the core passes it to the browser as it passes XDG_CACHE_HOME

    cold = _launch(firefox_binary, tmp_path / "cold", xdg)
    cold_cache = cold["fontconfig_cache"]
    warm = _launch(firefox_binary, tmp_path / "warm", xdg)

    # "Before": the folder is never shown to fontconfig. Its own cache folder, so that it is not warmed by the others.
    before_xdg = tmp_path / "xdg-cache-before"
    before_xdg.mkdir()
    monkeypatch.setenv("XDG_CACHE_HOME", str(before_xdg))
    try:
        before = _launch(firefox_binary, tmp_path / "before", before_xdg, extra_prefs={"gfx.bundled-fonts.activate": 0})
    except Exception as err:  # evidence only: a launch that fails is written down, it does not change the verdict
        before = {"error": "%s: %s" % (type(err).__name__, str(err)[:600])}

    result = {
        "test": "the-parent-reads-the-bundle-from-fontconfig-cache",
        "platform": sys.platform,
        "seed": SEED,
        "xdg_cache_home": str(xdg),
        "criterion": ("window = what the PARENT opens before any page is opened (the strace log up to its size when the launch returned; "
                      "the opens after it are drawing and are in after_startup). Valid: the cold parent opened every font file of "
                      "<GRE>/fonts and fontconfig wrote its cache. Verdict: the warm parent does not open every font file of the folder "
                      "and opens a *.cache-* file. Font files = the folder as the disk lists it, by FONT_EXTENSIONS"),
        "cold": _public(cold),
        "warm": _public(warm),
        "before_gfx_bundled_fonts_activate_0": _public(before) if "error" not in before else before,
    }
    where = _evidence_dir()
    if where is not None:
        where.mkdir(parents=True, exist_ok=True)
        (where / "result.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        for name, measure in (("cold", cold), ("warm", warm), ("before", before)):
            if "_scan_opens" in measure:
                rows = ["%s\t%s\t%s" % (o["t"], "ok" if o["ok"] else "refused", o["path"])
                        for o in measure["_scan_opens"] + measure["_later_opens"]]
                (where / ("bundle-opens-%s.tsv" % name)).write_text("\n".join(rows) + "\n", encoding="utf-8")
                (where / ("trace-%s.tsv" % name)).write_text("\n".join(measure["_trace"]) + "\n", encoding="utf-8")
                (where / ("fc-debug-%s.txt" % name)).write_text(measure["_fc_debug"] + "\n", encoding="utf-8")

    # The measurement is valid or the test says it cannot judge.
    assert cold["bundle_cache_written"], (
        "the cold launch wrote no cache of the bundle under XDG_CACHE_HOME=%s (there: %s; the names of a cache of the bundle: %s). "
        "XDG_CACHE_HOME in the parent's environment: %s. Cache of the bundle opened by the parent before a page, in any folder: %s. "
        "What fontconfig printed about the folder of the bundle (FC_DEBUG=16): %s. The window: %s. Nothing for a warm launch to read."
        % (xdg, [f["name"] for f in cold_cache] or "nothing", cold["bundle_cache_names"], cold["parent_environ_xdg_cache_home"],
           cold["bundle_cache_read_anywhere_at_startup"] or "none", cold["fc_debug_about_the_bundle"] or "nothing (or FC_DEBUG not saved)",
           cold["startup"]))
    assert not cold["font_files_not_opened_at_startup"] and cold["bundle_font_files"], (
        "the parent of the cold launch did not open every font file of <GRE>/fonts (%d of %d) before a page was opened: the window does "
        "not see the scan; the test cannot judge. Not opened: %s"
        % (cold["font_files_opened_at_startup"], cold["bundle_font_files"], cold["font_files_not_opened_at_startup"][:5]))

    scanned_again = not warm["font_files_not_opened_at_startup"]
    assert not scanned_again and warm["cache_files_read_at_startup"], (
        "the warm parent %s. Cold: %s. Warm: %s. Cache under %s: cold %s, warm %s."
        % ("opened every one of the %d font files of <GRE>/fonts before a page: it scanned the folder again"
           % warm["bundle_font_files"] if scanned_again else "read no *.cache-* file of fontconfig at its start",
           result["cold"]["startup"], result["warm"]["startup"], xdg / "fontconfig", cold_cache, warm["fontconfig_cache"]))
