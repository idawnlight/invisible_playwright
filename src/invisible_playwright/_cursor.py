"""Which pointer engine a session runs, and the one thing the client still says.

WHERE THE POINTER MOVES NOW
---------------------------
The walk of the pointer onto an element, the landing off its centre, the fidget
that fills an idle gap and the burst of detents of a scroll are drawn by the
SERVER (`_juggler/pointer.py`), for every client alike. They used to be this
module: it patched `Frame._click`, `Mouse.click`, `Mouse.wheel` ... on the
in-process client and sent N `mouseMove` before each action, and a client that
was not ours (Go, Node, stock Python) sent one `Frame.click` and the page saw
one `mousemove`. Nothing of that is left here: the in-process client sends the
plain action, as every other client does, and the server walks.

What stays here is what has to be decided BEFORE a browser exists, in the
caller's process:

* WHO draws the motion (`resolve_cursor_engine`): this package's Python
  generator, or nobody. The answer travels to the server in the launch prefs
  (`_session.build_prefs`), once.
* the motion budget `humanize=<seconds>` implies (`max_seconds_for`);
* the page's motion seed, derived from the session seed and the page's number
  (`page_motion_seed`) - one function for the server that draws from it and for
  the tests that replay it;
* the opt-out for the off-centre landing (`landing_enabled`);
* the generator's contract (`_motion_factory`), which the server reads here so
  that a stub put in `_motion_mod` replaces it for both;
* ONE wrapper on the client: an EXPLICIT `Mouse.move`. That is the caller's
  and a server leaves it alone (a Go client's `mouse.move` reaches the page as
  exactly the point it named), but this client has always walked its own, so it
  marks the message `humanise` and the server draws the stroke. The mark is
  sent only for a page whose session armed the Python engine
  (`enable_for`), so a page of any other session - or a server that is not
  ours - receives the plain move.

``INVPW_CURSOR_ENGINE`` picks who generates the motion: ``python`` (default) or
``off``.
"""
from __future__ import annotations

import os
import warnings
from typing import Any, Callable, Dict, Optional
from weakref import WeakKeyDictionary

# ``INVPW_CURSOR_ENGINE`` picks who generates the motion:
#   "python"  (default) - the server's pointer, seeded from the session seed
#   "off"                - no humanisation at all, same as humanize=False
ENGINE_ENV = "INVPW_CURSOR_ENGINE"

ENGINE_PYTHON = "python"
ENGINE_OFF = "off"

# Opt-out for the one behaviour that changes what the automation layer itself
# does rather than only what happens before it: landing off the centre of an
# element (`pointer.Pointer._choose_landing`).
LANDING_ENV = "INVPW_CURSOR_LANDING"

# Hard ceiling on how far a single approach may be stretched, so a pathological
# generator cannot hang a click. Mirrors the constructor's own default cap.
_DEFAULT_MAX_SECONDS = 1.5


# ── warnings ────────────────────────────────────────────────────────────────
#
# A broken install and a page that simply had no element must not look the
# same. Everything that indicates the former says so, exactly once per process
# (a warning per click would be its own kind of breakage), and everything that
# indicates the latter stays silent.

_warned: set = set()


def _warn_once(key: str, message: str, category: type = RuntimeWarning) -> None:
    if key in _warned:
        return
    _warned.add(key)
    warnings.warn("invisible_playwright: " + message, category, stacklevel=3)


def _reset_warnings() -> None:
    """Test hook: forget which warnings have already been emitted."""
    _warned.clear()


# ── the _motion contract ────────────────────────────────────────────────────
#
# ``_motion`` supplies the stroke generator. It must expose a per-session
# class:
#
#     CursorMotion(seed: int, *, style=None)
#
# and the instance must expose:
#
#     .path(from_x, from_y, to_x, to_y) -> sequence of waypoints
#         A waypoint is either an object with ``x`` / ``y`` / ``dt_ms``, or a
#         plain ``(x, y, delay_seconds)`` triple, or a bare ``(x, y)`` pair.
#         The delay is the pause taken BEFORE emitting that waypoint.
#     .start_point(width, height) -> (x, y)      [optional]
#         Where the cursor sits before the session's first movement. Optional
#         because it is policy, not geometry; without it the planner places
#         it, since a cursor that is at the top-left corner at the start of
#         every session is an invariant shared by every install.
#
# The core is pinned with `==`, so the generator is always there: the import is
# direct, and an install without it fails here, at import.
#
# The package that publishes the generator: `_motion_factory` reads it from here,
# and the tests put a stub module in its place.
from invisible_core import juggler as _motion_mod


def _motion_factory() -> Callable[..., Any]:
    """The callable that builds a per-page stroke generator."""
    return _motion_mod.CursorMotion


def resolve_cursor_engine(humanize: Any) -> str:
    """Decide who generates the motion for a session.

    ``humanize`` keeps exactly the meaning it has always had on the public
    constructor: falsy disables humanisation, ``True`` enables it with the
    default cap, a number enables it with that cap in seconds. This function
    only decides WHETHER the Python generator draws it or nobody does.

    ``INVPW_CURSOR_ENGINE`` is strict: unset or empty (the default), ``python``
    or ``off``. Any other value is a ``ValueError`` naming the valid ones, even
    when ``humanize`` is falsy: a value that is ignored is a setting that does
    nothing while the caller believes it does something.
    """
    choice = os.environ.get(ENGINE_ENV, "")
    if choice not in ("", ENGINE_PYTHON, ENGINE_OFF):
        raise ValueError(
            "invisible_playwright: %s=%r is not a cursor engine; the valid values "
            "are %r (the default, same as unset), %r and %r"
            % (ENGINE_ENV, choice, "", ENGINE_PYTHON, ENGINE_OFF))
    if not humanize or choice == ENGINE_OFF:
        return ENGINE_OFF
    return ENGINE_PYTHON


def max_seconds_for(humanize: Any) -> float:
    """The motion-duration cap implied by ``humanize=``."""
    if humanize is True:
        return _DEFAULT_MAX_SECONDS
    try:
        value = float(humanize)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_SECONDS
    return value if value > 0 else _DEFAULT_MAX_SECONDS


def landing_enabled() -> bool:
    """Whether an action may be told to click somewhere other than the centre."""
    return (os.environ.get(LANDING_ENV) or "").strip().lower() not in (
        "off", "0", "false", "no",
    )


# ── per-page seeding ────────────────────────────────────────────────────────

def page_motion_seed(session_seed: int, ordinal: int) -> int:
    """Derive a page's motion seed from the session seed.

    Per PAGE rather than per session so that determinism survives more than
    one tab: a single shared stream would make every path depend on the order
    in which tabs happened to move. Same session seed plus same tab ordinal
    gives the same path, which is what makes a replayed seed replay the
    cursor. Kept inside int31 for consistency with the rest of the seeding.

    ⛔ EVERY BIT OF THE ORDINAL COUNTS, not only the low 16. A page the site
    opened is numbered in a space of its own above bit 62 (`_behaviour.
    popup_number`), and with the old mask two popups shared a path whenever
    their numbers agreed on 16 bits. The higher bits are folded in only when
    there are any, so the pages the script asks for (0, 1, 2 ...) keep the
    seeds they always had.
    """
    ordinal = int(ordinal)
    h = (int(session_seed) & 0xFFFFFFFF) * 0x9E3779B1
    h = (h ^ ((ordinal & 0xFFFF) * 0x85EBCA6B)) & 0xFFFFFFFF
    high = ordinal >> 16
    while high > 0:
        h = ((h ^ (high & 0xFFFF)) * 0xC2B2AE35) & 0xFFFFFFFF
        high >>= 16
    h ^= h >> 16
    return h & 0x7FFFFFFF


# ── the client's one wrapper ────────────────────────────────────────────────
#
# Weak keys: a closed browser or context must not be kept alive by our
# bookkeeping.
_SESSIONS: "WeakKeyDictionary[Any, Dict[str, Any]]" = WeakKeyDictionary()

_PATCH_STATE: Optional[bool] = None
_MARKER = "_invpw_cursor_wrapped"


def _impl(obj: Any) -> Any:
    """The implementation object behind a sync/async binding wrapper."""
    return getattr(obj, "_impl_obj", obj)


def enable_for(owner: Any, *, seed: int, max_seconds: float) -> bool:
    """Arm the Python cursor engine for a Browser or a BrowserContext.

    Registering the *owner* (rather than each page) is what lets pages created
    by any route be recognised: ``context.new_page()``, ``browser.new_page()`` -
    which makes its context inside the driver, out of reach of any wrapper we
    install on our side - and popups a site opens by itself. A page looks its
    session up lazily through its own context and browser.
    """
    if not _ensure_patched():
        return False
    try:
        _SESSIONS[_impl(owner)] = {"seed": int(seed), "max_seconds": float(max_seconds)}
    except TypeError:  # not weak-referenceable: refuse rather than leak
        return False
    return True


def _session_for_page(page: Any) -> Optional[Dict[str, Any]]:
    session = _SESSIONS.get(page)
    if session is not None:
        return session
    context = getattr(page, "_browser_context", None)
    if context is None:
        return None
    session = _SESSIONS.get(context)
    if session is not None:
        return session
    browser = getattr(context, "_browser", None)
    if browser is None:
        return None
    return _SESSIONS.get(browser)


def _page_of_mouse(mouse: Any) -> Optional[Any]:
    # A Mouse holds the page's own protocol channel, and a channel knows the
    # object it belongs to. That is the only back-reference there is.
    channel = getattr(mouse, "_channel", None)
    return getattr(channel, "_object", None) if channel is not None else None


def _wrap_mouse_move(original: Callable[..., Any]) -> Callable[..., Any]:
    """Marks an explicit ``mouse.move`` of a Python-engine session `humanise`,
    which is how the server knows to draw the stroke to it.

    ``steps=`` is the caller doing their own interpolation, and that is left
    alone, as is every page whose session did not arm the engine.
    """

    async def wrapper(self: Any, x: float, y: float, steps: int = None) -> Any:  # type: ignore[assignment]
        if steps is None or steps <= 1:
            page = _page_of_mouse(self)
            if page is not None and _session_for_page(page) is not None:
                await self._channel.send(
                    "mouseMove", None, {"x": x, "y": y, "humanise": True})
                return None
        return await original(self, x, y, steps=steps)

    setattr(wrapper, _MARKER, True)
    wrapper.__name__ = getattr(original, "__name__", "move")
    wrapper.__doc__ = original.__doc__
    return wrapper


def _ensure_patched() -> bool:
    """Install the wrapper once per process. Idempotent, and honest on failure."""
    global _PATCH_STATE
    if _PATCH_STATE is not None:
        return _PATCH_STATE
    try:
        from invisible_playwright._pw._impl._input import Mouse

        move = Mouse.move
        if not getattr(move, _MARKER, False):
            Mouse.move = _wrap_mouse_move(move)  # type: ignore[method-assign]
        _PATCH_STATE = True
    except Exception as exc:  # noqa: BLE001 - an unknown bindings layout is not fatal
        _warn_once(
            "bindings",
            "the Playwright bindings could not be wrapped (%s: %s), so pointer "
            "movement is not humanised in this process. The installed "
            "Playwright version is probably outside the supported range."
            % (type(exc).__name__, exc),
        )
        _PATCH_STATE = False
    return _PATCH_STATE


__all__ = [
    "ENGINE_ENV",
    "ENGINE_OFF",
    "ENGINE_PYTHON",
    "LANDING_ENV",
    "enable_for",
    "landing_enabled",
    "max_seconds_for",
    "page_motion_seed",
    "resolve_cursor_engine",
]
