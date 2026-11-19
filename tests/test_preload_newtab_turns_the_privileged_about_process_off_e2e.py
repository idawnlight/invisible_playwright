"""``preload_newtab=False`` leaves no privileged-about process; without the option the preloaded New Tab page has one.

A retail Firefox keeps a New Tab page preloaded in a process of its own (remote type ``privilegedabout``, about 200 MB). The core writes nothing
by default, so the session has it; ``preload_newtab=False`` declares ``browser.newtab.preload`` false and the process is not born (measured on the
candidate engine, three runs of three). The processes are read from the engine's own log, which names the type of every process it creates
(``Setting remoteType of process <pid> to <type>``): it needs ``MOZ_LOG=Process:5,timestamp,sync`` and a file base before the launch, and it holds
on every platform. The assertion is on ``privilegedabout`` only: a Mozilla site's process beside it depends on the country of the machine.
"""
import re
import time

import pytest

TYPE = re.compile(r"Setting remoteType of process (\d+) to (\S+)")


def _read_types(tmp_path, base):
    types = []
    for path in sorted(tmp_path.glob(base.name + "*")):
        types += [m.group(2) for m in TYPE.finditer(path.read_text(encoding="utf-8", errors="replace"))]
    return types


def _remote_types(tmp_path, binary, until, window=0.0, **option):
    """The types of the processes the engine made. It waits (30 s) until ``until(types)`` holds, which is the positive thing the verdict needs to read, and then leaves
    ``window`` seconds in which a process that must NOT be born could show: that window looks for something that must not happen, so a slow machine can only make it weaker."""
    from _waits import wait_until
    from invisible_playwright import InvisiblePlaywright

    mp = pytest.MonkeyPatch()
    base = tmp_path / ("proc-%s" % ("-".join("%s-%s" % kv for kv in sorted(option.items())) or "default"))
    mp.setenv("MOZ_LOG", "Process:5,timestamp,sync")
    mp.setenv("MOZ_LOG_FILE", str(base))
    try:
        with InvisiblePlaywright(seed=7, binary_path=binary, headless=True, humanize=False, locale="en-US",
                                 timezone="America/New_York", **option) as browser:
            page = browser.new_context().new_page()
            page.goto("about:blank")
            wait_until(lambda: until(_read_types(tmp_path, base)), 30, lambda: page.wait_for_timeout(100))
            time.sleep(window)
    finally:
        mp.undo()
    return _read_types(tmp_path, base)


@pytest.mark.e2e
def test_without_the_option_the_new_tab_page_is_preloaded_in_its_own_process(firefox_binary, tmp_path):
    types = _remote_types(tmp_path, firefox_binary, lambda found: "privilegedabout" in found)
    assert types, "the engine wrote no line about its processes: the control cannot judge"
    assert "privilegedabout" in types, "the retail keeps the New Tab page preloaded: %s" % sorted(set(types))


@pytest.mark.e2e
def test_asked_to_turn_the_preload_off_there_is_no_privileged_about_process(firefox_binary, tmp_path):
    types = _remote_types(tmp_path, firefox_binary, bool, window=10, preload_newtab=False)
    assert types, "the engine wrote no line about its processes: the control cannot judge"
    assert "privilegedabout" not in types, "a process of the preloaded New Tab page was born: %s" % sorted(set(types))
