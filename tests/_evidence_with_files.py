"""``FailureEvidence`` that also keeps a folder of extra files (the engine's own log of the launch, a lab server's record, a page's timeline).

The test writes whatever it wants to keep into ``evidence.files`` while it runs. When the body fails, the folder is copied next to the
rest of the evidence (``<evidence folder>/files/``) and ``meta.json`` lists it; when the body passes, it is deleted with the rest.
Nothing else differs from ``tests/_evidence_on_failure.py``.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import time

from _evidence_on_failure import FailureEvidence


class FailureEvidenceWithFiles(FailureEvidence):
    def __init__(self, name: str):
        super().__init__(name)
        #: Under the helper's own temporary folder: removed with it when the body passes.
        self.files = self.work / "files"
        self.files.mkdir(parents=True, exist_ok=True)

    def _save(self, exc_type, exc) -> None:
        super()._save(exc_type, exc)
        if self.saved_to is None or not self.files.exists():
            return
        shutil.copytree(self.files, self.saved_to / "files", dirs_exist_ok=True)
        meta_path = self.saved_to / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["files"] = sorted(p.name for p in self.saved_to.iterdir())
        meta["extra_files"] = sorted(str(p.relative_to(self.files)).replace("\\", "/") for p in self.files.rglob("*") if p.is_file())
        meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")


class _TeeStream:
    """The browser's merged stdout and stderr as the core reads it, copied line by line to a file: ``<wall clock> <perf_counter> <line>``.

    The core reads the pipe with ``readline`` for the whole life of the process (``invisible_core.juggler.connection.ProcessOutput``) and keeps only the last
    200 lines, so this sits between the pipe and that reader: every line it returns is also appended to ``path``, nothing else changes. The content processes
    inherit the parent's stdout (ipc/chromium/src/base/process_util_win.cc ``GetStdHandle(STD_OUTPUT_HANDLE)``; a plain fork on Linux), so what they
    ``printf`` arrives here. The file is opened for each line and closed again: the folder it is in can be removed at any moment (a launch that went right)
    with nothing left open. It never raises."""

    def __init__(self, stream, path):
        self._stream = stream
        self._path = path
        self._lock = threading.Lock()

    def readline(self, *args):
        raw = self._stream.readline(*args)
        if raw and self._path:
            try:
                with self._lock, open(self._path, "ab") as out:
                    out.write(("%.4f %.6f " % (time.time(), time.perf_counter())).encode("ascii") + raw.rstrip(b"\r\n") + b"\n")
            except Exception:
                pass
        return raw

    def __getattr__(self, name):
        return getattr(self._stream, name)


_TEE = {"path": None}


def tee_browser_output(monkeypatch, path) -> bool:
    """From now on, until the end of the test, the stdout of the browser the core launches next is also written to ``path`` (see ``_TeeStream``). Test side
    only: it wraps the stream the core hands to its own reader, in the test process, and touches nothing of the product. Called again with another
    ``path`` it only changes where the next launch's lines go. False (and nothing changed) when the core has no such reader."""
    try:
        from invisible_core.juggler import connection
        output = connection.ProcessOutput
        original = output.__init__
    except Exception:
        return False
    _TEE["path"] = str(path)
    if getattr(original, "_invpw_tee", False):
        return True

    def init(self, stream):
        original(self, _TeeStream(stream, _TEE["path"]))

    init._invpw_tee = True
    monkeypatch.setattr(output, "__init__", init)
    return True


#: The most a command of the probe is waited for, in seconds (the suite's 30 s: a command that hangs is cut and the file says so).
_PROBE_COMMAND_S = 30


def _run(argv):
    started = time.perf_counter()
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=_PROBE_COMMAND_S)
        text = (done.stdout or "") + (("\n[stderr]\n" + done.stderr) if done.stderr and done.stderr.strip() else "")
        return "$ %s\n[exit %s, %.2f s]\n%s\n" % (" ".join(argv), done.returncode, time.perf_counter() - started, text.rstrip())
    except Exception as problem:
        return "$ %s\n[could not run: %r]\n" % (" ".join(argv), problem)


_SHOW_PROCESSES = (
    "foreach ($p in @(%s)) { try { Get-Process -Id $p -ErrorAction Stop | Select-Object Id,ProcessName,StartTime,Path | Format-List } "
    "catch { 'pid ' + $p + ': ' + $_.Exception.Message } }")


def probe_machine(path) -> None:
    """Who holds UDP 5353 on this machine, now, written to ``path``: the failure evidence of the mDNS test (the engine's responder binds it, and so does any
    other browser of the machine). Windows: ``Get-NetUDPEndpoint -LocalPort 5353`` and ``Get-Process`` of each owning pid. Linux: ``ss -uapn``. Meant to be
    called while the browser of the launch is still up. It only reads, never raises and never changes a verdict."""
    try:
        text = "taken_t %.4f\ntaken_m %.6f\nplatform %s\n\n" % (time.time(), time.perf_counter(), sys.platform)
        if sys.platform == "win32":
            endpoints = _run(["powershell", "-NoProfile", "-Command",
                              "Get-NetUDPEndpoint -LocalPort 5353 | Select-Object LocalAddress,LocalPort,OwningProcess,CreationTime | Format-List"])
            text += endpoints + "\n"
            pids = sorted({int(n) for n in re.findall(r"OwningProcess\s*:\s*(\d+)", endpoints)})
            if pids:
                text += _run(["powershell", "-NoProfile", "-Command", _SHOW_PROCESSES % ",".join(str(n) for n in pids)])
        else:
            text += _run(["ss", "-uapn", "sport", "=", ":5353"])
        with open(path, "w", encoding="utf-8") as out:
            out.write(text)
    except Exception as problem:
        try:
            with open(path, "w", encoding="utf-8") as out:
                out.write("the machine probe failed: %r\n" % (problem,))
        except Exception:
            pass
