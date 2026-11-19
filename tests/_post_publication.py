"""The e2e files that need what exists only AFTER a publication: the index, GitHub releases, pip and uv.

``test_release_e2e.py`` installs the README's block from the real index (uv, pip), downloads the 110 MB binary from the GitHub release, walks every
published version against the changelog and the GitHub API; ``test_upgrade_e2e.py`` upgrades from the previous release with pip. They are the one place that
proves what a user receives, and for the same reason they can be red for a reason that is nobody's change: PyPI or GitHub down or slow, a rate limit, an index
mirror that lags. A gate of a pull request or of a candidate must not wait on any of that (the rule of the project: no real site in a gate; the real ones are
in the release round and the round after the publication), so they run only where ``user-install.yml`` runs them: named on the command line, after the upload.

How that is held, with no new pytest marker (the marker vocabulary is shared with ``invisible_core``, which compares the lists of the repositories):
``tests/conftest.py`` deselects the ``e2e`` tests of these files from every run that does not NAME the file. ``scripts/run_e2e.py`` (the run of ``e2e.yml``,
of the hunt and of a candidate) names none, the Windows legs build their lists from a collection that no longer holds them, and ``user-install.yml`` names both.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

FILES = ("test_release_e2e.py", "test_upgrade_e2e.py")


def named(args: Iterable[str]) -> "set[Path]":
    """The files a command line names (``path`` or ``path::test``), resolved."""
    return {Path(str(arg).split("::", 1)[0]).resolve() for arg in args}


def is_left_out(path: "str | Path", args: Iterable[str]) -> bool:
    """True for a test of a post-publication file in a run that does not name that file."""
    path = Path(str(path))
    return path.name in FILES and path.resolve() not in named(args)
