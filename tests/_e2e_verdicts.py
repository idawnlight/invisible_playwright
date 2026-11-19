"""Verdicts the e2e tests give from what they recorded, as pure functions of it (so a unit test can feed them the red
case and the green case without a browser).

A verdict here never reads a clock of the machine it runs on: it compares what happened to what happened, or to a
control measured in the same launch.
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple


def cold_association_problem(associations: Sequence[Sequence[Optional[float]]],
                             via: Iterable[Tuple[str, int, int, float]], host: str, born: float) -> Optional[str]:
    """What is wrong with the way the first STUN datagram of a PeerConnection left, or None.

    ``associations`` are the UDP associations the gateway granted, ``[start, end]`` in seconds from its start (``end``
    None while unanswered; the first granted one is the core's own probe, made before the browser exists, and is no
    association of the engine); ``via`` are the datagrams the relay carried, ``(host, port, association index, time)``;
    ``born`` is the birth of the PeerConnection on the gateway's clock.

    The promise is that the first datagram leaves on an association that was ready before the PeerConnection was
    born, not that it leaves within some number of milliseconds: a cold association is made AFTER the birth, so the
    datagram that leaves on one is the defect, whatever the speed of the machine."""
    granted = [i for i, a in enumerate(associations) if a[1] is not None][1:]
    if not granted:
        return "the engine never asked the proxy for an association (only the core's probe did)"
    ready = [i for i in granted if associations[i][1] < born]
    if not ready:
        return ("no association was completed before the PeerConnection was born (granted at %s s from the gateway's "
                "start, the PeerConnection at %.3f s): the first ICE socket made its own, from scratch"
                % ([round(associations[i][1], 3) for i in granted], born))
    sent = sorted((t, index) for h, _port, index, t in via if h == host)
    if not sent:
        return "no STUN datagram of the PeerConnection reached the relay"
    at, index = sent[0]
    if index not in ready:
        return ("the first STUN datagram (at %.3f s) left on association #%d, granted at %s s, after the PeerConnection "
                "was born at %.3f s: a cold association, made from scratch (ready before the birth: %s)"
                % (at, index, associations[index][1] and round(associations[index][1], 3), born, ready))
    return None


# ── the road of a TURN server written as a name ────────────────────────────────────────────────────────────────────────
#
# Two promises, and neither is a number of seconds. The first is CAUSAL: the test holds the handshakes that were begun after the
# pool of associations was ready (the gateway answers nothing on them, whatever the speed of the machine) and the allocation has to
# be answered all the same, while they are held; a handshake that waits for another leaves it waiting until the test lets them go.
# The second is a COUNT: the exchanges the gateway, the DoH and the TURN server recorded from the birth of the PeerConnection to
# the Allocate answered with credentials are no more than the protocols ask for. A slow machine makes each of them later, never more.

#: The exchanges of a SOCKS5 control connection (RFC 1928): the method negotiation (section 3) and the request with its reply
#: (sections 4 and 6). With a user and a password (RFC 1929, section 2) the sub-negotiation is one more.
SOCKS_EXCHANGES = 2
SOCKS_AUTH_EXCHANGES = 1
#: A name asked of the DoH by a resolver that was told the family it wants (`nriceresolver.cpp` passes RESOLVE_DISABLE_IPV6 for
#: AF_INET, and a SOCKS5 UDP relay carries IPv4 here): ONE question, the A record, answered in one HTTP exchange (RFC 8484, section 4).
DOH_QUESTIONS = 1
#: The Allocate of a TURN client (RFC 8656, section 7.1, with the long-term credential mechanism of RFC 8489, section 9.2): the first
#: request carries no credentials and is answered 401 with a REALM and a NONCE, the second carries them and is answered with the
#: relayed address. A retransmission of one is the same transaction (same transaction id), so it is not counted twice.
ALLOCATE_TRANSACTIONS = 2
#: The control connection of the road after the birth that carries the question of the TURN name: ONE, the CONNECT to the DoH, and none
#: when the connection that loaded the page is still open and carries it. A connection made again for a question that comes after
#: the first one, or a second one for the same name, is a handshake nobody needs. Other connections of the browser (the page's own
#: server, the DoH connections it opens in parallel for its own names) are not on this road: they are shown, they decide nothing.
CONNECTS_AFTER_THE_BIRTH = 1


def turn_road_connections(born: float, allocated_at: float, requests: Iterable[Tuple[float, int, str, str]],
                          doh_questions: Sequence[Tuple[str, int, Optional[int]]]) -> Tuple[List[Tuple[float, int, str, str]],
                                                                                           List[Tuple[float, int, str, str]]]:
    """The CONNECTs of ``requests`` after the birth, split in (the ones that carried a question of the TURN name, the others).

    ``doh_questions`` are the (name, type, connection) of ``FakeDoh.question_connections`` for the TURN name: ``connection`` is the
    index of the control connection of the gateway that carried the CONNECT to the DoH, the same number as the second field of
    ``Gateway.requests``. The match is by that number, exact: a connection that carried no question of the name is not on the road."""
    carried = {q[2] for q in doh_questions if q[2] is not None}
    after = [r for r in requests if r[2] == "connect" and born <= r[0] <= allocated_at]
    return [r for r in after if r[1] in carried], [r for r in after if r[1] not in carried]


def turn_exchanges_most(auth: bool = False) -> int:
    """The most exchanges the road may cost (see :func:`turn_exchanges_problem`): five with no user and password, six with them."""
    per_connection = SOCKS_EXCHANGES + (SOCKS_AUTH_EXCHANGES if auth else 0)
    return CONNECTS_AFTER_THE_BIRTH * per_connection + DOH_QUESTIONS + ALLOCATE_TRANSACTIONS


def turn_exchanges(born: float, allocated_at: float, requests: Iterable[Tuple[float, int, str, str]],
                   doh_questions: Sequence[Tuple[str, int, Optional[int]]], allocates: Sequence[Tuple[float, bytes, int, bool]],
                   auth: bool = False) -> Tuple[int, List[str]]:
    """The exchanges of the road from the birth to the Allocate answered with credentials, as (their number, what is more than the
    protocols ask for, in words). The control connection that carried the question of the TURN name costs its greeting and its
    CONNECT (and the authentication); every question costs one; every distinct Allocate transaction one."""
    road, _others = turn_road_connections(born, allocated_at, requests, doh_questions)
    transactions = [t for t in allocates if t[0] <= allocated_at]
    per_connection = SOCKS_EXCHANGES + (SOCKS_AUTH_EXCHANGES if auth else 0)
    found = []
    if len(road) > CONNECTS_AFTER_THE_BIRTH:
        found.append("%d SOCKS5 control connections made after the birth carried a question of the TURN name (%s), the design needs "
                     "%d at most" % (len(road), [r[3] for r in road], CONNECTS_AFTER_THE_BIRTH))
    if len(doh_questions) > DOH_QUESTIONS:
        found.append("%d DoH questions for the name (%s), a resolver told its family asks %d"
                     % (len(doh_questions), [(q[0], q[1]) for q in doh_questions], DOH_QUESTIONS))
    if len(transactions) > ALLOCATE_TRANSACTIONS:
        found.append("%d Allocate transactions (%s), the long-term credential asks %d: the one without credentials, answered 401, and "
                     "the one with them" % (len(transactions), [("credentials" if t[3] else "no credentials") for t in transactions],
                                            ALLOCATE_TRANSACTIONS))
    return len(road) * per_connection + len(doh_questions) + len(transactions), found


def turn_exchanges_problem(born: float, allocated_at: float, requests: Iterable[Tuple[float, int, str, str]],
                           doh_questions: Sequence[Tuple[str, int, Optional[int]]],
                           allocates: Sequence[Tuple[float, bytes, int, bool]], auth: bool = False) -> Optional[str]:
    """What is wrong with the NUMBER of exchanges of the road, from the birth of the PeerConnection (``born``) to the Allocate answered
    with credentials (``allocated_at``), or None. The times are seconds on the gateway's clock and only ORDER the records: nothing
    here compares one with a duration, so a machine whose every stamp is later gives the same count.

    ``requests`` is ``Gateway.requests``; ``doh_questions`` the (name, type, connection) of every question the DoH heard for the TURN
    name (nothing else asks it, so the question is the PeerConnection's); ``allocates`` the transactions of
    ``TurnServer.transactions()``.

    The most the road may cost: the one SOCKS5 control connection that carried the question, with its CONNECT to the DoH
    (``SOCKS_EXCHANGES``, one more with a user and a password), the one DoH question, the two transactions of the Allocate. What the
    browser does on other connections (its page, the DoH for other names, in parallel) is not on the road: it is listed in the message
    as evidence and decides nothing. An ASSOCIATE is no exchange of this road either: the association that carries the Allocate is
    made before the birth (``cold_association_problem`` judges it, and the held handshakes of ``held_handshake_problem`` prove that
    nothing waits for one made after)."""
    requests = list(requests)
    seen, found = turn_exchanges(born, allocated_at, requests, doh_questions, allocates, auth)
    most = turn_exchanges_most(auth)
    if not found and seen <= most:
        return None
    _road, others = turn_road_connections(born, allocated_at, requests, doh_questions)
    return ("the road made %d exchanges from the birth to the Allocate answered with credentials, the protocols ask for %d at most "
            "(%d per SOCKS5 control connection that carried the question x %d, %d DoH question, %d Allocate transactions): %s; other "
            "CONNECTs after the birth, not counted: %s"
            % (seen, most, SOCKS_EXCHANGES + (SOCKS_AUTH_EXCHANGES if auth else 0), CONNECTS_AFTER_THE_BIRTH, DOH_QUESTIONS,
               ALLOCATE_TRANSACTIONS, "; ".join(found) or "more than that in all", [r[3] for r in others] or "none"))


def held_handshake_problem(allocated_at: Optional[float], released_at: float, held: Sequence[float]) -> Optional[str]:
    """What is wrong with the way the road behaved while the handshakes begun after the pool was ready were HELD, or None.

    ``held`` are the starts (seconds on the gateway's clock) of the associations whose answer the test held; ``released_at`` is the
    instant the test let them go (once the page had posted its srflx, or at its 30 s deadline); ``allocated_at`` is the moment the
    TURN server answered the Allocate that carried credentials (None: it never did).

    The promise is an ORDER, read off two stamps of the same clock: the allocation was answered BEFORE the held handshakes were let
    go. A road whose Allocate needs no handshake made after the birth, and whose DoH connection does not wait behind one, does not
    notice them; one whose handshakes are made one after the other on the same thread (or whose Allocate leaves on an association
    made from scratch) is left waiting for the test, whatever the speed of the machine."""
    shown = [round(t, 3) for t in held]
    if allocated_at is None:
        return ("the TURN server never answered an Allocate that carried credentials, with %d association handshake(s) held after the "
                "pool was ready (begun at %s s): something of the road waits for a handshake" % (len(held), shown))
    if allocated_at > released_at:
        return ("the Allocate was answered at %.3f s, after the %d held association handshake(s) (begun at %s s) were let go at "
                "%.3f s: the road waited for a handshake it does not need, one handshake waits for another"
                % (allocated_at, len(held), shown, released_at))
    return None


def turn_chain_problem(born: float, allocated_at: Optional[float], released_at: float, held: Sequence[float],
                       requests: Iterable[Tuple[float, int, str, str]], doh_questions: Sequence[Tuple[str, int, Optional[int]]],
                       allocates: Sequence[Tuple[float, bytes, int, bool]],
                       associations: Sequence[Sequence[Optional[float]]], via: Iterable[Tuple[str, int, int, float]], host: str,
                       auth: bool = False) -> Optional[str]:
    """What is wrong with the road of a TURN server written as a name, or None: the allocation was answered while the later handshakes
    were held (:func:`held_handshake_problem`), it cost no exchange more than the protocols ask for (:func:`turn_exchanges_problem`),
    and the Allocate left on an association that was ready before the birth (:func:`cold_association_problem`). ``host`` is the
    address of the TURN server: the datagrams of the allocation are the ones judged. Nothing here reads a duration."""
    problem = held_handshake_problem(allocated_at, released_at, held)
    if problem is not None or allocated_at is None:
        return problem
    return (turn_exchanges_problem(born, allocated_at, requests, doh_questions, allocates, auth)
            or cold_association_problem(associations, via, host, born))


#: The first retry of the mDNS responder: a name that does not come back opens a connection only after it.
MDNS_RETRY_MS = 3000
#: A page whose own loop did not run for this long (milliseconds) while it waited was stalled by the machine, not by the product.
STALL_MS = 1000


def opened_at_the_retry_problem(opened_ms: float, stalled_ms: float, retry_ms: float = MDNS_RETRY_MS, stall_ms: float = STALL_MS) -> Optional[str]:
    """What is wrong with the time a connection took to open, or None. The promise is that it opens before the responder's retry (a name
    that did not come back is the slow road), and a clock cannot say it alone: a machine that stops for seconds opens late on the best of roads.
    ``stalled_ms`` is the longest time the page's own loop did not run while it waited, read in the same run; a page that was stopped for
    ``stall_ms`` or more says nothing of the road, so no time is judged then."""
    if opened_ms < retry_ms or stalled_ms >= stall_ms:
        return None
    return "opened after %d ms, at the responder's retry (%d ms), and the page's own loop was never stopped for %d ms (longest %d ms)" % (
        opened_ms, retry_ms, stall_ms, stalled_ms)
