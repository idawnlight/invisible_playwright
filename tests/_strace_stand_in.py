"""The strace stand-in of the Linux font tests: the engine's directory as links, whose executable runs the real one under ``strace`` in the
engine's place (same pid, same process group), and what reads its log (who is the parent and who a content process) and cleans its group.

Used by tests/test_the_parent_reads_the_bundle_from_fontconfig_s_cache_linux_e2e.py. No test of its own, no browser."""
from __future__ import annotations

import os
import re
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

_SYSCALLS = ("execve,clone,clone3,fork,vfork,open,openat,stat,lstat,"
             "newfstatat,statx,access,faccessat,faccessat2,readlink")


def _strace_options() -> str:
    """strace's own flags: quiet, follow forks, and die with the traced process
    when this build knows --kill-on-exit."""
    help_text = subprocess.run([shutil.which("strace"), "-h"], capture_output=True,
                               text=True).stdout
    return "-f -qq -s 4096" + (" --kill-on-exit" if "--kill-on-exit" in help_text else "")


def _require_strace_can_trace(tmp: Path) -> None:
    """strace needs ptrace: a container or yama can refuse it, and then the
    failure is the environment's, said as such, not the launch's."""
    probe = subprocess.run([shutil.which("strace"), "-o", str(tmp / "probe.log"), "true"],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.fail("strace cannot trace here: %s" % (probe.stderr.strip() or probe.returncode),
                    pytrace=False)


def _stand_in(binary: str, tree: Path, log: Path, pidfile: Path, syscalls: str = _SYSCALLS,
              timestamps: bool = False) -> str:
    """A copy of the engine's directory made of links, whose executable runs
    the real one under strace, in the engine's place.

    The script writes its pid and execs strace: the pid stays, and so does the
    process group its launcher gave it. The core spawns the engine as the
    leader of a process group of its own (``posix_spawn`` with ``setpgroup=0``,
    as Playwright's launcher does with ``detached: true``), so that group holds
    strace and everything it traces, and ``_kill_group`` kills the group of the
    pid (``os.getpgid``). The script used to exec ``setsid``: setsid(1) forks
    when its caller leads a process group, its parent exited 0 at once, and the
    core saw the engine exit during startup (candidate 44161687675).

    ``syscalls`` is what strace traces; ``timestamps`` adds ``-ttt`` (the epoch
    seconds after each pid), which ``_LINE`` does not read: a caller that asks
    for them strips them first (``test_the_parent_reads_the_bundle_from_fontconfig_s_cache_linux_e2e``)."""
    real = Path(os.path.realpath(binary))
    tree.mkdir()
    for entry in real.parent.iterdir():
        if entry.name != real.name:
            (tree / entry.name).symlink_to(entry)
    script = tree / real.name
    script.write_text(
        "#!/bin/sh\n"
        "echo $$ > %s\n"
        'exec %s %s -o %s -e trace=%s %s "$@"\n'
        % (pidfile, shutil.which("strace"), _strace_options() + (" -ttt" if timestamps else ""), log, syscalls, real))
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


_LINE = re.compile(r"^(\d+)\s+(\w+)\((.*)$")
# strace -f splits a call that another thread interrupts in two lines:
# "PID clone3({...} <unfinished ...>" and "PID <... clone3 resumed>) = CHILD".
_RESUMED = re.compile(r"^(\d+)\s+<\.\.\. (\w+) resumed>(.*)$")
_RESULT = re.compile(r"=\s+(\d+)\s*$")


def _roles(lines: list, real_binary: str):
    """``(role, argv_of)`` for a strace log read as lines: ``role(pid)`` is ``"parent"``, ``"content"`` or ``"other"`` (walking up the
    clones to the process that executed the engine), ``argv_of`` maps a pid to the arguments of the last execve of the engine it made."""
    parent_of = {}
    argv_of = {}
    for line in lines:
        m = _LINE.match(line) or _RESUMED.match(line)
        if not m:
            continue
        pid, call, rest = int(m.group(1)), m.group(2), m.group(3)
        if call in ("clone", "clone3", "fork", "vfork"):
            r = _RESULT.search(rest)
            if r:
                parent_of[int(r.group(1))] = pid
        elif call == "execve" and real_binary in rest:
            # The arguments are on the first line even when the result comes
            # on a resumed one; the last execve of a process is what it runs.
            argv_of[pid] = rest

    def role(pid: int) -> str:
        seen = set()
        while pid not in seen:
            seen.add(pid)
            argv = argv_of.get(pid)
            if argv is not None and real_binary in argv:
                return "content" if "-contentproc" in argv else "parent"
            if pid not in parent_of:
                break
            pid = parent_of[pid]
        return "other"

    return role, argv_of


def _group_of(pidfile: Path):
    """The process group of the stand-in, read from its living pid (``os.getpgid``); None when it is gone or wrote no pid."""
    try:
        return os.getpgid(int(pidfile.read_text().strip()))
    except (OSError, ValueError):
        return None


def _kill_group(pidfile: Path):
    """Kill the stand-in's process group: strace, the browser and its children. Returns the group killed, or None.

    Looking for the stand-in's path in the command lines found nothing (strace
    and its children carry the real binary's path), so the group is the handle:
    the group of the stand-in's pid, not the pid taken as a group id. Never the
    group of this test: a stand-in started inside it is killed alone."""
    pgid = _group_of(pidfile)
    if pgid is None:
        return None
    if pgid == os.getpgrp():
        try:
            os.kill(int(pidfile.read_text().strip()), signal.SIGKILL)
        except (OSError, ValueError):
            pass
        return None
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    return pgid


def _detached(argv: list, **kwargs) -> subprocess.Popen:
    """A process started as the core starts the engine: the leader of a process group of its own (``posix_spawn`` with
    ``setpgroup=0`` in ``invisible_core.juggler.connection._spawn_posix``)."""
    return subprocess.Popen(argv, process_group=0, **kwargs)


def _group_members(pgid: int) -> list:
    """Pids still in the process group ``pgid``."""
    members = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            stat_line = Path("/proc/%s/stat" % pid).read_text()
        except OSError:
            continue
        fields = stat_line.rsplit(")", 1)[1].split()
        # fields: state ppid pgrp ...; a zombie is already dead.
        if fields[0] != "Z" and int(fields[2]) == pgid:
            members.append(int(pid))
    return members


def _wait_for_exit(pgid: int, seconds: float = 30.0) -> list:
    end = time.monotonic() + seconds
    left = _group_members(pgid)
    while left and time.monotonic() < end:
        time.sleep(0.1)
        left = _group_members(pgid)
    return left
