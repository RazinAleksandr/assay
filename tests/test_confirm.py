"""A failure must reproduce before it blocks: which journeys get a second run, how confirm.json changes the gate, and
what the pull request is told. No browsers: the second run itself is a normal `assay run` and is faked here."""
import json
from argparse import Namespace
from pathlib import Path

import pytest

from assay import cli, explain
from assay.analyze import analyze
from assay.ci import comment_markdown
from assay.config import Profile
from assay.quality import blocking_failures, case_changes, gate

# the target branch: both journeys passed there, so a failure now is new
REF = {"cases": [{"case_id": "a", "verdict": "PASS"}, {"case_id": "b", "verdict": "PASS"}], "issues": []}
PHRASE = "the app state has `n` = 0, expected 1"


@pytest.fixture()
def run(tmp_path):
    """A pull request run where `a` (Checkout) and `b` (Comment) fail a `must` check; both are critical, so their
    journey failure is also a new critical issue, which the default gate counts too."""
    run = tmp_path / "run"
    for cid, title in (("a", "Checkout"), ("b", "Comment")):
        (run / "cases" / cid).mkdir(parents=True)
        (run / "cases" / cid / "evidence.json").write_text(json.dumps({
            "case_id": cid, "title": title, "case": {"id": cid, "severity": "critical", "must": [{"state": {"n": 1}}]},
            "state_before": {"n": 0}, "state_after": {"n": 0}, "steps": [], "sensors": {}}))
    (run / "run.json").write_text(json.dumps({"id": "r", "case_order": ["a", "b"], "ci": {"pr": "5"}}))
    return run


def _profile(tmp_path, **gate):
    prof = Profile(name="t", root=tmp_path, base_url="http://x")
    prof.gate.update(new_code=True, **gate)
    return prof


def _second_run(run, **verdicts):
    (run / "confirm.json").write_text(json.dumps({"run": "confirm", "cases": {
        cid: {"verdict": v, **({"phrase": PHRASE} if v == "FAIL" else {})} for cid, v in verdicts.items()}}))


# ---- which journeys get a second run -------------------------------------------------------------------------------------

def test_only_the_failures_that_block_get_a_second_run():
    cases = [{"case_id": "old", "verdict": "FAIL"}, {"case_id": "new", "verdict": "FAIL"},
             {"case_id": "bug", "verdict": "FAIL"}, {"case_id": "ok", "verdict": "PASS"},
             {"case_id": "again", "verdict": "FAIL", "confirm": {"verdict": "FAIL"}}]
    pr = {"gate": {"status": "FAILED", "conditions": [{"id": "new_failed_journeys", "ok": False}]}, "cases": cases,
          "changes": {"regressions": ["new", "bug", "again"]}, "known_bugs": ["bug"]}
    assert blocking_failures(pr) == ["new"]          # not the old failure, the known bug, or one already run twice
    branch = {**pr, "gate": {"status": "FAILED", "conditions": [{"id": "failed_journeys", "ok": False}]}}
    assert blocking_failures(branch) == ["old", "new"]
    issues_only = {**pr, "gate": {"status": "FAILED", "conditions": [{"id": "new_failed_journeys", "ok": True},
                                                                      {"id": "new_issues:critical", "ok": False}]}}
    assert blocking_failures(issues_only) == []
    assert blocking_failures({**pr, "gate": {**pr["gate"], "status": "PASSED"}}) == []
    assert blocking_failures({**pr, "gate": {**pr["gate"], "first_analysis": True}}) == []


# ---- confirm.json → the gate ----------------------------------------------------------------------------------------------

def test_a_failure_that_does_not_reproduce_blocks_nothing(tmp_path, run):
    first = analyze(run, _profile(tmp_path), judge=None, baseline=REF)
    assert first["gate"]["status"] == "FAILED" and blocking_failures(first) == ["a", "b"]

    _second_run(run, a="PASS", b="FAIL")
    a = analyze(run, _profile(tmp_path), judge=None, baseline=REF)
    by = {c["case_id"]: c for c in a["cases"]}
    assert by["a"]["verdict"] == "FAIL" and by["a"]["confirm"] == {"verdict": "PASS", "run": "confirm"}   # its evidence failed
    assert by["b"]["confirm"]["verdict"] == "FAIL" and a["gate"]["status"] == "FAILED"
    assert a["changes"]["regressions"] == ["b"] and a["summary"]["flaky"] == 1
    assert [i["sources"] for i in a["issues"] if i["rule"] == "CASE-FAIL"] == [["b"]]       # no finding for the flake
    assert blocking_failures(a) == []                                                     # never a third run
    # `assay grade` reads the same folder: the same gate
    again = analyze(run, _profile(tmp_path), judge=None, baseline=REF)
    assert again["gate"] == a["gate"] and [c.get("confirm") for c in again["cases"]] == [c.get("confirm") for c in a["cases"]]

    _second_run(run, a="PASS", b="PASS")
    ok = analyze(run, _profile(tmp_path), judge=None, baseline=REF)
    assert ok["gate"]["status"] == "PASSED" and ok["summary"]["FAIL"] == 2 and ok["summary"]["flaky"] == 2


def test_a_second_run_that_could_not_check_leaves_the_failure_standing(tmp_path, run):
    """Fail closed: only a pass on the second run makes a failure flaky. One the second run did not record (it crashed,
    the app did not start) counts as not checked there."""
    (run / "confirm.json").write_text(json.dumps({"run": "confirm", "cases": {"a": {}, "b": {"verdict": "PASS"}}}))
    a = analyze(run, _profile(tmp_path), judge=None, baseline=REF)
    assert a["cases"][0]["confirm"]["verdict"] == "INCONCLUSIVE"
    assert a["gate"]["status"] == "FAILED" and a["changes"]["regressions"] == ["a"]
    assert explain.verdict(a)["headline"] == f"Checkout: {PHRASE}. A second run could not check it."


def test_a_flaky_failure_on_the_target_counts_as_passing():
    """main failed once and passed on its second run: a pull request where it fails for real broke it, one where it
    passes fixed nothing."""
    failing = {"case_id": "x", "verdict": "FAIL", "cause": "must:state", "steps": [],
               "items": [{"kind": "state", "level": "must", "status": "fail", "detail": "n: expected 1, got 0"}]}
    ref = {"cases": [{**failing, "confirm": {"verdict": "PASS"}}]}
    assert case_changes([failing], ref) == {"regressions": ["x"], "fixed": []}
    assert case_changes([{"case_id": "x", "verdict": "PASS"}], ref) == {"regressions": [], "fixed": []}
    assert gate({"fail_on_case_fail": True, "new_code": True}, [failing], [], {}, baseline=ref)["status"] == "FAILED"
    flaky_now = {**failing, "confirm": {"verdict": "PASS"}}
    assert gate({"fail_on_case_fail": True, "new_code": True}, [flaky_now], [], {}, baseline=ref)["status"] == "PASSED"
    assert gate({"fail_on_case_fail": True}, [flaky_now], [], {})["status"] == "PASSED"          # a branch, too


# ---- what the pull request is told ---------------------------------------------------------------------------------------

def test_the_comment_says_reproduced_and_lists_flaky_ones_calmly(tmp_path, run):
    _second_run(run, a="PASS", b="FAIL")
    a = analyze(run, _profile(tmp_path), judge=None, baseline=REF)
    head, details = comment_markdown(a, None, {"pr": "5"}, "t").split("<details>")
    head = head.strip().splitlines()
    assert len(head) == 5 and head[1] == "### ❌ Don't ship: 1 journey broke"
    assert head[2] == f"Comment: {PHRASE}. Reproduced on a second run."
    assert "- **Not blocking:** Checkout: failed once, passed on a second run\n" in details   # named, not described

    _second_run(run, a="PASS", b="PASS")
    ok = analyze(run, _profile(tmp_path), judge=None, baseline=REF)
    head = comment_markdown(ok, None, {"pr": "5"}, "t").split("<details>")[0].strip().splitlines()
    assert head[1] == "### ✅ Ship it: nothing broke"
    assert head[2] == "Nothing broke; 2 journeys failed once and passed on a second run: not blocking."
    assert head[3] == "**From you:** nothing."
    from assay.report import junit
    xml = junit(ok)                                    # CI test tabs: passed, and flaky
    assert 'failures="0"' in xml and xml.count("<flakyFailure") == 2 and "<failure" not in xml


def test_the_brief_tells_an_agent_what_the_second_run_found(tmp_path, run):
    from assay.brief import build
    _second_run(run, a="PASS", b="FAIL")
    analyze(run, _profile(tmp_path), judge=None, baseline=REF)
    b = build(run)
    assert [f["case_id"] for f in b["failures"]] == ["b"] and b["also_failing"] == []
    second = b["failures"][0]["second_run"]
    assert second["verdict"] == "FAIL" and second["note"] == "Reproduced on a second run"
    assert b["failures"][0]["artifacts"]["second_run"].endswith("confirm/cases/b/evidence.json")
    assert b["flaky"] == [{"case_id": "a", "phrase": f"Checkout: {PHRASE}",       # the first failure, for whoever fixes the flake
                           "note": "failed once, passed on a second run: not blocking"}]


# ---- the second run itself -------------------------------------------------------------------------------------------------

def _fake_run(calls, verdicts):
    """Stands in for cmd_run: records how it was asked and writes the analysis a second run would have written."""
    def run(ns):
        calls.append(ns)
        out = Path(ns.out) / ns.run_id
        out.mkdir(parents=True, exist_ok=True)
        (out / "analysis.json").write_text(json.dumps({"cases": [
            {"case_id": cid, "verdict": verdicts[cid], **({"failure": {"phrase": PHRASE}} if verdicts[cid] == "FAIL" else {})}
            for cid in ns.select["ids"]]}))
        return 0
    return run


def test_the_second_run_runs_exactly_the_blocking_journeys_and_is_applied(tmp_path, run, monkeypatch):
    prof = _profile(tmp_path)
    analyze(run, prof, judge=None, baseline=REF)
    calls = []
    monkeypatch.setattr(cli, "cmd_run", _fake_run(calls, {"a": "PASS", "b": "FAIL"}))
    args = Namespace(project=None, profile_obj=prof, baseline=REF, no_start=False, no_reset=False, no_judge=True, k=["*"],
                     run_id="r", out=str(tmp_path), scan=True, no_scan=False, confirm=True, select=None, diff_text="x")
    assert cli._confirm(args, prof, run, judge=None) == cli.EXIT["FAILED"]
    ns = calls[0]
    assert ns.select["ids"] == ["a", "b"] and ns.k is None and ns.no_scan and not ns.scan and not ns.confirm
    assert (ns.out, ns.run_id, ns.baseline, ns.no_reset) == (str(run), "confirm", None, False)   # a fresh app, no reference
    assert json.loads((run / "confirm.json").read_text()) == {"run": "confirm", "cases": {
        "a": {"verdict": "PASS"}, "b": {"verdict": "FAIL", "phrase": PHRASE}}}
    a = json.loads((run / "analysis.json").read_text())
    assert [c["confirm"]["verdict"] for c in a["cases"]] == ["PASS", "FAIL"] and a["changes"]["regressions"] == ["b"]

    calls.clear()
    monkeypatch.setattr(cli, "cmd_run", _fake_run(calls, {"a": "PASS", "b": "PASS"}))
    (run / "confirm.json").unlink()
    analyze(run, prof, judge=None, baseline=REF)
    assert cli._confirm(args, prof, run, judge=None) == cli.EXIT["PASSED"]


def test_too_many_failures_get_no_second_run(tmp_path, run, monkeypatch):
    prof = _profile(tmp_path, confirm_max=1)
    analyze(run, prof, judge=None, baseline=REF)
    calls = []
    monkeypatch.setattr(cli, "cmd_run", _fake_run(calls, {}))
    args = Namespace(no_start=False, baseline=REF)
    assert cli._confirm(args, prof, run, judge=None) is None and calls == [] and not (run / "confirm.json").exists()


# ---- guard and upload ------------------------------------------------------------------------------------------------------

def test_letting_more_failures_pass_on_a_second_run_is_a_weaker_gate():
    from assay.guard import settings_changes
    said = "let more failures pass on a second run (gate.confirm)"
    assert said in settings_changes({"gate": {"confirm": False}}, {"gate": {}})
    assert said in settings_changes({"gate": {}}, {"gate": {"confirm_max": 50}})
    assert said not in settings_changes({"gate": {}}, {"gate": {"confirm": False}}) + settings_changes({}, {})


def test_an_upload_cannot_say_anything_in_confirm():
    from assay.uploads import check_analysis
    up = lambda confirm: {"gate": {"status": "PASSED", "conditions": []},
                          "cases": [{"case_id": "a", "verdict": "FAIL", "confirm": confirm}]}
    got = check_analysis(up({"verdict": "PASS", "run": "confirm", "x": "<script>"}))
    assert got["cases"][0]["confirm"] == {"verdict": "PASS", "run": "confirm", "phrase": None}
    for bad in ({"verdict": "OK"}, {"verdict": "PASS", "run": "../../etc"}, "PASS"):
        with pytest.raises(ValueError):
            check_analysis(up(bad))


def test_first_analysis_of_a_branch_lists_what_the_product_had_and_blocks_on_journeys_only():
    from assay.explain import verdict
    from assay.model import Issue
    from assay.quality import gate
    cfg = {"fail_on_case_fail": True, "max_new": {"critical": 0}, "no_reference": True}
    iss = Issue(rule="A11Y-button-name", title="Buttons must have discernible text", dimension="accessibility", severity="critical",
                where="/", message="Buttons must have discernible text")
    ok = [{"case_id": "a", "verdict": "PASS"}]
    g = gate(cfg, ok, [iss], {})
    a = {"gate": g, "cases": [{"case_id": "a", "verdict": "PASS", "title": "A"}], "issues": [{**iss.__dict__, "new": True}]}
    v = verdict(a)
    assert v["state"] == "ship" and v["action"] == "look" and "already exist" in v["headline"]
    assert all(not p["blocking"] for p in v["problems"])
    broken = [{"case_id": "a", "verdict": "FAIL"}]
    a2 = {"gate": gate(cfg, broken, [iss], {}), "cases": [{"case_id": "a", "verdict": "FAIL", "title": "A"}],
          "issues": [{**iss.__dict__, "new": True}]}
    v2 = verdict(a2)
    assert v2["state"] == "no_ship" and "new problem" not in v2["title"]
    assert [p["blocking"] for p in v2["problems"] if p["kind"] == "issue"] == [False]


def test_a_problem_only_a_known_bug_hits_does_not_block():
    from assay.model import Issue
    from assay.quality import gate
    cfg = {"fail_on_case_fail": True, "max_new": {"critical": 0}}
    kb = {"case_id": "privacy.not_found", "verdict": "FAIL", "tags": ["known-bug"], "case": {"tags": ["known-bug"]}}
    http500 = Issue(rule="REL-003", title="HTTP 500", dimension="reliability", severity="critical", where="/link/edit/:id",
                    sources=["privacy.not_found"])
    other = Issue(rule="REL-003", title="HTTP 500", dimension="reliability", severity="critical", where="/x",
                  sources=["privacy.not_found", "feed.scroll"])
    assert gate(cfg, [kb], [http500], {}, baseline={"cases": []})["status"] == "PASSED"
    assert gate(cfg, [kb, {"case_id": "feed.scroll", "verdict": "PASS"}], [other], {}, baseline={"cases": []})["status"] == "FAILED"
