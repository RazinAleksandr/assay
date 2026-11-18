"""Regression coverage for false-pass paths found during the Excalidraw audit."""
from pathlib import Path

import pytest

from assay import checks
from assay.grade import grade_case
from assay.quality import case_changes, gate


def test_failed_accessibility_audit_is_inconclusive():
    ev = {"case_id": "audit", "case": {"must": [{"a11y": {"max_impact": "serious"}}]},
          "audit": {"a11y": {"error": "axe crashed", "violations": []}}}
    result = grade_case(ev, Path("."), volatile=[], judge=None)
    assert result.verdict == "INCONCLUSIVE"
    assert "axe crashed" in result.summary


@pytest.mark.parametrize("field", ["probe_errors_before", "probe_errors_after"])
def test_state_diff_is_unavailable_when_a_probe_failed(field):
    ev = {"state_before": {"elements": [], "theme": "light"},
          "state_after": {"elements": [{"id": "x"}], "theme": "light"}, field: {"theme": "read failed"}}
    assert checks.d_allowed_changes(["elements"], ev, None, {})[0] == "undecided"
    assert checks.d_unchanged(["theme"], ev, None, {})[0] == "undecided"


def test_missing_probe_cannot_silently_disappear_from_diff():
    ev = {"state_before": {"elements": [], "theme": "light"}, "state_after": {"elements": [{"id": "x"}]}}
    assert checks.d_allowed_changes(["elements"], ev, None, {})[0] == "undecided"


@pytest.mark.parametrize("first", [False, True])
def test_empty_run_is_inconclusive_even_with_a_baseline(first):
    result = gate({"first_analysis": first}, [], [], {}, {"cases": [{"case_id": "save", "verdict": "PASS"}]})
    assert result["status"] == "INCONCLUSIVE"


def failed(kind):
    return {"case_id": "save", "verdict": "FAIL", "tags": ["known-bug"], "cause": f"must:{kind}",
            "items": [{"kind": kind, "level": "must", "status": "fail", "spec": True, "detail": kind}]}


def test_same_expected_failure_can_stay_nonblocking():
    bug = failed("no_errors")
    assert gate({"new_code": True}, [bug], [], {}, {"cases": [bug]})["status"] == "PASSED"


def test_known_console_failure_does_not_hide_new_state_failure():
    before, after = failed("no_errors"), failed("state")
    baseline = {"cases": [before]}
    assert gate({"new_code": True}, [after], [], {}, baseline)["status"] == "FAILED"
    assert case_changes([after], baseline)["regressions"] == ["save"]


def test_stale_known_bug_tag_does_not_hide_regression_after_passing():
    baseline = {"cases": [{"case_id": "save", "verdict": "PASS", "tags": ["known-bug"]}]}
    assert gate({"new_code": True}, [failed("state")], [], {}, baseline)["status"] == "FAILED"


def test_same_bug_keeps_its_signature_across_runs():
    """Run-specific values (generated ids, counts) in the spec and the detail must not make an old bug look new."""
    def run(uid):
        return {"case_id": "save", "verdict": "FAIL", "tags": ["known-bug"], "cause": "must:state",
                "items": [{"kind": "state", "level": "must", "status": "fail", "spec": {"c": {"contains": {"body": uid}}},
                           "detail": f"closest record body = {uid[:4]}"}]}
    main, pr = run("QA words 1a2b3c"), run("QA words 9f8e7d")
    assert gate({"new_code": True}, [pr], [], {}, {"cases": [main]})["status"] == "PASSED"
    assert case_changes([{**pr, "tags": []}], {"cases": [main]})["regressions"] == []


def test_a_new_known_bug_journey_does_not_block_the_pull_request_that_adds_it():
    bug = failed("state")
    assert gate({"new_code": True}, [bug], [], {}, {"cases": [], "dataset": []})["status"] == "PASSED"


def test_known_bug_failing_at_another_step_is_a_regression():
    def at(step):
        return {"case_id": "save", "verdict": "FAIL", "tags": ["known-bug"], "cause": "step_failed",
                "steps": [{"i": i, "ok": i != step} for i in range(step + 1)], "items": []}
    assert gate({"new_code": True}, [at(3)], [], {}, {"cases": [at(1)]})["status"] == "FAILED"


def test_an_unmeasured_dimension_has_no_rating_and_cannot_pass_a_rating_condition():
    from assay.quality import measured, ratings
    m = measured([{"verdict": "PASS", "items": [{"status": "pass", "dimension": "functional"}]}],
                 [{"sensors": {}, "audit": {"a11y": {"violations": []}, "overflowX": False}}], [])
    r = ratings([], measured_dims=m)
    assert r["functional"] == "A" and r["accessibility"] == "A" and r["ai"] is None and r["security"] is None
    g = gate({"min_rating": {"ai": "B"}}, [{"case_id": "x", "verdict": "PASS"}], [], r)
    assert g["status"] == "INCONCLUSIVE" and any(c["actual"] == "not measured" for c in g["conditions"])


def test_a_failed_axe_run_does_not_measure_accessibility():
    from assay.quality import measured
    assert "accessibility" not in measured([], [{"sensors": {}, "audit": {"a11y": {"error": "axe crashed"}}}], [])


def test_a_probe_error_only_undecides_the_checks_that_need_it():
    ev = {"state_before": {"comments": [], "theme": "light"}, "state_after": {"comments": [{"id": 1}]},
          "probe_errors_after": {"theme": "read failed"}}
    assert checks.d_changed(["comments"], ev, None, {})[0] == "pass"
    assert checks.d_unchanged(["theme"], ev, None, {})[0] == "undecided"
    assert checks.d_allowed_changes(["comments"], ev, None, {})[0] == "undecided"


def test_a_scan_without_journeys_is_not_inconclusive():
    assert gate({"fail_on_case_fail": False, "min_cases": 0}, [], [], {})["status"] == "PASSED"
