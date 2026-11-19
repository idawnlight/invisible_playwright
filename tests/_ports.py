"""One port number that TCP and UDP both bind on an address, found the way the witness finds it (tests/_witness.py), and said when it is not found.

Windows hands out ephemeral TCP ports in sequence and keeps excluded ranges of a hundred ports for UDP in the same zone, so asking the system for
fifty TCP ports in a row can give fifty consecutive numbers inside one range UDP may not bind: the first try is the system's own pick, the others
are drawn at random below the ephemeral range (run 43230437094, wrapper.e2e.web on Windows: ``H3Server`` found "no port free for both TCP and UDP"
three times, and the refusals it had swallowed were never read). Every refusal is kept, with its code, and the last ones are in the message.
"""
from __future__ import annotations

import random
import socket
import subprocess
from typing import Any, Callable, List, Tuple

TRIES = 200
#: Below Linux's ephemeral range (32768): a number drawn here is not the next of a sequence the system hands out.
DRAW_FROM = (20000, 32767)


def who_holds(port: int) -> str:
    """What the machine says about the sockets of `port` (ss, or netstat), for the message of a picker or a trap that found no number."""
    for command in (["ss", "-H", "-lntup"], ["netstat", "-lntup"]):
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        lines = [line for line in done.stdout.splitlines() if (":%d " % port) in line]
        return "; held by: " + (" | ".join(lines) if lines else "nothing listed by %s" % command[0])
    return "; no ss or netstat to say who holds it"


def candidates(tries: int = TRIES, draw: Any = None):
    """The port numbers to try, in order: 0 (the system's own pick) first, then numbers drawn at random below the ephemeral range. The ONE
    sequence of the harness for "the same number on TCP and UDP": the HTTP/3 server, the witness and the resolver trap all walk it."""
    draw = draw or random.Random()
    for attempt in range(tries):
        yield 0 if attempt == 0 else draw.randint(*DRAW_FROM)


def bind_tcp_and_hold_udp(address: str, make_tcp: Callable[[str, int], Any], tries: int = TRIES, socket_module: Any = None,
                          draw: Any = None) -> Tuple[Any, int, Any]:
    """``make_tcp(address, port)`` makes the TCP server (it binds, with port 0 for the system's pick; it has ``server_address`` and
    ``server_close``). Returns ``(that server, its port, the UDP socket bound to the same number)`` once UDP binds that number too. Raises
    RuntimeError saying how many tries were made and the last refusals, codes included.

    The caller owns the UDP socket and closes it, or hands it to the loop that serves it (``create_datagram_endpoint(sock=...)``): from the first
    look at the number to the use of it nobody else can bind it, on TCP or on UDP. (A picker that closed its UDP probe and left "whoever needs the
    socket binds it next" held the number on TCP only: another picker of the harness could not take it, but any other bind of that UDP number
    could, the system's own pick of an ephemeral port for another process first among them.)"""
    return _bind_both(address, make_tcp, tries, socket_module, draw)


def _bind_both(address: str, make_tcp: Callable[[str, int], Any], tries: int, socket_module: Any, draw: Any) -> Tuple[Any, int, Any]:
    sock = socket_module or socket
    refusals: List[str] = []
    last = 0
    for wanted in candidates(tries, draw):
        tcp = None
        try:
            tcp = make_tcp(address, wanted)
            port = tcp.server_address[1]
            probe = sock.socket(sock.AF_INET, sock.SOCK_DGRAM)
            try:
                probe.bind((address, port))
            except BaseException:
                probe.close()
                raise
            return tcp, port, probe
        except OSError as exc:
            refusals.append("%s: %s" % (wanted or "system pick", exc))
            if tcp is not None:
                last = tcp.server_address[1]
                tcp.server_close()
            else:
                last = wanted
    raise RuntimeError("no port free for both TCP and UDP on %s, after %d tries (the last number tried: %s)%s; the last refusals: %s"
                       % (address, tries, last or "the system's pick", who_holds(last) if last else "", "; ".join(refusals[-3:])))
