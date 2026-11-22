"""The verdicts of `test_closing_one_of_four_browsers_does_not_hold_the_loop_e2e.py`, as pure functions of what its client recorded (so a
unit test feeds them the red case and the green case without a browser).

None of them reads how long something took: a loop that is held is told by WHERE the thread of the loop was (inside a blocking step of
the close), a browser that keeps answering is told by the ORDER of the events (a reply that arrived while the close was in flight,
and answers that came while the removal of the profile was held), and the removal of the profile is told by WHICH THREAD
reached it (the reaper's or its continuation's, never the loop's nor one of the close's) and by the order of the close's return and
the release of the hold. A machine that is slow stretches every duration and changes none of these.

``gate`` is what the client recorded of the removal of the closing browser's profile, which the test holds still (the stimulus):
``{"reached": bool, "released_by": "answered" | "deadline" | None, "order": [the events in the order they happened: "reached",
"close_returned", "released"], "removal": {"thread": name, "ident": id of the thread that reached it, "loop_ident": id of the loop's
thread, "stack": ["<file>:<function>", ... outermost first] of the call}}``.

``inside`` is what the watcher saw of the loop's thread, one entry per frame that was ever on its stack while it was running something
(not waiting in the selector): ``{"<file>:<function>": {"n": samples, "stack": [the innermost frames of the first stack it was seen in]}}``.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: A blocking step of a close: (a part of the file's path, the functions of it). They WAIT (for the processes of the tree, for the
#: workers, for the directories to go), so none of them may be on the stack of the thread that runs the loop of the user:
#: `InProcessTransport.request_stop`'s `shutdown` and `_join_workers`, `reap` / `reap_session` of the core, `op_close` of the engine's
#: dispatcher, and the blocking calls those make (`psutil.wait_procs`, `shutil.rmtree`, `Thread.join`).
BLOCKING_STEPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("/invisible_playwright/", ("shutdown", "_join_workers", "op_close")),
    ("/invisible_core/", ("shutdown", "reap", "reap_session")),
    ("/psutil/", ("wait_procs",)),
    ("/shutil.py", ("rmtree",)),
    ("/threading.py", ("join",)),
)

#: The steps of the close that the user waits for (the first two lines of `BLOCKING_STEPS`): a removal reached inside one of them is a
#: removal the close waits for.
CLOSE_STEPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = BLOCKING_STEPS[:2]

#: Where a step handed to `Reaper.then` runs: `run` of the reaper's own thread while it works, `_continue` of the thread it starts for the
#: steps handed over after it has finished (the thread that is not the loop's nor the close's).
REAPER_STEPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (("/invisible_core/process.py", ("run", "_continue")),)


def blocked_by(inside: Dict[str, dict], steps: Iterable[Tuple[str, Sequence[str]]] = BLOCKING_STEPS) -> List[str]:
    """The frames of ``steps`` that were on the stack of the loop's thread while it ran something, sorted."""
    found = []
    for frame in inside:
        path, _, function = frame.rpartition(":")
        for part, functions in steps:
            if part in path and function in functions:
                found.append(frame)
                break
    return sorted(found)


def close_on_the_loop_problem(inside: Dict[str, dict]) -> Optional[str]:
    """What is wrong when a step of the close that waits ran on the thread of the loop, or None. Red on a close made on the loop
    (the defect), whatever the speed of the machine; green on one made in threads of their own."""
    found = blocked_by(inside)
    if not found:
        return None
    lines = []
    for frame in found:
        entry = inside[frame]
        lines.append("%s (seen in %d samples of the loop's thread), the innermost frames of the first one:\n    %s"
                     % (frame, entry["n"], "\n    ".join(entry["stack"])))
    return "the loop's own thread was inside a step of the close that waits:\n" + "\n".join(lines)


def blind_instrument_problem(inside: Dict[str, dict], steps: Iterable[Tuple[str, Sequence[str]]]) -> Optional[str]:
    """The control: a loop that was held on purpose, inside the function ``steps`` names, must have been seen there at least once."""
    if blocked_by(inside, steps):
        return None
    return ("the watcher never saw the loop's thread inside %s while the loop was held on purpose: it could not see a close made on "
            "the loop (frames it did see: %s)" % (list(steps), sorted(inside)[:12]))


def charged_more_than_ran_problem(held: float, busy_run: float) -> Optional[str]:
    """The second control: a loop the machine did not schedule waits in its selector, and only the time its thread was seen RUNNING is
    held. ``busy_run`` is the longest stretch the watcher saw the thread run without a pause in the selector, read off the same
    samples as ``held``: what is charged can never be more than that. Charging the whole lateness (the wait in the selector too, as
    the first instrument did: 94 ms with nothing on the loop's thread, candidate 43727128285) is more than that, whatever the machine."""
    if held <= busy_run:
        return None
    return ("the loop was charged %.0f ms of hold and its thread was seen running for at most %.0f ms in a row: the time it waited in "
            "its selector (the machine did not schedule it) was counted as held" % (held * 1000, busy_run * 1000))


def others_answered_problem(others: Sequence[dict], gate: dict, loop_inside: Dict[str, dict]) -> Optional[str]:
    """What is wrong with the way the other browsers answered while the first one closed, or None.

    ``others`` has one record per other browser: ``answered`` is the replies that ARRIVED while the close was in flight, whenever the
    call began (a reply read during the close is proof that the loop ran then; a call that began a moment before the close and ended
    in it is one), ``inside`` the calls that began AND ended in it (printed, not judged), ``during_removal`` the calls that began after
    the removal of the closing browser's profile was reached and ended while it was still held. ``gate`` says how that hold ended:
    ``reached`` (the removal ran), ``released_by``: ``answered`` (the close had returned and every other browser had answered a call
    that began after the removal was reached: the loop was free while the removal waited) or ``deadline`` (they had not, in 30 s: the
    loop could not run while the removal was waiting, which is a removal made on the loop's thread). ``loop_inside`` is what the
    watcher saw of the loop's thread during the close (see the head of this file).

    A browser that answered nothing inside the close is the loop's fault only if the loop was HELD: the watcher saw its thread inside
    a step of the close that waits, or no browser answered at all (nothing shows that the loop ran). One engine that did not answer
    while the others did, and the loop was seen free, is a browser the machine did not run: the removal's hold below still asks every
    browser to answer, once the close is over."""
    if len(others) != 3:
        return "the client followed %d other browsers instead of 3: %r" % (len(others), others)
    silent = [i for i, r in enumerate(others) if r["answered"] < 1]
    if silent:
        held = blocked_by(loop_inside)
        counts = [r["answered"] for r in others]
        if held:
            return ("the other browsers %s did not answer a single call inside the close (replies that arrived inside it, per "
                    "browser: %s) and the watcher saw the loop's thread inside %s: the loop that serves them did not run during "
                    "the close" % (silent, counts, held))
        if len(silent) == len(others):
            return ("no other browser answered a single call inside the close (replies that arrived inside it, per browser: %s): "
                    "nothing shows that the loop ran during the close" % counts)
    if gate.get("reached") and gate.get("released_by") != "answered":
        return ("the removal of the profile was held until every other browser answered, and they did not (%s): the loop that "
                "serves them could not run while the removal waited, so the removal ran on it (answers during the removal, per "
                "browser: %s)" % (gate.get("released_by"), [r["during_removal"] for r in others]))
    if gate.get("reached"):
        mute = [i for i, r in enumerate(others) if r["during_removal"] < 1]
        if mute:
            return ("the other browsers %s did not answer a call that began and ended while the removal of the profile was held "
                    "(answers during the removal, per browser: %s)" % (mute, [r["during_removal"] for r in others]))
    return None


def removal_never_reached_problem(gate: dict) -> Optional[str]:
    """What is wrong when the removal of the closing browser's profile was never reached, or None. The client waits for it after the
    close has returned (30 s, the deadline of a hang): a close that removes nothing through the instrumented call would pass every other
    verdict by not being gated."""
    if gate.get("reached"):
        return None
    return ("the removal of the profile never ran through `shutil.rmtree` after the first browser closed (waited 30 s after the close "
            "returned): there was nothing held, so the others were not judged on a removal in flight (%r)" % (gate,))


def removal_on_the_users_thread_problem(gate: dict) -> Optional[str]:
    """What is wrong when the removal ran on a thread that the user waits on, or None.

    The close of a browser returns once the browser has exited and the removal of its profile is the reaper's: the thread that reaches
    it is the one that ends the tree (`Reaper.run`) or the continuation of a reaper that had finished (`Reaper._continue`). Red when it
    is the loop's thread, a thread inside a step of the close (`CLOSE_STEPS`), or any thread that is not the reaper's. A removal that
    was never reached is `removal_never_reached_problem`'s."""
    removal = gate.get("removal")
    if not gate.get("reached"):
        return None
    if not removal:
        return "the removal was reached and the client recorded no thread for it: %r" % (gate,)
    frames = "\n    ".join(removal.get("stack", [])[-8:])
    where = "on the thread %r, in:\n    %s" % (removal.get("thread"), frames)
    if removal.get("ident") == removal.get("loop_ident"):
        return "the removal of the profile ran on the thread of the loop, %s" % where
    stack = {frame: {} for frame in removal.get("stack", [])}
    waits = blocked_by(stack, CLOSE_STEPS)
    if waits:
        return "the removal of the profile ran inside a step of the close that the user waits for (%s), %s" % (waits, where)
    if not blocked_by(stack, REAPER_STEPS):
        return "the removal of the profile ran on a thread that is not the reaper's nor its continuation, %s" % where
    return None


def close_waited_for_the_removal_problem(gate: dict) -> Optional[str]:
    """What is wrong when the close of the first browser returned only after the removal of its profile was released, or None.

    The removal is held by the test until the close has returned and the other browsers have answered. A close that waits for the
    removal cannot return while it is held: it returns after the hold ended, at its deadline. The order of the events decides."""
    order = list(gate.get("order") or [])
    if "close_returned" not in order:
        return "the close of the first browser never returned (events: %s)" % order
    if "released" in order and order.index("released") < order.index("close_returned"):
        return ("the close of the first browser returned only after the removal of its profile was released (%s, events: %s): the "
                "close waited for the removal" % (gate.get("released_by"), order))
    return None
