"""The browser's stdout and stderr are read for the whole life of the process.

The known-bad input of [B229], measured on 2026-09-25: the pipe was read only
until the readiness line, so once Firefox had written a pipe buffer's worth
after startup its next write blocked, and the browser froze without an error.
Forced with `devtools.console.stdout.content`: 2 KB logged, the page answered;
8 KB logged, it never answered again, 5085 bytes sitting unread.

The unit half needs no browser: a writer on a real OS pipe stands in for
Firefox, and a pipe blocks the same way whoever writes to it.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest

from invisible_core.juggler.connection import (
    Connection, ProcessOutput, TargetClosedError, _READY)


#: How long a test looks for what a local thread of this process does (a read of a pipe, a write that ends): 30 s, the far end of
#: any stall of a loaded runner. Only a thread that never does it makes the test wait this long, and then it is red.
_LOOK_FOR = 30


class _Alive:
    """A process that has not exited."""

    returncode = None

    def poll(self):
        return None


def _writer(fd: int, lines: list, done: threading.Event) -> None:
    with os.fdopen(fd, "wb") as w:
        for line in lines:
            w.write(line.encode() + b"\n")
            w.flush()
    done.set()


def test_output_after_readiness_is_still_read_so_the_writer_never_blocks():
    """256 KB after the readiness line, far past any pipe buffer. With the old
    reader, which stopped at readiness, the writer blocks on the first buffer
    and never finishes."""
    r, w = os.pipe()
    output = ProcessOutput(os.fdopen(r, "rb"))
    lines = [_READY] + ["line %05d %s" % (i, "x" * 1000) for i in range(256)] + ["the last one"]
    done = threading.Event()
    threading.Thread(target=_writer, args=(w, lines, done), daemon=True).start()

    # 30 s is the hang detector, not a speed: a reader that never reads leaves the wait running to it and the test red.
    assert output.wait_for_ready(_Alive(), _LOOK_FOR) is True
    assert done.wait(_LOOK_FOR), (
        "the writer is still blocked: nobody read the pipe after the readiness "
        "line, which is how the browser froze ([B229])")
    deadline = time.monotonic() + _LOOK_FOR
    while output.tail(1) != ["the last one"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert output.tail(1) == ["the last one"], output.tail(3)
    assert len(output.tail(ProcessOutput.KEEP + 50)) == ProcessOutput.KEEP


def test_readiness_that_never_comes_is_answered_with_false_not_a_hang():
    r, w = os.pipe()
    output = ProcessOutput(os.fdopen(r, "rb"))
    os.write(w, b"something else\n")
    # The wait runs on a thread of its own so that "a hang" is a join that does not come back, whatever the speed of the
    # machine; the verdict is "False, after the 0.3 s it was given", never a stopwatch on how late it came.
    asked: dict = {}

    def ask():
        asked["answer"] = output.wait_for_ready(_Alive(), 0.3)
        asked["at"] = time.monotonic()

    started = time.monotonic()
    waiter = threading.Thread(target=ask, daemon=True)
    waiter.start()
    waiter.join(_LOOK_FOR)
    try:
        assert not waiter.is_alive(), "wait_for_ready(0.3 s) was still waiting after %d s: a hang, not an answer" % _LOOK_FOR
        assert asked["answer"] is False
        assert asked["at"] - started >= 0.3, "answered False before the 0.3 s it was given"
    finally:
        os.close(w)


def test_a_pipe_that_closes_mid_session_says_what_the_browser_printed():
    """"the pipe is closed" alone names neither a crash nor a kill. The error
    now carries the browser's last lines."""
    out_r, out_w = os.pipe()
    output = ProcessOutput(os.fdopen(out_r, "rb"))
    os.write(out_w, b"Crash Annotation GraphicsCriticalError: something broke\n")

    to_browser_r, to_browser_w = os.pipe()
    from_browser_r, from_browser_w = os.pipe()
    c = Connection(to_browser_w, from_browser_r, None)
    c.output = output
    deadline = time.monotonic() + _LOOK_FOR
    while not output.tail() and time.monotonic() < deadline:
        time.sleep(0.02)
    os.close(from_browser_w)                  # the browser side goes away
    deadline = time.monotonic() + _LOOK_FOR
    while not c._closed and time.monotonic() < deadline:
        time.sleep(0.02)
    with pytest.raises(TargetClosedError) as e:
        c.send("Browser.getInfo")
    assert "the pipe is closed" in str(e.value)
    assert "GraphicsCriticalError: something broke" in str(e.value), str(e.value)
    for fd in (to_browser_r, to_browser_w, out_w):
        try:
            os.close(fd)
        except OSError:
            pass


@pytest.mark.e2e
def test_a_page_that_writes_to_stdout_keeps_answering(firefox_binary):
    """The real browser, with console output routed to stdout: 64 KB, eight
    times what froze it."""
    from invisible_playwright.async_api import InvisiblePlaywright

    async def run():
        async with InvisiblePlaywright(
                seed=4244, binary_path=firefox_binary, headless=True,
                extra_prefs={"devtools.console.stdout.content": True}) as browser:
            page = await browser.new_page()
            await page.goto("data:text/html,<p>x</p>")
            await asyncio.wait_for(page.evaluate(
                "n => { const s = 'x'.repeat(1000); "
                "for (let i = 0; i < n; i++) console.log(s); }", 64), 30)
            return await asyncio.wait_for(page.evaluate("1 + 1"), 30)

    try:
        assert asyncio.run(run()) == 2
    except asyncio.TimeoutError:
        pytest.fail("the page stopped answering after writing 64 KB to stdout: "
                    "the output pipe is not being read ([B229])")
