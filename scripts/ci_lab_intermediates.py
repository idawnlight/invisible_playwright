"""The preloaded-intermediates list of a CI leg: a fixed (empty) file of the repository, and the variable that makes the core use it.

    python scripts/ci_lab_intermediates.py set [--pin ci/intermediates.pin.json]     # checks the file against the pin, exports STEALTHFOX_INTERMEDIATES

Why this exists: without ``STEALTHFOX_INTERMEDIATES`` the core downloads Mozilla's intermediates list on a cold cache (in line, at the first launch or at
`invisible_playwright fetch`) and asks once a week whether it changed, from the python of the test. A CI leg reaches no site for its data and every run has to
have the same bytes, so the leg launches with ``ci/intermediates.json``: a valid list in the engine's format 1 with no certificate. With the variable set the
core neither downloads nor checks, ever. ``ci/intermediates.pin.json`` holds the sha256 of the file: ``set`` refuses to go on when the file is not that.

``set`` writes ``STEALTHFOX_INTERMEDIATES=<absolute path>`` to ``$GITHUB_ENV`` (every later step of the job sees it) and, when there is no
``$GITHUB_ENV``, to stdout. The paths of the pin are relative to the repository, not to the working directory of the caller.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ENV = "STEALTHFOX_INTERMEDIATES"
ROOT = Path(__file__).resolve().parents[1]


class Refused(Exception):
    pass


def sha256_of(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_pin(path) -> dict:
    try:
        pin = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused("the pin %s cannot be read: %s" % (path, exc))
    sha = pin.get("sha256") if isinstance(pin, dict) else None
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise Refused("the pin %s has no sha256 that is a sha256" % path)
    if not isinstance(pin.get("file"), str) or not pin["file"]:
        raise Refused("the pin %s names no file" % path)
    return pin


def resolve(pin: dict, pin_path) -> Path:
    """The file the pin names, checked against its sha256: a file that is not the pinned one is refused."""
    target = Path(pin["file"])
    if not target.is_absolute():
        target = ROOT / target
    if not target.is_file():
        raise Refused("the file %s of the pin cannot be found" % pin["file"])
    got = sha256_of(target)
    if got != pin["sha256"]:
        raise Refused("%s has sha256 %s, the pin says %s: change both in one commit" % (pin["file"], got, pin["sha256"]))
    return target.resolve()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set")
    s.add_argument("--pin", default="ci/intermediates.pin.json")
    ns = ap.parse_args(argv)
    try:
        path = resolve(load_pin(ns.pin), ns.pin)
    except Refused as exc:
        print("ci_lab_intermediates: %s" % exc, file=sys.stderr)
        return 1
    line = "%s=%s" % (ENV, path)
    env_file = os.environ.get("GITHUB_ENV")
    if env_file:
        with open(env_file, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
    else:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
