"""Closing one of four browsers in one process does not hold the event loop that serves the other three.

The close of a browser WAITS (for its processes to exit, for its profile to be removed): that is the contract, Playwright's `close()`
returns once the processes are gone. What was wrong was WHO waited. In the async API the user's own event loop did, three times:

* `InProcessTransport.request_stop` ran `server.shutdown()` (the hooks that close the engine's pipe and wait for its tree, and the
  removal of the profile) on the loop that asked for the stop;
* `InProcessTransport.wait_until_stopped` joined the four `juggler-N` workers on the loop, up to 5 s each, and a worker is busy
  for as long as the engine's `conn.close()` waits (`BrowserDispatcher.op_close`);
* `InvisiblePlaywright._teardown` reaped the session's token (`reap` waits for the tree) on the loop.

Measured with py-spy on four browsers in one Python process: closing one stopped the loop that serves the other three for as long as
it waited. Now the same waits run in threads of their own (`invisible_core._offloop.off_the_loop`, from invisible-core 64.72.0).

The test runs the real engine, four of them, in a client program of its own (the loop under test is that program's main thread). A
sentinel task asks to wake every 5 ms; a watcher thread notes how late it woke and where the main thread was: waiting in the
selector (the machine did not run the loop: on a loaded host an idle loop is 100 ms late now and then, which is not the close's
doing and is not counted) or INSIDE something (held). The instrument is the one of the core's
`tests/test_a_close_does_not_hold_the_loop.py`. A control with the same instrument holds the loop on purpose first, so a watcher
that could not see a held loop does not pass the test for nothing.

Nothing here is judged by how long it took (that is a verdict on the runner: a loop that a starved machine runs late is late without
a defect). The loop is held when its thread was SEEN inside a step of the close that waits (`_close_loop_verdicts.blocked_by`), and the
other browsers keep answering when, in the order of the events, a call of each began and ended while the close was in flight and
while the removal of the closing browser's profile was held.

THE REMOVAL OF THE PROFILE IS NOT THE CLOSE'S. `close()` returns once the browser has exited; the removal of its session's directories
is handed to the launch's reaper (`invisible_core.process.Reaper.then`) and runs on the thread that ends the tree, or on the
continuation of a reaper that has finished, after the close has returned. The test holds that removal still (the stimulus: the
client's `shutil.rmtree` waits for the test) and judges, without a clock:

* WHO reached it: a thread of the reaper, not the loop's thread and not a thread inside a step of the close;
* the ORDER: the close of the first browser returned while the removal was held (a close that waited for the removal cannot return
  until the hold ends at its deadline of 30 s);
* the other three browsers answered while it was held, and only then does the test release it (a removal made on the loop would
  wait for answers that only the loop can read);
* after the release `wait_reaped(token)` returns and the profile's directory is gone.

A removal that is never reached fails the test 30 s after the close returned. The milliseconds are printed, never judged.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from _close_loop_verdicts import (
    blind_instrument_problem,
    charged_more_than_ran_problem,
    close_on_the_loop_problem,
    close_waited_for_the_removal_problem,
    others_answered_problem,
    removal_never_reached_problem,
    removal_on_the_users_thread_problem,
)

pytestmark = pytest.mark.e2e

#: The client. Four browsers by the async API, each with a page; one is closed while a sentinel measures the loop and one pinger per
#: other browser keeps asking its page a question. Prints one JSON line.
_CLIENT = textwrap.dedent("""
    import asyncio, json, os, shutil, sys, threading, time, traceback
    import psutil
    from invisible_core.juggler._profile import PROFILE_PREFIX
    from invisible_playwright._juggler._reaped import wait_reaped
    from invisible_playwright.async_api import InvisiblePlaywright

    BINARY = sys.argv[1]
    main_id = threading.get_ident()


    class Instrument:
        \"\"\"A sentinel on the loop and a watcher thread. `held` is the longest wake that was late while the main thread was INSIDE
        something, not idle in the loop's selector.\"\"\"

        def __init__(self):
            self.gaps, self.holes, self.held = [], [], 0.0
            #: The longest stretch the watcher saw the main thread running something without a pause in the selector, and every
            #: frame that was on its stack in those samples (file:function -> samples, and the innermost frames of the first stack
            #: it was seen in): WHERE the thread was, which is the cause, where `held` only says for how long.
            self.busy_run, self.inside = 0.0, {}
            self.beat = [time.monotonic()]
            self.stop = asyncio.Event()
            self.over = threading.Event()
            self.on_the_loop = []
            self._real = {}
            #: Set to make the sentinel's next wait that long: the loop then waits in its selector, late by that much, as on a loaded
            #: machine that does not schedule it.
            self.pause_once = 0

        async def sentinel(self):
            last = time.monotonic()
            while not self.stop.is_set():
                wait, self.pause_once = self.pause_once or 0.005, 0
                await asyncio.sleep(wait)
                now = time.monotonic()
                self.gaps.append(now - last)
                last = self.beat[0] = now

        @staticmethod
        def idle(frame):
            # True when the main thread is waiting in the event loop's selector: it is not running anything of anybody's.
            for _ in range(3):
                if frame is None:
                    return False
                code = frame.f_code
                if code.co_name in ("_poll", "select") and code.co_filename.endswith(("windows_events.py", "selectors.py")):
                    return True
                frame = frame.f_back
            return False

        def stacks(self):
            frames = sys._current_frames()
            names = {t.ident: t.name for t in threading.enumerate()}
            return {
                "main": "".join(traceback.format_stack(frames[main_id])[-6:]),
                "others": {names.get(i, str(i)): "".join(traceback.format_stack(f)[-3:])
                           for i, f in frames.items() if i != main_id and i != threading.get_ident()}}

        def note(self, frame):
            names = []
            while frame is not None:
                names.append("%s:%s" % (frame.f_code.co_filename.replace(os.sep, "/"), frame.f_code.co_name))
                frame = frame.f_back
            names.reverse()
            for name in names:
                entry = self.inside.get(name)
                if entry is None:
                    self.inside[name] = {"n": 1, "stack": names[-6:]}
                else:
                    entry["n"] += 1

        def watcher(self):
            # Only the part of a lateness during which the loop's thread was RUNNING counts. A loop the machine did not schedule waits in
            # the selector and then, once woken, runs the callbacks that queued meanwhile (the pingers' replies): counting the whole
            # lateness at that moment charged the machine's 90 ms to whoever ran first (candidate 43727128285, Windows: 94 ms, the main
            # thread in `self._selector.select`, no blocking call on it). `busy_since` is when the thread last left the selector.
            looked, busy_since = 0, None
            while not self.over.is_set():
                time.sleep(0.003)
                now = time.monotonic()
                late = now - self.beat[0]
                level = int(late / 0.03)
                frame = sys._current_frames().get(main_id)
                if self.idle(frame):
                    busy_since = None
                else:
                    if busy_since is None:
                        busy_since = now
                    self.busy_run = max(self.busy_run, now - busy_since)
                    self.note(frame)
                    held = now - max(self.beat[0], busy_since)
                    if held > 0.03:
                        self.held = max(self.held, held)
                if level > looked:
                    self.holes.append({"late_ms": round(late * 1000), **self.stacks()})
                looked = level

        def watched(self, owner, name):
            self._real[(owner, name)] = original = getattr(owner, name)

            def call(*x, **k):
                if threading.get_ident() == main_id and not self.over.is_set():
                    self.on_the_loop.append(name)
                return original(*x, **k)
            setattr(owner, name, call)

        async def start(self):
            self.watched(time, "sleep")
            self.watched(shutil, "rmtree")
            self.watched(psutil, "wait_procs")
            self.watched(threading.Thread, "join")
            threading.Thread(target=self.watcher, daemon=True, name="the-watcher").start()
            self.task = asyncio.create_task(self.sentinel())
            await asyncio.sleep(0.1)

        async def finish(self):
            self.stop.set()
            await self.task
            self.over.set()
            for (owner, name), original in self._real.items():
                setattr(owner, name, original)
            self._real.clear()

        def report(self):
            return {"held": self.held, "busy_run": self.busy_run, "inside": self.inside, "gap": max(self.gaps),
                    "holes": self.holes[:4], "on_the_loop": sorted(set(self.on_the_loop))}


    def engines():
        out = []
        for child in psutil.Process().children(recursive=True):
            try:
                if "firefox" in child.name().lower():
                    out.append(child.pid)
            except psutil.Error:
                pass
        return out


    def where_the_removal_runs():
        # Where the call that is held was made: the thread (its name and id), the loop's thread, and the frames of its stack, outermost
        # first, as "<file>:<function>" (this function and the hook that calls it left out). The verdict reads the frames, never a clock.
        frames = traceback.extract_stack()[:-2]
        return {"thread": threading.current_thread().name, "ident": threading.get_ident(), "loop_ident": main_id,
                "stack": ["%s:%s" % (f.filename.replace(os.sep, "/"), f.name) for f in frames]}


    async def pinger(page, record, closing, gate, records):
        # A call counts as answered DURING the close when it began after the close began and ended before it ended, and as answered
        # DURING THE REMOVAL when it began after the removal of the profile was reached and ended while it was still held: the order
        # of the events, not how long it took. The close has returned by then or not: the removal is the reaper's, not the close's.
        while not record["stop"]:
            while_closing = closing.is_set()
            started = time.monotonic()
            await page.evaluate("1 + 1")
            took = time.monotonic() - started
            if while_closing and closing.is_set():
                record["inside"] += 1
                record["slowest"] = max(record["slowest"], took)      # printed, never judged
            since = gate["since"]
            if since is not None and started >= since and gate["released_by"] is None:
                record["during_removal"] += 1
            await asyncio.sleep(0.02)


    def hold_the_loop(seconds):
        time.sleep(seconds)


    async def main():
        # The control: the same instrument on a loop that is held on purpose.
        control = Instrument()
        await control.start()
        await asyncio.sleep(0.05)
        hold_the_loop(0.4)
        await asyncio.sleep(0.05)
        await control.finish()

        # The second control: a loop that is LATE without being held. The sentinel's next wake is 300 ms away, so the loop waits in its
        # selector (as when the machine does not schedule it), and 150 ms in it runs 45 ms of a callback that was due then, while the
        # sentinel is still about 145 ms late. Only those 45 ms are the loop's thread at work (45, not less: a watcher on Windows sleeps
        # in 15.6 ms ticks and would not see a shorter one): the 145 ms before them must not count.
        unscheduled = Instrument()
        await unscheduled.start()
        await asyncio.sleep(0.05)
        unscheduled.pause_once = 0.3
        loop = asyncio.get_running_loop()

        def burst():
            end = time.monotonic() + 0.045
            while time.monotonic() < end:
                pass
        loop.call_later(0.15, burst)
        await asyncio.sleep(0.5)
        await unscheduled.finish()

        # A slow disk: removing a profile takes 150 ms more (a scanner of files at work, a spinning disk, a loaded runner). On a fast
        # disk the removal of a profile is a few ms and a close made on the loop hides in the noise; with a disk that is slow the
        # wait is what the loop pays, wherever it is made. Every thread pays it the same.
        #
        # And the removal of the closing browser's profile (the first one made after the close was asked for) is HELD by the test, the
        # stimulus: it stays inside `shutil.rmtree` until the test releases it (30 s at most, the deadline of a hang). The close of a
        # browser returns once the browser has exited and that removal is the reaper's (or its continuation's), so it is reached after
        # or around the return, and never on a thread the user waits on. Held, it says:
        #   * which thread reached it (`where_the_removal_runs`): the reaper's, not the loop's nor one of the close's;
        #   * the order of the events: the close returned BEFORE the release (a close that waited for the removal cannot return while it
        #     is held, it returns after the deadline released it);
        #   * whether the other browsers answered while it was held (a removal on the loop's thread stops the only thread that could
        #     read their answers: it goes on only at the deadline).
        # The test releases it when the close has returned and every other browser has answered a call that began after it was
        # reached. How the hold ended (`released_by`) and the order of the events are the verdicts; a slow machine only makes them later.
        real_rmtree = shutil.rmtree
        # set while the first browser closes; it exists before the hook is installed, because a launch removes directories too
        # (Windows, run 43796271124: "cannot access free variable 'closing'" from the launch of the first browser)
        closing = asyncio.Event()
        gate = {"armed": False, "since": None, "released_by": None, "order": [], "removal": None,
                "reached": threading.Event(), "release": threading.Event()}
        gate_lock = threading.Lock()

        def release(by):
            with gate_lock:
                if gate["released_by"] is not None:
                    return
                gate["released_by"] = by
                gate["order"].append("released")
            gate["release"].set()

        def slow_rmtree(*x, **k):
            time.sleep(0.15)
            path = os.fsdecode(x[0]) if x else ""
            first = False
            # Only a profile this process made counts (a launch removes other directories too), and only after the close was asked for.
            if gate["armed"] and os.path.basename(path.rstrip("/" + os.sep)).startswith(PROFILE_PREFIX):
                with gate_lock:
                    if gate["since"] is None:
                        gate["since"], first = time.monotonic(), True
                        gate["order"].append("reached")
                        gate["removal"] = dict(where_the_removal_runs(), path=path)
            if first:
                gate["reached"].set()
                if not gate["release"].wait(30):
                    release("deadline")
            return real_rmtree(*x, **k)

        shutil.rmtree = slow_rmtree

        sessions = [InvisiblePlaywright(seed=7100 + i, binary_path=BINARY, headless=True) for i in range(4)]
        browsers = []
        for session in sessions:
            browsers.append(await session.__aenter__())
        token = sessions[0]._session_token.value
        pages = [await b.new_page() for b in browsers]
        for i, page in enumerate(pages):
            await page.set_content("<p id=n>browser %d</p>" % i)
        launched = len(engines())

        records = []
        tasks = []
        for page in pages[1:]:
            record = {"stop": False, "inside": 0, "during_removal": 0, "slowest": 0.0}
            records.append(record)
            tasks.append(asyncio.create_task(pinger(page, record, closing, gate, records)))
        await asyncio.sleep(0.5)

        instrument = Instrument()
        await instrument.start()
        gate["armed"] = True
        closing.set()
        started = time.monotonic()
        await sessions[0].__aexit__(None, None, None)
        took = time.monotonic() - started
        with gate_lock:
            gate["order"].append("close_returned")
        closing.clear()
        await instrument.finish()
        report = instrument.report()

        # After the return of the close: wait (30 s at most) for the removal to be reached; once it is, the loop goes on serving the
        # others while it is held, and the test releases it when every one of them has answered a call that began after it was reached.
        # A removal that is never reached ends this wait at the 30 s: the test is red then, after the close had returned.
        deadline = time.monotonic() + 30
        while not gate["reached"].is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        if gate["reached"].is_set():
            deadline = time.monotonic() + 30
            while not all(r["during_removal"] >= 1 for r in records) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            release("answered" if all(r["during_removal"] >= 1 for r in records) else "deadline")
        # The reaper of the session ends when the hold does and the profile is gone then (with a reaper that had finished already
        # nothing is left to wait for, and it is gone as well).
        reaped = await asyncio.to_thread(wait_reaped, token, 30)
        removal = gate["removal"] or {}
        gone = bool(removal.get("path")) and not os.path.exists(removal["path"])
        report.update({
            "took": took, "launched": launched,
            "control": control.report(),
            "unscheduled": unscheduled.report(),
            "others": [{"inside": r["inside"], "during_removal": r["during_removal"], "slowest": r["slowest"]} for r in records],
            "gate": {"reached": gate["since"] is not None, "released_by": gate["released_by"], "order": gate["order"],
                     "removal": gate["removal"]},
            "ended": {"reaped": reaped, "profile_gone": gone},
        })
        # The other three still work after it.
        shutil.rmtree = real_rmtree
        report["texts"] = [await p.text_content("#n") for p in pages[1:]]
        for record in records:
            record["stop"] = True
        await asyncio.gather(*tasks)
        for session in sessions[1:]:
            await session.__aexit__(None, None, None)
        time.sleep(0.5)
        report["left"] = engines()
        print("REPORT", json.dumps(report), flush=True)

    try:
        asyncio.run(asyncio.wait_for(main(), 240))
    finally:
        for pid in engines():
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass
""")


def _holes(report):
    out = []
    for hole in report.get("holes") or []:
        out.append("--- the loop was %d ms late. MAIN THREAD:\n%s" % (hole["late_ms"], hole["main"]))
        for name, stack in hole["others"].items():
            out.append("--- thread %s:\n%s" % (name, stack))
    return "\n".join(out) or "(no look was taken)"


@pytest.fixture(scope="module")
def report(firefox_binary, tmp_path_factory):
    """One run of the client: its numbers are asserted one claim at a time below."""
    script = tmp_path_factory.mktemp("close-loop") / "client.py"
    script.write_text(_CLIENT, encoding="utf-8")
    import invisible_playwright

    # The client imports the checkout under test, the one this run imported.
    source = os.path.dirname(os.path.dirname(os.path.abspath(invisible_playwright.__file__)))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([source, os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep))
    done = subprocess.run([sys.executable, str(script), str(firefox_binary)], capture_output=True, text=True, timeout=300, env=env)
    lines = [x for x in done.stdout.splitlines() if x.startswith("REPORT ")]
    assert done.returncode == 0 and lines, (
        "the client failed (%s):\n%s\n%s" % (done.returncode, done.stdout[-2000:], done.stderr[-4000:]))
    return json.loads(lines[-1][len("REPORT "):])


#: The function the client holds its loop in on purpose (the control), in the script the test writes.
_HOLDER = (("/client.py", ("hold_the_loop",)),)


@pytest.mark.wall_clock
def test_the_instrument_sees_a_loop_that_is_held(report):
    """The control: the loop held on purpose for 400 ms is seen as held, and seen INSIDE the function that holds it. Without this, a
    watcher that could not tell a held loop from a starved one, or could not read where the thread was, would pass the test below for
    nothing."""
    control = report["control"]
    assert control["held"] > 0.3, (
        "the watcher did not see a loop held for 400 ms: held %.0f ms" % (control["held"] * 1000))
    assert "sleep" in control["on_the_loop"]
    assert blind_instrument_problem(control["inside"], _HOLDER) is None, blind_instrument_problem(control["inside"], _HOLDER)


@pytest.mark.wall_clock
def test_the_instrument_does_not_charge_a_loop_the_machine_did_not_schedule(report):
    """The second control: a loop that waited 150 ms in its selector and then ran a 45 ms callback was held for those 45 ms, not 195.
    Charging it the whole lateness made a loaded Windows runner fail a bound of 90 ms with 94 ms and nothing on the loop's thread
    (candidate 43727128285). What is charged is judged against what the same samples saw run, not against a number of milliseconds:
    a held time greater than the longest stretch the thread was seen running counted a wait that was not the loop's."""
    unscheduled = report["unscheduled"]
    assert unscheduled["gap"] > 0.25, "the sentinel was not late: %r" % unscheduled
    assert charged_more_than_ran_problem(unscheduled["held"], unscheduled["busy_run"]) is None, charged_more_than_ran_problem(
        unscheduled["held"], unscheduled["busy_run"])


def test_the_removal_of_the_profile_was_reached_on_a_thread_of_the_reaper(report):
    """The slow disk of the client is only a stimulus if the removal of the closing browser's profile ran, through `shutil.rmtree`
    (the call the test holds), and on a thread that nobody waits on: the reaper's, or the continuation of a reaper that had finished.
    Not reached is red (a close that removed nothing through it would pass the test of the others by not being gated); reached on the
    loop's thread, or inside a step of the close, or on any other thread is red."""
    gate = report["gate"]
    assert removal_never_reached_problem(gate) is None, removal_never_reached_problem(gate)
    assert removal_on_the_users_thread_problem(gate) is None, removal_on_the_users_thread_problem(gate)


def test_the_close_returned_while_the_removal_of_the_profile_was_held(report):
    """The test releases the removal only after the close of the first browser has returned and the others have answered. A close
    that waited for the removal could not return while it was held: it returns after the deadline released it, and the order of the
    events says so."""
    assert report["gate"]["reached"], "the removal was never held: %r" % report["gate"]
    problem = close_waited_for_the_removal_problem(report["gate"])
    assert problem is None, problem


def test_the_removal_ended_when_it_was_released_and_the_profile_is_gone(report):
    """After the release the reaper of the session finishes (`wait_reaped(token)` returns True) and the directory of the profile is
    gone. The same whether the reaper was still alive when the close returned (it waits for the sender of the shutdown pings: on
    Windows it is) or had finished (the removal then ran on a continuation of it)."""
    assert report["gate"]["reached"], "the removal was never held: %r" % report["gate"]
    assert report["gate"]["released_by"] == "answered", "the removal was not released by the others' answers: %r" % report["gate"]
    assert report["ended"]["reaped"], "the reaper of the session did not finish within 30 s of the release: %r" % report["ended"]
    assert report["ended"]["profile_gone"], "the directory of the profile is still there after the release: %r" % (
        report["gate"]["removal"]["path"],)


@pytest.mark.wall_clock
def test_closing_one_of_four_browsers_does_not_hold_the_loop(report):
    """Four browsers, one closes: the thread that runs the loop of the program is never seen inside a step of the close that waits
    (the shutdown of the server, the join of its workers, the reaping of the tree, the removal of the profile)."""
    assert report["launched"] >= 4, "fewer than four engines were running before the close: %r" % report["launched"]
    assert report["took"] > 0.3, "the close finished before the engine could have exited: nothing was waited for"
    problem = close_on_the_loop_problem(report["inside"])
    assert problem is None, (
        "%s\n(printed, not judged: the loop was held %.0f ms while one of four browsers closed, the close took %.0f ms, the longest "
        "wake was %.0f ms late; calls made by the loop's own thread: %s). Where the threads were while it was late:\n%s"
        % (problem, report["held"] * 1000, report["took"] * 1000, report["gap"] * 1000, report["on_the_loop"], _holes(report)))


def test_no_blocking_step_of_the_close_runs_on_the_loop_s_thread(report):
    """The cause, asked directly and without the clock: `time.sleep`, `rmtree`, `psutil.wait_procs` and `Thread.join` are what a close
    does while it waits and removes, and none of them may be called by the thread that runs the loop. Red on a close made on it."""
    assert report["on_the_loop"] == [], "the loop's own thread made these calls while it awaited the close: %s" % report["on_the_loop"]


def test_the_other_three_keep_answering_during_the_close(report):
    """Each of the other three browsers answers a call that began and ended while the first one closes, the removal of the first one's
    profile is held until every one of them has answered, and the three are alive after. The order of the events decides; the
    seconds a call took are printed."""
    problem = others_answered_problem(report["others"], report["gate"])
    assert problem is None, "%s\n(printed, not judged: the slowest call of each browser inside the close, in s: %s)" % (
        problem, [round(r["slowest"], 3) for r in report["others"]])
    assert report["texts"] == ["browser 1", "browser 2", "browser 3"]


def test_every_engine_is_gone_when_the_four_have_closed(report):
    assert report["left"] == [], "engine processes are left after every browser closed: %r" % report["left"]
