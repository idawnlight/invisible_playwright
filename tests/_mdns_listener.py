"""A passive listener on the mDNS group (224.0.0.251, UDP 5353): what the lab heard of the engine's responder, packet by packet.

Failure evidence of ``tests/test_declared_host_network_e2e.py``. The engine's responder (dom/media/webrtc/transport/mdns_service/src/lib.rs) is
not heard in a release build: its ``trace!`` lines are compiled out (``release_max_level_info``), so the lab listens to the group itself.

IT ONLY LISTENS. It never sends a packet, never answers, never sets a multicast interface or the loop flag (those are the sender's), and it never raises:
every refusal (a bind, a join, a read, a parse) is written into its own record and the test goes on unchanged.

HOW IT BINDS, against the engine's own socket (lib.rs:124-135): the engine makes a UDP socket with SO_REUSEADDR, SO_REUSEPORT where the system has it
(not on Windows) and binds 0.0.0.0:5353. Both sockets here set the same options, so two sockets share the port and each receives every datagram sent
to the group, and the listener takes nothing from the engine:

* Linux: SO_REUSEADDR and SO_REUSEPORT, bound to the group address 224.0.0.251:5353. A socket bound to a multicast address receives only datagrams
  addressed to that group, never a unicast one, so it cannot take a datagram that was meant for the engine's wildcard socket.
* Windows: SO_REUSEADDR, bound to 0.0.0.0:5353 (a multicast address cannot be bound there). The listener is started BEFORE the browser is launched, so the
  engine's socket is the later one: Windows gives a unicast datagram to the socket bound last, and the engine's responder sends and answers on the group
  address only (lib.rs: ``send_to(&buf, mdns_addr)``).

It joins the group on the interfaces the engine joins on: ``mdns_service_start`` is given ``nr_stealth_mdns_interfaces()``, which is ``"127.0.0.1;"``
(dom/media/webrtc/transport/nr_stealth_network.cpp:229), and the engine joins each address it is given (lib.rs:138-145) and sends through the first
(lib.rs:150-153). So the listener joins on 127.0.0.1 and nothing else.

The record (``write``): JSON lines, the first line ``{"kind": "listener", ...}`` (how the bind and the join went, and the packet counts), then one
line per packet: ``m`` (``time.perf_counter()`` of the test process, the scale of the page timeline once joined by the launch's ``started_m``), ``t`` (wall
clock), ``src`` ([address, port]), ``bytes``, ``kind`` (``query``, ``response`` or ``unparsed``), ``id``, ``questions`` ([{name, type, unicast_bit}]) and
``answers`` ([{name, type, ttl, data}]) as read by a minimal DNS parse.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import struct
import sys
import threading
import time

GROUP = "224.0.0.251"
#: The engine's group, and the one interface it joins on (nr_stealth_mdns_interfaces).
MDNS_ADDRESS = (GROUP, 5353)
JOIN_INTERFACE = "127.0.0.1"

_TYPES = {1: "A", 12: "PTR", 16: "TXT", 28: "AAAA", 33: "SRV", 255: "ANY"}


def _name(data: bytes, offset: int, depth: int = 0):
    """The domain name at ``offset`` (compression followed) and the offset after it in the record the name started in."""
    labels, end, jumped = [], None, False
    while True:
        if offset >= len(data) or depth > 20:
            raise ValueError("name out of range")
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if end is None:
                end = offset + 2
            offset, jumped, depth = pointer, True, depth + 1
            continue
        labels.append(data[offset + 1: offset + 1 + length].decode("utf-8", "replace"))
        offset += 1 + length
    return ".".join(labels), (end if jumped else offset)


def parse(data: bytes) -> dict:
    """A DNS message, minimally: its id, whether it is a query or a response, the questions and the answers."""
    identifier, flags, qd, an, ns, ar = struct.unpack("!6H", data[:12])
    out = {"id": identifier, "kind": "response" if flags & 0x8000 else "query", "counts": [qd, an, ns, ar], "questions": [], "answers": []}
    offset = 12
    for _ in range(qd):
        name, offset = _name(data, offset)
        qtype, qclass = struct.unpack("!HH", data[offset: offset + 4])
        offset += 4
        out["questions"].append({"name": name, "type": _TYPES.get(qtype, qtype), "unicast_bit": bool(qclass & 0x8000)})
    for _ in range(an + ns + ar):
        name, offset = _name(data, offset)
        rtype, _rclass, ttl, length = struct.unpack("!HHIH", data[offset: offset + 10])
        offset += 10
        raw = data[offset: offset + length]
        if rtype == 1 and length == 4:
            value = str(ipaddress.IPv4Address(raw))
        elif rtype == 28 and length == 16:
            value = str(ipaddress.IPv6Address(raw))
        else:
            value = raw.hex()
        out["answers"].append({"name": name, "type": _TYPES.get(rtype, rtype), "ttl": ttl, "data": value})
        offset += length
    return out


class MdnsListener:
    def __init__(self):
        self.packets = []
        #: What happened to the listener itself: the bind, the join, a read or a parse that failed. Never raised.
        self.status = []
        self._socket = None
        self._thread = None
        self._stop = threading.Event()

    def start(self) -> "MdnsListener":
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if sys.platform != "win32" and hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind((GROUP if sys.platform != "win32" else "0.0.0.0", MDNS_ADDRESS[1]))
            self.status.append({"what": "bound", "address": sock.getsockname()[0], "port": sock.getsockname()[1]})
        except Exception as exc:
            self.status.append({"what": "bind failed: the listener hears nothing", "error": repr(exc)})
            return self
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(GROUP) + socket.inet_aton(JOIN_INTERFACE))
            self.status.append({"what": "joined", "group": GROUP, "interface": JOIN_INTERFACE})
        except Exception as exc:
            self.status.append({"what": "join failed", "group": GROUP, "interface": JOIN_INTERFACE, "error": repr(exc)})
        try:
            sock.settimeout(0.2)
            self._socket = sock
            self._thread = threading.Thread(target=self._run, name="mdns-listener", daemon=True)
            self._thread.start()
        except Exception as exc:
            self.status.append({"what": "no reader thread", "error": repr(exc)})
        return self

    def _run(self) -> None:
        sock = self._socket
        while not self._stop.is_set():
            try:
                data, source = sock.recvfrom(9000)
            except socket.timeout:
                continue
            except Exception as exc:
                if not self._stop.is_set():
                    self.status.append({"what": "read failed: the listener stops", "error": repr(exc)})
                return
            record = {"m": round(time.perf_counter(), 6), "t": round(time.time(), 4), "src": [source[0], source[1]], "bytes": len(data)}
            try:
                record.update(parse(data))
            except Exception as exc:
                record.update(kind="unparsed", error=repr(exc), hex=data[:64].hex())
            self.packets.append(record)

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._socket is not None:
                self._socket.close()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join()

    def summary(self) -> dict:
        kinds = {}
        for packet in list(self.packets):
            kinds[packet["kind"]] = kinds.get(packet["kind"], 0) + 1
        return {"packets": len(self.packets), "by_kind": kinds, "status": list(self.status)}

    def write(self, path) -> None:
        with open(path, "w", encoding="utf-8") as out:
            out.write(json.dumps(dict(kind="listener", group=GROUP, join_interface=JOIN_INTERFACE, **self.summary()), default=str) + "\n")
            for packet in list(self.packets):
                out.write(json.dumps(packet, default=str) + "\n")
