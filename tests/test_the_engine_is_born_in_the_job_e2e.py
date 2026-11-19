"""A launch scans no process of the machine, and the engine's tree still dies with its launcher.

THE KNOWN-BAD INPUT, measured with py-spy on a Windows runner: at every launch `InvisiblePlaywright._bind_process_tree` called the
core's `JobObjectGuard.bind`, which read the environment of EVERY process of the machine (`psutil.process_iter` and
`Process.environ`, under the GIL on Windows), slept a quarter of a second and scanned again until a second passed with nothing new,
up to ten, synchronously on the caller's thread or event loop: 12-29% of the main thread's time and most of the GIL samples.

The core now creates the engine inside the process's job object (`invisible_core.process.engine_job`, used by the spawn in
`juggler.connection`), so its whole tree, late children included, is in the job by construction, and the wrapper no longer binds
anything. Three tests, on the real engine:

1. a launch (`__enter__`, a page, up to the moment before `__exit__`) makes no call to `psutil.process_iter` and none to
   `psutil.Process.environ`. Red on the old wrapper on Windows, where the guard is the job guard;
2. after `browser.close()` no process of the session is left, the late children (the pingsender) included;
3. a client that launches and is ended with TerminateProcess leaves no engine process: the job closes them.

Windows is where the job exists; the first two run on Linux too, where they hold for the same reason in a different way (nothing
scans, `close()` ends the tree).
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import traceback

import pytest

pytestmark = pytest.mark.e2e

WINDOWS = sys.platform == "win32"


def _token_value(session) -> str:
    return session._session_token.value


def test_a_launch_scans_no_process_of_the_machine(firefox_binary, monkeypatch):
    import psutil
    from invisible_playwright import InvisiblePlaywright

    calls = []
    real_iter, real_environ = psutil.process_iter, psutil.Process.environ

    def watched_iter(*a, **k):
        calls.append(("process_iter", "".join(traceback.format_stack(limit=6))))
        return real_iter(*a, **k)

    def watched_environ(self):
        calls.append(("environ", "".join(traceback.format_stack(limit=6))))
        return real_environ(self)

    monkeypatch.setattr(psutil, "process_iter", watched_iter)
    monkeypatch.setattr(psutil.Process, "environ", watched_environ)

    session = InvisiblePlaywright(seed=7101, binary_path=firefox_binary,
                                  humanize=False, headless=True)
    with session as browser:
        page = browser.new_page()
        page.set_content("<p>born in the job</p>")
        seen = list(calls)
    assert not seen, (
        "the launch read the machine's processes %d times; the first from:\n%s"
        % (len(seen), seen[0][1]))


def test_after_close_no_process_of_the_session_is_left(firefox_binary):
    from invisible_core.process import find_processes, wait_until_gone, SessionToken
    from invisible_playwright import InvisiblePlaywright
    from invisible_playwright._juggler._reaped import wait_reaped

    session = InvisiblePlaywright(seed=7102, binary_path=firefox_binary,
                                  humanize=False, headless=True)
    with session as browser:
        page = browser.new_page()
        page.set_content("<p>one page, so a content process exists</p>")
        token = SessionToken(_token_value(session))
        assert len(find_processes(token)) >= 2, "no tree to leave behind"
        browser.close()
    # ⛔ WITH A CORE THAT HAS A REAPER the close returns when the browser has exited and the sender of the pings is the
    # reaper's: it is waited for here, once the session is over (the removal of the directories is handed to it then).
    assert wait_reaped(token, timeout=30), "the reaper of the launch did not finish within 30 s"
    left = [] if wait_until_gone(token, timeout=30) else find_processes(token)
    assert not left, "processes of the session after close(): %s" % [
        (p.pid, p.name()) for p in left]


_CLIENT = textwrap.dedent("""
    import sys, time
    from invisible_playwright import InvisiblePlaywright

    session = InvisiblePlaywright(seed=7103, binary_path=sys.argv[1],
                                  humanize=False, headless=True)
    browser = session.__enter__()
    page = browser.new_page()
    page.set_content("<p>a page, so a content process exists</p>")
    print("TOKEN", session._session_token.value, flush=True)
    time.sleep(300)
""")


@pytest.mark.skipif(not WINDOWS, reason="the job object is Windows; Linux has no equivalent here")
def test_when_the_client_is_killed_no_engine_process_is_left(firefox_binary, tmp_path):
    import psutil
    from invisible_core.process import SessionToken, find_processes, terminate

    script = tmp_path / "client.py"
    script.write_text(_CLIENT, encoding="utf-8")
    client = subprocess.Popen([sys.executable, str(script), firefox_binary],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    lines = []

    def read():
        for raw in iter(client.stdout.readline, b""):
            lines.append(raw.decode("utf-8", "replace").strip())
            if lines[-1].startswith("TOKEN "):
                return

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(240)
    token = SessionToken(next((x.split()[1] for x in lines if x.startswith("TOKEN ")), ""))
    assert token, "the client never launched: %r" % lines
    try:
        tree = find_processes(token)
        assert len(tree) >= 2, "the engine's tree was not there to be killed"
        client.kill()                         # TerminateProcess: no teardown runs
        _, alive = psutil.wait_procs(tree, timeout=30)
        assert not alive, "survivors of the killed client: %s" % [
            (p.pid, p.name()) for p in alive]
    finally:
        client.kill()
        terminate(find_processes(token))
