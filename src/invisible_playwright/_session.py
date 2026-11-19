"""Session logic shared by the sync and async entry points.

WHY THIS FILE EXISTS. There are two `InvisiblePlaywright` classes, and the only
thing keeping them equal was somebody remembering. Measured 2026-07-27: 203 of
252 normalised lines of `async_api.py` also appear in `launcher.py` - 80.6% -
and `__init__`, `_teardown`, `_build_env` and `_default_context_kwargs` are 100%
identical once the word `await` is deleted. So the duplication is not the price
of async; async was the excuse for it.

That is a defect GENERATOR, and it has already produced three:

  * the 0.4.0 process-leak fix reached the sync class only, and shipped to the
    other half of the users under a release note saying it was fixed;
  * `INVPW_TRUE_HEADLESS`, a documented env var, worked on the async API alone;
  * `_build_prefs` was a method on one side and twenty inline lines on the other.

Anything here must stay free of Playwright imports: it is the part that has
nothing to do with which API a caller picked. What legitimately differs between
the two classes is `await`, and nothing else belongs in this file.
"""
from __future__ import annotations

import asyncio
import os
from ._cursor import (ENGINE_PYTHON,
                      enable_for as _enable_cursor_engine,
                      max_seconds_for as _cursor_max_seconds)
from ._engine import resolve_executable
from ._juggler.server import COMPOSED_PREF, MOTION_BUDGET_PREF, POINTER_PREF, SESSION_SEED_PREF, UDP_STUN_PREF
from typing import Any, Dict, Optional

from invisible_core import compose_session_prefs, context_geometry, make_virtual_display
from invisible_core.launch import (FontManifestMismatch,
                                   build_launch_env,
                                   cached_font_manifest_path,
                                   verify_font_manifest)

__all__ = ["BACKEND_ENV", "SUPPORTED_BACKENDS", "resolve_backend",
           "build_prefs", "true_headless_requested", "TRUE_HEADLESS_ENV",
           "ProxyEgressDrifted", "egress_ancora_valido"]

BACKEND_ENV = "INVPW_BACKEND"
SUPPORTED_BACKENDS = ("firefox", "chromium")


def resolve_backend(backend: Optional[str] = None) -> str:
    chosen = backend if backend is not None else os.environ.get(BACKEND_ENV, "firefox")
    norm = str(chosen).strip().lower()
    if norm not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"Unsupported backend {chosen!r}; expected one of {SUPPORTED_BACKENDS}"
        )
    return norm

#: Opt-in to real headless. Read HERE rather than in one of the two classes,
#: because reading it in one of them is exactly how it came to work on the async
#: API only.
#:
#: ⛔ THE REASON THIS USED TO BE A WORKAROUND IS NOW KNOWN, AND FIXED. This
#: comment said: "the default headful+cloak path intermittently hangs
#: launch_persistent_context on Windows (~40%, a window/compositor race with a
#: persistent profile); true headless ... is reliable". It had the right
#: neighbourhood - window and compositor - and no mechanism, so it stood as an
#: escape hatch for months.
#:
#: The mechanism, measured 2026-08-14: Windows declared the chrome window FULLY
#: OCCLUDED and in the hanging runs the verdict was never revoked
#: (`nsWindow::NotifyOcclusionState() mIsFullyOccluded 1` with no later 0). An
#: inactive BrowsingContext never paints, so delayed startup never finishes,
#: `browser-delayed-startup-finished` never fires, no target is created and the
#: launch times out at 180 s. True headless was reliable for a reason nobody had
#: named: there is no window to occlude.
#:
#: Fixed by `widget.windows.window_occlusion_tracking.enabled=false`, applied to
#: EVERY session by invisible_core - 24 relaunches out of 24 against 6 hangs in 9.
#: So this variable is no longer a workaround for that hang; it remains as a
#: deliberate choice of a different rendering path. Full account:
#: `71-bug-archive.md` [B150].
TRUE_HEADLESS_ENV = "INVPW_TRUE_HEADLESS"


def udp_stun_list(servers: Optional[Any]) -> Optional[list]:
    """The `udp_stun_servers=` of a launch, validated by the core ("ip:port" IPv4 literals; a name or a bad port
    raises ValueError naming the parameter) and normalised to "ip:port" strings; None for the two public defaults."""
    if servers is None:
        return None
    from invisible_core._udp_exit import stun_servers
    return ["%s:%d" % server for server in stun_servers(servers)]


def session_geo(timezone: str, proxy: Optional[Dict[str, str]], locale: Any,
                seed: Optional[int] = None):
    """`invisible_core.prepare_session_geo`, the one network step of composing a
    session (the egress IP). One name, so a hermetic test replaces it alone.
    The seed places the session on the map (the declared position)."""
    from invisible_core import prepare_session_geo
    return prepare_session_geo(timezone, proxy, locale, seed=seed)


def default_session(executable: Optional[str] = None,
                    proxy: Optional[Dict[str, str]] = None) -> "tuple[Dict[str, Any], Dict[str, str]]":
    """The session `InvisiblePlaywright()` composes with the server's defaults (a new
    seed, the launch's proxy, locale "auto", humanize off: the default of `launch_server`, the
    other entry that is a server), as (prefs, the variables of its environment).

    The variables are what `build_env` adds to an environment, composed onto an
    empty one: the font manifest, the WebRTC declaration, the display backend.
    What `server.session_for_launch` gives a launch that names no session."""
    from .launcher import InvisiblePlaywright
    session = InvisiblePlaywright(binary_path=executable, humanize=False, proxy=proxy or None)
    geo = session_geo(session._timezone, session._proxy, session._locale_requested,
                      seed=session.seed)
    session._timezone, session._locale = geo.timezone, geo.locale
    session._geolocation = geo.geolocation_override()
    prefs = session._build_prefs()
    env = build_env(network_declared=declared_network(session), profile=session._profile,
                    executable=executable, base_env={})
    return prefs, env


def declared_network(session: Any) -> str:
    """The host network this session declares to the engine (the value of
    STEALTHFOX_WEBRTC_NETWORK), drawn from its seed, with or without a proxy."""
    from invisible_core import network_to_declare
    return network_to_declare(session.seed)


def true_headless_requested(env: Optional[Dict[str, str]] = None) -> bool:
    return (env if env is not None else os.environ).get(TRUE_HEADLESS_ENV) == "1"


#: The IANA -> POSIX conversion comes from the core, which is where the table
#: lives. It was duplicated here byte-for-byte - ten entries, including the
def build_env(
    *,
    #: The host network the engine reads (STEALTHFOX_WEBRTC_NETWORK), drawn from
    #: the session seed (`declared_network`); None declares nothing.
    network_declared: Optional[str],
    profile: Any = None,
    executable: Optional[str] = None,
    base_env: Optional[Dict[str, str]] = None,
    #: What the session's hidden surface wants in the browser's environment
    #: (`make_virtual_display().launch_env()`), or nothing. A value of `None`
    #: names a variable the browser must NOT carry (the Wayland ones an Xvfb
    #: session drops). ⛔ Applied LAST and over a cleared slot: the surface is
    #: a fact of THIS session, and a headed session opened while a hidden one
    #: is still alive in the same process must not inherit the hidden one's
    #: desktop or display through `os.environ`. `INVPW_DESKTOP` is removed
    #: unconditionally first for the same reason.
    display_env: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, str]:
    """The environment the Firefox subprocess is launched with, minus the token.

    The session token is stamped by the caller, because it is the one part that
    is genuinely per-session; everything here was written twice, identically,
    in `launcher._build_env` and `async_api._build_env`.

    Fonts DO need an env, and only an env will do. The engine builds its font
    list during app startup, inside InitSharedFontListForPlatform, and on this
    path the prefs never reach the profile at all - Playwright sends them over
    the wire and JS applies them once the browser is already running. Measured
    2026-08-08: the pref of the same name always read empty there, so the
    engine's packaged copy won and the manifest this package declares was
    inert. An environment variable is set at process creation and inherited by
    every content process.

    `profile=None` sets nothing, which leaves the engine on its own copy - the
    floor that keeps a browser launched without this package rendering.

    What is composed here is only what needs the EXECUTABLE: the manifest is
    verified against the engine's own fonts before its path is handed over.
    The environment itself is composed by the core's `build_launch_env`, the
    one place that knows the WebRTC declaration, the manifest variable and
    the hidden surface's `launch_env()`; until 0.25.2 this function was a twin
    of that body, and the hidden-surface half of the contract lived only here.
    """
    manifest_path = None
    manifest = getattr(getattr(profile, "font", None), "manifest", "")
    if manifest:
        # Refuse rather than hand the engine metrics for faces it does not
        # carry. That failure is not an error at runtime - it is a page laid
        # out a few pixels wrong, which nothing observes. A browser that will
        # not start is at least loud, and the engine still has its own copy to
        # fall back to if this variable is simply absent.
        if executable:
            missing = verify_font_manifest(manifest, executable)
            # `None` means the engine has no fonts/ beside it, so there was
            # nothing to check against - not that the check passed. Proceeding
            # is right anyway: an engine with no bundled fonts cannot be made
            # worse by a manifest naming files it does not have either way, and
            # refusing would reject every layout that is not ours.
            if missing:
                raise FontManifestMismatch(
                    f"the font manifest this package declares names "
                    f"{len(missing)} face file(s) the engine does not carry "
                    f"(e.g. {missing[:3]}). Engine: {executable}. The metrics "
                    f"would describe fonts that are not there.")
        manifest_path = cached_font_manifest_path(manifest)
    return build_launch_env({}, network_declared=network_declared,
                            manifest_path=manifest_path, base_env=base_env,
                            display_env=display_env)


def build_prefs(
    *,
    profile: Any,
    locale: Any,
    timezone: Optional[str],
    extra_prefs: Optional[Dict[str, Any]],
    virtual_display: bool,
    cursor_engine: str,
    humanize: Any,
    show_cursor: Optional[bool] = None,
    session_seed: Optional[int] = None,
    udp_stun_servers: Optional[Any] = None,
    preload_newtab: Optional[bool] = None,
) -> Dict[str, Any]:
    """Fingerprint prefs plus the humanize toggle, which is always set explicitly.

    Takes values rather than a session object so it stays callable from a test
    without constructing either class - and so that adding a field to one class
    cannot silently change what the other one builds.
    """
    # The core composes it. This was the third of
    # three places that stacked layers on top of translate_profile_to_prefs in
    # its own order, and nothing compared the results: the one in
    # get_default_stealth_prefs never called configure_proxy at all, so a caller
    # driving Playwright themselves with a SOCKS endpoint got no network.proxy.*
    # pref and went out on the host's own address.
    #
    # What stays here is this path's DELIVERY and its two decisions:
    #
    #   humanize   Always false: the wrapper draws the path, and a true value
    #              would make the browser expand every waypoint we send into a
    #              path of its own.
    #   show_cursor  Passed through UNTRANSLATED, and that is the difference
    #              from the line above: `humanize` needs a decision here
    #              because two engines compete for it, while the visible dot
    #              has exactly one meaning and the core owns it. Anything this
    #              function computed about it would be a second place that
    #              knows, which is the defect the paragraph above is about.
    #
    # The namespace MUST be stealthfox.* - that is what the binary's Juggler
    # reads. An earlier `invisible_playwright.*` spelling was a dead no-op, so
    # humanize never fired and every click teleported the cursor.
    prefs = compose_session_prefs(
        profile,
        locale=locale,
        timezone=timezone,
        extra_prefs=extra_prefs,
        # The REAL value, not a guess: B172, 2026-08-24. The caller passes
        # whether `make_virtual_display()` actually created something - an
        # Xvfb on Linux, a Win32 desktop on Windows - and the sandbox
        # workarounds meant for that desktop apply only then. Nothing here
        # reads the platform: the fact travels, not a prediction of it.
        virtual_display=virtual_display,
        humanize=False,
        show_cursor=show_cursor,
        # None (the caller did not say) writes nothing in the core; False / True are declarations.
        preload_newtab=preload_newtab,
    ).prefs
    # ⛔ THE TYPING SEED RIDES IN THE PREFS AND NEVER REACHES THE PROFILE.
    #
    # The engine client has no seed of its own - it is constructed by a
    # transport that knows nothing about a session - and this dict is the one
    # thing the launcher composes in full that already arrives there. The
    # server pops the key in `op_launch` before writing `user.js`, so no
    # session identifier is written to disk; the alternatives were a module
    # global (wrong the moment a process holds two sessions) and a walk through
    # four private attributes of the vendored client.
    #
    # It follows `humanize`, like the cursor: a caller who turned human motion
    # off asked for a machine, and giving them a hand on the keyboard anyway
    # would be a second answer to a question they already answered.
    # ⛔ AND THE MOTION BUDGET RIDES WITH IT, under exactly the same condition:
    # it caps the walk the server draws before every pointer action
    # (`_juggler/pointer.py`) and the travel of a drag, which happens with the
    # button already down and whose far end is only resolved after the press.
    # Both are generated inside the server, so the caller's `humanize=<n>` caps
    # every click and every drag, whichever client sent them.
    #
    # The condition is shared and not merely similar: the server draws a path
    # only when it has the seed, so a budget without one would cap a movement
    # nobody is making.
    #
    # ⛔ AND WHO DRAWS THE WALK TRAVELS TOO (`POINTER_PREF`): with the seed
    # alone the server could not tell the Python generator from
    # `INVPW_CURSOR_ENGINE=off`, and would draw a walk nobody asked for.
    #
    # Milliseconds, and an int: Gecko has no float pref type, and `1.0` written
    # as a number arrives with the right value and the wrong type - the defect
    # `ui.textScaleFactor` had.
    # The persona's keyboards are in the prefs already: the core declares them
    # with the language and country (invisible_core.prefs._apply_locale).
    prefs[COMPOSED_PREF] = True
    # ⛔ THE STUN SERVERS OF THE UDP-EXIT MEASUREMENT ride here for the server only (`UDP_STUN_PREF`): `op_launch`
    # takes the key out before the profile is written, as it does the seed. Not written when the launch named none.
    if udp_stun_servers:
        prefs[UDP_STUN_PREF] = ",".join(udp_stun_servers)
    if session_seed is not None and humanize:
        prefs[SESSION_SEED_PREF] = int(session_seed)
        prefs[POINTER_PREF] = cursor_engine == ENGINE_PYTHON
        prefs[MOTION_BUDGET_PREF] = int(round(
            _cursor_max_seconds(humanize) * 1000.0))
    return prefs


class ProxyEgressDrifted(RuntimeError):
    """The egress IP changed MID-SESSION.

    This is not a condition the product can recover from, and it is deliberate
    that it is an error instead of a silent update.

    The timezone and the locale are decided ONCE, at launch, from the egress
    country. If the egress moves afterwards, the page exits from one place
    while the timezone and the language still describe another: a mismatch no
    real connection produces.

    Updating them on the fly would be just as unnatural (the site would see
    them change mid-session). If the egress does not hold for the duration of
    the session, that proxy is not sticky and is not usable for this purpose.

    Measured on 2026-08-25: the two providers tried both declare sessions
    sticky by TIME (60 minutes at most for one, a sliding timeout for the
    other, which decays even earlier if the residential peer disconnects), so
    on a long enough session the drift is not a risk: it is a certainty.
    """


class ProxyEgressNonVerificabile(RuntimeError):
    """The egress could not be MEASURED, repeatedly.

    This is not drift and it is not parity: it is absence of measurement. A
    proxy that does not answer the probe for several checks in a row is not a
    proxy that holds, it is a proxy we are flying blind on while the timezone
    and the locale, decided from the egress at launch, go on being declared
    to every page with nobody confirming the egress they came from any more.
    """


#: The three outcomes of the check. There are THREE and not two on purpose:
#: see why in the docstring of `egress_ancora_valido`.
USCITA_REGGE = "regge"
USCITA_DERIVATA = "derivata"
USCITA_NON_MISURABILE = "non_misurabile"


def egress_ancora_valido(proxy: Optional[Dict[str, str]],
                         atteso: Optional[str],
                         *, timeout: int = 20) -> "tuple[str, Optional[str]]":
    """(outcome, current_ip), with outcome among the three `USCITA_*`.

    ⛔ THERE ARE THREE OUTCOMES BECAUSE TWO LIE. Until 2026-08-25 this function
    returned `(True, None)` when the probe FAILED, with a comment that argued
    the right half of the thing well - "a failed discovery is not a drift",
    and that is true, a network problem is not turned into an accusation
    against the proxy. But the returned value said `regge` (holds), i.e. it
    **asserted parity on the basis of a measurement that had not taken
    place**.

    It is the same class of defect this project has already paid for twice
    and fixed twice:

    - `fppro_consistency.py` printed CONSISTENCY PASS when `visitor_id` was
      mute in BOTH runs, because two `None`s come out identical. It has had a
      third outcome since 2026-08-15, exit code 2 = NOT INTERPRETABLE.
    - `repair_core` set `verdict = None` under a handler that said "a broken
      probe is not a licence", above a test `verdict is not None` that then
      let the reinstall proceed.

    The caller now distinguishes: on `USCITA_DERIVATA` it refuses right away,
    on `USCITA_NON_MISURABILE` it counts and refuses only if it repeats,
    because a probe that fails once is the network and one that always fails
    is blindness.

    The case with no proxy or no expected value stays `USCITA_REGGE`, and it
    is different from the other two: there is nothing to betray there, no
    measurement was missed.
    """
    if not proxy or not atteso:
        return USCITA_REGGE, None
    from invisible_core import _geo
    try:
        attuale = _geo.discover_egress_ip(proxy, timeout=timeout)
    except Exception:
        return USCITA_NON_MISURABILE, None
    return (USCITA_REGGE if attuale == atteso else USCITA_DERIVATA), attuale


class CommonLaunch:
    """The six methods both entry points had, written once.

    ⛔ THE DUPLICATION THIS CLOSES IS THE ONE THIS FILE WAS CREATED FOR, and it
    survived the file's own creation. `_session` was written on 2026-07-27 to
    hold what the sync and async classes share, and it did extract the
    functions - `build_env`, `build_prefs`, `true_headless_requested`. What it
    left behind were the METHODS that call them: six of them, in both classes,
    with bodies that are identical byte for byte once the docstrings are
    removed. 222 lines saying the same thing twice.

    The measurement that says it is the same thing: the two classes use the
    SAME FOURTEEN attributes of `self` across those six methods, and the ASTs
    of the bodies compare equal. What made them look different - similarity
    ratios between 32% and 69% - was entirely comments worded differently.

    ⛔ AND THE COST OF THE SPLIT IS NOT HYPOTHETICAL. The three defects listed
    at the top of this file all have the same shape, and one of them is in the
    code that moved here: `INVPW_TRUE_HEADLESS` was honoured by the async class
    alone, so a documented environment variable worked or not depending on
    which entry point the caller had picked.

    ⛔ WHAT THIS CLASS EXPECTS, said out loud because a mixin's contract is
    otherwise invisible: both subclasses set `seed`, `_binary_path`,
    `_cursor_engine`, `_extra_prefs`, `_headless`, `_humanize`,
    `_lifetime_guard`, `_locale` (the core's SessionLocale, None until
    `__enter__` decides it), `_profile`, `_session_token`, `_show_cursor`,
    `_network_declared`, `_timezone` and `_virtual_display` in their own
    `__init__`; `_geolocation` (the position the session declares, the
    ``geolocation`` object of a context, None until `__enter__` decides it
    and when the exit has no point) is the class default below. They already did, identically, which is why this works at all.
    """

    @property
    def backend(self) -> str:
        return getattr(self, "_backend", "firefox")

    #: The position this session declares to the engine
    #: (``SessionGeo.geolocation_override()``: latitude, longitude, accuracy),
    #: or None. Set by `__enter__` from the egress, like `_timezone`.
    _geolocation: Optional[Dict[str, float]] = None

    #: Where this session measures a SOCKS5 proxy's UDP exit from: the ``udp_stun_servers=`` of the launch, as
    #: "ip:port" strings (validated by the core), or None for the two public defaults. Kept on the session so the
    #: launch geo step and the browser's later context proxies use the same list.
    _udp_stun_servers: Optional[list] = None

    def _start_display(self, vd: Any) -> Any:
        vd.start()
        wm = None
        if getattr(self, "_backend", "firefox") == "chromium":
            launch_env_fn = getattr(vd, "launch_env", None)
            d_env = (launch_env_fn() if callable(launch_env_fn) else None) or {}
            if d_env.get("DISPLAY"):
                from ._cdp.engine import OpenboxWorkarea

                wm = OpenboxWorkarea(d_env)
                wm.start()
        return wm

    def _resolve_headless(self) -> bool:
        """Translate the user's ``headless`` flag.

        When ``True``, Firefox stays in headed mode (real rendering pipeline →
        coherent fingerprint) and the window is hidden by drawing on a screen
        nobody looks at: on Linux a fresh Xvfb spawned here; on Windows a
        fresh Win32 desktop the spawner creates the browser on
        (``STARTUPINFO.lpDesktop``, read from ``INVPW_DESKTOP``). The engine
        is stock on this surface: no pref hides anything.

        ``self._virtual_display`` is the fact ``_build_prefs`` reads to decide
        whether the desktop workarounds apply (B172).
        """
        pw_headless, vd = self._plan_headless()
        if vd is not None:
            self._openbox = self._start_display(vd)
            self._virtual_display = vd
        return pw_headless

    def _plan_headless(self) -> "tuple[bool, Any]":
        """THE decision, made once for both APIs: ``(pw_headless, display)``.

        ``pw_headless`` is what Playwright is told; ``display`` is the hidden
        surface the caller must START (and then record in
        ``self._virtual_display``, which `_build_prefs` reads), or None when
        none is wanted: headless off, true headless asked for, or a platform
        with no display to make. The two callers only decide WHERE the start
        runs - inline in the sync client, in a worker in the async one - so
        the decision cannot drift between them (B172 was one such drift).
        """
        if not self._headless:
            return False, None
        # Opt-in TRUE headless, shared with the async class. It existed on the
        # async API ONLY until 2026-07-27: a documented env var that worked
        # depending on which entry point the caller happened to pick, which is
        # the same drift that shipped the process-leak fix to half the users.
        if true_headless_requested():
            return True, None
        return False, make_virtual_display()

    async def _resolve_headless_async(self) -> bool:
        """`_resolve_headless` for the async API: the display starts in a worker.

        Starting the surface is a blocking call - on Linux it spawns an Xvfb
        and waits for it to report ready (~75 ms measured, seconds on a loaded
        host) - and `__aenter__` made it on the user's loop, so every other
        browser of the process stood still for that long, `headless=True`
        or not. The surface is the same and is still created before the prefs
        are composed; only the thread changes. The sync API keeps the direct
        call: nothing else runs on its thread.
        """
        pw_headless, vd = self._plan_headless()
        if vd is None:
            return pw_headless
        started = asyncio.ensure_future(asyncio.to_thread(self._start_display, vd))
        try:
            wm = await asyncio.shield(started)
        except asyncio.CancelledError:
            # The worker cannot be interrupted and may yet create the surface
            # for a launch nobody is waiting for: release it when it ends.
            loop = asyncio.get_running_loop()

            def release(done: "asyncio.Future") -> None:
                if not done.cancelled() and done.exception() is None:
                    built_wm = done.result()

                    def _stop_both() -> None:
                        if built_wm is not None:
                            try:
                                built_wm.stop()
                            except Exception:
                                pass
                        vd.stop()

                    loop.run_in_executor(None, _stop_both)

            started.add_done_callback(release)
            raise
        self._openbox = wm
        self._virtual_display = vd
        return pw_headless

    def _take_display(self) -> Any:
        """THE release, made once for both APIs: the hidden surface this session started, taken out of it (None when there is none).

        The counterpart of `_plan_headless`: the two teardowns only decide WHERE the stop runs - inline in the sync client, off the
        loop in the async one. Stopping waits for the Xvfb to exit (`Popen.wait`, a loop of sleeps on POSIX), and on the user's loop
        that wait held every other browser of the process (e2e Linux, run 43701692047)."""
        display, self._virtual_display = self._virtual_display, None
        wm = getattr(self, "_openbox", None)
        self._openbox = None
        if wm is None:
            return display

        class _CompositeDisplay:
            @staticmethod
            def stop() -> None:
                try:
                    wm.stop()
                except Exception:
                    pass
                if display is not None:
                    display.stop()

        return _CompositeDisplay()

    def _default_context_kwargs(self) -> Dict[str, Any]:
        if getattr(self, "_backend", "firefox") == "chromium":
            pin = getattr(self, "_pin", None) or {}
            kwargs_chr: Dict[str, Any] = {}
            if "viewport.width" in pin or "viewport.height" in pin:
                kwargs_chr["viewport"] = {
                    "width": int(pin.get("viewport.width") or 1280),
                    "height": int(pin.get("viewport.height") or 720),
                }
            else:
                kwargs_chr["no_viewport"] = True
            if self._timezone:
                kwargs_chr["timezone_id"] = self._timezone
            if self._locale is not None:
                kwargs_chr["locale"] = self._locale.primary
            if self._geolocation is not None:
                kwargs_chr["geolocation"] = dict(self._geolocation)
            return kwargs_chr
        # The screen and viewport in CSS pixels, derived once in the core:
        # the profile's screen is the PANEL, and until wrapper 0.28.0 the
        # panel's device pixels went out here as CSS (screen.width 1920 at
        # DPR 1.25).
        kwargs: Dict[str, Any] = {
            **context_geometry(self._profile),
            # ⛔ device_scale_factor and color_scheme are NO LONGER passed.
            # They were a second source for two facts invisible_core already
            # declares (layout.css.devPixelsPerPx and, since 2026-08-24,
            # layout.css.prefers-color-scheme.content-override), and this one
            # won: measured, setting the pref to a different value the
            # browser did not move. Now there is only one path.
        }
        # Pass timezone via Playwright's per-realm override (docShell.overrideTimezone
        # → JS::SetRealmTimezoneOverride). The juggler.timezone.override pref path
        # uses JS::SetTimeZoneOverride globally, which is broken on Windows ICU for
        # no-DST IANA names (America/Phoenix, Pacific/Honolulu, ...) - those silently
        # fall back to the host system TZ. The per-realm path works for every zone.
        if self._timezone:
            kwargs["timezone_id"] = self._timezone
        # Playwright's option is ONE tag, so the context gets the decision's
        # primary: what navigator.language reports for the launch prefs. The
        # server turns it back into the same list (SessionLocale.of), so
        # the default context declares exactly what the profile declared.
        # None before __enter__ has decided.
        if self._locale is not None:
            kwargs["locale"] = self._locale.primary
        # The position the core declares for this machine. It travels as the
        # standard `geolocation` context option, so the engine gets
        # `Browser.setGeolocationOverride` for EVERY context (the default one
        # and each `new_context`/`new_page`), a caller's own `geolocation=`
        # wins like any other default, and the PERMISSION is not granted here:
        # the page's grant (the caller's `grant_permissions`) is what makes the
        # engine answer. The override belongs to each context (Juggler keys it
        # by `browserContextId`), so a context that closes takes its own with it.
        if self._geolocation is not None:
            kwargs["geolocation"] = dict(self._geolocation)
        return kwargs

    def _build_env(self, prefs: Dict[str, Any]) -> Dict[str, str]:
        """Env for the browser subprocess, then stamped with this session's token."""
        vd = self._virtual_display
        if getattr(self, "_backend", "firefox") == "chromium":
            env = dict(os.environ)
            env.pop("INVPW_DESKTOP", None)
            if vd is not None:
                for k, v in (vd.launch_env() or {}).items():
                    if v is None:
                        env.pop(k, None)
                    else:
                        env[k] = v
            hw = getattr(self, "_chromium_hw", None)
            if hw is not None:
                env["INVISIBLE_CHROMIUM_PROFILE"] = hw.to_json()
            if self._timezone:
                env["TZ"] = self._timezone
            return self._session_token.stamp(env)
        return self._session_token.stamp(
            build_env(network_declared=self._network_declared,
                      profile=self._profile,
                      executable=resolve_executable(self._binary_path),
                      display_env=vd.launch_env() if vd is not None else None))

    def _build_prefs(self) -> Dict[str, Any]:
        """Fingerprint prefs plus humanize toggle (always set explicitly)."""
        if getattr(self, "_backend", "firefox") == "chromium":
            out: Dict[str, Any] = {COMPOSED_PREF: True}
            if self._locale is not None:
                from invisible_core.juggler.keyboard import KEYBOARD_LAYOUT_PREF, profiles_pref

                out[KEYBOARD_LAYOUT_PREF] = profiles_pref(self._locale.primary)
            if self._humanize:
                out[SESSION_SEED_PREF] = int(self.seed)
                out[POINTER_PREF] = self._cursor_engine == ENGINE_PYTHON
                cap = _cursor_max_seconds(self._humanize)
                if cap is not None:
                    out[MOTION_BUDGET_PREF] = int(round(cap * 1000.0))
            return out
        return build_prefs(
            profile=self._profile,
            locale=self._locale,
            timezone=self._timezone,
            extra_prefs=self._extra_prefs,
            virtual_display=self._virtual_display is not None,
            cursor_engine=self._cursor_engine,
            humanize=self._humanize,
            show_cursor=self._show_cursor,
            session_seed=self.seed,
            udp_stun_servers=self._udp_stun_servers,
            preload_newtab=self._preload_newtab,
        )

    def _arm_cursor_engine(self, owner: Any) -> None:
        """Register this session so its pages move through the Python generator.

        Registered on the browser (or on the persistent context, which is all
        there is in that mode) rather than on each page: pages appear by
        several routes we do not control - ``browser.new_page()`` builds its
        context inside the driver, and a site can open a popup on its own - and
        every one of them can find its way back to this owner. The seed is the
        session seed, so a replayed seed replays the cursor exactly as it
        replays the fingerprint.
        """
        if self._cursor_engine != ENGINE_PYTHON:
            return
        _enable_cursor_engine(
            owner, seed=self.seed, max_seconds=_cursor_max_seconds(self._humanize)
        )
