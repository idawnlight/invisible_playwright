import faulthandler
import http.server
import os
import random
import re
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from _servers import LocalServer
from _browser_census import _a_module_leaves_no_browser_open, _the_connections_a_test_made_are_closed_with_it  # noqa: F401  (autouse)


@pytest.fixture
def session_executable(monkeypatch, tmp_path):
    """An engine location for tests of session options and environment."""
    executable = tmp_path / "firefox"
    monkeypatch.setattr("invisible_playwright._session.resolve_executable",
                        lambda binary_path=None: executable)
    return executable

# ── The watchdog that says WHERE it is stuck, while it is still stuck ─────────
#
# The e2e hangs in 12% of CI runs (4 out of 33, from 2026-08-01 to 08-13) and
# until 2026-08-13 it left nothing behind: the job was killed by the 40-minute
# ceiling, so it came back `cancelled` and not `failure`, and the following
# steps never ran. `--timeout 420 --timeout-method thread` in run_e2e.py now
# kills it at 7 minutes printing the stack, but seven minutes is a long time
# and a single stack alone does not say whether the process is STUCK or just
# slow.
#
# This is what says it: at 150 seconds inside ONE test it prints the stack of
# every thread, and repeats it. Two identical dumps mean stuck; two different
# dumps mean slow. Those are opposite diagnoses and no other tool tells them
# apart.
#
# 150 seconds because the slowest legitimate test measured on CI is
# `test_webgl_readpixels_no_masking_signature` at 94.4 s, and the second at
# 11.4: on a healthy run it prints nothing, and on a slow runner it prints at
# most once, which is output and not a failure.
#
# To a FILE, not stderr, and this is not a detail: with pytest's default
# capture it redirects file descriptors 1 and 2 at the OS level, so even
# writing to the original `sys.__stderr__` ends up in the capture buffer -
# which pytest shows only once a failure has occurred, and here the process
# is being KILLED. Tested: with the threshold at 3 seconds on a test stuck in
# `accept()`, the same call prints four stacks when run by hand and ZERO
# under pytest. A file survives the kill and gets uploaded as an artifact on
# CI.
_WATCHDOG_S = float(os.environ.get("INVPW_TEST_WATCHDOG_S", "150"))
_WATCHDOG_FILE = os.environ.get(
    "INVPW_TEST_WATCHDOG_FILE",
    str(Path(tempfile.gettempdir()) / "invpw-e2e-watchdog.log"))
_watchdog_handle = None


def _watchdog_open():
    """Opens the file on the FIRST occurrence, not at session start.

    Only `e2e` tests are watched: they are the only ones that open a browser,
    and the one measured hang lives there. Arming it on the unit suite too
    wrote 439 header lines for 38 KB of file on every run, i.e. noise on a
    bench that did not need it.
    """
    global _watchdog_handle
    if _watchdog_handle is not None or _WATCHDOG_S <= 0:
        return _watchdog_handle
    try:
        _watchdog_handle = open(_WATCHDOG_FILE, "a", buffering=1, encoding="utf-8")
    except OSError:
        # A watchdog that fails the suite over its OWN problem is worse than
        # the defect it is watching for.
        return None
    _watchdog_handle.write("\n=== session started, threshold %ss ===\n" % _WATCHDOG_S)
    print("[watchdog] test stacks past %ss -> %s" % (_WATCHDOG_S, _WATCHDOG_FILE))
    return _watchdog_handle


def pytest_configure(config):
    # The run stops at the first refusal of the engine (tests/engine_refusal.py).
    from engine_refusal import StopAtTheFirstRefusal
    config.pluginmanager.register(StopAtTheFirstRefusal(config), "engine-refusal-stop")
    # What the core did about the GeoIP database during the run, for scripts/ci_lab_geoip.py verify (the CI legs set GEOIP_USED_OUT).
    if os.environ.get("GEOIP_USED_OUT"):
        from geoip_used import GeoipUsedRecorder
        recorder = GeoipUsedRecorder(os.environ["GEOIP_USED_OUT"])
        recorder.start()
        config.pluginmanager.register(recorder, "geoip-used")


def pytest_runtest_setup(item):
    if item.get_closest_marker("e2e") is None:
        return
    handle = _watchdog_open()
    if handle is not None:
        handle.write("\n--- %s ---\n" % item.nodeid)
        faulthandler.dump_traceback_later(_WATCHDOG_S, repeat=True, file=handle)


def pytest_runtest_teardown(item, nextitem):
    if _watchdog_handle is not None:
        faulthandler.cancel_dump_traceback_later()


def pytest_collection_modifyitems(config, items):
    """The e2e tests of the post-publication files (``tests/_post_publication.py``: pip, uv, PyPI, GitHub) belong to the round after a publication: a run
    that does not NAME their file does not run them. ``user-install.yml`` names both files; ``scripts/run_e2e.py`` and every PR or candidate leg do not."""
    import _post_publication

    left = [item for item in items
            if item.get_closest_marker("e2e") is not None and _post_publication.is_left_out(item.path, config.args)]
    if left:
        items[:] = [item for item in items if item not in left]
        config.hook.pytest_deselected(items=left)


def pytest_unconfigure(config):
    global _watchdog_handle
    if _watchdog_handle is not None:
        faulthandler.cancel_dump_traceback_later()
        _watchdog_handle.close()
        _watchdog_handle = None

# GUARDED, and not for tidiness. The user-path e2e files are collected in an
# environment that deliberately does NOT have this package installed - they
# build their own venv and install from the index, because a second copy on the
# runner's path would mean their assertions read the wrong one. A hard import
# here made collection fail outright (exit 4, "No module named
# invisible_playwright") on both CI runners while passing locally, where a
# checkout is always importable.
#
# The fixtures below still need it; they fail loudly at USE time instead, which
# is a clear message about one test rather than a collection error covering the
# whole run.
try:
    from invisible_core._fpforge import generate_profile
    from invisible_core.seal import DEFAULT_ENTRY_REL
    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - only in the e2e-only environment
    generate_profile = None
    DEFAULT_ENTRY_REL = None
    _IMPORT_ERROR = exc


def _require_package():
    if _IMPORT_ERROR is not None:
        raise pytest.UsageError(
            f"this fixture needs invisible_playwright importable, and it is "
            f"not: {_IMPORT_ERROR}. That is expected only when running the "
            f"user-path e2e files, which install the package into their own "
            f"venv; any other test needs `pip install -e .`")


@pytest.fixture(autouse=True)
def _offline_session_geo(request, monkeypatch):
    """Outside the e2e, the one network step of composing a session (the egress
    IP, `_session.session_geo`) answers a fixed geo: New York, en-US. A launch
    that names no session composes one on the server (`session_for_launch`),
    and a unit test that drives `op_launch` must not ask the network who it is."""
    if request.node.get_closest_marker("e2e") is not None:
        return
    try:
        from invisible_playwright import _session
        from invisible_core import decide_session_locale
        from invisible_core._geo import SessionGeo
    except Exception:  # the package is absent: the tests that need it say so
        return
    geo = SessionGeo("America/New_York", None, locale=decide_session_locale("en-US"))
    monkeypatch.setattr(_session, "session_geo", lambda *_a, **_k: geo)


@pytest.fixture(autouse=True)
def _what_the_core_declares_for_a_proxy_is_recorded(monkeypatch):
    """The core keeps no UDP verdict in memory, so a test that wants to know what the launch declared reads it where it is
    written: `_udp_line.record_declared` spies the one function that renders the engine's proxy command (unit and e2e)."""
    from _udp_line import record_declared
    record_declared(monkeypatch)


@pytest.fixture(autouse=True)
def _a_unit_test_asks_mozilla_for_no_security_state(request, monkeypatch):
    """Outside the e2e, a launch asks Mozilla for nothing and runs no generation.

    The core's `write_user_js` (every launch the server makes) places the security state in the profile: on a cold cache it downloads Mozilla's collections in line and
    has the engine generate the state from them, and what it copies would differ with the cache of whoever runs the test. The function the launch calls
    (`ensure_security_state`) is stubbed, as the core's own suite does (tests/conftest.py of invisible-core): nothing ready, no request, no engine. The tests of `fetch`
    set their own; a test marked `e2e` gets the real one (the CI legs point STEALTHFOX_SECURITY_STATE at the ready directory of `ci/security-state/`)."""
    if request.node.get_closest_marker("e2e") is not None:
        return
    try:
        import invisible_core._security_state as _ss
    except Exception:  # the package is absent: the tests that need it say so
        return
    monkeypatch.setattr(_ss, "ensure_security_state", lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def _a_unit_test_does_not_depend_on_the_seal_of_whoever_runs_it(request, monkeypatch):
    """Outside the e2e, a test does not read ``INVISIBLE_SEAL_FILE``.

    The CI jobs export it (a LOCAL seal of the candidate, with no published assets) for the e2e tests that launch the engine under test. A unit test
    that composes a launch (``build_env``, ``launch_persistent_context``, a second browser served by a virtual display) reaches ``ensure_binary`` and
    died with ``SealError: the active seal ... is a LOCAL seal with no published assets`` only because of where it ran. Here the variable is taken out
    of the test's environment (``monkeypatch`` puts it back); the core reads the seal on every call, so the test resolves the
    packaged one; a test that wants a seal sets the variable itself, after this. The e2e tests, which are the ones that need it, keep it."""
    if request.node.get_closest_marker("e2e") is not None:
        yield
        return
    monkeypatch.delenv("INVISIBLE_SEAL_FILE", raising=False)
    yield


@pytest.fixture(scope="session", autouse=True)
def _the_lab_declares_the_exit(request, tmp_path_factory):
    """Where an e2e launch leaves from is DECLARED, not asked of three services of the internet.

    A launch with no proxy asks the core's ``discover_egress_ip`` for its exit, which asks api.ipify.org, icanhazip.com and checkip.amazonaws.com: a
    third party in every one of the 222 launches that name neither ``timezone=`` nor ``proxy=``. For a session in which an e2e test is selected, the
    question with no proxy is answered the exit of the lab (``tests/_lab_egress.py``), and the GeoIP database is the lab's when no leg set one. SESSION
    scope, because the module-scoped browsers of several files launch before any function-scoped fixture; a test that replaces the question (an exit
    behind its own gateway) does it with ``monkeypatch`` and goes back to this answer. A session with no e2e test selected is left as it is: the unit
    tests of the discovery call the real function."""
    if not any(item.get_closest_marker("e2e") is not None for item in request.session.items):
        yield None
        return
    import _lab_egress

    with pytest.MonkeyPatch.context() as patch:
        yield _lab_egress.install(patch, tmp_path_factory.mktemp("lab-geoip"))


@pytest.fixture(autouse=True)
def _no_test_asks_the_world_where_it_leaves_from():
    """A test fails when the question of the exit reached a proxy of the tests that forwards for real.

    The product asks "where do I leave from" through the proxy of a launch, HTTPS, to api.ipify.org and its siblings. A proxy of the tests that forwards
    for real would have carried it to the internet, and the answer is the address of the machine that runs the test: it changes on a runner, in the
    middle of a session (``ProxyEgressDrifted``). Each such proxy declares its exit and the lab answers with it (``tests/_lab_egress.py``); one that
    was not declared, or a test that bypassed the lab, makes the proxy REFUSE the question (``_socks_servers.asks_the_world``), and this fixture turns
    that into a failure with the name of the proxy and of the service, instead of a launch that works until the network of the runner changes."""
    import _socks_servers

    yield
    asked = _socks_servers.nobody_asked_the_world()
    if asked:
        pytest.fail("the question of the exit went toward the internet: " + "; ".join(asked), pytrace=False)


@pytest.fixture
def local_egress_echo(monkeypatch):
    """The REAL discovery of the exit, for the test that must prove it, against an echo service of its own on loopback: the three services of the
    product are replaced by one that answers :data:`_lab_egress.LAB_EGRESS_IP` and counts its questions. Nothing goes to the internet."""
    from invisible_core import _geo

    import _lab_egress

    current = _geo.discover_egress_ip
    monkeypatch.setattr(_geo, "discover_egress_ip", getattr(current, "original", current))
    echo = _lab_egress.EchoService()
    monkeypatch.setattr(_geo, "_IP_ECHO_ENDPOINTS", (echo.url,))
    yield echo
    echo.close()


@pytest.fixture
def deterministic_rng():
    """Seeded RNG for reproducible tests."""
    return random.Random(42)


@pytest.fixture
def sample_profile():
    """A Profile generated from seed=42 for reuse across tests."""
    _require_package()
    return generate_profile(seed=42)


@pytest.fixture(scope="session")
def first_run_prefs():
    """What invisible_core declares for a new profile's first run. Since firefox-55 the
    engine leaves Mozilla's first-run lines, so a test that starts the engine without
    the core has to bring this, or a second tab fails Browser.newPage."""
    from invisible_core._fpforge import generate_profile
    from invisible_core.prefs import translate_profile_to_prefs

    prefs = translate_profile_to_prefs(generate_profile(970411), timezone="America/New_York")
    return {k: v for k, v in prefs.items() if k.startswith(("termsofuse.", "browser.preonboarding."))}


@pytest.fixture(scope="session")
def firefox_binary():
    """Locate the patched Firefox binary for E2E tests, or skip cleanly.

    Single source of truth for every E2E test (previously each test file had its
    own copy - and three of them silently ignored INVPW_BINARY_PATH, so they kept
    testing whatever was in the cache even when you pointed the suite at a
    specific build: a false-confidence trap). Lookup order:

      1. ``INVPW_BINARY_PATH`` env var - point the whole suite at a local build
         or a freshly-extracted release (this is how the full-suite gate runs).
      2. Cached binary under ``cache_dir_for_seal()`` (post ``fetch``).
      3. Skip - we never trigger an implicit multi-hundred-MB network download
         inside a test run.
    """
    env_path = os.environ.get("INVPW_BINARY_PATH")
    if env_path:
        if Path(env_path).exists():
            return env_path
        pytest.skip(f"INVPW_BINARY_PATH={env_path!r} does not exist")

    # Before the table is read: with the package not importable it is None, and
    # `in None` answered a TypeError that hid the reason - measured 2026-10-25
    # with a core whose version the wrapper's pin did not name.
    _require_package()
    if sys.platform not in DEFAULT_ENTRY_REL:
        pytest.skip(f"unsupported platform: {sys.platform}")
    from invisible_core.download import cache_dir_for_seal
    entry = cache_dir_for_seal() / DEFAULT_ENTRY_REL[sys.platform]
    if not entry.exists():
        pytest.skip(
            "patched Firefox binary not cached and INVPW_BINARY_PATH unset; "
            "set INVPW_BINARY_PATH=<firefox binary> or run `invisible-playwright fetch`"
        )
    return str(entry)


# ---------------------------------------------------------------------------
# The engine a test runs against. main's e2e runs against the engine the
# published core pins AND against the candidate of the next engine (the gate),
# so a property only the newer engine promises must not run on the older one.
#
# The engine is read from the seal the process runs under, the same one that
# checks the launch: `active_seal().tag`. Only the number counts: `firefox-64`
# and `firefox-64-candidate` are both 64. A tag with no number (`local`, or
# anything else) does not say which engine it is, so the test does not skip and
# does not run: it fails and says why. A candidate is sealed as
# `firefox-N-candidate` by the gate.
#
# No new pytest marker: the marker vocabulary is shared with invisible_core
# and a marker only here would make the two drift.
_ENGINE_TAG = re.compile(r"^firefox-(\d+)(?:-candidate)?$")


def _engine_number(tag):
    """N of `firefox-N` or `firefox-N-candidate`, or None."""
    m = _ENGINE_TAG.match(tag or "")
    return int(m.group(1)) if m else None


def _skip_unless_engine_at_least(minimum, tag):
    want = _engine_number(minimum)
    if want is None:
        raise pytest.UsageError("engine_at_least wants 'firefox-N', got %r" % (minimum,))
    have = _engine_number(tag)
    if have is None:
        pytest.fail("the engine's seal has no number: a candidate must be sealed as "
                    "firefox-N-candidate (seal tag %r)" % (tag,), pytrace=False)
    if have < want:
        pytest.skip("the property needs %s or later; this engine is %s"
                    % (minimum, tag))


@pytest.fixture(scope="session")
def engine_at_least():
    """``engine_at_least("firefox-65")``: skip the test unless the engine in
    use is that release or a later one (see above for how it is read). A seal
    whose tag has no number (``local``, anything but firefox-N or
    firefox-N-candidate) makes the test FAIL, saying so: it neither skips nor
    runs."""
    _require_package()
    from invisible_core.seal import active_seal
    tag = active_seal().tag

    def check(minimum):
        _skip_unless_engine_at_least(minimum, tag)
    return check


@pytest.fixture(scope="session")
def engine_reaches():
    """``engine_reaches("firefox-68")``: whether the engine in use is that release or a later one, for a test of a
    required suite that asserts more on a newer engine and must not skip on an older one (a required suite allows no
    skip, scripts/ci_verdict.py). A seal whose tag has no number fails the test, as with ``engine_at_least``."""
    _require_package()
    from invisible_core.seal import active_seal
    tag = active_seal().tag

    def check(minimum):
        want, have = _engine_number(minimum), _engine_number(tag)
        if want is None:
            raise pytest.UsageError("engine_reaches wants 'firefox-N', got %r" % (minimum,))
        if have is None:
            pytest.fail("the engine's seal has no number: a candidate must be sealed as "
                        "firefox-N-candidate (seal tag %r)" % (tag,), pytrace=False)
        return have >= want
    return check


@pytest.fixture
def the_udp_line(engine_at_least):
    """The UDP line (the declared network, the DoH through the proxy, the SOCKS5 UDP layer, HTTP/3 through it) is the engine
    firefox-63: a test of it skips on an older engine (the published one that main's e2e fetches) and runs on the candidate."""
    engine_at_least("firefox-63")


# ---------------------------------------------------------------------------
# Local HTTP fixture server - service workers need a real http(s) origin
# (data: and about:blank are opaque-origin, no SW registration possible).
# It lives here because two modules use it (test_service_worker.py and
# test_service_worker_controller_e2e.py, which needs its own module for a
# fresh browser per case): importing a fixture from another test module is
# what ruff reports as F811.
# ---------------------------------------------------------------------------


class _SWFixtureHandler(http.server.BaseHTTPRequestHandler):
    """Serves a tiny set of routes for SW lifecycle testing."""

    PAGES = {
        "/": (200, "text/html", b"""<!doctype html>
<html><head><title>sw-host</title></head>
<body>
<script>
window.__swState = 'loading';
if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js')
        .then(reg => { window.__swState = 'registered'; })
        .catch(err => { window.__swState = 'failed:' + err.message; });
} else {
    window.__swState = 'unsupported';
}
</script>
</body></html>
"""),
        "/sw.js": (200, "application/javascript", b"""
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
self.addEventListener('fetch', e => {
    if (e.request.url.endsWith('/from-sw')) {
        e.respondWith(new Response('hello from SW', {
            headers: {'content-type': 'text/plain'},
        }));
    }
    // Fall through for everything else - exercises the
    // interceptAfterServiceWorkerResets path that was broken pre-firefox-6.
});
"""),
        "/from-sw": (200, "text/plain", b"network-fallback"),
        "/from-network": (200, "text/plain", b"net-only"),
    }

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in self.PAGES:
            status, ctype, body = self.PAGES[path]
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # SW requires HTTPS or localhost - we're on localhost so plain http is fine
            self.send_header("Service-Worker-Allowed", "/")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args, **kwargs):
        pass  # silence stdout


@pytest.fixture(scope="module")
def fixture_server():
    """Spin up a localhost HTTP server with SW-friendly headers. Yields
    the base URL (e.g., 'http://127.0.0.1:54321')."""
    httpd = LocalServer(("127.0.0.1", 0), _SWFixtureHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


# ======================================================================================================================
#  Fixtures shared by several e2e files. They live here, not in one file that the others import from: an imported fixture
#  that a test then names as an argument is a redefinition (ruff F811, the lint of the `tests` job).
# ======================================================================================================================
@pytest.fixture
def doh():
    import h2  # noqa: F401 - the dev extra installs it: a missing one is a failure, never a skip
    from _doh_servers import FakeDoh
    from _doh_support import ZONE

    server = FakeDoh(ZONE)
    yield server
    server.close()


@pytest.fixture
def gateway(doh):
    from _doh_servers import DohGateway

    made = []

    def make(name, **kwargs):
        gw = DohGateway(name, doh, **kwargs)
        made.append(gw)
        return gw

    yield make
    for gw in made:
        gw.close()


@pytest.fixture
def trap():
    """The resolver trap, counting every question the machine's resolver is asked.

    None only where it cannot run AND the gate does not require it (a developer's machine). With INVPW_REQUIRE_TRAP=1
    (the gate's Linux leg) a trap that is unavailable or whose port 53 cannot be bound is a failure with its reason."""
    import _trap_policy
    from _leaks import ResolverTrap

    if not ResolverTrap.available:
        why = _trap_policy.failure(None, False, ResolverTrap.reason)
        if why:
            pytest.fail(why)
        yield None
        return
    resolver = ResolverTrap()
    try:
        resolver.__enter__()
    except OSError as exc:  # port 53 taken (another worker holds the trap)
        why = _trap_policy.failure(None, True, bind_error=exc)
        if why:
            pytest.fail(why)
        yield None
        return
    try:
        yield resolver
    finally:
        resolver.__exit__(None, None, None)


@pytest.fixture
def watcher(firefox_binary):
    """Started BEFORE the test body, so before any launch; stopped by the test or here."""
    from pathlib import Path

    from _leaks import Watcher

    w = Watcher(match=("firefox", Path(firefox_binary).stem.lower())).start()
    yield w
    w.stop()


@pytest.fixture
def pages():
    from _doh_support import Pages

    server = Pages()
    yield server
    server.close()


@pytest.fixture
def socks5_tcp_only():
    from _socks_servers import Socks5TcpOnly

    srv = Socks5TcpOnly()
    yield srv
    srv.close()


@pytest.fixture
def local_https_page():
    """A trivial localhost page (used by the no-proxy srflx test)."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        #: A silent socket no longer pins a thread down: after five seconds it drops.
        timeout = 5

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>wrtc</body></html>")

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


@pytest.fixture(scope="module", params=["in-process", "stock-client"])
def browser(request, firefox_binary):
    """One browser per transport for the whole file (the popup tests of two files use it)."""
    import importlib
    if request.param == "in-process":
        from invisible_playwright import InvisiblePlaywright

        with InvisiblePlaywright(binary_path=firefox_binary, headless=True) as b:
            yield b
        return
    try:     # the stock client (a pinned dev dependency), imported by name: the fork's guard forbids a static import
        sync_playwright = importlib.import_module("playwright.sync_api").sync_playwright
    except ImportError as exc:
        pytest.fail("the stock Playwright client is missing (%s): install the wrapper with its dev extra" % exc,
                    pytrace=False)
    from invisible_playwright._juggler.remote import launch_server

    server = launch_server(headless=True, binary_path=firefox_binary, locale="en-US", timezone="Europe/Rome")
    try:
        with sync_playwright() as p:
            b = p.firefox.connect(server.ws_endpoint)
            try:
                yield b
            finally:
                b.close()
    finally:
        server.close()
