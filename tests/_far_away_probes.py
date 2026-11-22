"""What a failed far-away gather says about the machine and the core's own measurement (the recorders of ``_Watch`` in tests/test_webrtc_realness.py).

The gateway of these tests simulates distance with sleeps and timers inside the process of pytest, which also runs the core's UDP measurement.
When the gather fails the question is where the time went: in the gateway's own answers, in a stall of the whole worker process, or in a
runner with no CPU left. Three recorders answer it, each kept as a list of records the evidence saves (``FailureEvidence.note_records``) and
each said in a few lines of the failure message:

* the times of every control connection of the gateway (connect, greeting, authentication, ASSOCIATE asked and answered), read from the
  records the watch already makes, and how long the gateway sat on an answer;
* the records of the core's logger ``invisible_core._udp_exit`` at DEBUG (the outcome of its UDP measurement), set and removed around the test,
  next to the marks the test makes (the launch that runs the measurement starts and returns);
* a witness of the whole process (a daemon thread that beats every 50 ms and records the gaps over 200 ms) and the runner's CPU load,
  sampled every 0.5 s.

Nothing here changes an assertion, a wait, a deadline or a measurement: a recorder only listens, never raises into the test or into a server,
and takes every record as ONE dict (no keyword that can collide with a field of the data).
"""
from __future__ import annotations

import logging
import threading
import time

#: The logger of the core whose DEBUG records say how its UDP measurement ended (``invisible_core/_udp_exit.py``: ``log = getLogger(__name__)``).
UDP_EXIT_LOGGER = "invisible_core._udp_exit"
#: The beat of the witness and the gap it calls a stall, in seconds.
BEAT_S, STALL_S = 0.05, 0.2
#: How often the runner's load is sampled, in seconds.
LOAD_EVERY_S = 0.5
#: The most control connections and stalls said in the message (the evidence has all of them).
MESSAGE_CONNECTIONS, MESSAGE_STALLS = 3, 4


class Records:
    """A list of records for ``FailureEvidence.note_records`` (``lock``, ``records``). ``add`` never raises: it is called from threads of the test
    and, through the watch, from the gateway's own."""

    def __init__(self):
        self.lock = threading.Lock()
        self.records = []

    def add(self, record):
        try:
            record = dict(record)
            record.setdefault("t", round(time.time(), 4))
            with self.lock:
                self.records.append(record)
        except Exception as problem:  # noqa: BLE001 - recorded, never raised
            try:
                with self.lock:
                    self.records.append({"origin": "probe", "event": "a record could not be kept", "error": repr(problem)})
            except Exception:  # noqa: BLE001
                pass

    def all(self):
        with self.lock:
            return list(self.records)


def _say(value):
    return "?" if value is None else ("%.2f" % value)


# ---- the witness of the whole process ----------------------------------------------------------------------------------------------
class StallWitness:
    """A daemon thread that beats every ``beat_s`` and keeps the gaps over ``stall_s``. A call that holds the GIL (a long call into C) or a
    machine with no CPU left for this process delays the beat of every thread alike, so a gap is a stall of the whole worker process, the one
    that also holds the gateway's own threads. Times are seconds from ``epoch`` (``time.monotonic()`` of the start of the gateway: the scale
    of its records) and ``t``, the wall clock."""

    def __init__(self, records, epoch, beat_s=BEAT_S, stall_s=STALL_S, clock=time.monotonic):
        self.records, self.epoch, self.beat_s, self.stall_s, self._clock = records, epoch, beat_s, stall_s, clock
        self.beats = 0
        self.first = self.last = None
        self.longest_gap_s = 0.0
        self._stop = threading.Event()
        self._thread = None

    def beat(self):
        """One beat. Never raises: a clock that fails is a record."""
        try:
            now = self._clock()
            if self.last is None:
                self.first = now
            else:
                gap = now - self.last
                self.longest_gap_s = max(self.longest_gap_s, gap)
                if gap > self.stall_s:
                    self.records.add({"origin": "witness", "event": "stall", "from_gw": round(self.last - self.epoch, 4),
                                      "to_gw": round(now - self.epoch, 4), "gap_s": round(gap, 4)})
            self.last = now
            self.beats += 1
        except Exception as problem:  # noqa: BLE001
            self.records.add({"origin": "witness", "event": "a beat could not be made", "error": repr(problem)})

    def _run(self):
        try:
            while not self._stop.wait(self.beat_s):
                self.beat()
        except Exception as problem:  # noqa: BLE001
            self.records.add({"origin": "witness", "event": "the witness ended", "error": repr(problem)})

    def start(self):
        try:
            self.beat()
            self._thread = threading.Thread(target=self._run, name="far-away-stall-witness", daemon=True)
            self._thread.start()
        except Exception as problem:  # noqa: BLE001
            self.records.add({"origin": "witness", "event": "the witness did not start", "error": repr(problem)})

    def stop(self):
        """Ends the witness and says what it saw: the last gap (from its last beat to now) is a stall too if it is long."""
        try:
            self._stop.set()
            if self._thread is not None:
                self._thread.join(5)
            self.beat()
            self.records.add({"origin": "witness", "event": "the witness stopped", "beats": self.beats, "beat_s": self.beat_s,
                              "stall_s": self.stall_s, "longest_gap_s": round(self.longest_gap_s, 4),
                              "watched_s": None if self.first is None or self.last is None else round(self.last - self.first, 4)})
        except Exception as problem:  # noqa: BLE001
            self.records.add({"origin": "witness", "event": "the witness did not stop cleanly", "error": repr(problem)})

    def stalls(self):
        return [r for r in self.records.all() if r.get("event") == "stall"]


# ---- the load of the runner -------------------------------------------------------------------------------------------------------
class LoadSampler:
    """The runner's CPU load every ``every_s`` seconds: ``cpu_percent(percpu=False)`` since the previous sample and, where the system has it,
    the load average. ``source`` is ``psutil`` (a stand-in in the unit tests)."""

    def __init__(self, records, epoch, every_s=LOAD_EVERY_S, source=None, clock=time.monotonic):
        self.records, self.epoch, self.every_s, self._clock = records, epoch, every_s, clock
        self.source = source
        self._stop = threading.Event()
        self._thread = None

    def sample(self):
        """One sample. Never raises: a source that fails is a record."""
        try:
            record = {"origin": "load", "event": "sample", "gw": round(self._clock() - self.epoch, 4)}
            record["cpu_percent"] = self.source.cpu_percent(percpu=False)
            if hasattr(self.source, "getloadavg"):
                try:
                    record["loadavg_1_5_15"] = [round(v, 2) for v in self.source.getloadavg()]
                except Exception as problem:  # noqa: BLE001
                    record["loadavg_error"] = repr(problem)
            self.records.add(record)
        except Exception as problem:  # noqa: BLE001
            self.records.add({"origin": "load", "event": "a sample could not be taken", "error": repr(problem)})

    def _run(self):
        try:
            while not self._stop.wait(self.every_s):
                self.sample()
        except Exception as problem:  # noqa: BLE001
            self.records.add({"origin": "load", "event": "the sampler ended", "error": repr(problem)})

    def start(self):
        try:
            if self.source is None:
                import psutil
                self.source = psutil
            self.source.cpu_percent(percpu=False)       # the first reading has no window: it is only the start of the first one
            if hasattr(self.source, "getloadavg"):
                self.source.getloadavg()                 # on Windows the first call starts the thread that makes the average
            self._thread = threading.Thread(target=self._run, name="far-away-load-sampler", daemon=True)
            self._thread.start()
        except Exception as problem:  # noqa: BLE001
            self.records.add({"origin": "load", "event": "the sampler did not start", "error": repr(problem)})

    def stop(self):
        try:
            self._stop.set()
            if self._thread is not None:
                self._thread.join(5)
            self.sample()
        except Exception as problem:  # noqa: BLE001
            self.records.add({"origin": "load", "event": "the sampler did not stop cleanly", "error": repr(problem)})

    def samples(self):
        return [r for r in self.records.all() if r.get("event") == "sample" and isinstance(r.get("cpu_percent"), (int, float))]


# ---- the core's logger ------------------------------------------------------------------------------------------------------------
class _KeepRecords(logging.Handler):
    def __init__(self, records, epoch, clock):
        super().__init__(level=logging.DEBUG)
        self.records, self.epoch, self._clock = records, epoch, clock

    def emit(self, record):
        try:
            self.records.add({"origin": "core", "event": "log", "logger": record.name, "level": record.levelname,
                              "message": record.getMessage(), "thread": record.threadName, "gw": round(self._clock() - self.epoch, 4)})
        except Exception as problem:  # noqa: BLE001 - a message that cannot be formatted is a record, not an error of the core's thread
            try:
                self.records.add({"origin": "probe", "event": "a log record could not be kept", "logger": getattr(record, "name", None),
                                  "error": repr(problem)})
            except Exception:  # noqa: BLE001 - not even that: nothing is raised into the logging call of the core
                pass


class LogCapture:
    """The records of the logger ``name`` at DEBUG while it is on: its level is set and the handler added at ``start``, and both are put back at
    ``stop`` whatever happened in between. The logger keeps passing every record on to its parents as before."""

    def __init__(self, records, epoch, name=UDP_EXIT_LOGGER, clock=time.monotonic):
        self.records, self.name = records, name
        self._handler = _KeepRecords(records, epoch, clock)
        self._level = None
        self._on = False

    def start(self):
        try:
            logger = logging.getLogger(self.name)
            self._level = logger.level
            logger.addHandler(self._handler)
            self._on = True
            logger.setLevel(logging.DEBUG)
        except Exception as problem:  # noqa: BLE001
            self.records.add({"origin": "probe", "event": "the log capture did not start", "error": repr(problem)})

    def stop(self):
        try:
            if self._on:
                logger = logging.getLogger(self.name)
                logger.removeHandler(self._handler)
                logger.setLevel(self._level)
                self._on = False
        except Exception as problem:  # noqa: BLE001
            self.records.add({"origin": "probe", "event": "the log capture did not stop", "error": repr(problem)})


# ---- the times of the control connections ----------------------------------------------------------------------------------------
def control_times(records):
    """From the records of the watch's control journal (``gateway-control-connections``): for each control connection, in seconds from the start
    of the gateway, when it was accepted (connect), when the greeting and the authentication were answered, when the ASSOCIATE was asked and
    answered, and the longest time the gateway sat on an answer between the moment it was asked to send it and the moment it was sent.
    ``None`` is a step that was not seen. Never raises."""
    table = {}
    try:
        for r in records:
            conn = r.get("conn")
            if conn is None or conn < 0:
                continue
            row = table.setdefault(conn, {"connect": None, "greeting": None, "authentication": None, "associate_asked": None,
                                          "associate_answered": None, "longest_answer_wait_s": None})
            event = r.get("event")
            if event == "control connection accepted":
                row["connect"] = r.get("gw")
            elif event == "request" and r.get("command") == "UDP ASSOCIATE":
                row["associate_asked"] = r.get("gw")
            elif event == "answer sent":
                what = str(r.get("what", ""))
                if what.startswith("method selection"):
                    row["greeting"] = r.get("gw")
                elif what.startswith("authentication status"):
                    row["authentication"] = r.get("gw")
                elif r.get("kind") == "associate" and r.get("rep") == 0:
                    row["associate_answered"] = r.get("gw")
                if r.get("gw") is not None and r.get("asked_gw") is not None:
                    wait = round(r["gw"] - r["asked_gw"], 4)
                    if row["longest_answer_wait_s"] is None or wait > row["longest_answer_wait_s"]:
                        row["longest_answer_wait_s"] = wait
    except Exception as problem:  # noqa: BLE001
        table["error"] = repr(problem)
    return table


def control_line(table):
    """The control connections in one line: ``#<n> connect|greeting|authentication|ASSOCIATE asked|ASSOCIATE answered`` in seconds from the start of
    the gateway, the longest wait inside the gateway for an answer, and a reminder that #0 is the core's (the first accepted)."""
    try:
        rows = sorted((k, v) for k, v in table.items() if isinstance(k, int))
        if not rows:
            return "control connections of the gateway: none seen"
        shown = " ".join("#%d %s|%s|%s|%s|%s (longest wait in the gateway %ss)" % (
            k, _say(v["connect"]), _say(v["greeting"]), _say(v["authentication"]), _say(v["associate_asked"]), _say(v["associate_answered"]),
            _say(v["longest_answer_wait_s"])) for k, v in rows[:MESSAGE_CONNECTIONS])
        return ("control connections of the gateway, s from its start, connect|greeting answered|authentication answered|ASSOCIATE asked|"
                "ASSOCIATE answered (#0 is the first accepted: the core's own measurement), %d of %d: %s" % (min(len(rows), MESSAGE_CONNECTIONS), len(rows), shown))
    except Exception as problem:  # noqa: BLE001
        return "control connections of the gateway: not read (%r)" % (problem,)


def core_line(records):
    """The core's UDP measurement in one line: its DEBUG records and the marks of the test, each with its time in seconds from the start of the gateway."""
    try:
        parts = []
        for r in records:
            if r.get("event") == "log":
                parts.append("%s: %r at %s" % (r.get("thread"), r.get("message"), _say(r.get("gw"))))
            elif r.get("event") == "mark":
                parts.append("%s at %s" % (r.get("what"), _say(r.get("gw"))))
        return "the core's UDP measurement (logger %s, s from the gateway start): %s" % (UDP_EXIT_LOGGER, "; ".join(parts) or "no record")
    except Exception as problem:  # noqa: BLE001
        return "the core's UDP measurement: not read (%r)" % (problem,)


def process_line(witness, sampler):
    """The stalls of the process and the runner's load in one line each."""
    try:
        stalls = sorted(witness.stalls(), key=lambda s: -s["gap_s"])
        samples = sampler.samples() if sampler is not None else []
        if stalls:
            said = ", ".join("%.2fs (%.2f to %.2f)" % (s["gap_s"], s["from_gw"], s["to_gw"]) for s in stalls[:MESSAGE_STALLS])
            stall = "%d stall(s) of the whole process over %.0f ms, longest first, gap (from to, s from the gateway start): %s" % (
                len(stalls), witness.stall_s * 1000, said)
        else:
            stall = "no stall of the whole process over %.0f ms in %d beats of %.0f ms (longest gap %.3fs)" % (
                witness.stall_s * 1000, witness.beats, witness.beat_s * 1000, witness.longest_gap_s)
        if samples:
            cpu = [s["cpu_percent"] for s in samples]
            loads = [s["loadavg_1_5_15"][0] for s in samples if s.get("loadavg_1_5_15")]
            load = "runner CPU every %.1fs: %d samples, max %.0f%%, mean %.0f%%, at least 90%% in %d%s" % (
                sampler.every_s, len(cpu), max(cpu), sum(cpu) / len(cpu), sum(1 for c in cpu if c >= 90),
                "; load average max %.2f" % max(loads) if loads else "")
        else:
            load = "runner CPU: no sample"
        return stall + "\n" + load
    except Exception as problem:  # noqa: BLE001
        return "stall witness and runner load: not read (%r)" % (problem,)


class FarAwayProbes:
    """The three recorders of one gather, started and stopped together. ``epoch`` is ``time.monotonic()`` at the start of the gateway."""

    def __init__(self, epoch, load_source=None):
        self.epoch = epoch
        self.stall_records, self.load_records, self.core_records = Records(), Records(), Records()
        self.witness = StallWitness(self.stall_records, epoch)
        self.sampler = LoadSampler(self.load_records, epoch, source=load_source)
        self.core = LogCapture(self.core_records, epoch)

    def start(self):
        self.core.start()
        self.sampler.start()
        self.witness.start()

    def stop(self):
        self.witness.stop()
        self.sampler.stop()
        self.core.stop()

    def mark(self, what):
        """A moment of the test (the launch that runs the core's measurement starts, returns), on the scale of the core's records."""
        self.core_records.add({"origin": "test", "event": "mark", "what": what, "gw": round(time.monotonic() - self.epoch, 4)})

    def lines(self, control_records):
        """The failure message: the times of the control connections, the core's measurement, the stalls and the load."""
        return "\n".join(["far-away probes:", control_line(control_times(control_records)), core_line(self.core_records.all()),
                          process_line(self.witness, self.sampler)])

    def cells(self, control_records):
        return {"control_connection_times_s_from_the_gateway_start": control_times(control_records),
                "stalls_of_the_whole_process": self.witness.stalls(), "stall_beats": self.witness.beats,
                "longest_gap_between_beats_s": round(self.witness.longest_gap_s, 4),
                "runner_load_samples": self.sampler.samples(), "core_udp_exit_log_and_marks": self.core_records.all()}
