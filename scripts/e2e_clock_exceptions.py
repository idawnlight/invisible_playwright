#!/usr/bin/env python3
"""The reviewed list of the check that keeps third parties out of the verdict of the e2e tests (``tests/gates/e2e_clock_exceptions.json``).

    python scripts/e2e_clock_exceptions.py            # what the list says and what the sources say: counts by class, and every difference
    python scripts/e2e_clock_exceptions.py --write    # make the ``debt`` again: every occurrence no ``entries`` item covers (a correction removed some)
    python scripts/e2e_clock_exceptions.py --write --prune   # and drop the ``entries`` that match nothing any more

``entries`` are the occurrences a person reviewed and decided are deliberate (each has a closed ``kind`` and a ``reason``); ``--write`` never adds one. ``debt`` is every
other occurrence: the dangerous ones, not yet corrected. It is made from the sources, so it is regenerated, not edited: when a branch that corrects tests merges, run
``--write`` and the corrected occurrences leave the list. A list that grows is refused by ``tests/test_the_e2e_tests_do_not_wait_on_clocks.py`` (against main).
Exit 1 when the file on disk is not what ``--write`` would write.
"""
from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import _e2e_clocks as clocks  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--write", action="store_true", help="write the file with the debt made again")
    ap.add_argument("--prune", action="store_true", help="with --write: drop the entries that match no occurrence")
    ns = ap.parse_args(argv)
    found = clocks.scan()
    path = clocks.GATE_FILE
    document = clocks.load(path) if path.is_file() else clocks.empty_document()
    made = clocks.regenerate(document, found, prune_entries=ns.prune)
    text = clocks.dump(made)
    differences = clocks.compare(found, document)
    by_class = collections.Counter(i["class"] for i in made["debt"])  # type: ignore[index, union-attr]
    kinds = collections.Counter(i["kind"] for i in made["entries"])  # type: ignore[index, union-attr]
    print("entries: %d %s" % (len(made["entries"]), dict(sorted(kinds.items()))))  # type: ignore[arg-type]
    print("debt:    %d %s" % (len(made["debt"]), dict(sorted(by_class.items()))))  # type: ignore[arg-type]
    for name in ("new", "stale_entries", "stale_debt", "both"):
        for key in differences[name]:
            print("  %-13s %s" % (name, key))
    if ns.write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        print("written: %s" % path.relative_to(ROOT))
        return 0
    if not path.is_file() or path.read_text(encoding="utf-8") != text:
        print("%s is not what --write makes" % path.relative_to(ROOT), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
