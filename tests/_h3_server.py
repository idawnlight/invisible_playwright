"""A local HTTPS server that speaks HTTP/3 as well, reached by its address.

The same port answers TCP (TLS, HTTP/2 by ALPN with an `Alt-Svc: h3` header, HTTP/1.1 for a client that offers nothing
else) and UDP (QUIC, HTTP/3, aioquic), with a self-signed certificate for the address it is given. The TCP side speaks HTTP/2
because that is what a real site does: the FIRST visit of a page reads `h2`, and only after the Alt-Svc it carries may a
later one read `h3` (retail Firefox 159, native DNS in loopback: h2, h3, h3; behind a proxy h3 within a few visits, and at the first one when the DNS answer carries the HTTPS record: 3 sessions of 15 in the third run). It records, for every QUIC connection, the address the packets
came from: a connection that comes from anywhere but the relay of a gateway on this host (127.0.0.1) did not go through
the proxy.

A client that has a QUIC handshake in progress toward the server is served its next TCP connection only once that handshake has ended (``_QuicFirst``): a real network completes
QUIC in 1 round trip and TCP+TLS in 2, and a browser takes the first connection that is ready.

It also keeps its own record of what it saw (``H3Server.record``, ``write_record``): per TCP connection its accept, the ALPN, every request
path and its end, per QUIC connection its first datagram, its handshake, every request and its end (with the reason), each with ``m``
(``time.perf_counter()`` of the test process) and ``t`` (``time.time()``). It only listens: no answer depends on it.

Needs `aioquic` and `h2` (pip install aioquic h2; the dev extra installs both).
"""
from __future__ import annotations

import asyncio
import datetime
import ipaddress
import itertools
import json
import socket
import socketserver
import ssl
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Tuple

from _ports import bind_tcp_and_hold_udp


def _certificate(address: str, directory: Path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, address)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(address))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


#: The one document every path answers, over TCP and over QUIC.
PAGE = b"<!doctype html><meta charset=utf-8><title>h3</title><p>ok</p>"

class _QuicFirst:
    """Keeps the order of a real network between the two connections a browser opens to one server: QUIC is ready after 1 round trip and TCP+TLS after 2, so the QUIC one is
    taken. The Python QUIC server of the lab needs longer than TCP+TLS under load and would invert that. For a peer address that has a QUIC handshake in progress toward the
    server, the TLS of a new TCP connection from that address waits until the handshake has ended. It waits for an event, never for a time: ``ended`` is called when the
    handshake completes (the client has had its connection ready for a round trip by then), fails, or the connection terminates (the idle timeout is one of them), and
    ``release_all`` when the server closes. With no handshake in progress for the address nothing is held.

    The residual case: a client that starts a QUIC handshake and vanishes without a CONNECTION_CLOSE leaves its handshake in progress, so the TCP connections of the same
    IP are held until the QUIC idle timeout ends the connection (aioquic: 60 s by default, ``H3Server(quic_idle_timeout=...)``). A real network does not hold the TCP of a
    client for that long, but a client of the lab that opens QUIC and goes away is not one a test of the lab depends on; it is measured, with the order asserted and not a
    duration, in tests/test_the_h3_lab_server_lets_quic_finish_first.py."""

    def __init__(self):
        self._changed = threading.Condition()
        self._in_progress: Dict[str, int] = {}
        self._closed = False

    def began(self, peer: str) -> None:
        with self._changed:
            self._in_progress[peer] = self._in_progress.get(peer, 0) + 1

    def ended(self, peer: str) -> None:
        with self._changed:
            left = self._in_progress.get(peer, 0) - 1
            if left > 0:
                self._in_progress[peer] = left
            else:
                self._in_progress.pop(peer, None)
            self._changed.notify_all()

    def hold(self, peer: str, held: List[str]) -> None:
        """Returns at once when no QUIC handshake of ``peer`` is in progress; otherwise records the hold in ``held`` and returns when none is."""
        with self._changed:
            if peer not in self._in_progress or self._closed:
                return
            held.append(peer)
            self._changed.wait_for(lambda: self._closed or peer not in self._in_progress)

    def release_all(self) -> None:
        with self._changed:
            self._closed = True
            self._changed.notify_all()


class _TcpServer(socketserver.ThreadingTCPServer):
    # On Windows SO_REUSEADDR is "share the port": with a number the picker drew (tests/_ports.py) it would bind over a listener that is there.
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True


def _tcp_server_on_a_port_udp_binds(address: str):
    """A TCP server on a port number that UDP can bind too, with the UDP socket already bound to it, as ``(server, port, udp)``: the one picker of the harness (tests/_ports.py), which the
    witness and the resolver trap walk the same sequence of. The first try is the system's own pick (port 0); the others are drawn at random
    below the dynamic range, because Windows hands out ephemeral TCP ports in sequence and keeps excluded ranges of a hundred ports for UDP
    inside the same zone (run 43230437094, ``wrapper.e2e.web`` on Windows: fifty consecutive picks refused, three tests red). Every refusal is
    kept with its code and the last three are in the error. The UDP socket is NOT closed again: it goes to the QUIC server's loop, so that the
    number is held on UDP from the first look at it to its use (a probe that was closed left a window for any other bind of that number)."""
    return bind_tcp_and_hold_udp(address, lambda host, number: _TcpServer((host, number), _TcpHandler), socket_module=socket)


class _TcpHandler(socketserver.BaseRequestHandler):
    """One TCP connection: the TLS handshake here (not in the accept loop), then HTTP/2 or HTTP/1.1 by the ALPN."""

    def handle(self):
        owner = self.server.owner
        cid = "tcp-%d" % next(owner._ids)
        owner.note("tcp_accept", conn=cid, peer=list(self.client_address[:2]))
        reason = "handler ended"
        try:
            owner._quic_first.hold(self.client_address[0], owner.tcp_held)
            self.request.settimeout(10)
            tls = owner._tls.wrap_socket(self.request, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            owner.note("tcp_handshake_failed", conn=cid, error=repr(exc))
            return
        try:
            tls.settimeout(60)
            alpn = tls.selected_alpn_protocol() or "http/1.1"
            owner.tcp_alpn.append(alpn)
            owner.note("tcp_handshake_done", conn=cid, alpn=alpn)
            if alpn == "h2":
                reason = self._h2(tls, owner, cid)
            else:
                reason = self._http1(tls, owner, cid)
        except (ssl.SSLError, OSError) as exc:
            reason = "error: %r" % (exc,)
        finally:
            owner.note("tcp_close", conn=cid, reason=reason)
            try:
                tls.close()
            except OSError:
                pass

    @staticmethod
    def _h2(tls, owner, cid):
        import h2.config
        import h2.connection
        import h2.events
        import h2.exceptions

        conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=False, header_encoding="utf-8"))
        conn.initiate_connection()
        tls.sendall(conn.data_to_send())
        while True:
            data = tls.recv(65536)
            if not data:
                return "the client closed the connection (EOF)"
            try:
                events = conn.receive_data(data)
            except h2.exceptions.ProtocolError as exc:
                tls.sendall(conn.data_to_send())
                return "h2 protocol error: %r" % (exc,)
            for event in events:
                if isinstance(event, h2.events.RequestReceived):
                    owner.tcp_requests.append(dict(event.headers).get(":path", ""))
                    owner.note("tcp_request", conn=cid, stream=event.stream_id, path=dict(event.headers).get(":path", ""))
                    conn.send_headers(event.stream_id, [
                        (":status", "200"), ("content-type", "text/html; charset=utf-8"),
                        ("content-length", str(len(PAGE))), ("alt-svc", owner.alt_svc),
                        ("access-control-allow-origin", "*")])
                    conn.send_data(event.stream_id, PAGE, end_stream=True)
                elif isinstance(event, h2.events.DataReceived):
                    conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, h2.events.ConnectionTerminated):
                    return "h2 GOAWAY from the client (error code %s)" % (event.error_code,)
            out = conn.data_to_send()
            if out:
                tls.sendall(out)

    @staticmethod
    def _http1(tls, owner, cid):
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = tls.recv(4096)
            if not chunk:
                return
            head += chunk
        parts = head.split(b"\r\n", 1)[0].split(b" ")
        owner.tcp_requests.append(parts[1].decode("latin-1") if len(parts) > 1 else "")
        tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: %d\r\n"
                    b"Alt-Svc: %s\r\nAccess-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n%s"
                    % (len(PAGE), owner.alt_svc.encode(), PAGE))
        return "answered, Connection: close"


class H3Server:
    """TCP+TLS (HTTP/2) and QUIC (HTTP/3) on one port of `address`."""

    def __init__(self, address: str, quic_idle_timeout: float = None):
        """``quic_idle_timeout`` (seconds) is the QUIC idle timeout of the server; None keeps aioquic's default (60 s)."""
        from aioquic.asyncio.protocol import QuicConnectionProtocol
        from aioquic.asyncio.server import QuicServer
        from aioquic.h3.connection import H3_ALPN, H3Connection
        from aioquic.h3.events import HeadersReceived
        from aioquic.quic.configuration import QuicConfiguration
        from aioquic.quic.events import ConnectionTerminated, HandshakeCompleted, ProtocolNegotiated

        self.address = address
        #: The address each QUIC connection came from.
        self.quic_peers: List[str] = []
        #: The same peers with the port they sent from: (address, port). On a machine where a gateway's relay sends from an address of
        #: this host (Windows cannot send from 127.0.0.1 to it) the address cannot tell the relay from a client that came direct; its port can.
        self.quic_peer_addrs: List[Tuple[str, int]] = []
        #: The paths of the requests that were answered over HTTP/3, and over TCP.
        self.h3_requests: List[str] = []
        self.tcp_requests: List[str] = []
        #: The ALPN each TCP connection negotiated ("h2", or "http/1.1").
        self.tcp_alpn: List[str] = []
        #: The peer address of each TCP connection whose TLS was held until a QUIC handshake of that address had ended (see ``_QuicFirst``).
        self.tcp_held: List[str] = []
        self._quic_first = _QuicFirst()
        #: The server's own record of what it saw, in order (``note``): a list of dicts, each with ``m``, ``t`` and ``kind``.
        self.record: List[dict] = []
        self._ids = itertools.count(1)
        self._dir = tempfile.TemporaryDirectory()
        cert, key = _certificate(address, Path(self._dir.name))

        server = self

        # One port for both: the TCP port first, then the same number for UDP.
        tcp, port, udp = _tcp_server_on_a_port_udp_binds(address)
        self.port = port
        #: What the TCP answer says about HTTP/3: the same port, for an hour.
        self.alt_svc = 'h3=":%d"; ma=3600' % port
        self._udp = udp
        self._tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._tls.load_cert_chain(str(cert), str(key))
        self._tls.set_alpn_protocols(["h2", "http/1.1"])
        tcp.owner = self
        self._tcp = tcp
        threading.Thread(target=tcp.serve_forever, daemon=True).start()

        class Protocol(QuicConnectionProtocol):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._http = None
                #: The address whose handshake this connection holds open in ``_quic_first``; None before its first datagram and once the handshake has ended.
                self._handshaking = None
                self._began = False
                self._cid = "quic-%d" % next(server._ids)
                self._datagrams = 0
                self._last_datagram_m = None

            def datagram_received(self, data, addr):
                self._datagrams += 1
                self._last_datagram_m = round(time.perf_counter(), 6)
                if self._datagrams == 1:
                    server.note("quic_first_datagram", conn=self._cid, peer=list(addr[:2]), bytes=len(data))
                # The first datagram of a peer is the start of its handshake: it is registered before the server answers it.
                if not self._began:
                    self._began = True
                    self._handshaking = addr[0]
                    server._quic_first.began(addr[0])
                super().datagram_received(data, addr)
                # A close (the peer's, or ours after a failed handshake) ends the handshake before the event of the end of the connection.
                if self._quic._close_event is not None:
                    self._handshake_ended()

            def _handshake_ended(self):
                if self._handshaking is not None:
                    peer, self._handshaking = self._handshaking, None
                    server._quic_first.ended(peer)

            def quic_event_received(self, event):
                if isinstance(event, (HandshakeCompleted, ConnectionTerminated)):
                    self._handshake_ended()
                kind = type(event).__name__
                if isinstance(event, ProtocolNegotiated):
                    server.note("quic_protocol_negotiated", conn=self._cid, alpn=event.alpn_protocol, datagrams=self._datagrams)
                elif kind == "HandshakeCompleted":
                    server.note("quic_handshake_done", conn=self._cid, alpn=getattr(event, "alpn_protocol", None),
                                datagrams=self._datagrams)
                elif kind == "ConnectionTerminated":
                    server.note("quic_close", conn=self._cid, error_code=event.error_code, frame_type=event.frame_type,
                                reason=event.reason_phrase, datagrams=self._datagrams, last_datagram_m=self._last_datagram_m)
                elif kind == "StreamReset":
                    server.note("quic_stream_reset", conn=self._cid, stream=event.stream_id, error_code=event.error_code)
                if isinstance(event, ProtocolNegotiated):
                    self._http = H3Connection(self._quic)
                    peer = self._quic._network_paths[0].addr
                    server.quic_peers.append(peer[0])
                    server.quic_peer_addrs.append((peer[0], peer[1]))
                if self._http is not None:
                    for http_event in self._http.handle_event(event):
                        if isinstance(http_event, HeadersReceived):
                            headers = dict(http_event.headers)
                            server.h3_requests.append(headers.get(b":path", b"").decode())
                            server.note("quic_request", conn=self._cid, stream=http_event.stream_id, path=server.h3_requests[-1])
                            self._http.send_headers(http_event.stream_id, [
                                (b":status", b"200"), (b"content-type", b"text/html; charset=utf-8"),
                                (b"access-control-allow-origin", b"*")])
                            self._http.send_data(http_event.stream_id, PAGE, end_stream=True)
                            self.transmit()

        configuration = QuicConfiguration(is_client=False, alpn_protocols=H3_ALPN)
        if quic_idle_timeout is not None:
            configuration.idle_timeout = quic_idle_timeout
        configuration.load_cert_chain(str(cert), str(key))
        self._loop = asyncio.new_event_loop()
        started = threading.Event()

        def run():
            asyncio.set_event_loop(self._loop)
            # What ``aioquic.asyncio.serve(address, port)`` does (1.x), with the socket that already holds the number.
            _transport, self._quic = self._loop.run_until_complete(self._loop.create_datagram_endpoint(
                lambda: QuicServer(configuration=configuration, create_protocol=Protocol), sock=self._udp))
            started.set()
            try:
                self._loop.run_forever()
            finally:
                # ⛔ THE LOOP IS CLOSED HERE, ON ITS OWN THREAD, after the QUIC server. A loop that is only stopped is closed later by
                # BaseEventLoop.__del__ on whatever thread the garbage collector runs in, and on Windows the proactor's close then
                # waits for the UDP receive still pending on it, for ever: the HTTP/3 e2e hung inside a later test's goto or
                # new_page, the test thread in base_events.__del__ -> proactor close -> _poll (Windows hunt of 13/11, run
                # 43377799126, 3 runs of 15; PR #486 part 1 twice).
                self._quic.close()
                self._loop.run_until_complete(asyncio.sleep(0.05))
                self._loop.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        if not started.wait(10):
            raise RuntimeError("the QUIC server did not start")

    @property
    def url(self) -> str:
        return "https://%s:%d/" % (self.address, self.port)

    def note(self, kind: str, **fields) -> None:
        """One line of the server's record: ``m`` is ``time.perf_counter()`` of this process, ``t`` the wall clock. Never raises."""
        try:
            self.record.append({"m": round(time.perf_counter(), 6), "t": round(time.time(), 4), "kind": kind, **fields})
        except Exception:
            pass

    def write_record(self, path) -> None:
        """The record as JSON lines (what the failure evidence of a test keeps)."""
        with open(path, "w", encoding="utf-8") as out:
            for line in list(self.record):
                out.write(json.dumps(line, default=str) + "\n")

    def close(self) -> None:
        self._quic_first.release_all()
        self.note("server_close", quic_connections=len(self.quic_peers), tcp_connections=len(self.tcp_alpn))
        self._tcp.shutdown()
        self._tcp.server_close()
        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)
        self._dir.cleanup()

    def __enter__(self) -> "H3Server":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
