"""The check that no test module leaves a browser open.

A connection's server owns ONE daemon thread, "invpw-thread-starter" (`Server.thread_starter`, made when the first thread is asked of it), and
stops it only on `shutdown`. A unit test that builds a dispatcher on a server and never shuts the server down leaves that thread alive, and the thread keeps the connection,
the browser and every context alive for the rest of the worker (a gc census of a closed context once found 7 of them, and one file alone
left 635). So the number of live starters, counted when the last test of a module has torn down, must be what it was before the module.

Module level and not per test: a module-scoped browser (the e2e ones) is legitimately open between its tests and closed at the end.
"""
import sys
import time

import pytest

STARTER_NAME = "invpw-thread-starter"
_THREADS_MODULE = "invisible_playwright._juggler._threads"
# `stop` is asynchronous: it queues a None and the thread leaves on its next turn. A moment, not a wait for a leak that will not leave.
_JOIN_SECONDS = 5.0


def starters_alive():
    """The starters built and not ended, counted from their construction: `ThreadStarter` begins its thread without waiting for it, so
    one that has not run yet is in no `threading.enumerate()` (a census of live threads missed 2 leaks on a Linux runner).

    NO IMPORT HERE. A module that never loaded `_threads` cannot have built a starter, so the answer is empty without importing anything:
    the census runs from conftest in every job, including ones that install pytest alone and have no `invisible_core` (the `version`
    job failed on `invisible_playwright._pin` when this function imported the package)."""
    threads = sys.modules.get(_THREADS_MODULE)
    if threads is None:
        return []
    return threads.ThreadStarter.open_starters()


def wait_until_back_to(before, seconds=_JOIN_SECONDS):
    """The starters still open after `seconds` beyond the `before` that were there already (0 when the module closed everything)."""
    deadline = time.monotonic() + seconds
    while True:
        extra = len(starters_alive()) - before
        if extra <= 0 or time.monotonic() >= deadline:
            return max(extra, 0)
        time.sleep(0.05)


@pytest.fixture(scope="module", autouse=True)
def _a_module_leaves_no_browser_open(request):
    before = len(starters_alive())
    yield
    left = wait_until_back_to(before)
    if left:
        pytest.fail(
            "%s left %d browser(s) open: close their connection (fire its close listeners) or shut the server down, in a "
            "finally or a fixture teardown. Each one keeps a thread named %r alive, and with it the connection, the browser "
            "and every context, for the rest of the worker." % (request.module.__name__, left, STARTER_NAME),
            pytrace=False,
        )


_made = []


def closes_with_the_test(connection):
    """Register a fake connection a test builds: when the test ends it is closed the way a real one ends, `announce_closed()`, which fires
    its close listeners (a `BrowserDispatcher` registered `_starter.stop` on them). Returns the connection, so it goes inline:
    `conn = closes_with_the_test(Engine())`, or from the `__init__` of a fake class so that every instance is covered.

    Nothing intercepts the starter: a test that builds a browser on a connection it did not register still leaves the thread open, and the
    module check above says so.
    """
    _made.append(connection)
    return connection


@pytest.fixture(autouse=True)
def _the_connections_a_test_made_are_closed_with_it():
    yield
    made, _made[:] = list(_made), []
    for connection in reversed(made):
        connection.announce_closed()
