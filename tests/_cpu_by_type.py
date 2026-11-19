"""The CPU the engine's processes use, by type of process, read from the processes of this test.

A Firefox browser is a tree: the parent, content processes (`tab`), and, where there are any, a GPU process (`gpu`), a socket process,
a media process (`rdd`) and utility processes. Every one but the parent is started with `-contentproc ... <type>` as the last argument
of its command line, so the type is read from there; no `-contentproc` is the parent.

`CpuSampler` reads the cumulative CPU time (user + kernel) of every engine process below the process that runs the test, every
`period` seconds, from a thread. A process that ends inside the window keeps the last value read before its end (at most one period of
its end is lost). The CPU of a type in a window is the sum, over its processes, of the last value minus the value at the start of the
window (zero for a process born after it).

The process of a test is the root: the browser a test starts is its descendant, and so is nothing else of the engine (an e2e run opens
several browsers at once, in several workers, each in its own process tree).
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Tuple

TYPES = ("parent", "tab", "gpu", "socket", "rdd", "utility", "forkserver", "other")


def classify(cmdline: List[str]) -> str:
    """parent = no `-contentproc`; otherwise the last argument (tab, gpu, socket, rdd, utility, forkserver), `other` for any other."""
    if "-contentproc" not in cmdline:
        return "parent"
    last = str(cmdline[-1]).strip('"').lower() if cmdline else "?"
    return last if last in TYPES else "other"


def delta_by_type(start: Dict[int, Tuple[str, float]], last: Dict[int, Tuple[str, float]]) -> Dict[str, float]:
    """CPU seconds per type over a window: `start` and `last` map pid -> (type, cumulative CPU seconds)."""
    out = {t: 0.0 for t in TYPES}
    for pid, (kind, cpu) in last.items():
        out[kind] = out.get(kind, 0.0) + max(0.0, cpu - start.get(pid, (kind, 0.0))[1])
    return out


def is_engine(proc) -> bool:
    """True when the process runs the firefox executable (its file name, whatever name the process gave itself)."""
    import psutil

    try:
        exe = proc.exe()
    except (psutil.Error, OSError):
        exe = ""
    # Either separator: the file name of the executable, whichever system wrote the path.
    return (exe or proc.name()).replace("\\", "/").rsplit("/", 1)[-1].lower().startswith("firefox")


def engine_processes(root_pid: int) -> Dict[int, Tuple[str, float]]:
    """pid -> (type, cumulative CPU seconds) of every process of the engine below `root_pid`, now.

    ⛔ AN ENGINE PROCESS IS KNOWN BY ITS EXECUTABLE, NOT BY ITS NAME. On Linux a child names itself after its role ("Web Content",
    "Isolated Web Co", "forkserver", "Socket Process", read from /proc/<pid>/comm) while its executable stays the firefox binary, so a
    filter on the name kept the parent alone and the content processes were never counted (e2e Linux, run 43701692047). On Windows the
    name and the executable are the same file."""
    import psutil

    out: Dict[int, Tuple[str, float]] = {}
    try:
        below = psutil.Process(root_pid).children(recursive=True)
    except psutil.Error:
        return out
    for proc in below:
        try:
            if not is_engine(proc):
                continue
            times = proc.cpu_times()
            out[proc.pid] = (classify(proc.cmdline()), times.user + times.system)
        except psutil.Error:
            continue
    return out


class CpuSampler:
    def __init__(self, root_pid: int, period: float = 0.25) -> None:
        self.root_pid = root_pid
        self.period = period
        self.last: Dict[int, Tuple[str, float]] = {}
        self.start: Dict[int, Tuple[str, float]] = {}
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._run, name="cpu-sampler", daemon=True)

    def _take(self) -> None:
        self.last.update(engine_processes(self.root_pid))

    def _run(self) -> None:
        while not self._halt.is_set():
            self._take()
            self._halt.wait(self.period)

    def begin(self) -> None:
        """Mark the start of the window (what every process has used until now is not counted) and start sampling."""
        self._take()
        self.start = dict(self.last)
        self._thread.start()

    def end(self) -> Dict[str, float]:
        """Stop, take one more sample, and return the CPU seconds of the window by type.

        The window ends HERE. The counters are cumulative, so the sample taken now already holds everything the processes used until
        this call: waiting before it (the old ``sleep(0.3)``) added a third of a second of whatever the browser did after the
        measured work to the number, and a slower machine added more. The sampler's thread is waited for up to 30 s (it ends at once:
        its only wait is the event just set, after the sample it was in the middle of), and a sample still running then is a hang."""
        self._halt.set()
        self._thread.join(timeout=30)
        assert not self._thread.is_alive(), "the CPU sampler's thread did not end within 30 s of being told to stop"
        self._take()
        return delta_by_type(self.start, self.last)
