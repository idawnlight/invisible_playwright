"""The exit address of the lab: what a launch of an e2e test is told about where it leaves from, so that no test asks a service of the internet.

A launch (``InvisiblePlaywright(...)``, ``launch_server(...)``) composes its session in the core (``invisible_core.prepare_session_geo``), and the first step
of it is ``invisible_core._geo.discover_egress_ip``: with no proxy, an HTTP request to three "what is my address" services (api.ipify.org, icanhazip.com,
checkip.amazonaws.com, a budget of 15 s), whose answer is looked up in the GeoIP database for the zone, the language and the position of the session.
Every e2e test that names neither ``timezone=`` nor ``proxy=`` (222 launches in 125 files), and every test that names only a zone, therefore depended on
three services and on the network of the runner reaching them. The product is not wrong to ask: a user has no other way to know their exit. A TEST has one:
it declares it.

This module is that declaration, made once per session by the autouse fixture of ``conftest.py`` (``_the_lab_declares_the_exit``):

* the question with NO proxy is answered :data:`LAB_EGRESS_IP`, a public address (so the core does not refuse it as unroutable) that the GeoIP database of
  the lab (``scripts/ci_lab_geoip.py``: one record for every public address that is not one of its six exits) maps to New York, en-US. The same session
  as before on every runner, whatever its address and whatever its network;
* the question WITH a proxy is left to what answered it before: the local gateways of the tests answer ``api.ipify.org`` themselves
  (``tests/_socks_servers.py``), and a test that replaces the question (``tests/_udp_line.egress_by_proxy``) does it with ``monkeypatch`` for its own
  duration, which undoes to this answer and not to the network;
* the database is the lab's too: when ``STEALTHFOX_GEOIP_MMDB`` is not set (a developer's machine; the CI legs set it) it is built here, from the same table
  and the same bytes, instead of the core asking GitHub which database is the latest.

A test that must prove the discovery itself asks for the fixture ``local_egress_echo``: the real function, against an echo service of its own on
loopback. Nothing in a test goes to the internet to know an address.
"""
from __future__ import annotations

import importlib.util
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from _servers import LocalServer

ROOT = Path(__file__).resolve().parent.parent


def _address(*parts: int) -> str:
    return ".".join(str(part) for part in parts)


#: The exit every e2e launch with no proxy declares. A public address that is in no reserved range and in none of the six exits of the lab database, so
#: the lab database answers it with its one record for the rest of the public space (US, America/New_York, en-US), the same on every machine.
LAB_EGRESS_IP = _address(8, 8, 8, 8)


class DeclaredExit:
    """The replacement of ``discover_egress_ip``: counts the questions it answers, and passes a proxied one on to what answered it before."""

    def __init__(self, original: Callable[..., str], ip: str = LAB_EGRESS_IP) -> None:
        self.original = original
        self.ip = ip
        self.direct_questions = 0
        self.proxied_questions: List[Any] = []

    def __call__(self, proxy: Optional[Dict[str, Any]] = None, *args: Any, **kwargs: Any) -> str:
        if not proxy:
            self.direct_questions += 1
            return self.ip
        self.proxied_questions.append(proxy.get("server") if isinstance(proxy, dict) else proxy)
        return self.original(proxy, *args, **kwargs)


def lab_database_bytes() -> bytes:
    """The GeoIP database of the lab, from ``scripts/ci_lab_geoip.py`` (the same bytes on every run)."""
    spec = importlib.util.spec_from_file_location("ci_lab_geoip_of_the_tests", ROOT / "scripts" / "ci_lab_geoip.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_bytes()


def use_the_lab_database(patch: Any, directory: Path, environ: Optional[Dict[str, str]] = None) -> Optional[Path]:
    """Point ``STEALTHFOX_GEOIP_MMDB`` at the lab database when nothing does yet (a CI leg has set it, and its sha256 is the pin's). ``patch`` is a
    ``pytest.MonkeyPatch``: the variable goes back with it. Returns the file written, or None when the variable was already set."""
    import os

    seen = os.environ if environ is None else environ
    if seen.get("STEALTHFOX_GEOIP_MMDB"):
        return None
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "geoip-aio-all.mmdb"
    path.write_bytes(lab_database_bytes())
    patch.setenv("STEALTHFOX_GEOIP_MMDB", str(path))
    return path


def install(patch: Any, directory: Path) -> Optional[DeclaredExit]:
    """Declare the exit of the lab for the duration of ``patch`` (a ``pytest.MonkeyPatch``). None when the core is not importable: the user-path e2e
    files run in an environment that has not got it and make their own."""
    try:
        import invisible_core
        from invisible_core import _geo
    except ImportError:
        return None
    declared = DeclaredExit(_geo.discover_egress_ip)
    patch.setattr(_geo, "discover_egress_ip", declared)
    patch.setattr(invisible_core, "discover_egress_ip", declared, raising=False)
    use_the_lab_database(patch, directory)
    return declared


class EchoService:
    """What the three services of the product answer, on loopback: the address of the caller as the body, and the number of questions it was asked."""

    def __init__(self, answer: str = LAB_EGRESS_IP) -> None:
        owner = self
        self.answer = answer
        self.questions = 0

        class Handler(BaseHTTPRequestHandler):
            timeout = 5

            def do_GET(self) -> None:
                owner.questions += 1
                body = owner.answer.encode("ascii")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any, **kwargs: Any) -> None:
                pass

        self._server = LocalServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/" % self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
