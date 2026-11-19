"""The verdicts of ``test_a_second_goto_that_starts_while_the_first_changes_process_...`` (``test_navigation_across_processes_e2e.py``).

A first goto to b.test/partial changes process and has its response half there (the server holds the rest); a second goto to
c.test/second starts as soon as the client has heard that response. Either may win, as in stock Firefox; what is judged is how
each ended and where the page landed, read only once it has landed: when the first wins, the page commits /partial only after the
server has sent the rest of it, so the address read before that is still the page that was left (a.test), which is not a defect.

The outcomes the engine gives (the hunt on the candidate of firefox-65, 200 runs per OS, run 43790642333; Linux / Windows):

  * the second wins: the first is "interrupted by another navigation", the second commits, the page is at c.test/second (116 / 88);
  * the first wins: the first is "interrupted by another navigation" (the client ends a goto that waits when another one starts,
    even if its navigation goes on and wins), the second ends NS_BINDING_ABORTED, the page lands at b.test/partial (77 / 102);
  * the first wins and returns its commit (after the rest of /partial is sent), the second ends NS_BINDING_ABORTED, the page lands
    at b.test/partial (7 / 10).
"""
from __future__ import annotations

from typing import Iterable, Optional
from urllib.parse import urlsplit

#: What a goto that waits says when another navigation of its frame starts (the client's own end of it).
INTERRUPTED = "is interrupted by another navigation"
#: What a navigation replaced by another one ends with (the engine's).
ABORTED = "NS_BINDING_ABORTED"
#: What a goto that ended only at its deadline says: the defect, whoever won.
_TIMEOUT_WORDS = ("timeout", "exceeded", "not reached")

#: Where each goto lands when it wins: (host, path).
FIRST = ("b.test", "/partial")
SECOND = ("c.test", "/second")


def landed_on(url: str) -> Optional[str]:
    """``"first"`` when the page is at the address of the first goto, ``"second"`` at that of the second, ``None`` anywhere else
    (still at the page it left: nothing has landed yet)."""
    parts = urlsplit(url or "")
    where = (parts.hostname, parts.path)
    if where == FIRST:
        return "first"
    if where == SECOND:
        return "second"
    return None


def _told(error: Optional[str]) -> bool:
    return error is not None and (INTERRUPTED in error or ABORTED in error)


def second_goto_problem(first: Optional[str], second: Optional[str], url: str, asked: Iterable[str]) -> Optional[str]:
    """What is wrong with how the two gotos ended, or None.

    ``first`` and ``second`` are the errors the gotos raised (``None``: it returned its commit), ``url`` the address of the page once
    it has landed (or the deadline has passed), ``asked`` the paths the server was asked for.

    * The request of the second goto reached the server (it did not wait behind the held response).
    * Neither goto ended at its timeout.
    * The page landed on the address of one of them; still at the page it left is red.
    * The second won (c.test/second): it returned its commit (an abort of the replaced first one taken for its end is the defect),
      and the first ended with an end the engine or the client tells (interrupted, or NS_BINDING_ABORTED).
    * The first won (b.test/partial): the second ended with an end that is told (NS_BINDING_ABORTED, or interrupted), and the first
      returned its commit or was interrupted by the client; NS_BINDING_ABORTED for the goto whose navigation landed is the abort of
      the process it left taken for the end of its navigation, red.
    """
    detail = (first, second, url, sorted(asked))
    if "/second" not in set(asked):
        return "the request of the second goto never reached the server (it waited behind the held response): %r" % (detail,)
    for name, error in (("first", first), ("second", second)):
        text = (error or "").lower()
        if any(word in text for word in _TIMEOUT_WORDS):
            return "the %s goto ended only at its timeout: %r" % (name, detail)
    winner = landed_on(url)
    if winner is None:
        return ("the page landed on neither goto (it is still at %s after the rest of the first response was sent): %r"
                % (url, detail))
    if winner == "second":
        if second is not None:
            return "the page landed on the second goto, which did not return its commit but an error: %r" % (detail,)
        if not _told(first):
            return ("the page landed on the second goto and the first did not end interrupted or aborted: %r" % (detail,))
        return None
    if not _told(second):
        return ("the page landed on the first goto and the second did not end aborted or interrupted (it %s): %r"
                % ("returned a commit" if second is None else "ended otherwise", detail))
    if first is not None and INTERRUPTED not in first:
        return ("the page landed on the first goto, which ended with an error that is not the client's interruption: the abort of "
                "the process it left was taken for the end of its navigation: %r" % (detail,))
    return None
