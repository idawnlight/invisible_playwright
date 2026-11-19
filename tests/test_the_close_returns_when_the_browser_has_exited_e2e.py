"""`close()` returns when the BROWSER has exited, while what outlives it is still alive; the rest is the reaper's.

⛔ THE KNOWN-BAD INPUT: invisible-core's `Connection.close` stopped waiting for the sender of the shutdown pings (it leaves by
itself within about a second) and gave it to the launch's reaper, but the wrapper's server still ran `SessionFiles.remove`
right after the shutdown, and that waits for the sender and the tree: the close of the wrapper took as long as the sender
lived. The server hands the removal to the reaper now (`_juggler/_reaped.py`), and the teardown's last reap is the reaper's too.

CAUSAL, NOT TIMED: no clock decides this test. The test keeps alive, itself, the process that outlives the browser: a fake
sender that blocks on a pipe the test holds and exits only when the test closes it. The core knows it for what it is by its
command line (`process.outlives_the_browser`: `--backgroundtask pingsender`), and it is a member of the launch's tree as the core
sees it:

- Linux: the engine is the leader of a process group of its own and the tree is that group; the sender joins it with
  `setpgid(0, engine's pgid)`, which is what a child of the engine is born with;
- Windows: the tree is the launch's job (`EngineJob`); the sender is assigned to it with `AssignProcessToJobObject` on the job's
  own handle, which is what the kernel does for a child of the engine.

Then: `close()` returns while the sender is ALIVE (red on the code that waited for it: there the close returns only once the
sender is gone, ended by the grace or released by the safety timer below, and the message says which). The test releases it,
waits for the reaper's end by its condition (`wait_reaped`; the 30 s is a safety net, never a measure), and nothing of the
session is left: no process, and no profile directory.

A core with no reaper fails this test, with the reason: it is the test of the core that has one, and a skip would let the pin
move to a core that waits again without anyone seeing it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading

import pytest

pytestmark = pytest.mark.e2e

#: The test releases the sender by itself after this long if `close()` has not returned, so that the run of a code that waits for
#: the sender ends and says so instead of hanging. A safety net: the test passes or fails by what is alive, never by this.
SAFETY_RELEASE_S = 60.0

#: The fake sender: it says it is there, then blocks until the test closes its stdin.
_SENDER = textwrap.dedent("""
    import sys
    sys.stdout.write("up\\n")
    sys.stdout.flush()
    sys.stdin.read()
""")


def _the_core_has_a_reaper() -> None:
    import invisible_core

    try:
        from invisible_core.process import Reaper  # noqa: F401
    except ImportError:
        pytest.fail(
            "the pinned invisible-core %s has no `process.Reaper`: its close still waits for the sender of the shutdown "
            "pings, so there is nothing for the wrapper to hand over. This test needs the core that returns when the "
            "browser has exited (oss-invisible/core#284); move the pin to it." % invisible_core.__version__,
            pytrace=False)


def _the_engine_parent(members):
    """The process of the engine itself among the session's: not a child process of it (`-contentproc`), not the sender."""
    import psutil

    pids = {p.pid for p in members}
    parents = []
    for proc in members:
        try:
            cmd = proc.cmdline()
            if "-contentproc" in cmd or proc.ppid() in pids:
                continue
            parents.append(proc)
        except psutil.Error:
            continue
    assert parents, "no engine process among the session's: %r" % [p.pid for p in members]
    return min(parents, key=lambda p: p.create_time())


def _profile_of(proc):
    cmd = proc.cmdline()
    for flag in ("-profile", "--profile"):
        if flag in cmd and cmd.index(flag) + 1 < len(cmd):
            return cmd[cmd.index(flag) + 1]
    return None


def _start_sender_in(tree):
    """A process the core takes for the sender of the shutdown pings, and a member of ``tree``: it lives until its stdin is closed."""
    from invisible_core.process import outlives_the_browser

    argv = [sys.executable, "-c", _SENDER, "--backgroundtask", "pingsender"]
    assert outlives_the_browser(argv), "the core would not take this command line for the sender's: %r" % (argv,)
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        job = tree.job
        assert job is not None and job.handle, "the launch has no job on Windows: nothing to put the sender in"
        sender = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        if not kernel32.AssignProcessToJobObject(job.handle, int(sender._handle)):
            error = ctypes.get_last_error()
            sender.kill()
            pytest.fail("the sender could not be put in the launch's job (Windows error %d)" % error, pytrace=False)
    else:
        assert tree.group, "the launch's tree is not the engine's process group: nowhere to put the sender"
        sender = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  preexec_fn=lambda: os.setpgid(0, tree.pid))
    assert sender.stdout.readline().strip() == b"up", "the fake sender did not start"
    return sender


def test_the_close_returns_while_the_sender_lives_and_the_reaper_leaves_nothing(firefox_binary):
    _the_core_has_a_reaper()
    from invisible_core.process import SessionToken, find_processes, registered_trees, wait_until_gone
    from invisible_playwright import InvisiblePlaywright
    from invisible_playwright._juggler._reaped import wait_reaped

    sender = None
    released_by_the_timer = threading.Event()

    def release() -> None:
        try:
            sender.stdin.close()
        except Exception:
            pass

    def release_after_the_safety_period() -> None:
        released_by_the_timer.set()
        release()

    timer = None
    session = InvisiblePlaywright(seed=7104, binary_path=firefox_binary, humanize=False, headless=True)
    try:
        with session as browser:
            page = browser.new_page()
            page.set_content("<p>a page, so a content process exists</p>")
            token = SessionToken(session._session_token.value)
            trees = registered_trees(token)
            assert trees, "the launch registered no tree for its token"
            engine = _the_engine_parent(find_processes(token))
            profile = _profile_of(engine)
            assert profile and os.path.isdir(profile), "the engine's profile was not found: %r" % (profile,)
            sender = _start_sender_in(trees[0])
            assert sender.pid in {p.pid for p in trees[0].members()}, "the sender is not a member of the launch's tree"
            timer = threading.Timer(SAFETY_RELEASE_S, release_after_the_safety_period)
            timer.daemon = True
            timer.start()
        # The session is closed: the browser has exited.
        assert sender.poll() is None, (
            "close() returned only after the sender was gone (%s): it waited for what outlives the browser, which is the "
            "reaper's" % ("released by the test's safety timer" if released_by_the_timer.is_set()
                          else "ended by the grace of the removal"))
    finally:
        if timer is not None:
            timer.cancel()
        if sender is not None:
            release()

    sender.wait(timeout=30)
    assert wait_reaped(token, timeout=30), "the reaper did not finish within 30 s of the sender's end"
    assert wait_until_gone(token, timeout=30), "processes of the session after the reaper: %s" % [
        (p.pid, p.name()) for p in find_processes(token)]
    assert not os.path.exists(profile), "the profile is still there once the reaper has finished: %s" % profile
