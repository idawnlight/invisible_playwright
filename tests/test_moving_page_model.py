"""Where the frame goes after a `goto` of a page that moves is raised: the model of `_moving_page._goto`, on a fake page (no browser).

Playwright raises "is interrupted by another navigation to X" as soon as a DIFFERENT navigation commits (the `location.replace` of the page
of the round before, a process switch already on its way), and NS_BINDING_ABORTED when that move stops ours. Our own navigation can still go
on, commit and move to its own landing after that (run 44688029161: the frame "navigated to .../landed?i=x50-4" after `goto` of x50-4 had
raised for x-3). So after a raise the frame may go with the page before or with ours, and the next round may be interrupted by the landing of
either. Known-bad inputs: that interleaving (red on a model that picks one page), an error text that is not one of the two answers, an
"interrupted" naming a page that cannot be moving, a `goto` that never answers. The right answers stay green.
"""
from __future__ import annotations

import pytest

import _moving_page as mp
from invisible_playwright.sync_api import Error, TimeoutError as PlaywrightTimeout

pytestmark = pytest.mark.unit

SITE = "http://127.0.0.1:64063"
LANDED_SITE = "http://localhost:64063"


def sent(i, path="x50"):
    return "%s/%s?i=%s-%d" % (SITE, path, path, i)


def landed(i, path="x50"):
    return "%s/landed?i=%s-%d" % (LANDED_SITE, path, i)


def interrupted(i, naming, path="x50"):
    return ('Navigation to "%s" is interrupted by another navigation to "%s"' % (sent(i, path), landed(naming, path)))


def aborted(i):
    return 'page.goto: NS_BINDING_ABORTED; navigating to "%s"' % sent(i)


class FakePage:
    """The part of a page `_goto` uses. Each `goto` takes the next step of the script: the error it raises (or None when it answers) and the
    url the frame has at that instant; `later` are the urls the frame goes to afterwards, one at each `land()` or each wait that needs it."""

    def __init__(self):
        self.url = "about:blank"
        self.script = []
        self.later = []
        self.asked = []

    def step(self, error=None, now=None, later=()):
        self.script.append((error, now, list(later)))
        return self

    def goto(self, url, **kwargs):
        self.asked.append(url)
        error, now, later = self.script.pop(0)
        self.url = now if now is not None else url
        self.later = later
        if error is not None:
            raise Error(error)

    def land(self):
        self.url = self.later.pop(0)

    def wait_for_url(self, predicate, timeout=None):
        while not predicate(self.url):
            if not self.later:
                raise PlaywrightTimeout("Timeout %sms exceeded." % timeout)
            self.land()


@pytest.fixture(autouse=True)
def _a_clean_ledger():
    mp._SENT.clear()
    yield
    mp._SENT.clear()


def go(page, i, *, before_may_move=True, ours_may_stop=False, path="x50"):
    return mp._goto(page, sent(i, path), the_page_before_may_move=before_may_move, our_page_may_stop_ours=ours_may_stop,
                    wait_until="domcontentloaded", timeout=30_000)


def test_a_goto_that_answers_is_green():
    page = FakePage().step().step()
    go(page, 3)
    go(page, 4)
    assert page.asked == [sent(3), sent(4)]


def test_a_goto_interrupted_by_the_page_before_is_green():
    page = FakePage().step().step(interrupted(4, 3), now=landed(3))
    go(page, 3)
    go(page, 4)


def test_a_goto_stopped_by_the_page_before_is_green():
    page = FakePage().step().step(aborted(4), now=landed(3))
    go(page, 3)
    go(page, 4)


def test_a_goto_stopped_on_an_older_engine_may_land_on_our_own_page():
    page = FakePage().step(aborted(0), now=sent(0), later=[landed(0)])
    go(page, 0, before_may_move=False, ours_may_stop=True)
    assert page.url == landed(0)


def test_the_frame_that_goes_on_to_our_own_page_after_an_interrupted_goto_does_not_fail_the_next_round():
    """The interleaving of run 44688029161: `goto(x-4)` raises for the landing of x-3, then the frame lands on x-4. The next round is
    interrupted by x-4's landing, and that is a right answer: ours was the page moving."""
    page = (FakePage().step()
            .step(interrupted(4, 3), now=landed(3), later=[landed(4)])
            .step(interrupted(5, 4), now=landed(4)))
    go(page, 3)
    go(page, 4)
    page.land()
    assert page.url == landed(4)
    go(page, 5)


def test_the_frame_that_goes_on_to_our_own_page_after_a_stopped_goto_does_not_fail_the_next_round():
    page = (FakePage().step()
            .step(aborted(4), now=landed(3), later=[landed(4)])
            .step(interrupted(5, 4), now=landed(4)))
    go(page, 3)
    go(page, 4)
    page.land()
    go(page, 5)


def test_a_stopped_goto_waits_for_the_landing_of_any_page_that_may_be_moving():
    """After an interrupted round the frame may go with the page of round 3 or with ours (round 4): the wait of the next stopped `goto` ends
    on either landing, and here ours comes."""
    page = (FakePage().step()
            .step(interrupted(4, 3), now=landed(3), later=[landed(4)])
            .step(aborted(5), now=sent(5), later=[landed(4)]))
    go(page, 3)
    go(page, 4)
    go(page, 5)
    assert page.url == landed(4)


def test_a_stopped_goto_whose_frame_never_lands_is_red():
    page = FakePage().step().step(aborted(4), now=sent(4))
    go(page, 3)
    with pytest.raises(PlaywrightTimeout):
        go(page, 4)


@pytest.mark.parametrize("text", [
    "Execution context was destroyed, most likely because of a navigation",
    "Failed to find execution context",
    "net::ERR_CONNECTION_REFUSED",
])
def test_an_error_that_is_neither_answer_is_red(text):
    page = FakePage().step().step(text)
    go(page, 3)
    with pytest.raises(AssertionError):
        go(page, 4)


def test_a_goto_that_never_answers_is_red_with_its_own_timeout():
    page = FakePage().step().step('Timeout 30000ms exceeded.\nnavigating to "%s", waiting until "domcontentloaded"' % sent(4))
    go(page, 3)
    with pytest.raises(AssertionError):
        go(page, 4)


@pytest.mark.parametrize("naming", [0, 1, 2, 5, 7])
def test_an_interrupted_goto_naming_a_page_that_cannot_be_moving_is_red(naming):
    page = FakePage().step().step(interrupted(4, naming))
    go(page, 3)
    with pytest.raises(AssertionError):
        go(page, 4)


def test_an_interrupted_goto_naming_a_page_that_cannot_be_moving_is_red_after_an_interrupted_round_too():
    page = (FakePage().step()
            .step(interrupted(4, 3), now=landed(3), later=[landed(4)])
            .step(interrupted(5, 1), now=landed(4)))
    go(page, 3)
    go(page, 4)
    page.land()
    with pytest.raises(AssertionError):
        go(page, 5)


def test_an_interrupted_goto_is_red_when_no_page_before_may_move():
    page = FakePage().step(interrupted(0, 9), now=landed(9))
    with pytest.raises(AssertionError):
        go(page, 0, before_may_move=False)


def test_a_stopped_goto_is_red_when_no_page_may_stop_it():
    page = FakePage().step(aborted(0), now=landed(0))
    with pytest.raises(AssertionError):
        go(page, 0, before_may_move=False, ours_may_stop=False)


def test_a_goto_says_which_pages_the_frame_may_go_with():
    page = (FakePage().step()
            .step(interrupted(4, 3), now=landed(3), later=[landed(4)])
            .step())
    assert go(page, 3) == {sent(3)}
    assert go(page, 4) == {sent(3), sent(4)}
    assert go(page, 5) == {sent(5)}


def test_a_page_forgotten_has_no_page_that_may_be_moving():
    """The ledger is by `id`, which a new page may reuse: the fixture forgets a page when its test ends."""
    page = FakePage().step().step(interrupted(4, 3), now=landed(3))
    go(page, 3)
    mp.forget(page)
    with pytest.raises(AssertionError):
        go(page, 4)
