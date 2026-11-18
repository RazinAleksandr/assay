"""Unit tests for the pure parts of the engine: diff, matchers, verdict rule, ratings, gate, stats."""
from pathlib import Path

from assay import checks as C
from assay.bench import stats
from assay.grade import grade_case
from assay.model import Issue
from assay.probes import diff, get_path, path_matches
from assay.quality import gate, rating
from assay.rules import route_of


# ---- state diff -----------------------------------------------------------------

def test_diff_keys_lists_by_id_so_inserts_do_not_shift():
    before = {"posts": [{"id": 1, "c": "a"}, {"id": 2, "c": "b"}]}
    after = {"posts": [{"id": 0, "c": "new"}, {"id": 1, "c": "a"}, {"id": 2, "c": "B"}]}
    assert diff(before, after) == ["posts.id=0", "posts.id=2.c"]


def test_allowed_changes_patterns():
    assert path_matches("posts.id=2.c", ["posts"])
    assert path_matches("posts[len]", ["posts"])
    assert path_matches("users.id=u1.appearance", ["users.id=u1.appearance"])
    assert not path_matches("users.id=u2.appearance", ["users.id=u1.appearance"])
    assert path_matches("posts.id=2.updated_at", ["*.updated_at"])


def test_get_path_plucks_and_indexes():
    data = {"posts": [{"id": 1, "t": ["x"]}, {"id": 2, "t": []}]}
    assert get_path(data, "posts.id") == [1, 2]
    assert get_path(data, "posts.-1.id") == 2
    assert get_path(data, "posts.length") == 2


# ---- matchers ---------------------------------------------------------------------

def test_matchers():
    assert C.match([{"a": 1, "b": 2}], {"contains": {"a": 1}})[0]
    assert C.match("Hello World", {"contains": "world"})[0]
    assert C.match([1, 2, 3], {"len": 3})[0]
    assert C.match([1, 2, 3], {"delta": 1}, before=[1, 2])[0]
    assert not C.match([1, 2], {"delta": 1}, before=[1, 2])[0]
    assert C.match([{"n": "Slow Start"}], {"contains": {"n": "~slow"}})[0]
    assert C.match([{"x": 1}, {"x": 2}], {"includes_all": [{"x": 2}, {"x": 1}]})[0]
    assert not C.match(5, {"gt": 5})[0]


def test_allowed_changes_decider_flags_side_effects():
    ev = {"state_before": {"posts": [{"id": 1}], "members": []},
          "state_after": {"posts": [{"id": 1}], "members": [{"key": "c:u3", "status": "pending"}]}}
    st, detail = C.d_allowed_changes([], ev, None, {"volatile": []})
    assert st == "fail" and "members" in detail
    st, _ = C.d_allowed_changes(["members"], ev, None, {"volatile": []})
    assert st == "pass"


def test_numbers_check():
    ev = {"answers": [{"answer": "RSI is 23.4 and close 22,734.70"}]}
    assert C.d_numbers([{"value": 23.33, "tolerance": 0.4}, {"value": 22734.67, "tolerance": 1}], ev, None, {})[0] == "pass"
    assert C.d_numbers([{"value": 50, "tolerance": 1}], ev, None, {})[0] == "fail"


# ---- verdict rule -------------------------------------------------------------------

def _ev(**kw):
    base = {"case_id": "c", "title": "t", "case": {"must": [{"state": {"p": 1}}], "should": [{"state": {"q": 1}}]},
            "state_before": {"p": 0, "q": 0}, "state_after": {"p": 1, "q": 0}, "steps": []}
    base.update(kw)
    return base


def test_should_never_changes_verdict():
    r = grade_case(_ev(), Path("."), volatile=[], judge=None)
    assert r.verdict == "PASS" and r.score == 0.5


def test_must_fail_is_fail_owned_by_product():
    r = grade_case(_ev(state_after={"p": 2, "q": 0}), Path("."), volatile=[], judge=None)
    assert r.verdict == "FAIL" and r.owner == "product"


def test_environment_is_inconclusive_not_fail():
    r = grade_case(_ev(cause="setup_failed", error="QA API down"), Path("."), volatile=[], judge=None)
    assert r.verdict == "INCONCLUSIVE" and r.owner == "environment"
    assert all(i.status == "skipped" for i in r.items)


def test_undecided_must_is_inconclusive():
    ev = _ev(case={"must": [{"judge": "looks right"}]})
    r = grade_case(ev, Path("."), volatile=[], judge=None)
    assert r.verdict == "INCONCLUSIVE" and r.owner == "judge"


def test_step_failure_is_fail():
    r = grade_case(_ev(cause="step_failed", error="button gone"), Path("."), volatile=[], judge=None)
    assert r.verdict == "FAIL"


# ---- quality model --------------------------------------------------------------------

def _issue(sev, dim="reliability", new=True):
    i = Issue(rule="R", title="t", dimension=dim, severity=sev, where="/")
    i.new = new
    return i


def test_rating_is_sonar_rule():
    assert rating([]) == "A"
    assert rating([_issue("info")]) == "A"
    assert rating([_issue("minor")]) == "B"
    assert rating([_issue("minor"), _issue("major")]) == "C"
    assert rating([_issue("critical")]) == "D"
    assert rating([_issue("blocker")]) == "E"


def test_gate_three_valued():
    cfg = {"fail_on_case_fail": True, "max_new": {"critical": 0}, "max_inconclusive": 0.2}
    ok = [{"verdict": "PASS"}] * 9 + [{"verdict": "INCONCLUSIVE"}]
    assert gate(cfg, ok, [], {})["status"] == "PASSED"
    assert gate(cfg, [{"verdict": "PASS"}, {"verdict": "INCONCLUSIVE"}], [], {})["status"] == "INCONCLUSIVE"
    assert gate(cfg, ok, [_issue("critical")], {}, baseline={"cases": []})["status"] == "FAILED"
    assert gate(cfg, ok, [_issue("critical")], {})["status"] == "FAILED"          # no server, no reference: fail closed
    first = gate({**cfg, "no_reference": True}, ok, [_issue("critical")], {})   # the branch's first analysis: the baseline
    assert first["status"] == "PASSED" and any(c["id"] == "new_issues:critical" and c["baseline"] for c in first["conditions"])
    assert gate({**cfg, "no_reference": True}, ok[:9] + [{"verdict": "FAIL"}], [_issue("critical")], {})["status"] == "FAILED"
    assert gate(cfg, ok, [_issue("critical", new=False)], {})["status"] == "PASSED"


def test_route_patterns():
    assert route_of("http://x/p/demo-post-12") == "/p/:id"
    assert route_of("http://x/u/mila.chen") == "/u/mila.chen"
    assert route_of("http://x/items/42?x=1") == "/items/:id"


# ---- benchmark statistics --------------------------------------------------------------

def test_bench_drops_inconclusive_in_all_arms_and_finds_flaky():
    v = {"A": {"c1": ["PASS", "PASS", "INCONCLUSIVE"], "c2": ["PASS", "FAIL", "PASS"]},
         "B": {"c1": ["PASS", "FAIL", "PASS"], "c2": ["PASS", "PASS", "PASS"]}}
    s = stats(v)
    assert s["arms"]["A"]["flaky"] == ["c2"]
    # c1 attempt 3 dropped for both arms → B's c1 = 1/2
    assert s["arms"]["B"]["pass@1"] == round((0.5 + 1.0) / 2, 3)


# ---- goal agent without a model ---------------------------------------------------------

def test_goal_agent_without_model_is_inconclusive_not_a_product_fail():
    r = grade_case(_ev(cause="agent_unavailable", error="cached step 3 failed and no model to re-plan"), Path("."),
                   volatile=[], judge=None)
    assert r.verdict == "INCONCLUSIVE" and r.owner == "environment"


def test_cached_trajectory_tolerates_relative_times_and_counters():
    from assay.agent import _normalise, relaxed
    step = {"click": {"role": "link", "name": "Leo Martin A perfect plan. See you Saturday ☀️ 23h 2", "exact": True}}
    assert relaxed(step) == {"click": {"role": "link", "name": "Leo Martin A perfect plan. See you Saturday ☀️", "exact": False}}
    assert _normalise([step])[0]["click"]["name"] == "Leo Martin A perfect plan. See you Saturday ☀️"
    # a number that is part of the name stays in the cache; plain names are left alone
    keep = {"click": {"role": "button", "name": "Add 2 items", "exact": True}}
    assert _normalise([keep]) == [keep] and relaxed({"click": {"role": "button", "name": "Send message"}}) is None
