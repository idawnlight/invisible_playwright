"""What an e2e test that fails leaves for a reader, and nothing when it passes.

``with FailureEvidence("name") as evidence:`` around the body of a test: while the body runs, the helper collects, in a temporary folder,
the sources the test points it at (the fake web's own record, the Juggler wire of the served browser, the engine's own Juggler trace,
the client's view of every request); when the body raises (an assertion included), it copies what it holds to
``$RUNNER_TEMP/invpw-evidence/<name>/<pid>-<ms>/``, the folder the e2e jobs upload (``invpw-evidence-*``), with a ``meta.json``; when the
body passes, it deletes it all. Off a runner (no ``RUNNER_TEMP``) nothing is copied. It changes no assertion, wait or deadline of the
test: it only listens.

A test that passed slowly may keep the evidence too: ``evidence.keep(why)`` saves what the helper holds at that moment (the same folder, a
``meta.json`` whose ``kept`` says why, judged after the fact and never a verdict). ``arm(moz_log=...)`` also starts the engine's own log
(``MOZ_LOG``, its file in the evidence), and ``note_records(name, state)`` adds a record the test keeps itself (``state.lock``,
``state.records``) as ``<name>.jsonl``.

The times written: ``t``, wall-clock seconds since the epoch (``time.time()``; the engine's own trace writes milliseconds in ``t``, its
``performance.timeOrigin + now``, also since the epoch), and ``m``, ``time.perf_counter()`` of the test process, finer on Windows (whose wall
clock ticks every 15 ms) and shared by the fake web's record, the client's log and the Juggler wire.
"""
from __future__ import annotations

import json
import os
import pathlib
import platform
import shutil
import sys
import tempfile
import threading
import time

import pytest

#: The folder under ``$RUNNER_TEMP`` the e2e workflows upload (``invpw-evidence-*``).
EVIDENCE_FOLDER = "invpw-evidence"
#: The most characters of one Juggler message kept in the wire log: a message longer than this is cut and says so.
WIRE_LIMIT = 4000
#: The engine's trace: the variable `Trace.js` of the engine reads in the parent process (off without it).
TRACE_ENV = "INVISIBLE_JUGGLER_TRACE_DIR"
#: The base name of the engine's log file in the evidence (``MOZ_LOG_FILE``; the engine adds a suffix per process).
MOZ_LOG_BASE = "engine"


class FailureEvidence:
    def __init__(self, name: str):
        self.name = name
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="invpw-failure-evidence-"))
        self.trace_dir = self.work / "juggler-engine-trace"
        self.wire_path = self.work / "juggler-wire.jsonl"
        self.moz_log_dir = self.work / "moz-log"
        #: The records the test keeps itself, by name (``note_records``).
        self.records = {}
        #: What the client heard of each request (a list ``profile_p2.drive(client_log=...)`` fills).
        self.client_log = []
        #: The cells of the run, once it has returned (``note_cells``).
        self.cells = None
        self._stop = None
        self._patch = None
        self._wire = None
        self._wire_lock = threading.Lock()
        self.saved_to = None

    # ---- what the test points the helper at ---------------------------------------------------------------------------
    def watch_server(self, stop) -> None:
        """The ``stop`` function ``tests/fakeweb/sites.start`` returned: it carries the server's own record (``stop.state``)."""
        self._stop = stop

    def note_cells(self, cells) -> None:
        self.cells = cells

    def note_records(self, name: str, state) -> None:
        """A record the test keeps itself (``state.lock``, ``state.records``: a list of dicts), saved as ``<name>.jsonl``."""
        self.records[name] = state

    def arm(self, moz_log: str = "") -> None:
        """Before the browser under test is launched: the engine writes its trace, and the Juggler wire of this process is logged.
        Armed after the other processes of the test were started, so that they do not inherit the variable. ``moz_log``, a value of
        ``MOZ_LOG`` (modules and levels), also starts the engine's own log into the evidence (a diagnostic no page reads: the core passes it)."""
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self._patch = pytest.MonkeyPatch()
        self._patch.setenv(TRACE_ENV, str(self.trace_dir))
        if moz_log:
            self.moz_log_dir.mkdir(parents=True, exist_ok=True)
            self._patch.setenv("MOZ_LOG", moz_log)
            self._patch.setenv("MOZ_LOG_FILE", str(self.moz_log_dir / MOZ_LOG_BASE))
        self._wire = open(self.wire_path, "w", encoding="utf-8")
        try:
            from invisible_core.juggler import connection
        except Exception as exc:            # the trace of the engine is still written; say that the wire is not
            self._write_wire({"dir": "note", "what": "the Juggler wire is not logged: %r" % (exc,)})
            return
        owner = self
        original_deliver, original_write = connection.Connection._deliver, connection.Connection._write

        def deliver(connection_self, raw):
            owner._log_wire("in", raw)
            return original_deliver(connection_self, raw)

        def write(connection_self, msg):
            owner._log_wire("out", msg)
            return original_write(connection_self, msg)

        self._patch.setattr(connection.Connection, "_deliver", deliver)
        self._patch.setattr(connection.Connection, "_write", write)

    # ---- the wire ------------------------------------------------------------------------------------------------------
    def _write_wire(self, record: dict) -> None:
        try:
            with self._wire_lock:
                if self._wire is not None:
                    self._wire.write(json.dumps(record, default=str) + "\n")
                    self._wire.flush()
        except Exception:
            pass

    def _log_wire(self, direction: str, message) -> None:
        try:
            if isinstance(message, (bytes, bytearray)):
                text = bytes(message).decode("utf-8", "replace")
            else:
                text = json.dumps(message, default=str)
            cut = len(text) > WIRE_LIMIT
            self._write_wire({"t": round(time.time(), 4), "m": round(time.perf_counter(), 6), "dir": direction, "len": len(text), "cut": cut,
                              "msg": text[:WIRE_LIMIT]})
        except Exception:
            pass

    # ---- the end ---------------------------------------------------------------------------------------------------------
    def keep(self, why: str) -> None:
        """Saves what the helper holds now, though the test has not failed: a run judged after the fact as one to read (a step slower
        than any healthy run). Not a verdict: nothing is raised and the test goes on. Never raises."""
        try:
            self._save(None, None, kept=why)
        except Exception as problem:
            sys.stderr.write("evidence: could not be kept: %r\n" % (problem,))

    def __enter__(self) -> "FailureEvidence":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if self._patch is not None:
                self._patch.undo()
            with self._wire_lock:
                if self._wire is not None:
                    self._wire.close()
                    self._wire = None
            if exc_type is not None:
                self._save(exc_type, exc)
        except Exception as problem:         # the evidence never hides the failure it is about
            sys.stderr.write("failure evidence: could not be saved: %r\n" % (problem,))
        finally:
            shutil.rmtree(self.work, ignore_errors=True)
        return False

    def _save(self, exc_type, exc, kept: str = "") -> None:
        base = os.environ.get("RUNNER_TEMP")
        if not base:
            return
        target = pathlib.Path(base) / EVIDENCE_FOLDER / self.name / ("%d-%d" % (os.getpid(), int(time.time() * 1000)))
        target.mkdir(parents=True, exist_ok=True)
        if self._stop is not None and getattr(self._stop, "state", None) is not None:
            state = self._stop.state
            with state.lock:
                records = list(state.records)
            self._jsonl(target / "fakeweb-server-record.jsonl", records)
        for name, state in self.records.items():
            with state.lock:
                records = list(state.records)
            self._jsonl(target / ("%s.jsonl" % name), records)
        self._jsonl(target / "client-request-log.jsonl", list(self.client_log))
        if self.cells is not None:
            (target / "cells.json").write_text(json.dumps(self.cells, indent=1, default=str), encoding="utf-8")
        if self.wire_path.exists():
            shutil.copyfile(self.wire_path, target / "juggler-wire.jsonl")
        if self.trace_dir.exists():
            shutil.copytree(self.trace_dir, target / "juggler-engine-trace", dirs_exist_ok=True)
        if self.moz_log_dir.exists():
            shutil.copytree(self.moz_log_dir, target / "moz-log", dirs_exist_ok=True)
        meta = {"test": self.name, "platform": sys.platform,
                "machine": platform.machine(), "python": platform.python_version(), "saved_at": round(time.time(), 4),
                "times": "t: wall-clock seconds since the epoch (the engine's trace: milliseconds, in t); m: time.perf_counter() of the test process, the scale shared by the server record, the client log and the wire",
                "files": sorted(p.name for p in target.iterdir())}
        if exc_type is not None:
            meta["failure"] = "%s: %s" % (exc_type.__name__, str(exc)[:1500])
        if kept:
            meta["kept"] = kept
        (target / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        self.saved_to = target

    @staticmethod
    def _jsonl(path: pathlib.Path, records) -> None:
        with open(path, "w", encoding="utf-8") as out:
            for record in records:
                out.write(json.dumps(record, default=str) + "\n")
