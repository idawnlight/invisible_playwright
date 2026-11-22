"""The recorders a failed far-away gather leaves (tests/_far_away_probes.py, started by ``_Watch`` of tests/test_webrtc_realness.py), no browser.

They say where the time went when the core's UDP measurement or the gather misses its window: the times of the control connections of the
gateway, the DEBUG records of the core's logger of that measurement, a witness of stalls of the whole process, and the runner's load. Each
one is bookkeeping and must never change what it watches, so each is proved twice: it sees what it is made to see (a stall of the whole
process made on purpose, a log record, a slow gateway), and it is made to fail on purpose (a clock, a load source, a message that cannot be
formatted, a record that cannot be kept) and then raises nothing into the test or into a server.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time

import pytest

import _far_away_probes as probes
import test_the_far_away_watch_never_breaks_the_gateway as watched
import test_webrtc_realness as realness
from _socks_servers import coherent

pytestmark = pytest.mark.unit


class _Clock:
    """A clock the test moves: each call gives the next value (an exception is raised)."""

    def __init__(self, *values):
        self.values = list(values)

    def __call__(self):
        value = self.values.pop(0) if len(self.values) > 1 else self.values[0]
        if isinstance(value, Exception):
            raise value
        return value


def _stalls(records):
    return [r for r in records.all() if r["event"] == "stall"]


# ---- the witness of the whole process -------------------------------------------------------------------------------------------------
def test_a_gap_between_two_beats_over_the_threshold_is_a_stall_with_its_times():
    records = probes.Records()
    witness = probes.StallWitness(records, epoch=100.0, clock=_Clock(100.0, 100.05, 100.10, 101.60, 101.65))
    for _ in range(5):
        witness.beat()
    [stall] = _stalls(records)
    assert stall["from_gw"] == pytest.approx(0.1) and stall["to_gw"] == pytest.approx(1.6) and stall["gap_s"] == pytest.approx(1.5)
    assert witness.beats == 5 and witness.longest_gap_s == pytest.approx(1.5)


def test_beats_that_keep_their_pace_are_no_stall():
    records = probes.Records()
    witness = probes.StallWitness(records, epoch=0.0, clock=_Clock(*[i * 0.05 for i in range(40)]))
    for _ in range(40):
        witness.beat()
    assert _stalls(records) == [] and witness.beats == 40


def test_the_end_of_the_watch_counts_the_gap_since_the_last_beat():
    """A process stalled until the test ended has no later beat: stopping makes the last one, and the long gap is said."""
    records = probes.Records()
    witness = probes.StallWitness(records, epoch=0.0, clock=_Clock(0.0, 0.05, 2.0))
    witness.beat()
    witness.beat()
    witness.stop()
    [stall] = _stalls(records)
    assert stall["from_gw"] == pytest.approx(0.05) and stall["gap_s"] == pytest.approx(1.95)
    assert [r for r in records.all() if r["event"] == "the witness stopped"][0]["beats"] == 3


def _hold_the_gil(seconds):
    """A call into C that keeps the GIL for ``seconds``: every other thread of this process is stopped, as a long call of a library would."""
    if sys.platform == "win32":
        ctypes.PyDLL("kernel32").Sleep(int(seconds * 1000))
    else:
        ctypes.PyDLL(None).usleep(int(seconds * 1000000))


def test_a_stall_of_the_whole_process_is_seen_by_the_thread_that_beats():
    records = probes.Records()
    epoch = time.monotonic()
    witness = probes.StallWitness(records, epoch)
    witness.start()
    try:
        time.sleep(0.2)
        before = time.monotonic() - epoch
        _hold_the_gil(0.6)
        after = time.monotonic() - epoch
        time.sleep(0.2)
    finally:
        witness.stop()
    stalls = _stalls(records)
    assert stalls, records.all()
    longest = max(stalls, key=lambda s: s["gap_s"])
    assert longest["gap_s"] >= 0.5 and longest["from_gw"] <= before + 0.1 and longest["to_gw"] >= after - 0.1, (longest, before, after)
    assert witness.beats > 5 and not witness._thread.is_alive()


def test_a_clock_that_fails_is_a_record_and_the_witness_goes_on():
    records = probes.Records()
    witness = probes.StallWitness(records, epoch=0.0, clock=_Clock(0.0, RuntimeError("the clock is broken"), 0.1, 0.15))
    for _ in range(4):
        witness.beat()
    said = [r for r in records.all() if r["event"] == "a beat could not be made"]
    assert len(said) == 1 and "the clock is broken" in said[0]["error"]
    assert witness.beats == 3


def test_records_that_cannot_be_kept_raise_nothing():
    records = probes.Records()
    records.add({"event": "x", "data": object()})
    records.add(None)                                                              # not a record at all
    records.records = None                                                         # a list that is gone
    records.add({"event": "y"})
    witness = probes.StallWitness(records, epoch=0.0, clock=_Clock(0.0, 5.0))
    witness.beat()
    witness.beat()
    witness.stop()


# ---- the load of the runner -------------------------------------------------------------------------------------------------------------
class _Load:
    def __init__(self, cpu=(10.0, 97.5), loadavg=(1.0, 0.5, 0.25)):
        self.cpu, self.loadavg = list(cpu), loadavg
        self.calls = []

    def cpu_percent(self, percpu=None):
        self.calls.append(percpu)
        value = self.cpu.pop(0) if len(self.cpu) > 1 else self.cpu[0]
        if isinstance(value, Exception):
            raise value
        return value

    def getloadavg(self):
        if isinstance(self.loadavg, Exception):
            raise self.loadavg
        return self.loadavg


def test_the_load_is_sampled_with_percpu_false_and_the_load_average():
    records, source = probes.Records(), _Load(cpu=(0.0, 12.5))
    sampler = probes.LoadSampler(records, epoch=time.monotonic(), source=source)
    sampler.start()
    sampler.stop()
    samples = sampler.samples()
    assert samples[-1]["cpu_percent"] == 12.5 and set(source.calls) == {False}
    assert samples[-1]["loadavg_1_5_15"] == [1.0, 0.5, 0.25]


def test_a_source_without_a_load_average_gives_the_cpu_only():
    class Cpu:
        def cpu_percent(self, percpu=None):
            return 40.0

    records = probes.Records()
    sampler = probes.LoadSampler(records, epoch=0.0, source=Cpu())
    sampler.sample()
    [sample] = sampler.samples()
    assert sample["cpu_percent"] == 40.0 and "loadavg_1_5_15" not in sample and "loadavg_error" not in sample


def test_the_real_psutil_gives_a_percentage_and_a_load_average():
    records = probes.Records()
    sampler = probes.LoadSampler(records, epoch=time.monotonic())
    sampler.start()
    time.sleep(0.6)
    sampler.stop()
    samples = sampler.samples()
    assert samples and all(0.0 <= s["cpu_percent"] <= 100.0 for s in samples), records.all()
    assert all(len(s["loadavg_1_5_15"]) == 3 for s in samples), records.all()


@pytest.mark.parametrize("source", [_Load(cpu=(0.0, RuntimeError("psutil is broken"))), _Load(cpu=(0.0, 1.0), loadavg=OSError("no load"))],
                         ids=["the_cpu_reading", "the_load_average"])
def test_a_load_source_that_fails_is_a_record_and_raises_nothing(source):
    records = probes.Records()
    sampler = probes.LoadSampler(records, epoch=0.0, source=source)
    sampler.start()
    sampler.sample()
    sampler.stop()
    said = [r for r in records.all() if "error" in r or "loadavg_error" in r]
    assert said, records.all()


def test_a_module_that_is_not_there_is_said_and_raises_nothing(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    records = probes.Records()
    sampler = probes.LoadSampler(records, epoch=0.0)
    sampler.start()
    sampler.stop()
    assert [r for r in records.all() if r["event"] == "the sampler did not start"]


# ---- the core's logger ----------------------------------------------------------------------------------------------------------------
def test_the_logger_name_is_the_one_of_the_core():
    from invisible_core import _udp_exit
    assert probes.UDP_EXIT_LOGGER == _udp_exit.log.name


def test_the_debug_records_of_the_core_are_kept_and_the_logger_is_put_back():
    from invisible_core import _udp_exit
    logger = logging.getLogger(probes.UDP_EXIT_LOGGER)
    level, handlers = logger.level, list(logger.handlers)
    logger.setLevel(logging.WARNING)                  # the level of a run: DEBUG would not reach any handler
    try:
        records = probes.Records()
        capture = probes.LogCapture(records, epoch=time.monotonic())
        capture.start()
        try:
            _udp_exit.log.debug("the UDP exit through the proxy: %s", "unmeasured")
            thread = threading.Thread(target=_udp_exit.log.debug, args=("the UDP exit could not be measured: %s", "OSError"),
                                      name="invisible-udp-exit")
            thread.start()
            thread.join()
        finally:
            capture.stop()
        kept = [(r["message"], r["thread"]) for r in records.all() if r["event"] == "log"]
        assert kept == [("the UDP exit through the proxy: unmeasured", threading.current_thread().name),
                        ("the UDP exit could not be measured: OSError", "invisible-udp-exit")]
        assert logger.level == logging.WARNING and logger.handlers == handlers
        _udp_exit.log.debug("after the capture")
        assert len([r for r in records.all() if r["event"] == "log"]) == 2
    finally:
        logger.setLevel(level)


def test_the_logger_is_put_back_when_the_body_fails():
    logger = logging.getLogger(probes.UDP_EXIT_LOGGER)
    level, handlers = logger.level, list(logger.handlers)
    capture = probes.LogCapture(probes.Records(), epoch=0.0)
    with pytest.raises(ZeroDivisionError):
        capture.start()
        try:
            1 / 0
        finally:
            capture.stop()
    assert logger.level == level and logger.handlers == handlers


def test_a_log_record_that_cannot_be_formatted_is_a_record_and_raises_nothing_into_the_core():
    logger = logging.getLogger(probes.UDP_EXIT_LOGGER)
    records = probes.Records()
    capture = probes.LogCapture(records, epoch=0.0)
    bad = logging.LogRecord(logger.name, logging.DEBUG, __file__, 1, "%d", ("not a number",), None)       # getMessage() raises TypeError
    capture.start()
    try:
        capture._handler.emit(bad)                   # the handler alone: the other handlers of the run (pytest's) have their own rules
        logger.debug("fine")
    finally:
        capture.stop()
    said = [r for r in records.all() if r["event"] == "a log record could not be kept"]
    assert said and "TypeError" in said[0]["error"]
    assert [r["message"] for r in records.all() if r["event"] == "log"] == ["fine"]


def test_a_clock_that_fails_in_the_handler_raises_nothing_into_the_core():
    logger = logging.getLogger(probes.UDP_EXIT_LOGGER)
    records = probes.Records()
    capture = probes.LogCapture(records, epoch=0.0, clock=_Clock(RuntimeError("the clock is broken")))
    capture.start()
    try:
        logger.debug("a record")
    finally:
        capture.stop()
    assert [r for r in records.all() if r["event"] == "a log record could not be kept"]


# ---- the times of the control connections --------------------------------------------------------------------------------------------
def _control(conn, gw0, wait):
    return [
        {"event": "control connection accepted", "conn": conn, "gw": gw0},
        {"event": "answer sent", "conn": conn, "what": "method selection (0x00 none, 0x02 user/password, 0xff none acceptable)", "gw": gw0 + 0.1,
         "asked_gw": gw0 + 0.02, "kind": None},
        {"event": "answer sent", "conn": conn, "what": "authentication status (0x00 accepted)", "gw": gw0 + 0.2, "asked_gw": gw0 + 0.12, "kind": None},
        {"event": "request", "conn": conn, "command": "UDP ASSOCIATE", "gw": gw0 + 0.25},
        {"event": "answer sent", "conn": conn, "what": "reply (REP 0 = succeeded)", "rep": 0, "kind": "associate", "gw": gw0 + 0.26 + wait,
         "asked_gw": gw0 + 0.26},
    ]


def test_the_times_of_a_control_connection_are_read_from_the_watch_and_the_slow_answer_shows():
    table = probes.control_times(_control(0, 0.5, 1.5) + _control(1, 2.0, 0.1))
    assert table[0]["connect"] == 0.5 and table[0]["greeting"] == 0.6 and table[0]["authentication"] == 0.7
    assert table[0]["associate_asked"] == 0.75 and table[0]["associate_answered"] == 2.26 and table[0]["longest_answer_wait_s"] == 1.5
    assert table[1]["longest_answer_wait_s"] == 0.1 and table[1]["connect"] == 2.0
    line = probes.control_line(table)
    assert "#0 0.50|0.60|0.70|0.75|2.26 (longest wait in the gateway 1.50s)" in line and "#1 2.00|2.10|2.20|2.25|2.36" in line
    assert "#0 is the first accepted" in line


def test_a_step_that_was_not_seen_is_said_with_a_question_mark_and_a_broken_record_raises_nothing():
    line = probes.control_line(probes.control_times([{"event": "control connection accepted", "conn": 0, "gw": 0.25}]))
    assert "#0 0.25|?|?|?|?" in line
    assert "none seen" in probes.control_line({})
    broken = probes.control_line(probes.control_times([object()]))        # not a record: nothing is read, and nothing is raised
    assert isinstance(broken, str) and ("not read" in broken or "none seen" in broken)


def test_the_message_names_the_core_measurement_the_stalls_and_the_load():
    kept = probes.FarAwayProbes(epoch=time.monotonic(), load_source=_Load(cpu=(0.0, 91.0)))
    kept.core.start()
    try:
        kept.mark("launch starts")
        logging.getLogger(probes.UDP_EXIT_LOGGER).debug("the UDP exit through the proxy: %s", "unmeasured")
        kept.mark("launch returned")
    finally:
        kept.core.stop()
    kept.sampler.start()
    kept.sampler.stop()
    kept.stall_records.add({"origin": "witness", "event": "stall", "from_gw": 12.3, "to_gw": 13.81, "gap_s": 1.51})
    text = kept.lines(_control(0, 0.5, 1.5))
    assert "launch starts at" in text and "'the UDP exit through the proxy: unmeasured'" in text and "launch returned at" in text
    assert "1 stall(s) of the whole process over 200 ms" in text and "1.51s (12.30 to 13.81)" in text
    assert "runner CPU every 0.5s" in text and "max 91%" in text and "load average max 1.00" in text


# ---- the watch on a real gateway ----------------------------------------------------------------------------------------------------
def _watch_with_probes(monkeypatch, load_source):
    gateway = coherent(user="alice", password="s3cret")
    evidence = watched._Evidence()
    evidence.note_cells({})
    watch = realness._Watch(gateway, monkeypatch, evidence, "127.0.0.1")
    watch.probes.sampler.source = load_source
    return gateway, watch, evidence


def test_a_watched_gather_keeps_the_probes_for_the_evidence_and_says_them_in_its_summary(monkeypatch):
    gateway, watch, evidence = _watch_with_probes(monkeypatch, _Load(cpu=(0.0, 3.0)))
    try:
        with watch:
            watch.mark("launch starts")
            logging.getLogger(probes.UDP_EXIT_LOGGER).debug("the UDP exit through the proxy: %s", "coherent")
            answer, _bnd = watched._associate_and_relay(gateway, ("alice", "s3cret"))
            watched._the_datagram_was_relayed(gateway, answer)
            watch.mark("launch returned")
    finally:
        gateway.close()
    assert {"stall-witness", "runner-load", "core-udp-exit-log", "gateway-control-connections"} <= set(evidence.records)
    text = watch.summary()
    assert "control connections of the gateway" in text and "#0 " in text and "|" in text
    assert "'the UDP exit through the proxy: coherent'" in text and "launch starts at" in text
    assert "no stall of the whole process" in text or "stall(s) of the whole process" in text
    assert "runner CPU" in text
    cells = evidence.cells
    assert set(cells) >= {"control_connection_times_s_from_the_gateway_start", "stalls_of_the_whole_process", "runner_load_samples",
                          "core_udp_exit_log_and_marks", "gateway"}
    assert cells["control_connection_times_s_from_the_gateway_start"][0]["associate_answered"] is not None
    assert not [h for h in logging.getLogger(probes.UDP_EXIT_LOGGER).handlers if isinstance(h, probes._KeepRecords)]


def test_a_failure_inside_the_watch_carries_the_probes_as_a_note_of_the_exception(monkeypatch):
    gateway, watch, _evidence = _watch_with_probes(monkeypatch, _Load(cpu=(0.0, 3.0)))
    try:
        with pytest.raises(TimeoutError) as raised:
            with watch:
                raise TimeoutError("no association in 30 s")
    finally:
        gateway.close()
    assert str(raised.value) == "no association in 30 s"                         # the message is the test's own
    assert raised.value.__notes__ and "far-away probes:" in raised.value.__notes__[0]


@pytest.mark.parametrize("breakage", ["clock", "load", "log"])
def test_a_probe_that_breaks_never_stops_the_gateway(monkeypatch, breakage):
    gateway, watch, _evidence = _watch_with_probes(monkeypatch, _Load(cpu=(0.0, 3.0)))
    if breakage == "clock":
        monkeypatch.setattr(watch.probes.witness, "_clock", _Clock(RuntimeError("the clock is broken")))
    elif breakage == "load":
        watch.probes.sampler.source = _Load(cpu=(0.0, RuntimeError("psutil is broken")))
    else:
        watch.probes.core.records = None                                        # a list of records that is not there
        watch.probes.core._handler.records = None
    try:
        with watch:
            logging.getLogger(probes.UDP_EXIT_LOGGER).debug("a record")
            answer, _bnd = watched._associate_and_relay(gateway, ("alice", "s3cret"))
            watched._the_datagram_was_relayed(gateway, answer)
        assert gateway.associates == 1
        assert isinstance(watch.summary(), str)
    finally:
        gateway.close()
