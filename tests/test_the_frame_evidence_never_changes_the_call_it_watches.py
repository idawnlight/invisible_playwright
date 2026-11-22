"""The evidence of the Fission frame tests (``tests/_frame_evidence.py``) is a listener: it never changes the call it watches.

A probe that raised, or took a keyword that a field of the data also used, has already broken two tests (``what=`` and ``source=``). Here every
recording is made to fail on purpose, and the watched call is the same:

* ``observed`` returns the very object the call returned and raises the very exception it raised, when the list that keeps the records raises on
  every append, when the description raises, and when the data holds what JSON cannot write;
* a record keeps the data under ``data``: fields named ``what``, ``source``, ``event``, ``t``, ``m`` collide with nothing;
* the handlers a page is given never raise into the client's dispatch, and a page that refuses ``on`` is still listened to as far as it can;
* ``OnFailure`` lets the body's exception through, the same one, when the frames cannot be read, the wire cannot be read and the save raises; it
  saves nothing for a skip;
* the reading of the wire joins the context id an evaluate was sent to with the frame and url that context belongs to, and the answer; a log
  with lines that are not JSON, or a file that is not there, gives records and no exception;
* the reading of the wire also says, for every command with no answer whatever its code, where it was sent (context, frame, url), how long it has
  waited and whether its context was destroyed since: the ``Runtime.evaluate`` of ``set_content`` has no marker in its code;
* the stall witness and the load sampler that ``OnFailure`` may start never raise into the body, whatever they or the exception's own
  ``add_note`` do, and what they say is a note on the exception, not a change of it.
"""
from __future__ import annotations

import json

import pytest

from _frame_evidence import FrameWatch, OnFailure, ProcessProbes, Records, summarize_wire

pytestmark = pytest.mark.unit


class _Refuses(list):
    """The list that keeps the records, made to fail on every append."""

    def append(self, item):
        raise OSError("the record cannot be kept")


def _failing_watch():
    return FrameWatch(records=_Refuses())


def test_a_call_returns_what_it_returned_when_every_recording_fails():
    watch = _failing_watch()
    sentinel = object()

    def describe(stage, value):
        raise RuntimeError("the description fails at " + stage)

    assert watch.observed("a call", lambda: sentinel, describe) is sentinel


def test_a_call_raises_what_it_raised_when_every_recording_fails():
    watch = _failing_watch()
    problem = ValueError("the call's own error")

    def call():
        raise problem

    with pytest.raises(ValueError) as caught:
        watch.observed("a call", call, lambda stage, value: 1 / 0)
    assert caught.value is problem


def test_a_call_that_is_interrupted_is_still_interrupted():
    watch = _failing_watch()

    def call():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        watch.observed("a call", call)


def test_data_that_json_cannot_write_is_recorded_and_changes_nothing():
    watch = FrameWatch()
    loop = []
    loop.append(loop)
    assert watch.observed("a call", lambda: 7, lambda stage, value: {"loop": loop, "object": object()}) == 7
    assert [r["event"] for r in watch.records] == ["a call: start", "a call: returned"]
    assert all(isinstance(r["data"], (dict, type(None))) for r in watch.records)


def test_a_field_named_like_a_keyword_collides_with_nothing():
    watch = FrameWatch()
    data = {"what": "w", "source": "s", "event": "e", "t": "T", "m": "M", "data": "d", "label": "l"}
    watch.note("a record", data)
    assert watch.observed("a call", lambda: "ok", lambda stage, value: dict(data, stage=stage)) == "ok"
    record = watch.records[0]
    assert record["event"] == "a record" and record["data"] == data
    assert isinstance(record["t"], float) and isinstance(record["m"], float)
    assert watch.records[2]["data"] == dict(data, stage="returned")


def test_what_a_call_raised_is_in_the_record_and_the_clock_runs_forward():
    watch = FrameWatch()

    def call():
        raise LookupError("gone")

    with pytest.raises(LookupError):
        watch.observed("a call", call)
    start, raised = watch.records
    assert raised["event"] == "a call: raised" and raised["data"]["type"] == "LookupError" and "gone" in raised["data"]["message"]
    assert raised["m"] >= start["m"]


class _Frame:
    def __init__(self, url, name="", parent=None):
        self.url, self.name, self.parent_frame, self._guid = url, name, parent, "frame@" + url

    def is_detached(self):
        return False


class _Page:
    """A page that keeps its handlers and can be made to raise on ``on`` or on ``frames``."""

    def __init__(self, refuse_on=False, refuse_frames=False):
        self.handlers = {}
        self.refuse_on, self.refuse_frames = refuse_on, refuse_frames
        self._frames = [_Frame("http://a/")]

    def on(self, event, handler):
        if self.refuse_on:
            raise RuntimeError("no listener")
        self.handlers[event] = handler

    @property
    def frames(self):
        if self.refuse_frames:
            raise RuntimeError("no frames")
        return list(self._frames)


def test_the_handlers_given_to_a_page_record_the_frame_and_never_raise():
    watch = FrameWatch()
    page = _Page()
    watch.listen(page, "second")
    assert set(page.handlers) == {"frameattached", "framenavigated", "framedetached", "domcontentloaded", "load"}
    top = _Frame("http://a/")
    page.handlers["framenavigated"](_Frame("http://b/x", "b", top))
    page.handlers["load"](page)
    first, loaded = watch.records
    assert first["event"] == "framenavigated" and first["data"]["page"] == "second"
    assert first["data"]["frame"]["url"] == "http://b/x" and first["data"]["frame"]["parent_url"] == "http://a/"
    assert loaded["data"]["frames"][0]["url"] == "http://a/"
    failing = _failing_watch()
    failing.listen(page, "second")
    for event, handler in page.handlers.items():
        handler(object())       # the argument has none of a frame's fields
        handler(_page_that_raises())


def _page_that_raises():
    class Bad:
        def __getattr__(self, name):
            raise RuntimeError("no " + name)
    return Bad()


def test_a_page_that_refuses_listeners_is_not_an_error():
    watch = FrameWatch()
    watch.listen(_Page(refuse_on=True), "second")
    assert watch.records == []


def test_a_frame_that_cannot_be_read_is_said_not_raised():
    data = FrameWatch.frame_data(_page_that_raises())
    assert set(data) >= {"url", "name"} and all(str(v).startswith("unreadable") for v in data.values() if isinstance(v, str))


class _Evidence:
    """What ``FailureEvidence`` gives ``OnFailure``: the wire log's path, the wire note, and a save that can be made to raise."""

    def __init__(self, wire_path, save_raises=False):
        self.wire_path, self.save_raises, self.saved, self.notes = wire_path, save_raises, [], []

    def _write_wire(self, record):
        self.notes.append(record)
        if self.save_raises:
            raise OSError("the wire log is closed")

    def _save(self, exc_type, exc):
        self.saved.append((exc_type, exc))
        if self.save_raises:
            raise OSError("the folder cannot be written")


def test_the_body_fails_with_its_own_exception_when_everything_the_evidence_does_fails(tmp_path):
    evidence = _Evidence(tmp_path / "missing.jsonl", save_raises=True)
    watch = _failing_watch()
    watch.listen(_Page(refuse_frames=True), "second")
    problem = AssertionError("the test's own assertion")
    with pytest.raises(AssertionError) as caught:
        with OnFailure(evidence, watch):
            raise problem
    assert caught.value is problem
    assert evidence.saved == [(AssertionError, problem)]


def test_a_body_that_passes_saves_nothing_and_a_skip_saves_nothing(tmp_path):
    evidence = _Evidence(tmp_path / "wire.jsonl")
    with OnFailure(evidence, FrameWatch()):
        pass
    with pytest.raises(pytest.skip.Exception):
        with OnFailure(evidence, FrameWatch()):
            pytest.skip("not here")
    assert evidence.saved == []


def test_a_failure_leaves_the_frames_of_every_page_and_the_reading_of_the_wire(tmp_path):
    wire = tmp_path / "wire.jsonl"
    wire.write_text(json.dumps({"t": 1.0, "m": 1.0, "dir": "in", "len": 2, "cut": False, "msg": json.dumps(
        {"method": "Page.frameAttached", "params": {"frameId": "f1", "parentFrameId": None}})}) + "\n", encoding="utf-8")
    evidence = _Evidence(wire)
    watch = FrameWatch()
    page = _Page()
    watch.listen(page, "second")
    with pytest.raises(AssertionError):
        with OnFailure(evidence, watch):
            raise AssertionError("red")
    events = [r["event"] for r in watch.records]
    assert events[0] == "test starts" and "page.frames at the failure" in events and "failure" in events
    shown = [r for r in watch.records if r["event"] == "page.frames at the failure"][0]["data"]
    assert shown["second"][0]["url"] == "http://a/"
    assert watch.wire.records[-1]["event"] == "summary end"
    assert evidence.notes[0]["dir"] == "note"


# ---- the reading of the wire --------------------------------------------------------------------------------------------
def _row(m, direction, message, cut=False):
    text = json.dumps(message)
    return {"t": 1000 + m, "m": m, "dir": direction, "len": len(text), "cut": cut, "msg": text if not cut else text[:10]}


def _write(path, rows, extra=()):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "".join(extra), encoding="utf-8")


def test_the_reading_joins_an_evaluate_with_its_context_its_frame_its_url_and_its_answer(tmp_path):
    wire = tmp_path / "wire.jsonl"
    rows = [
        _row(0.5, "in", {"method": "Page.frameAttached", "params": {"frameId": "old", "parentFrameId": None}}),     # before the window
        _row(1.0, "in", {"method": "Page.frameAttached", "params": {"frameId": "top", "parentFrameId": None}}),
        _row(1.1, "in", {"method": "Page.navigationCommitted", "params": {"frameId": "top", "url": "http://a/storage", "name": ""}}),
        _row(1.2, "in", {"method": "Page.frameAttached", "params": {"frameId": "b", "parentFrameId": "top"}}),
        _row(1.3, "in", {"method": "Page.navigationCommitted", "params": {"frameId": "b", "url": "http://localhost/storage-b", "name": "b"}}),
        _row(1.4, "in", {"method": "Runtime.executionContextCreated", "params": {"executionContextId": "ctx-12", "auxData": {"frameId": "b", "name": ""}}}),
        _row(1.5, "out", {"id": 7, "method": "Runtime.evaluate", "sessionId": "s1",
                          "params": {"executionContextId": "ctx-12", "expression": "window.__r", "returnByValue": True}}),
        _row(1.6, "in", {"method": "Runtime.executionContextDestroyed", "params": {"executionContextId": "ctx-12"}}),
        _row(1.7, "in", {"id": 7, "error": {"message": "Execution context was destroyed!"}}),
    ]
    _write(wire, rows, extra=["this is not json\n", json.dumps(_row(1.8, "in", {"method": "Page.x"}, cut=True)) + "\n"])
    records = summarize_wire(wire, since_m=1.0)
    marker = [r for r in records if r["event"] == "marker command"][0]
    assert marker["executionContextId"] == "ctx-12"
    assert marker["context"]["frame"] == "b" and marker["context"]["destroyed_m"] is None      # destroyed after the command went out
    assert marker["frame"]["url"] == "http://localhost/storage-b" and marker["parent"]["url"] == "http://a/storage"
    answer = [r for r in records if r["event"] == "answer"][0]
    assert answer["id"] == 7 and "destroyed" in answer["error"] and answer["to"] == "window.__r"
    assert not [r for r in records if r.get("params", {}).get("frameId") == "old"]
    end = records[-1]
    assert end["event"] == "summary end" and end["unparsed_lines"] == 2          # the line that is not JSON, the cut message
    assert [r["event"] for r in records].count("unread message") == 1


def test_the_reading_of_a_file_that_is_not_there_is_a_record(tmp_path):
    records = summarize_wire(tmp_path / "missing.jsonl", since_m=0.0)
    assert [r["event"] for r in records] == ["the wire could not be read"]


def test_a_records_list_keeps_what_it_is_given_and_never_raises():
    records = Records(records=_Refuses())
    records.add({"event": "x"})
    assert list(records.records) == []


# ---- a command with no answer, whatever its code -----------------------------------------------------------------------------
def _page_rows():
    return [
        _row(1.0, "in", {"method": "Page.frameAttached", "params": {"frameId": "top", "parentFrameId": None}}),
        _row(1.1, "in", {"method": "Page.navigationCommitted", "params": {"frameId": "top", "url": "about:blank", "name": ""}}),
        _row(1.2, "in", {"method": "Runtime.executionContextCreated", "params": {"executionContextId": "ctx-1", "auxData": {"frameId": "top", "name": ""}}}),
        _row(1.3, "out", {"id": 3, "method": "Runtime.evaluate", "sessionId": "s1",
                          "params": {"executionContextId": "ctx-1", "expression": "(() => { document.open(); document.write(1); })()"}}),
        _row(1.4, "out", {"id": 4, "method": "Runtime.callFunction", "sessionId": "s1",
                          "params": {"executionContextId": "ctx-1", "functionDeclaration": "() => 1"}}),
        _row(1.5, "in", {"id": 4, "result": {"value": 1}}),
    ]


def test_a_command_with_no_answer_is_joined_with_its_context_though_its_code_has_no_marker(tmp_path):
    wire = tmp_path / "wire.jsonl"
    _write(wire, _page_rows() + [
        _row(31.3, "in", {"method": "Runtime.executionContextDestroyed", "params": {"executionContextId": "ctx-1"}}),     # after it was sent
    ])
    records = summarize_wire(wire, since_m=1.0)
    assert not [r for r in records if r["event"] == "marker command"]
    (silent,) = [r for r in records if r["event"] == "command without an answer"]
    assert silent["id"] == 3 and silent["method"] == "Runtime.evaluate" and silent["executionContextId"] == "ctx-1"
    assert "document.open()" in silent["code"] and silent["sent_m"] == 1.3 and silent["waited_s"] == 30.0
    assert silent["context_known_when_sent"] and silent["context_when_sent"]["frame"] == "top"
    assert silent["context_when_sent"]["destroyed_m"] is None and silent["context_at_the_end"]["destroyed_m"] == 31.3
    assert silent["frame_when_sent"]["url"] == "about:blank"
    end = records[-1]
    assert end["event"] == "summary end" and end["commands_without_an_answer"] == 1 and end["last_m"] == 31.3
    answered = [r for r in records if r["event"] == "answer"][0]
    assert answered["id"] == 4 and abs(answered["after_s"] - 0.1) < 1e-9


def test_a_command_to_a_context_the_wire_never_announced_says_so(tmp_path):
    wire = tmp_path / "wire.jsonl"
    _write(wire, [_row(2.0, "out", {"id": 9, "method": "Runtime.evaluate", "params": {"executionContextId": "ctx-gone", "expression": "1"}}),
                  _row(9.0, "in", {"method": "Page.navigationStarted", "params": {"frameId": "top"}})])
    (silent,) = [r for r in summarize_wire(wire, since_m=0.0) if r["event"] == "command without an answer"]
    assert silent["context_known_when_sent"] is False and silent["context_when_sent"] is None and silent["waited_s"] == 7.0


def test_the_commands_without_an_answer_are_said_in_one_line_and_a_log_without_any_says_nothing(tmp_path):
    wire = tmp_path / "wire.jsonl"
    _write(wire, _page_rows())
    watch = FrameWatch()
    watch.wire.records.extend(summarize_wire(wire, since_m=1.0))
    line = watch.unanswered_line()
    assert "Runtime.evaluate #3 to context ctx-1" in line and "about:blank" in line and "alive" in line
    assert FrameWatch().unanswered_line() == ""
    assert _failing_watch().unanswered_line() == ""


# ---- the stall witness and the load of the process ------------------------------------------------------------------------------
class _Load:
    def __init__(self, raises=False):
        self.raises = raises

    def cpu_percent(self, percpu=False):
        if self.raises:
            raise OSError("no counters")
        return 40.0


class _Exploding:
    """The probes of an ``OnFailure``, every one of which raises."""

    def start(self):
        raise RuntimeError("start")

    def stop(self):
        raise RuntimeError("stop")

    def line(self):
        raise RuntimeError("line")


def test_the_probes_start_stop_and_say_one_line_in_wire_time():
    probes = ProcessProbes(load_source=_Load())
    probes.start()
    probes.stop()
    line = probes.line()
    assert "no stall of the test process" in line and "runner CPU max 40%" in line
    assert probes.witness.beats >= 2 and probes.load_records.records[-1]["cpu_percent"] == 40.0
    assert probes.stall_records.records[-1]["event"] == "the witness stopped"


def test_a_load_source_that_fails_is_a_record_and_a_line_still_comes():
    probes = ProcessProbes(load_source=_Load(raises=True))
    probes.start()
    probes.stop()
    assert "runner CPU: no sample" in probes.line()
    assert any(r.get("event") == "the sampler did not start" for r in probes.load_records.records)


def test_a_stall_is_said_with_its_gap_and_the_wire_time_it_began():
    probes = ProcessProbes(load_source=_Load())
    probes.stall_records.add({"origin": "witness", "event": "stall", "from_gw": 12.5, "to_gw": 14.0, "gap_s": 1.5})
    assert "1 stall(s) of the test process over 200 ms: 1.50s at m=12.50" in probes.line()


def test_probes_that_raise_never_change_the_exception_of_the_body(tmp_path):
    evidence = _Evidence(tmp_path / "missing.jsonl")
    problem = AssertionError("the test's own assertion")
    with pytest.raises(AssertionError) as caught:
        with OnFailure(evidence, _failing_watch(), _Exploding()):
            raise problem
    assert caught.value is problem and str(problem) == "the test's own assertion"
    assert evidence.saved == [(AssertionError, problem)]


def test_an_exception_that_refuses_a_note_is_still_the_same_exception(tmp_path):
    class Refuses(Exception):
        def add_note(self, note):
            raise RuntimeError("no notes")

    problem = Refuses("kept")
    evidence = _Evidence(tmp_path / "missing.jsonl")
    with pytest.raises(Refuses) as caught:
        with OnFailure(evidence, FrameWatch(), ProcessProbes(load_source=_Load())):
            raise problem
    assert caught.value is problem and evidence.saved == [(Refuses, problem)]


def test_a_failure_carries_the_process_line_as_a_note_and_the_probes_run_for_the_body(tmp_path):
    wire = tmp_path / "wire.jsonl"
    evidence = _Evidence(wire)
    watch = FrameWatch()
    probes = ProcessProbes(load_source=_Load())
    problem = ValueError("no response in 30s")
    with pytest.raises(ValueError) as caught:
        with OnFailure(evidence, watch, probes):
            assert probes.witness.beats >= 1            # started with the body
            _write(wire, [_row(watch.started_m + 1.0, "out", {"id": 1, "method": "Runtime.evaluate",
                                                              "params": {"executionContextId": "c", "expression": "1"}})])
            raise problem
    assert caught.value is problem and str(problem) == "no response in 30s"
    notes = getattr(problem, "__notes__", None)
    if notes is not None:                               # Python 3.11 and later
        assert len(notes) == 1 and "Runtime.evaluate #1 to context c" in notes[0] and "no stall of the test process" in notes[0]
    said = [r for r in watch.records if r["event"] == "said at the failure"][0]["data"]["lines"]
    assert said[0].startswith("wire: ") and said[1].startswith("process: ")
    assert probes.stall_records.records[-1]["event"] == "the witness stopped"          # stopped before the evidence was saved


def test_a_body_that_passes_stops_the_probes_and_says_nothing(tmp_path):
    evidence = _Evidence(tmp_path / "wire.jsonl")
    probes = ProcessProbes(load_source=_Load())
    watch = FrameWatch()
    with OnFailure(evidence, watch, probes):
        pass
    assert evidence.saved == [] and probes.stall_records.records[-1]["event"] == "the witness stopped"
    assert not [r for r in watch.records if r["event"] == "said at the failure"]
