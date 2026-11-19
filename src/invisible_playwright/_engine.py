"""Every route from a chosen path to a spawned Firefox goes through here.

``binary_path=`` never reaches ``ensure_binary()``, so none of the download-side
checks run on that route. The guard therefore sits on the
resolved executable, not inside the fetcher.
"""
from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import Optional, Union

from invisible_core import ensure_binary
from invisible_core.seal import EngineMismatch, active_seal, verify_engine


def resolve_executable(binary_path: Optional[Union[str, Path]]) -> Path:
    """``binary_path=`` skips the download path entirely, so the guard sits on the
    resolved executable rather than inside the fetcher.

    ⛔ Nothing here reads ``INVPW_BINARY_PATH``. It is a convention of this
    repo's harness (the test fixtures and ``run_e2e.py`` turn it into
    ``binary_path=``), not a feature of the library.

    Driving an engine the packaged seal does not describe is done with
    ``binary_path=`` plus ``INVISIBLE_SEAL_FILE`` pointing at a seal generated for
    it; without that, ``verify_engine`` refuses, and refusing is correct - the
    prefs and the spoofed User-Agent describe the sealed build.
    """
    seal = active_seal()
    if binary_path:
        return verify_engine(Path(binary_path), seal, source=f"binary_path={binary_path}")
    return ensure_binary(seal=seal)


def assert_wire_version(browser, *, backend: str = "firefox") -> None:
    """Compare what the engine reports over the protocol with the seal.

    browser.version is a cached property from the connection initializer
    (Juggler Browser.getInfo -> MOZ_APP_VERSION_DISPLAY, or CDP Browser.getVersion),
    so this costs zero round trips and no pref can spoof it.
    """
    if browser is None:
        return
    raw = getattr(browser, "version", "")
    if not isinstance(raw, str):
        return
    reported = raw.split("/")[-1].strip()
    if backend == "chromium":
        from ._cdp.engine import load_chromium_seal

        c_seal = load_chromium_seal()
        expected = str(c_seal.get("version") or "")
        if reported and expected and reported != expected:
            raise EngineMismatch(
                "engine/seal mismatch after launch - the running browser is not the sealed build\n"
                f"  protocol says: Chromium {reported}\n"
                f"  seal says    : Chromium {expected} (tag {c_seal.get('tag')})\n"
                "  fix          : INVPW_BACKEND=chromium python -m invisible_playwright fetch"
            )
        return
    seal = active_seal()
    if reported and reported != seal.upstream_version:
        raise EngineMismatch(
            "engine/seal mismatch after launch - the running browser is not the sealed build\n"
            f"  protocol says: Firefox {reported}\n"
            f"  seal says    : Firefox {seal.upstream_version} (tag {seal.tag}, "
            f"build {seal.build_id})\n"
            "  why          : application.ini can be edited; this value comes from the "
            "running engine itself.\n"
            "  fix          : python -m invisible_playwright fetch")


def assert_playwright_range() -> None:
    """The Juggler protocol schema is closed-world: a client outside the range
    the binary was tested with dies at context creation, or silently loses a
    feature. The range travels in the seal, so widening it does not need a
    wrapper release."""
    seal = active_seal()
    if not seal.playwright_min or not seal.playwright_max:
        return
    # The version of the VENDORED client, not of an installed package.
    #
    # Before the fork this line did `_pkg_version("playwright")` with an
    # `except PackageNotFoundError: return`. With the dependency removed, that
    # path would have become the ONLY path: the gate would have stopped
    # checking anything while still printing green, which is the exact way
    # nine workflows in this project were green while they were broken.
    #
    # The client now lives inside us and its version is a local fact, so
    # there is no longer any "absent" case to tolerate: if _repo_version
    # fails to import, the package is broken and must say so.
    from invisible_playwright._pw._repo_version import version as have

    def t(v: str):
        out = []
        for part in v.split(".")[:3]:
            digits = "".join(c for c in part if c.isdigit())
            out.append(int(digits or 0))
        return tuple(out + [0] * (3 - len(out)))

    if t(seal.playwright_min) <= t(have) <= t(seal.playwright_max):
        return
    # Spelled by the tool that installed this package (`invisible_core.installer`):
    # a written `pip install` is a command the venv the README makes does not have.
    from invisible_core.installer import install
    from invisible_core.pin import format_command

    fix = format_command(install("invisible-playwright", f"playwright=={seal.playwright_max}"))
    msg = (f"playwright {have} is outside the range this engine was tested with "
           f"({seal.playwright_min} .. {seal.playwright_max}, from the seal for {seal.tag}).\n"
           f"The Juggler protocol schema is closed-world: an untested client can kill every "
           f"session at context creation.\n"
           f"Run: {fix}   "
           f"(or set INVISIBLE_PLAYWRIGHT_SKEW=allow to proceed anyway)")
    if os.environ.get("INVISIBLE_PLAYWRIGHT_SKEW") == "allow":
        warnings.warn(msg, RuntimeWarning, stacklevel=2)
    else:
        raise RuntimeError(msg)
