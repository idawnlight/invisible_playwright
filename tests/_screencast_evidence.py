"""What a test of the screencast needs to say when a stream delivers nothing.

A dead stream is silent on the wrapper side (the engine keeps one frame in
flight and sends no error), so the only account of why is the engine's own
output and the clock: the lines it printed that start with ``screencast:`` and,
per stream, how long after the start reply the first frame came (if one did).
Shared by `test_screencast.py` and `test_hidden_desktop.py`.
"""
from __future__ import annotations

import time


def engine_screencast_lines(page):
    """The lines the engine printed that start with ``screencast:``, from the
    browser's own stdout/stderr (the core keeps the last 200 lines of the
    pipe for the whole life of the process, `ProcessOutput`). Where the
    engine has no line for it the answer says so, so a failure message never
    comes out empty. Read while the browser is still up."""
    try:
        impl = page._impl_obj
        server = impl._connection._transport._server
        output = server.object(impl._guid).conn.output
        lines = output.tail(output.KEEP)
    except Exception as failure:  # the message is the point, not the lookup
        return "(the engine output could not be read: %r)" % (failure,)
    mine = [l for l in lines if l.lstrip().startswith("screencast:")]
    return "\n".join(mine) if mine else "(the engine printed no line starting with 'screencast:')"


class Stream:
    """One screencast's frames and the clock around them.

    `on_frame` is a plain function held in an attribute, not a bound method:
    the sync client tags the handler with an attribute, which a bound method
    cannot carry.
    """

    def __init__(self, name="stream"):
        self.name = name
        self.frames = []
        self.started = None   # when `start()` returned, i.e. the reply was handled
        self.first = None     # when the first frame reached the handler
        self.arrivals = []    # when each frame reached the handler (time.monotonic), in order
        self.stamps = []      # the engine's `timestamp` of each frame (seconds, stamped in the browser when it sent it)

        def on_frame(frame):
            if self.first is None:
                self.first = time.monotonic()
            self.arrivals.append(time.monotonic())
            self.stamps.append(frame.get("timestamp") if isinstance(frame, dict) else getattr(frame, "timestamp", None))
            self.frames.append(frame)

        self.on_frame = on_frame

    def start(self, page, **kwargs):
        page.screencast.start(on_frame=self.on_frame, **kwargs)
        self.started = time.monotonic()

    def first_after_start(self):
        """Seconds from the start reply to the first frame, or None. Negative
        when the frame was handed to the handler before `start()` returned."""
        if self.first is None or self.started is None:
            return None
        return self.first - self.started

    def rate(self, stamps=None):
        """Frames a second between the first frame and the last one, on the ENGINE's stamps of the frames (default: all so
        far), or None with fewer than two stamped frames or no time between them. Not on the times the frames reached this
        process: a client that stalls receives the frames of the stall in one burst, and a burst is a rate the engine never
        produced (a cap of 2 fps read as 5). A rate of the frames that came, whatever the time they took to start."""
        stamps = [s for s in (self.stamps if stamps is None else stamps) if s]
        if len(stamps) < 2 or stamps[-1] <= stamps[0]:
            return None
        return (len(stamps) - 1) / (stamps[-1] - stamps[0])

    def describe(self):
        delay = self.first_after_start()
        return "%s: %d frame(s), first frame %s" % (
            self.name, len(self.frames),
            "never arrived" if delay is None else
            "%+.3f s after the start reply" % delay)
