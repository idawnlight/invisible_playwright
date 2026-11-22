"""What a failing Fission frame test leaves about the frames of its pages, and nothing it could change in the test.

``tests/_evidence_on_failure.py`` keeps the Juggler wire and the engine's own trace. This adds the client's view of the pages (the events
``frameattached``, ``framenavigated`` and ``framedetached`` with the url and name of the frame, the times at which a call of the test started and
came back, and ``page.frames`` at the failure) and a reading of the wire that answers "to which execution context, of which frame and url, was that
evaluate sent, and what came back".

The rule of a probe: it can never change the outcome of the call it watches. So

* nothing here raises into the test, into the browser's event dispatch or into the server: every recording runs inside one guard;
* nothing takes ``**fields``: a record keeps what it was given under ``data``, so that a field named ``what``, ``source`` or ``event`` of the thing
  being recorded cannot collide with a keyword of ours (two earlier probes broke their tests exactly that way);
* ``observed`` returns what the call returned, and raises what the call raised, the same object, whatever the recording did.
"""
from __future__ import annotations

import json
import sys
import threading
import time

from _far_away_probes import LoadSampler, StallWitness
from _far_away_probes import Records as ProbeRecords

#: The events of a page the client hears, recorded with the frame they are about.
FRAME_EVENTS = ("frameattached", "framenavigated", "framedetached")
#: The most records of the wire the summary keeps (the window of one test is a few hundred messages).
SUMMARY_LIMIT = 600
#: The most commands without an answer the summary says (a browser that is closed leaves a few; the evidence has the wire itself).
UNANSWERED_LIMIT = 20
#: The commands that carry code to a context.
_CALLS = ("Runtime.evaluate", "Runtime.callFunction")
#: The events of the engine that say which frame and which world exist, and when they come and go.
_EVENTS = ("Page.frameAttached", "Page.frameDetached", "Page.navigationStarted", "Page.navigationCommitted", "Page.navigationAborted",
           "Runtime.executionContextCreated", "Runtime.executionContextDestroyed", "Runtime.executionContextsCleared")


class Records:
    """A list of dicts with its lock: the shape ``FailureEvidence.note_records`` reads (``lock``, ``records``)."""

    def __init__(self, records=None):
        self.lock = threading.Lock()
        self.records = [] if records is None else records

    def add(self, record: dict) -> None:
        """Never raises."""
        try:
            with self.lock:
                self.records.append(record)
        except Exception:
            pass


def _plain(data):
    """``data`` as plain JSON values (what cannot be written is written as its ``repr``); never raises."""
    try:
        return json.loads(json.dumps(data, default=repr))
    except Exception:
        try:
            return {"unwritable": repr(data)[:500]}
        except Exception:
            return {"unwritable": "?"}


class FrameWatch(Records):
    """The client's own log of what the test did and heard, on the clocks of the evidence (``t`` wall, ``m`` ``time.perf_counter()``)."""

    def __init__(self, records=None):
        super().__init__(records)
        #: The reading of the wire (``summarize_wire``), saved as its own file.
        self.wire = Records()
        self._pages = {}
        self.started_m = None

    # ---- one record --------------------------------------------------------------------------------------------------
    def note(self, event: str, data=None) -> None:
        """One record: ``{t, m, event, data}``. What is recorded lives under ``data``, whatever fields it has. Never raises."""
        try:
            self.add({"t": round(time.time(), 4), "m": round(time.perf_counter(), 6), "event": str(event), "data": _plain(data)})
        except Exception:
            pass

    @staticmethod
    def frame_data(frame) -> dict:
        """What the client knows of a frame without asking the browser: url, name, whether it is the main frame, its parent's url, its guid.
        A field that cannot be read is left out. Never raises."""
        found = {}
        try:
            for field, read in (("url", lambda f: f.url), ("name", lambda f: f.name), ("guid", lambda f: getattr(f, "_guid", None)),
                                ("main", lambda f: f.parent_frame is None),
                                ("parent_url", lambda f: None if f.parent_frame is None else f.parent_frame.url),
                                ("detached", lambda f: f.is_detached())):
                try:
                    found[field] = read(frame)
                except Exception as problem:
                    found[field] = "unreadable: %r" % (problem,)
        except Exception:
            pass
        return found

    # ---- the pages ---------------------------------------------------------------------------------------------------
    def listen(self, page, label: str) -> None:
        """Records ``frameattached``, ``framenavigated`` and ``framedetached`` of ``page`` under ``label``, and the page's own ``domcontentloaded`` and
        ``load``. Call it right after the page is made. Never raises; a handler never raises into the client's dispatch."""
        try:
            self._pages[label] = page
        except Exception:
            pass
        for event in FRAME_EVENTS:
            self._on(page, event, lambda frame, event=event: self.note(event, {"page": label, "frame": self.frame_data(frame)}))
        for event in ("domcontentloaded", "load"):
            self._on(page, event, lambda _page, event=event: self.note(event, {"page": label, "frames": self.frames_of(_page)}))

    def _on(self, page, event: str, handler) -> None:
        def guarded(*args):
            try:
                handler(*args)
            except Exception:
                pass
        try:
            page.on(event, guarded)
        except Exception:
            pass

    def frames_of(self, page) -> list:
        """``page.frames`` as url, name and guid of each (local state of the client); never raises."""
        try:
            return [self.frame_data(f) for f in list(page.frames)]
        except Exception as problem:
            return [{"unreadable": repr(problem)}]

    def forget_pages(self) -> None:
        self._pages = {}

    # ---- a call of the test ------------------------------------------------------------------------------------------
    def observed(self, label: str, call, describe=None):
        """``call()``, with a record when it starts, when it returns and when it raises. It returns what ``call`` returned and raises what it raised.
        ``describe(stage, value)``, if given, adds to the record: stage is ``"start"``, ``"returned"`` (value: the result) or ``"raised"`` (value: the
        exception); what it returns, or what it raises, only goes into the record."""
        self.note(label + ": start", self._describe(describe, "start", None))
        try:
            result = call()
        except BaseException as problem:
            self.note(label + ": raised", {"type": type(problem).__name__, "message": str(problem)[:1500],
                                           "more": self._describe(describe, "raised", problem)})
            raise
        self.note(label + ": returned", self._describe(describe, "returned", result))
        return result

    @staticmethod
    def _describe(describe, stage: str, value):
        if describe is None:
            return None
        try:
            return describe(stage, value)
        except Exception as problem:
            return {"describe raised": repr(problem)}

    # ---- the test ----------------------------------------------------------------------------------------------------
    def start(self, evidence=None) -> None:
        """The test begins: one record, and a note in the wire log (the module's browser is shared by tests that came before)."""
        self.started_m = time.perf_counter()
        self.note("test starts")
        try:
            if evidence is not None:
                evidence._write_wire({"t": round(time.time(), 4), "m": round(self.started_m, 6), "dir": "note", "what": "the watched test starts"})
        except Exception:
            pass

    def at_failure(self, evidence) -> None:
        """At the failure: ``page.frames`` of every page listened to, and the reading of the wire. Never raises."""
        try:
            self.note("page.frames at the failure", {label: self.frames_of(page) for label, page in list(self._pages.items())})
        except Exception:
            pass
        try:
            for record in summarize_wire(evidence.wire_path, self.started_m):
                self.wire.add(record)
        except Exception:
            pass


    def unanswered_line(self, limit: int = 3) -> str:
        """The commands the wire reading found with no answer, in one line each (id, method, context and its frame's url, seconds waited, whether the
        context was announced and whether it was destroyed). Empty when there is none. Never raises."""
        try:
            with self.wire.lock:
                found = [r for r in self.wire.records if r.get("event") == "command without an answer"]
            parts = []
            for r in found[:limit]:
                end = r.get("context_at_the_end") or {}
                frame = r.get("frame_at_the_end") or {}
                parts.append("%s #%s to context %s (frame %s, %s) had no answer after %ss; context %s" % (
                    r.get("method"), r.get("id"), r.get("executionContextId"), end.get("frame"), frame.get("url"), r.get("waited_s"),
                    "not announced in the window" if not end else ("destroyed at m=%s" % end.get("destroyed_m") if end.get("destroyed_m") else "alive")))
            return "; ".join(parts)
        except Exception:
            return ""


class ProcessProbes:
    """The stall witness and the runner's CPU load of ``tests/_far_away_probes.py``, on the clock of the wire: ``m`` is ``time.perf_counter()``
    (their epoch is 0, so ``from_gw`` and ``to_gw`` are ``m``). A beat that is late is a stall of the whole test process, the one that also holds
    the client's reader of the Juggler pipe; a quiet process and a call that gets no answer put the stall in the browser or in the context, not
    in the client. ``evidence.note_records`` keeps both lists. Nothing here raises: both recorders keep a failure as a record."""

    def __init__(self, load_source=None):
        self.stall_records, self.load_records = ProbeRecords(), ProbeRecords()
        self.witness = StallWitness(self.stall_records, 0.0, clock=time.perf_counter)
        self.sampler = LoadSampler(self.load_records, 0.0, source=load_source, clock=time.perf_counter)

    def keep_in(self, evidence) -> None:
        evidence.note_records("stalls-of-the-test-process", self.stall_records)
        evidence.note_records("runner-load", self.load_records)

    def start(self) -> None:
        self.witness.start()
        self.sampler.start()

    def stop(self) -> None:
        self.witness.stop()
        self.sampler.stop()

    def line(self) -> str:
        """Stalls and load in one short line (``m`` of each stall). Never raises."""
        try:
            stalls = sorted(self.witness.stalls(), key=lambda r: -r["gap_s"])
            if stalls:
                said = ", ".join("%.2fs at m=%.2f" % (r["gap_s"], r["from_gw"]) for r in stalls[:3])
                stall = "%d stall(s) of the test process over %.0f ms: %s" % (len(stalls), self.witness.stall_s * 1000, said)
            else:
                stall = "no stall of the test process over %.0f ms (%d beats, longest gap %.3fs)" % (
                    self.witness.stall_s * 1000, self.witness.beats, self.witness.longest_gap_s)
            cpu = [r["cpu_percent"] for r in self.sampler.samples()]
            load = "runner CPU max %.0f%%, mean %.0f%% in %d samples" % (max(cpu), sum(cpu) / len(cpu), len(cpu)) if cpu else "runner CPU: no sample"
            return "%s; %s" % (stall, load)
        except Exception as problem:
            return "stalls and load: not read (%r)" % (problem,)


class OnFailure:
    """``with OnFailure(evidence, watch):`` around the body of a test: when the body fails, the frames and the wire reading are added and the
    evidence is saved (the folder the e2e jobs upload); the exception goes on, the very one the body raised."""

    def __init__(self, evidence, watch: FrameWatch, probes: ProcessProbes = None):
        self.evidence = evidence
        self.watch = watch
        self.probes = probes

    def __enter__(self):
        self.watch.start(self.evidence)
        self._probes("start")
        return self

    def _probes(self, action: str) -> None:
        try:
            if self.probes is not None:
                getattr(self.probes, action)()
        except Exception:
            pass

    def __exit__(self, exc_type, exc, tb):
        self._probes("stop")
        try:
            if exc_type is not None and issubclass(exc_type, Exception):      # not a skip, not an interrupt
                self.watch.note("failure", {"type": exc_type.__name__, "message": str(exc)[:1500]})
                self.watch.at_failure(self.evidence)
                self._say(exc)
                try:
                    self.evidence._save(exc_type, exc)
                except Exception as problem:
                    sys.stderr.write("frame evidence: could not be saved: %r\n" % (problem,))
        except Exception:
            pass
        finally:
            try:
                self.watch.forget_pages()
            except Exception:
                pass
        return False

    def _say(self, exc) -> None:
        """A note on the exception the body raised (the very object, its type and message untouched): what the wire says of the commands with no
        answer, and the stalls and load of the process. It is read in the failure report next to the traceback."""
        try:
            lines = []
            unanswered = self.watch.unanswered_line()
            if unanswered:
                lines.append("wire: " + unanswered)
            if self.probes is not None:
                lines.append("process: " + self.probes.line())
            if lines:
                self.watch.note("said at the failure", {"lines": lines})
                add_note = getattr(exc, "add_note", None)
                if add_note is not None:
                    add_note("evidence of this failure: " + " | ".join(lines))
        except Exception:
            pass


# ---- the wire --------------------------------------------------------------------------------------------------------
def _short(text, limit=160):
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + "..."


def summarize_wire(path, since_m=None, marker: str = "window.__r") -> list:
    """What the Juggler wire log (``FailureEvidence`` ``juggler-wire.jsonl``) says about frames, worlds and the evaluate of the test, from ``since_m``
    (``perf_counter``) on: the engine's frame and context events with their ids, urls and names; each ``Runtime.evaluate`` or ``callFunction`` sent
    with its context id and the beginning of its code; each answer to one of those (its error text, if it has one); and, for every command whose code
    contains ``marker``, a ``marker command`` record that joins the context id it was sent to with what the wire knows of that context (its frame id
    and name, when it was announced and when it was destroyed) and of that frame (the url it last committed, its parent). Whatever the code, every
    command that has no answer by the end of the log gets a ``command without an answer`` record with the same join, taken when it was sent and at
    the end of the log, and the time it has waited: the evaluate that a client gave up on (``no response in 30s``) is there whether or not its code
    has the marker. An answer says how long after its command it came (``after_s``). A line of the log that is not JSON, or was cut at the log's
    limit, is counted, not read. Never raises: a failure is a record."""
    try:
        return _summarize_wire(path, since_m, marker)
    except Exception as problem:
        return [{"event": "the wire could not be read", "error": repr(problem)}]


def _join(contexts, frames, context_id):
    """What the wire knows now of a context, of its frame and of the frame's parent (copies: the entries change later); ``None`` for what it does
    not know (a context announced before the window, or never)."""
    context = dict(contexts.get(context_id) or {}) or None
    frame = dict(frames.get((context or {}).get("frame")) or {}) or None
    parent = dict(frames.get((frame or {}).get("parent")) or {}) or None
    return context, frame, parent


def _after(start_m, end_m):
    try:
        return round(end_m - start_m, 4)
    except Exception:
        return None


def _summarize_wire(path, since_m, marker) -> list:
    out = []
    contexts = {}          # context id -> {"frame", "name", "created_m", "destroyed_m"}
    frames = {}            # frame id -> {"url", "name", "parent", "attached_m", "detached_m", "committed_m"}
    sent = {}              # command id -> the record of the command
    waiting = {}           # command id -> what the wire knew of its context and frame when it was sent, until it is answered
    last_m = None
    unparsed = 0
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except Exception:
                unparsed += 1
                continue
            m = row.get("m")
            if row.get("dir") == "note" or m is None or (since_m is not None and m < since_m):
                continue
            try:
                message = json.loads(row.get("msg") or "")
            except Exception:
                unparsed += 1
                out.append({"event": "unread message", "m": m, "t": row.get("t"), "dir": row.get("dir"), "len": row.get("len"),
                            "cut": row.get("cut"), "start": _short(row.get("msg"), 200)})
                continue
            if not isinstance(message, dict):
                continue
            last_m = m if last_m is None else max(last_m, m)
            base = {"t": row.get("t"), "m": m, "dir": row.get("dir")}
            method, params = message.get("method"), message.get("params") or {}
            if row.get("dir") == "out" and method in _CALLS:
                code = params.get("expression") or params.get("functionDeclaration") or ""
                record = dict(base, event="command", method=method, id=message.get("id"), sessionId=message.get("sessionId"),
                              executionContextId=params.get("executionContextId"), code=_short(code))
                sent[message.get("id")] = record
                out.append(record)
                context, frame, parent = _join(contexts, frames, params.get("executionContextId"))
                waiting[message.get("id")] = {"record": record, "context": context, "frame": frame, "parent": parent}
                if marker and marker in str(code):
                    out.append(dict(base, event="marker command", id=message.get("id"), executionContextId=params.get("executionContextId"),
                                    context=context, frame=frame, parent=parent))
            elif row.get("dir") == "in" and message.get("id") in sent:
                answer = message.get("error")
                result = message.get("result")
                command = sent[message.get("id")]
                waiting.pop(message.get("id"), None)
                out.append(dict(base, event="answer", id=message.get("id"), to=command.get("code"), after_s=_after(command.get("m"), m),
                                error=_short(answer, 400) if answer is not None else None,
                                result=_short(json.dumps(result, default=str), 300) if answer is None else None))
            elif row.get("dir") == "in" and method in _EVENTS:
                out.append(dict(base, event=method, sessionId=message.get("sessionId"), params=_plain(params)))
                if method == "Runtime.executionContextCreated":
                    aux = params.get("auxData") or {}
                    contexts[params.get("executionContextId")] = {"frame": aux.get("frameId"), "name": aux.get("name"), "created_m": m,
                                                                   "destroyed_m": None}
                elif method == "Runtime.executionContextDestroyed":
                    if params.get("executionContextId") in contexts:
                        contexts[params.get("executionContextId")]["destroyed_m"] = m
                elif method == "Page.frameAttached":
                    frames.setdefault(params.get("frameId"), {})
                    frames[params.get("frameId")].update(parent=params.get("parentFrameId"), attached_m=m)
                elif method == "Page.frameDetached":
                    frames.setdefault(params.get("frameId"), {})["detached_m"] = m
                elif method == "Page.navigationCommitted":
                    frames.setdefault(params.get("frameId"), {}).update(url=params.get("url"), name=params.get("name"), committed_m=m)
            if len(out) >= SUMMARY_LIMIT:
                out.append({"event": "summary cut", "limit": SUMMARY_LIMIT})
                break
    for command_id, known in list(waiting.items())[:UNANSWERED_LIMIT]:
        record = known["record"]
        context_now, frame_now, parent_now = _join(contexts, frames, record.get("executionContextId"))
        out.append({"event": "command without an answer", "id": command_id, "method": record.get("method"), "sessionId": record.get("sessionId"),
                    "executionContextId": record.get("executionContextId"), "code": record.get("code"), "sent_m": record.get("m"),
                    "waited_s": _after(record.get("m"), last_m), "context_known_when_sent": known["context"] is not None,
                    "context_when_sent": known["context"], "frame_when_sent": known["frame"], "parent_when_sent": known["parent"],
                    "context_at_the_end": context_now, "frame_at_the_end": frame_now, "parent_at_the_end": parent_now})
    out.append({"event": "summary end", "unparsed_lines": unparsed, "last_m": last_m, "commands_without_an_answer": len(waiting), "since_m": since_m,
                "contexts_known": len(contexts), "frames_known": len(frames)})
    return out
