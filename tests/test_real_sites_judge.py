"""The judge of the real-sites round (scripts/judge_real_sites.py): it is red where stock Firefox gets through a page and the published wrapper does not, and only there.

The judge is a pure function of the cells the two browsers wrote. A cell is *good* when the page loaded, reached ``networkidle``, answered 2xx, showed its marker
and the canary page after it was quiet. The rule, per site, on the runs available for that site (the first pass, plus two more runs of each browser for the
cells that differ):

* RED: stock is good in all three runs and ours is not good in at least two of the three;
* INCOMPLETE: a cell that differs in the first pass has three runs of each browser; without them it is a problem ("giro incompleto"), never a red cell;
* NO INFORMATION: stock is not good in every run (a blocked, fallen or changed site): never red, ours is reported as data;
* the job is red too when a site was not visited by both browsers, when the list has no sites, or when fewer cells carry information than half the sites rounded
  up ("il giro non ha misurato"); the minimum is computed from the list and is not a field of the file;
* the admission is red only for a defect of the list (a pass missing, a site never good on a system and not protetta, more than 4 protetta, more than 20 sites, a
  ``min_informative`` in the file), never because a site was good in some passes and not in all.

The six cases of the design come first, each on known-bad input; the rest are the helpers around them. No time is read or asserted anywhere here.
"""
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("judge_real_sites_under_test", ROOT / "scripts" / "judge_real_sites.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # a dataclass reads its own module
    spec.loader.exec_module(module)
    return module


judge_module = _load()
judge = judge_module.judge

GOOD = {"load": "ok", "networkidle": "ok", "status": "2xx", "marker": True, "canary": "ok"}
SITE = {"id": "site-a", "url": "https://a.example/", "marker": "#main", "class": "calma"}
OTHER = {"id": "site-b", "url": "https://b.example/", "marker": "#main", "class": "giornale"}


ANCHOR = {"id": "anchor", "url": "https://anchor.example/", "marker": "#main", "class": "calma"}


def anchored(runs):
    """The runs with one more site, good for both browsers, so that a round whose cells all lack information still has one that measures."""
    return [{**runs[0], "anchor": dict(GOOD)}] + list(runs[1:])


def cell(**over):
    return {**GOOD, **over}


def runs_of(site_id, *cells):
    """One run per cell: a run is the cells of one pass, keyed by site."""
    return [{site_id: c} for c in cells]


def row(result, site_id):
    return next(r for r in result.table if r["id"] == site_id)


# --- the six cases of the design --------------------------------------------------------------------------------------------------------------
def test_stock_good_three_of_three_and_ours_good_once_is_red():
    stock = runs_of("site-a", cell(), cell(), cell())
    ours = runs_of("site-a", cell(), cell(load="timeout"), cell(status="4xx"))
    result = judge(stock, ours, [SITE])
    assert row(result, "site-a")["state"] == "red"
    assert result.verdict == "red" and result.exit_code == 1
    assert result.red == 1


def test_stock_good_three_of_three_and_ours_good_twice_is_green():
    stock = runs_of("site-a", cell(), cell(), cell())
    ours = runs_of("site-a", cell(), cell(), cell(networkidle="timeout"))
    result = judge(stock, ours, [SITE])
    assert row(result, "site-a")["state"] == "green"
    assert result.verdict == "green" and result.exit_code == 0
    assert result.red == 0 and result.informative == 1


@pytest.mark.parametrize("stock_cells", [
    (cell(status="4xx"), cell(status="4xx"), cell(status="4xx")),
    (cell(), cell(), cell(marker=False)),
    (cell(load="timeout"), cell(), cell()),
    (cell(canary="no"), cell(canary="no"), cell(canary="no")),
], ids=["blocked in all runs", "bad in the last run", "bad in the first run", "canary of stock fails"])
def test_a_stock_that_is_not_good_gives_no_information_and_is_never_red(stock_cells):
    ours = runs_of("site-a", cell(load="error:NS_ERROR_NET_RESET"), cell(load="timeout"), cell(status="5xx"))
    result = judge(anchored(runs_of("site-a", *stock_cells)), anchored(ours), [ANCHOR, SITE])
    assert row(result, "site-a")["state"] == "no_information"
    assert result.red == 0 and result.informative == 1, "the anchor is the one cell with information"
    assert result.verdict == "green" and result.exit_code == 0


def test_the_outcome_of_ours_is_reported_where_stock_gives_no_information():
    stock = runs_of("site-a", cell(status="4xx"), cell(status="4xx"), cell(status="4xx"))
    ours = runs_of("site-a", cell(), cell(), cell())
    result = judge(anchored(stock), anchored(ours), [ANCHOR, SITE])
    assert row(result, "site-a")["state"] == "no_information"
    assert row(result, "site-a")["ours"]["good"] == 3 and row(result, "site-a")["stock"]["good"] == 0
    assert result.ours_good_stock_not == 1
    assert result.verdict == "green"


def test_a_site_missing_from_one_of_the_two_passes_is_red():
    stock = [{"site-a": cell(), "site-b": cell()}]
    ours = [{"site-a": cell()}]
    result = judge(stock, ours, [SITE, OTHER])
    assert row(result, "site-b")["state"] == "missing"
    assert result.verdict == "red" and result.exit_code == 1
    assert any("site-b" in p and "ours" in p for p in result.problems), result.problems
    # the same site missing from the pass of stock is just as red, and a pass that is not there at all misses every site
    flipped = judge(ours, stock, [SITE, OTHER])
    assert flipped.verdict == "red" and any("site-b" in p and "stock" in p for p in flipped.problems)
    nothing = judge([], stock, [SITE, OTHER])
    assert nothing.verdict == "red" and len(nothing.problems) >= 2


def test_a_canary_that_fails_only_for_ours_with_the_real_page_good_is_red():
    stock = runs_of("site-a", cell(), cell(), cell())
    ours = runs_of("site-a", cell(canary="no"), cell(canary="no"), cell(canary="no"))
    for r in ours:
        # the real page was good in every other respect
        assert {k: v for k, v in r["site-a"].items() if k != "canary"} == {k: v for k, v in GOOD.items() if k != "canary"}
    result = judge(stock, ours, [SITE])
    assert row(result, "site-a")["state"] == "red"
    assert "canary" in row(result, "site-a")["ours"]["why"]
    assert result.verdict == "red" and result.exit_code == 1


# --- the runs of a cell, and the numbers -----------------------------------------------------------------------------------------------------
def test_a_cell_that_did_not_differ_has_one_run_each_and_the_later_runs_hold_only_the_differing_cells():
    # good and good in the first pass is green and is not asked again
    result = judge([{"site-a": cell()}], [{"site-a": cell()}], [SITE])
    assert row(result, "site-a")["state"] == "green" and row(result, "site-a")["stock"]["runs"] == 1
    # the later runs hold only the differing cells: a site absent from them is not a missing site
    stock = [{"site-a": cell(), "site-b": cell()}, {"site-a": cell()}, {"site-a": cell()}]
    ours = [{"site-a": cell(), "site-b": cell()}, {"site-a": cell()}, {"site-a": cell()}]
    result = judge(stock, ours, [SITE, OTHER])
    assert result.verdict == "green" and row(result, "site-b")["stock"]["runs"] == 1 and row(result, "site-a")["stock"]["runs"] == 3


def test_the_judge_does_not_touch_what_it_is_given():
    stock = runs_of("site-a", cell(), cell(), cell())
    ours = runs_of("site-a", cell(load="timeout"), cell(load="timeout"), cell())
    before = copy.deepcopy((stock, ours, [SITE]))
    judge(stock, ours, [SITE])
    assert (stock, ours, [SITE]) == before


def test_the_three_numbers_of_the_summary():
    sites = [SITE, OTHER, {"id": "site-c", "url": "https://c.example/", "marker": "h1", "class": "negozio"}]
    stock = [{"site-a": cell(), "site-b": cell(), "site-c": cell(status="4xx")}, {"site-a": cell(), "site-c": cell(status="4xx")},
             {"site-a": cell(), "site-c": cell(status="4xx")}]
    ours = [{"site-a": cell(load="timeout"), "site-b": cell(), "site-c": cell()}, {"site-a": cell(status="5xx"), "site-c": cell()},
            {"site-a": cell(marker=False), "site-c": cell()}]
    result = judge(stock, ours, sites)
    assert (result.informative, result.red, result.ours_good_stock_not) == (2, 1, 1)
    text = judge_module.render_markdown(result)
    assert "site-a" in text and "site-b" in text and "site-c" in text
    assert "informative cells: 2" in text and "red cells: 1" in text and "ours good, stock not: 1" in text
    assert "| red |" in text and "| no information |" in text


def test_a_cell_is_good_only_when_all_five_outcomes_are():
    assert judge_module.is_good(cell())
    for field, bad in [("load", "timeout"), ("load", "error:NS_ERROR_NET_RESET"), ("networkidle", "timeout"), ("status", "3xx"), ("status", "none"),
                       ("marker", False), ("canary", "no")]:
        assert not judge_module.is_good(cell(**{field: bad})), (field, bad)
    assert not judge_module.is_good({}) and not judge_module.is_good(None)


def test_the_cells_that_differ_are_the_ones_that_get_two_more_runs():
    sites = [SITE, OTHER, {"id": "site-c", "url": "https://c.example/", "marker": "h1", "class": "negozio"}]
    stock = {"site-a": cell(), "site-b": cell(), "site-c": cell(status="4xx")}
    ours = {"site-a": cell(), "site-b": cell(load="timeout"), "site-c": cell()}
    assert judge_module.differing_sites(stock, ours, sites) == ["site-b", "site-c"]
    assert judge_module.differing_sites(stock, stock, sites) == []


# --- the admission of a list ------------------------------------------------------------------------------------------------------------------
def _site(site_id, klass="calma"):
    return {"id": site_id, "url": "https://%s.example/" % site_id, "marker": "#main", "class": klass}


MEDIAMARKT = {"id": "mediamarkt", "url": "https://www.mediamarkt.de/", "marker": "main", "class": "negozio"}
DUCKDUCKGO = {"id": "duckduckgo", "url": "https://duckduckgo.com/", "marker": "#searchbox_homepage", "class": "applicazione"}
#: What the two sites really did, once in five passes, on Linux: a challenge page for the address of the runner, and a load that did not finish.
MEDIAMARKT_CHALLENGE = {"load": "ok", "networkidle": "ok", "status": "4xx", "marker": False, "canary": "ok"}
DUCKDUCKGO_TIMEOUT = {"load": "timeout", "networkidle": "ok", "status": "none", "marker": False, "canary": "ok"}


def _passes(per_site, n=5):
    """``per_site``: id -> a cell, or a function of the pass number. ``n`` passes of stock."""
    return [{sid: (c(i) if callable(c) else c) for sid, c in per_site.items()} for i in range(n)]


def _admit_args(tmp_path, sites, passes_by_os, extra_in_file=None):
    """The file of the list and the passes of stock of each system, written; returns the arguments of the ``admit`` command."""
    sites_file = tmp_path / "sites.json"
    sites_file.write_text(json.dumps({**(extra_in_file or {}), "sites": sites}), encoding="utf-8")
    groups = []
    for label, passes in passes_by_os.items():
        files = []
        for n, cells in enumerate(passes, 1):
            path = tmp_path / ("%s-%d.json" % (label, n))
            path.write_text(json.dumps({"browser": "stock", "cells": cells}), encoding="utf-8")
            files.append(str(path))
        groups += ["--os", label, *files]
    return ["admit", "--sites", str(sites_file), *groups, "--summary", str(tmp_path / "admission.md")]


def _summary(tmp_path):
    return (tmp_path / "admission.md").read_text(encoding="utf-8")


def test_the_minimum_of_information_is_half_the_sites_rounded_up():
    assert [judge_module.information_minimum(n) for n in (0, 1, 2, 3, 14, 15, 20)] == [0, 1, 1, 2, 7, 8, 10]


def test_a_site_good_in_some_passes_stays_with_its_count_and_the_admission_is_green(tmp_path):
    sites = [_site("steady"), MEDIAMARKT, DUCKDUCKGO]
    linux = _passes({"steady": cell(), "mediamarkt": lambda i: MEDIAMARKT_CHALLENGE if i == 2 else cell(),
                     "duckduckgo": lambda i: DUCKDUCKGO_TIMEOUT if i == 4 else cell()})
    windows = _passes({s["id"]: cell() for s in sites})
    assert judge_module.main(_admit_args(tmp_path, sites, {"linux": linux, "windows": windows})) == 0
    text = _summary(tmp_path)
    assert "4 of 5 on linux" in text and "5 of 5 on windows" in text
    assert "mediamarkt" in text and "duckduckgo" in text
    assert "min_informative" not in text, "the admission computes no minimum"


def test_a_site_never_good_on_a_system_and_not_protected_is_red(tmp_path):
    sites = [_site("steady"), _site("wrong-marker", "giornale")]
    linux = _passes({"steady": cell(), "wrong-marker": cell(marker=False)})
    windows = _passes({s["id"]: cell() for s in sites})
    assert judge_module.main(_admit_args(tmp_path, sites, {"linux": linux, "windows": windows})) == 1
    assert "wrong-marker" in _summary(tmp_path) and "never good on linux" in _summary(tmp_path)
    # the same site, good on Linux and never good on Windows, is just as red
    assert judge_module.main(_admit_args(tmp_path, sites, {"linux": windows, "windows": linux})) == 1
    assert "never good on windows" in _summary(tmp_path)


def test_a_protected_site_never_good_is_kept_up_to_four(tmp_path):
    def blocked(count):
        sites = [_site("steady")] + [_site("shield%d" % i, "protetta") for i in range(count)]
        cells = {s["id"]: (cell() if s["id"] == "steady" else cell(status="4xx", marker=False)) for s in sites}
        return sites, {"linux": _passes(cells), "windows": _passes(cells)}

    sites, passes = blocked(4)
    assert judge_module.main(_admit_args(tmp_path, sites, passes)) == 0
    sites, passes = blocked(5)
    assert judge_module.main(_admit_args(tmp_path, sites, passes)) == 1
    assert "more than 4" in _summary(tmp_path)


@pytest.mark.parametrize("count, exit_code", [(20, 0), (21, 1)])
def test_a_list_of_more_than_twenty_sites_is_red(tmp_path, count, exit_code):
    sites = [_site("s%02d" % i) for i in range(count)]
    cells = {s["id"]: cell() for s in sites}
    assert judge_module.main(_admit_args(tmp_path, sites, {"linux": _passes(cells), "windows": _passes(cells)})) == exit_code
    if exit_code:
        assert "more than 20" in _summary(tmp_path)


def test_the_admission_is_red_for_a_pass_that_is_missing(tmp_path):
    cells = {"steady": cell()}
    assert judge_module.main(_admit_args(tmp_path, [_site("steady")], {"linux": _passes(cells, 4), "windows": _passes(cells)})) == 1
    assert "4 passes of stock, 5 expected" in _summary(tmp_path)


def test_the_admission_of_an_empty_list_is_green(tmp_path):
    assert judge_module.main(_admit_args(tmp_path, [], {"linux": _passes({}), "windows": _passes({})})) == 0


def test_a_list_with_a_min_informative_is_not_a_list(tmp_path):
    problems = judge_module.validate_sites({"min_informative": 3, "sites": [_site("a")]})
    assert any("min_informative" in p for p in problems), problems
    assert judge_module.validate_sites({"sites": [_site("a")]}) == []
    cells = {"a": cell()}
    with pytest.raises(SystemExit) as refused:
        judge_module.main(_admit_args(tmp_path, [_site("a")], {"linux": _passes(cells), "windows": _passes(cells)}, {"min_informative": 3}))
    assert "min_informative" in str(refused.value)


def test_what_a_pass_adds_to_diagnose_a_marker_does_not_enter_the_judgement():
    diagnosed = cell(marker=False, diagnosis={"status": 403, "title": "Blocked", "ids": ["a", "b"]})
    # the judge: the extra field changes nothing, a good cell stays good and a diagnosed one is the same as one without
    assert judge_module.is_good(cell(diagnosis={"status": 200, "title": "x", "ids": []}))
    stock = runs_of("site-a", cell(), cell(), cell())
    with_diagnosis = judge(stock, runs_of("site-a", diagnosed, diagnosed, diagnosed), [SITE])
    without = judge(stock, runs_of("site-a", cell(marker=False), cell(marker=False), cell(marker=False)), [SITE])
    assert (with_diagnosis.verdict, with_diagnosis.table[0]["state"], with_diagnosis.table[0]["ours"]["why"]) == \
           (without.verdict, without.table[0]["state"], without.table[0]["ours"]["why"])
    # the admission counts the good passes by the five fields: a diagnosis does not make a pass good or bad
    runs = [{"site-a": cell(marker=False, diagnosis={"status": 403, "title": "t%d" % i, "ids": [str(i)]})} for i in range(4)] + [{"site-a": cell()}]
    row = judge_module.admission({"linux": runs}, [_site("site-a", "protetta")], expected_runs=5)["linux"]["rows"][0]
    assert (row["good"], row["runs"]) == (1, 5)


# --- an incomplete round is not a red cell, and an empty round did not measure ---------------------------------------------------------------
def test_a_differing_cell_without_its_three_runs_is_an_incomplete_round_and_not_a_red_cell():
    for runs in (1, 2):
        stock = [{"site-a": cell(), "site-b": cell()}] + [{"site-a": cell()}] * (runs - 1)
        ours = [{"site-a": cell(load="timeout"), "site-b": cell()}] + [{"site-a": cell(load="timeout")}] * (runs - 1)
        result = judge(stock, ours, [SITE, OTHER])
        assert row(result, "site-a")["state"] == "incomplete", runs
        assert result.red == 0, "the missing runs are the round's fault, not a cell that is red"
        assert result.verdict == "red" and result.exit_code == 1
        assert any("giro incompleto" in p and "site-a" in p for p in result.problems), result.problems
    # a cell that differs the other way round (stock not good, ours good) is asked three times too
    reverse = judge([{"site-a": cell(status="4xx"), "site-b": cell()}], [{"site-a": cell(), "site-b": cell()}], [SITE, OTHER])
    assert row(reverse, "site-a")["state"] == "incomplete" and reverse.verdict == "red"
    # with its three runs the same cell is judged
    full = judge(runs_of("site-a", cell(), cell(), cell()), runs_of("site-a", cell(load="timeout"), cell(load="timeout"), cell()), [SITE])
    assert row(full, "site-a")["state"] == "red" and not any("giro incompleto" in p for p in full.problems)


def test_a_cell_that_did_not_differ_needs_no_more_runs():
    result = judge([{"site-a": cell(), "site-b": cell(status="4xx")}], [{"site-a": cell(), "site-b": cell(load="timeout")}], [SITE, OTHER])
    assert [r["state"] for r in result.table] == ["green", "no_information"]
    assert result.verdict == "green" and result.problems == []


def test_a_round_with_no_sites_did_not_measure():
    for stock, ours in (([{}], [{}]), ([], [])):
        result = judge(stock, ours, [])
        assert result.verdict == "red" and result.exit_code == 1
        assert any("il giro non ha misurato" in p for p in result.problems), result.problems


def _fifteen(informative):
    """Fifteen sites, the first ``informative`` of them good for both browsers, the others blocked for both (no information, and not a difference)."""
    sites = [_site("s%02d" % i) for i in range(15)]
    run = {s["id"]: (cell() if i < informative else cell(status="4xx")) for i, s in enumerate(sites)}
    return judge([dict(run)], [dict(run)], sites)


def test_eight_informative_cells_out_of_fifteen_is_enough_and_seven_is_not():
    enough = _fifteen(8)
    assert enough.informative == 8 and enough.verdict == "green" and enough.problems == []
    short = _fifteen(7)
    assert short.informative == 7 and short.verdict == "red" and short.exit_code == 1
    assert any("il giro non ha misurato" in p and "minimum is 8" in p for p in short.problems), short.problems


# --- the files ------------------------------------------------------------------------------------------------------------------------------
def test_the_cli_reads_the_cells_and_writes_the_summary_and_the_exit_code(tmp_path):
    sites_file = tmp_path / "sites.json"
    sites_file.write_text(json.dumps({"sites": [SITE]}), encoding="utf-8")

    def write(name, cells):
        path = tmp_path / name
        path.write_text(json.dumps({"browser": name.split("-")[0], "cells": cells}), encoding="utf-8")
        return str(path)

    stock = [write("stock-%d.json" % n, {"site-a": cell()}) for n in (1, 2, 3)]
    bad = [write("ours-%d.json" % n, {"site-a": cell(load="timeout")}) for n in (1, 2, 3)]
    summary = tmp_path / "summary.md"
    code = judge_module.main(["judge", "--sites", str(sites_file), "--stock", *stock, "--ours", *bad, "--summary", str(summary)])
    assert code == 1 and "site-a" in summary.read_text(encoding="utf-8")
    good = [write("ours-%d.json" % n, {"site-a": cell()}) for n in (1, 2, 3)]
    assert judge_module.main(["judge", "--sites", str(sites_file), "--stock", *stock, "--ours", *good, "--summary", str(summary)]) == 0


def test_the_sites_file_has_its_schema_and_every_site_a_class():
    data = json.loads((ROOT / "tests" / "realsites" / "sites.json").read_text(encoding="utf-8"))
    assert "min_informative" not in data and "min_informative" not in data["fields"], "the minimum is half the sites, computed by the judge"
    assert isinstance(data["sites"], list) and set(data["classes"]) == {"calma", "giornale", "negozio", "applicazione", "protetta"}
    for field in ("id", "url", "marker", "class"):
        assert data["fields"][field].strip()
    assert judge_module.validate_sites(data["sites"]) == []
    problems = judge_module.validate_sites([{"id": "x", "url": "https://x.example/", "marker": "a", "class": "other"},
                                            {"id": "x", "url": "https://y.example/", "marker": "", "class": "calma"}])
    assert len(problems) >= 3, problems
