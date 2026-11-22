"""The `goto` of the tests of pages that move on their own (`test_a_page_that_moves_on_e2e.py`), and the model of where the frame goes."""
from __future__ import annotations


def _landing(url: str) -> str:
    """Where the page of `url` sends itself: /landed with the query of its round, which no other round of the module has."""
    return "/landed" + url[url.index("?"):]


#: The pages (by address) the frame may still go with, for each page of the browser (by `id`): the page sent last when `goto` answered, and, from a
#: `goto` that raised, every page it may have gone with (`_goto`). `forget` takes a page out when its test ends: an `id` is reused.
_SENT: dict = {}


#: An optional hook for the evidence a failing test leaves: called as ``note(what, **fields)`` where `_goto` sends, hears the answer and hears the error.
#: The test module sets it; it only listens and changes no wait or assertion.
note = None


def forget(page) -> None:
    _SENT.pop(id(page), None)


def _goto(page, url, *, the_page_before_may_move, our_page_may_stop_ours, **kwargs) -> frozenset:
    """`page.goto`, and the pages the frame may go with: ``{url}`` when `goto` answers.

    - ``the_page_before_may_move``: the pages sent before ours may still be moving (the rounds of the read test go on
      while they do, module docstring). A `location.replace` of one of them may win: `goto` says ours was interrupted by it, or
      stopped (NS_BINDING_ABORTED), and the frame lands on the landing of THAT page.
    - ``our_page_may_stop_ours``: an engine before firefox-68, where `goto` may say NS_BINDING_ABORTED for our own
      document, which had committed and then moved
      (`test_networkidle_is_reached_only_on_the_page_that_replaced_a_document_still_arriving`); the frame then lands on
      our landing.

    When `goto` raises, the frame may still go with EITHER page: Playwright raises "interrupted" as soon as a different navigation commits,
    while ours goes on, commits and moves to its own landing (run 44688029161: x50-4 raised for the landing of x50-3, and the frame then
    navigated to the landing of x50-4). Nothing here knows which one it is, so the pages that may be moving are all of the pages before and
    ours, and the next round accepts the landing of any of them. Stock Playwright behaves the same: the model was the test's.

    Any other answer is red: an error that is neither answer, an "interrupted" that names a page that cannot be moving, and a navigation that
    never answers. The detector of that last one is the timeout of the `goto` itself (each caller passes one): a bound of the test stricter
    than it only judged the speed of the machine."""
    from invisible_playwright.sync_api import Error

    before = _SENT.get(id(page), frozenset()) if the_page_before_may_move else frozenset()
    if note:
        note("goto", url=url, before=sorted(before))
    try:
        page.goto(url, **kwargs)
        if note:
            note("goto-answered", url=url)
    except Error as e:
        text = str(e)
        if note:
            note("goto-raised", url=url, error=text[:300])
        if "NS_BINDING_ABORTED" in text:
            ends = tuple(_landing(u) for u in sorted(before | ({url} if our_page_may_stop_ours else frozenset())))
            assert ends, e
            page.wait_for_url(lambda u: u.endswith(ends), timeout=30_000)
        else:
            assert before, e
            assert "is interrupted by another navigation to" in text, e
            named = text.split("another navigation to")[1]
            assert any(_landing(u) in named for u in before), (e, sorted(before))
        possible = before | {url}
    else:
        possible = frozenset({url})
    _SENT[id(page)] = possible
    return possible
