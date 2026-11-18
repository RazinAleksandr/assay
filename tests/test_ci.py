"""CI integration: context detection, new-code gate, server round trip (upload → reference → pages)."""
import io
import json
import tarfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from assay.ci import Server, comment_markdown, detect_context
from assay.model import DIMENSIONS
from assay.quality import gate
from assay.server import Store, make_handler


def test_github_pull_request_context(tmp_path):
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps({"pull_request": {"number": 7, "title": "Fix search", "head": {"sha": "abc123"}},
                              "repository": {"default_branch": "main"}}))
    ctx = detect_context({"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF": "refs/pull/7/merge",
                          "GITHUB_HEAD_REF": "fix/search", "GITHUB_BASE_REF": "main", "GITHUB_SHA": "merge0",
                          "GITHUB_REPOSITORY": "org/app", "GITHUB_EVENT_PATH": str(ev), "GITHUB_RUN_ID": "42"})
    assert (ctx["provider"], ctx["pr"], ctx["branch"], ctx["target"], ctx["sha"]) == ("github", "7", "fix/search", "main", "abc123")
    assert ctx["pr_title"] == "Fix search" and ctx["run_url"].endswith("/org/app/actions/runs/42")


def test_gitlab_contexts():
    mr = detect_context({"GITLAB_CI": "true", "CI_MERGE_REQUEST_IID": "12", "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME": "feat",
                         "CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "develop", "CI_COMMIT_SHA": "s1", "CI_DEFAULT_BRANCH": "main"})
    assert (mr["pr"], mr["branch"], mr["target"]) == ("12", "feat", "develop")
    push = detect_context({"GITLAB_CI": "true", "CI_COMMIT_REF_NAME": "main", "CI_COMMIT_SHA": "s2"})
    assert (push["pr"], push["branch"]) == (None, "main")


def _res(cid, v):
    return {"case_id": cid, "verdict": v}


def test_new_code_gate_blocks_only_new_failures():
    base = {"cases": [_res("a", "FAIL"), _res("b", "PASS")]}
    cfg = {"fail_on_case_fail": True, "new_code": True}
    old_bug_only = gate(cfg, [_res("a", "FAIL"), _res("b", "PASS")], [], {}, baseline=base)
    assert old_bug_only["status"] == "PASSED"
    regression = gate(cfg, [_res("a", "FAIL"), _res("b", "FAIL")], [], {}, baseline=base)
    assert regression["status"] == "FAILED" and regression["conditions"][0]["actual"] == 1
    # on a branch (new_code off) every failing journey counts
    assert gate({"fail_on_case_fail": True}, [_res("a", "FAIL")], [], {}, baseline=base)["status"] == "FAILED"



def test_first_pull_request_lists_problems_but_blocks_nothing():
    """The pull request that connects Assay has nothing on main to compare with: old bugs must not make it red."""
    cfg = {"fail_on_case_fail": True, "new_code": True, "first_analysis": True, "max_new": {"critical": 0}}
    g = gate(cfg, [_res("a", "FAIL"), _res("b", "PASS")], [], {}, baseline=None)
    assert g["status"] == "PASSED" and g["first_analysis"] and all(c["soft"] for c in g["conditions"])
    assert not g["conditions"][0]["ok"]          # still shown, as a warning
    a = _analysis()
    a["gate"] = g
    a["cases"] = [{**a["cases"][0], "case_id": "a", "title": "Checkout", "verdict": "FAIL", "summary": "state: total is 0",
                   "failure": {"kind": "state", "phrase": "the database has `orders.total` = 0, expected 42"}}]
    text = comment_markdown(a, None, {"pr": "1", "target": "main"}, "demo")
    assert "✅ Ship it: nothing blocks yet (first analysis)" in text
    assert "Checkout: the database has `orders.total` = 0, expected 42" in text and "**From you:** look" in text
    # every other gate stays strict
    assert gate({"fail_on_case_fail": True}, [_res("a", "FAIL")], [], {})["status"] == "FAILED"


def test_known_bugs_are_listed_but_never_block():
    """A journey tagged known-bug encodes a bug nobody fixed yet: an expected failure, like pytest's xfail."""
    from assay.model import Issue
    bug = {**_res("cart.total", "FAIL"), "tags": ["known-bug"]}
    issue = Issue(rule="CASE-FAIL", title="Journey failed", dimension="functional", severity="critical", where="case:cart.total",
                  message="", evidence="", sources=["cart.total"], key="")
    issue.new = True
    cfg = {"fail_on_case_fail": True, "max_new": {"critical": 0}}
    g = gate(cfg, [bug, _res("b", "PASS")], [issue], {})
    assert g["status"] == "PASSED", g
    # an untagged failure still blocks
    assert gate(cfg, [_res("cart.total", "FAIL")], [issue], {})["status"] == "FAILED"
    a = _analysis()
    a["cases"] = [{**a["cases"][0], "case_id": "cart.total", "verdict": "FAIL", "tags": ["known-bug"]},
                  {**a["cases"][0], "case_id": "x", "title": "Old bug", "verdict": "PASS", "tags": ["known-bug"]}]
    a["known_bugs"] = ["cart.total"]
    text = comment_markdown(a, None, {"branch": "main"}, "demo")
    assert "✅ Ship it" in text and "Known bugs, still failing" in text and "`cart.total`" in text
    assert "Known bugs that now pass" in text and "(`x`)" in text

def _analysis(status="PASSED", branch="main", sha="s" * 40):
    return {"app": {"name": "demo", "title": "Demo", "base_url": "http://x"}, "run": {"id": "r1", "started": "2026-11-12T10:00:00"},
            "ci": {"branch": branch, "sha": sha}, "generated": "now", "baseline": None,
            "summary": {"cases": 1, "PASS": 1, "FAIL": 0, "INCONCLUSIVE": 0, "issues": 0, "new_issues": 0, "by_severity": {},
                        "pages_scanned": 0, "routes_scanned": 0},
            "gate": {"status": status, "conditions": []}, "ratings": {d: "A" for d in DIMENSIONS}, "ratings_new": None, "dimensions": {},
            "cases": [{"case_id": "c", "title": "C", "verdict": "PASS", "cause": None, "owner": None, "summary": "", "items": [],
                       "score": 1, "duration_s": 1.0, "severity": "major", "dimension": "functional", "steps": []}],
            "issues": [], "pages": [], "coverage": None, "changes": {}, "judge": {"backend": None, "model": None, "calls": 0}}


def test_server_round_trip(tmp_path):
    store = Store(tmp_path / "data")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, "tok", None, {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        run = tmp_path / "run"
        (run / "cases/c").mkdir(parents=True)
        (run / "analysis.json").write_text(json.dumps(_analysis()))
        (run / "cases/c/final.png").write_bytes(b"\x89PNG")
        assert Server(host, "tok").reference("demo", "main") is None
        res = Server(host, "tok").upload(run, {"project": "demo", "title": "Demo", "branch": "main", "sha": "s" * 40})
        assert res["url"].endswith(f"/projects/demo?branch=main&analysis={res['id']}") and res["status"] == "PASSED"
        ref = Server(host, "tok").reference("demo", "main")
        assert ref["server"]["id"] == res["id"] and ref["ci"]["branch"] == "main"
        import urllib.request
        for path in ("/", "/p/demo", f"/a/{res['id']}/", f"/a/{res['id']}/cases/c/final.png", "/api/projects/demo/badge.svg"):
            with urllib.request.urlopen(host + path) as r:
                assert r.status == 200, path
        try:
            Server(host, "wrong").upload(run, {"project": "demo"})
            raise AssertionError("upload without the right token must fail")
        except RuntimeError as e:
            assert "401" in str(e)
    finally:
        httpd.shutdown()


def test_pr_comment_says_the_verdict_in_four_lines():
    """A person answers "ship it or not, and why" from the first four lines: no condition tables, no engine terms."""
    a = _analysis(status="FAILED")
    a["baseline"] = "main @ abc1234"
    a["changes"] = {"regressions": ["c"], "fixed": []}
    a["scope"] = {"mode": "auto", "checked": 12, "total": 140}
    a["run"]["finished"] = "2026-11-12T10:03:00"
    a["gate"]["conditions"] = [{"id": "new_failed_journeys", "name": "New failed journeys", "actual": 1, "op": "=",
                                "threshold": 0, "ok": False}]
    a["cases"][0].update(verdict="FAIL", title="Mila comments on a post", failure={
        "kind": "state", "phrase": "`comments.body` was saved cut short in the database: 20 of 90 characters"})
    text = comment_markdown(a, "http://assay/a/9/", {"pr": "3", "target": "main", "sha": "f" * 40}, "demo",
                            brief_url="http://assay/api/analyses/9/brief")
    head = text.split("<details>")[0].strip().splitlines()
    assert head[0] == "<!-- assay-quality-gate:demo -->" and len(head) == 5
    assert head[1] == "### ❌ Don't ship: 1 journey broke"
    assert head[2] == "Mila comments on a post: `comments.body` was saved cut short in the database: 20 of 90 characters."
    assert head[3].startswith("**From you:** nothing, your agent can fix it from the brief.") and "http://assay/a/9/" in head[3]
    assert head[4] == "<sub>Checked 12 of 140 journeys (touched by this change) · 3 min · compared with `main @ abc1234`</sub>"
    for word in ("Condition", "issues", "critical", "rating", "INCONCLUSIVE"):
        assert word not in "\n".join(head), word


def test_pages_survive_odd_uploads(tmp_path):
    """Review findings: bad timestamps, null messages, odd branch names, a branch-less analysis, markup in metadata."""
    import urllib.request
    from urllib.parse import quote
    store = Store(tmp_path / "data")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, "tok", None, {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        run = tmp_path / "run"
        (run / "cases/c").mkdir(parents=True)
        a = _analysis()
        a["issues"] = [{"id": "i1", "rule": "CASE-FAIL", "title": "Broken", "message": None, "where": None, "severity": "major",
                        "dimension": "functional", "new": True, "fingerprint": "f1", "evidence": {}}]
        (run / "analysis.json").write_text(json.dumps(a))
        up = lambda meta: Server(host, "tok").upload(run, {"project": "odd", "title": "Odd", "sha": "s" * 40, **meta})
        up({"branch": "main", "created": "12/11/2026", "default_branch": "<img src=x onerror=alert(1)>"})
        up({"branch": "feat+x"})
        up({"branch": None})
        pages = ["/", "/projects/odd", "/projects/odd/journeys", "/projects/odd/pull-requests", "/projects/odd/branches",
                 "/projects/odd/journeys/c/", "/projects/odd?branch=" + quote("feat+x"), "/docs"]
        for path in pages:
            with urllib.request.urlopen(host + path) as r:
                body = r.read().decode()
                assert r.status == 200, path
                assert "<img src=x" not in body, path
        with urllib.request.urlopen(host + "/projects/odd/branches") as r:
            assert "branch=feat%2Bx" in r.read().decode()
    finally:
        httpd.shutdown()


def test_analysis_cache_is_per_data_folder(tmp_path):
    from assay.web.pages import analysis
    s1, s2 = Store(tmp_path / "one"), Store(tmp_path / "two")
    for st, title in ((s1, "first"), (s2, "second")):
        st.dir(1).mkdir(parents=True)
        (st.dir(1) / "analysis.json").write_text(json.dumps({"app": {"title": title}}))
    assert analysis(s1, 1)["app"]["title"] == "first" and analysis(s2, 1)["app"]["title"] == "second"


def test_times_from_a_utc_runner_read_as_local(monkeypatch):
    """A GitHub runner stamps UTC; the server and its readers may be in any zone."""
    import os
    import time
    from datetime import datetime, timedelta, timezone
    from assay.server import utc_iso
    from assay.web.ui import ago
    monkeypatch.setenv("TZ", "Europe/Moscow")
    time.tzset()
    try:
        five_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
        assert ago(five_min_ago) == "5 min ago"
        assert utc_iso("2026-11-12T18:00:00+03:00") == "2026-11-12T15:00:00+00:00"
        assert utc_iso("2026-11-12T18:00:00") == "2026-11-12T15:00:00+00:00"     # naive = this server's local time
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
