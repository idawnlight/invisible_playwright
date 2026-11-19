"""The reaper's guarantee that exists on Windows only: a job object takes the tree down when its owner is killed.

``test_the_tree_dies_when_this_process_is_KILLED_not_merely_closed`` is skipped on every other system (job objects are a Windows mechanism), and a skip is
red in the whole unit suite of the candidate, so it is a file of its own and only the row of Windows lists it (``tests/test_reaper.py`` keeps what runs on both).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from invisible_core import process as core_process

from test_reaper import _SLEEP, _reap_all

pytestmark = pytest.mark.unit

psutil = pytest.importorskip("psutil")


@pytest.mark.skipif(os.name != "nt", reason="job objects are a Windows mechanism")
def test_the_tree_dies_when_this_process_is_KILLED_not_merely_closed():
    """The path that in-process cleanup cannot reach, and the actual bug.

    Measured first, and it corrected the diagnosis: an exception out of the
    `with` block does NOT leak - __exit__ runs, Playwright tears down, zero
    survivors over an interleaved A/B with the reaper disabled. The leak was
    the timeout path, where the runner is killed and __exit__ never executes.
    With a real browser: eight survivors on the first attempt, twelve on the
    second; zero after this.

    Reproduced here without a browser, because the mechanism is the kernel's
    and not the browser's: a child binds a marked grandchild to its job, then
    is killed outright. The grandchild must not outlive it.
    """
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess,sys,time;"
         "sys.path.insert(0, r'" + os.path.dirname(os.path.dirname(
             os.path.abspath(core_process.__file__))) + "');"
         "from invisible_core.process import SessionToken, guard_for;"
         "tok=SessionToken.mint();"
         "env=tok.stamp(__import__('os').environ);"
         f"kid=subprocess.Popen([sys.executable,'-c','import time; time.sleep({_SLEEP})'],env=env);"
         "n=guard_for().bind(tok);"
         "print(kid.pid, n, flush=True);"
         f"time.sleep({_SLEEP})"],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        line = holder.stdout.readline().split()
        assert len(line) == 2, f"the holder did not report: {line!r}"
        kid_pid, bound = int(line[0]), int(line[1])
        assert bound >= 1, "nothing was bound to the job, so nothing is guaranteed"
        assert psutil.pid_exists(kid_pid)

        psutil.Process(holder.pid).kill()   # no __exit__, no finally, no atexit
        holder.wait(timeout=10)

        deadline = time.time() + 10
        while time.time() < deadline and psutil.pid_exists(kid_pid):
            time.sleep(0.2)
        assert not psutil.pid_exists(kid_pid), (
            f"pid {kid_pid} outlived the process that owned its job - this is "
            "exactly the leak"
        )
    finally:
        _reap_all(holder)
