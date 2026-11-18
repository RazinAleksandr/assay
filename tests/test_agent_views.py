"""The agent level of the project pages (assay/web/agent.py): every fact of a page as tables, the same tables as data.
Synthetic history: four analyses of main (a flaky journey, one broken since the first), two pull requests (one analysed
twice), and an analysis from before the dataset, the journey map, the scope and structured failures."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from assay.ci import Server
from assay.server import Store, make_handler
from assay.web import agent as A
from assay.web import pages as W

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
PUBLIC = "http://assay.test"
EXPECTED = "QA kind words 1a2b: the light in the second frame is lovely, where was this taken?"
FAILURE = {"kind": "state", "probe": "comments", "path": "comments", "store": "database", "record_is_new": True,
           "check": {"level": "must", "index": 0, "kind": "state", "detail": "comments: the new record: body = …"},
           "phrase": "`comments.body` was saved cut short in the database: 18 of 82 characters",
           "fields": {"body": {"expected": EXPECTED, "actual": EXPECTED[:18]}},
           "step": {"i": 3, "action": "wait_for", "error": "nothing matched within 15 s"}}
SUSPECT = {"file": "server/posts.ts", "lines": "384-391", "score": 10, "why": "changes code that writes `body`",
           "hunk": "@@ -384,6 +384,8 @@\n+    const preview = d.body.slice(0, 20);\n-        d.body,\n+        preview,"}
EVIDENCE = {"sensors": {"console": [{"type": "error", "text": "boom"}, {"type": "warning", "text": "meh"}], "page_errors": [],
                        "failed_requests": [{"url": "/api/x"}], "bad_responses": [], "requests": 40},
            "audit": {"a11y": {"violations": [{"id": "color-contrast"}, {"id": "link-name"}]}, "vitals": {"lcp": 210, "cls": 0.02}}}
DATASET = [{"id": "comment.add", "title": "Comment <script>alert(1)</script>", "source": "cases/core.yaml", "tags": ["posts"],
            "must": [{"state": {}}], "should": []},
           {"id": "post.like", "title": "Like", "source": "cases/core.yaml", "critical": True},
           {"id": "feed.load", "title": "Feed", "source": "cases/feed.yaml"},
           {"id": "search.find", "title": "Search", "group": "discovery", "source": "cases/core.yaml"},
           {"id": "admin.audit", "title": "Audit log", "source": "cases/admin.yaml", "ai": True}]
JMAP = {"comment.add": {"routes": ["GET /api/posts/12", "POST /api/posts/12/comments", "POST /api/posts/34/comments"],
                        "tables": ["comments", "notifications"], "files": ["web/a.tsx", "web/b.tsx"], "pages": ["/p/:id"]}}


def case(cid, verdict, duration=2.0, **kw):
    return {"case_id": cid, "title": cid, "verdict": verdict, "cause": "must:state" if verdict == "FAIL" else None,
            "summary": "", "items": [{"kind": "state", "level": "must", "status": "pass" if verdict == "PASS" else "fail",
                                      "spec": {}, "detail": "comments: wrong" if verdict == "FAIL" else "", "by": "code"}],
            "duration_s": duration, "severity": "major", "browser": "chromium", "viewport": "desktop",
            "screenshot": f"cases/{cid}/final.png",
            "steps": [{"i": 0, "action": "click", "summary": "Post", "ok": True, "screenshot": "step-00.png"}], **kw}


def analysis(cases, status="PASSED", conds=(), **extra):
    n = {v: sum(c["verdict"] == v for c in cases) for v in ("PASS", "FAIL", "INCONCLUSIVE")}
    return {"app": {"name": "morrow"}, "run": {"id": "r", "started": "2026-11-12T10:00:00", "finished": "2026-11-12T10:01:30"},
            "ci": {}, "generated": "now", "baseline": None,
            "summary": {"cases": len(cases), **n, "issues": 0, "new_issues": 0, "by_severity": {}, "pages_scanned": 0,
                        "routes_scanned": 0},
            "gate": {"status": status, "conditions": list(conds)}, "ratings": {}, "dimensions": {},
            "cases": cases, "issues": [], "pages": [], "changes": {}, **extra}


def upload(srv, tmp: Path, name: str, a: dict, evidence: dict | None = None, **meta) -> int:
    run = tmp / name
    for c in a["cases"]:
        d = run / "cases" / c["case_id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "final.png").write_bytes(PNG)
        (d / "evidence.json").write_text(json.dumps((evidence or {}).get(c["case_id"], {})))
    (run / "analysis.json").write_text(json.dumps(a))
    return srv.upload(run, {"project": "gh/acme/morrow", "title": "Morrow", **meta})["id"]   # meta may name another project


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("agent")
    store = Store(tmp / "data")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, "tok", None, {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{httpd.server_address[1]}"
    srv = Server(host, "tok")
    gate = [{"id": "failed_journeys", "name": "Failed journeys", "actual": 1, "op": "=", "threshold": 0, "ok": False}]
    like = ["PASS", "FAIL", "PASS", "FAIL"]                     # flips three times: flaky
    mains = []
    for i in range(4):
        cs = [case("comment.add", "PASS", 2.0 + i / 10), case("post.like", like[i], 1.0),
              case("feed.load", "FAIL", 3.0, failure={"phrase": "`feed` is empty", "check": {"kind": "state", "level": "must"}})]
        if i >= 2:
            cs.append(case("search.find", "PASS", 1.5))
        a = analysis(cs, "FAILED", gate, dataset=DATASET, journey_map=JMAP,
                     measured={"functional": ["journeys"], "security": ["cookie flags and tokens in storage"]},
                     screens={"scanned": 3, "uncovered": ["/admin", "/settings/billing"]})
        mains.append(upload(srv, tmp, f"main{i}", a, branch="main", sha=f"{i}" * 40, created=f"2026-11-0{i + 1}T10:00:00+00:00"))
    pr_gate = [{"id": "new_failed_journeys", "name": "New failed journeys", "actual": 1, "op": "=", "threshold": 0, "ok": False},
               {"id": "new_issues:critical", "name": "New critical issues", "actual": 0, "op": "≤", "threshold": 0, "ok": True}]
    issues = [{"rule": "CASE-FAIL", "severity": "major", "new": True, "where": "case:comment.add", "sources": ["comment.add"]},
              {"rule": "A11Y-link-name", "severity": "minor", "new": False, "where": "/", "message": "2 element(s): a"},
              {"rule": "A11Y-color-contrast", "severity": "major", "new": True, "where": "/p/:id", "dimension": "accessibility",
               "message": "3 element(s): .x", "sources": ["comment.add"], "count": 1}]
    pr = analysis([case("comment.add", "FAIL", 9.5, failure=FAILURE, suspects=[SUSPECT, {**SUSPECT, "lines": "393-399", "score": 4}]),
                   case("feed.load", "FAIL", 3.1), case("post.like", "PASS", 1.2)], "FAILED", pr_gate,
                  baseline="main @ 3333333", changes={"regressions": ["comment.add"], "fixed": []}, dataset=DATASET,
                  journey_map=JMAP, issues=issues,
                  scope={"mode": "auto", "checked": 3, "total": 5, "why": "1 touched by this change, 2 sampled",
                         "reasons": {"comment.add": "calls `POST /posts/:id/comments`, changed in `server/posts.ts`",
                                     "feed.load": "failed on the target branch", "post.like": "<b>sampled</b>"}},
                  weakened={"items": [{"case_id": "post.like", "phrase": "removed 1 `state` check from `post.like`"}],
                            "approved": False, "blocking": True})
    pr7 = upload(srv, tmp, "pr7", pr, {"comment.add": EVIDENCE}, branch="feat/comments", pr="7", pr_title="Comment previews",
                 target="main", sha="b" * 40, reference_id=mains[-1], created="2026-11-05T10:00:00+00:00")
    upload(srv, tmp, "pr8a", analysis([case("post.like", "FAIL")], "FAILED", pr_gate, changes={"regressions": ["post.like"]}),
           branch="fix/like", pr="8", pr_title="Like fix", target="main", sha="c" * 40, created="2026-11-05T11:00:00+00:00")
    pr8 = upload(srv, tmp, "pr8b", analysis([case("post.like", "PASS"), case("search.find", "PASS")], "PASSED", pr_gate[1:]),
                 branch="fix/like", pr="8", pr_title="Like fix", target="main", sha="d" * 40, created="2026-11-05T12:00:00+00:00")
    yield {"host": host, "store": store, "mains": mains, "pr7": pr7, "pr8": pr8, "srv": srv, "tmp": tmp}
    httpd.shutdown()


def ctx(env, key="gh/acme/morrow", **q):
    return W.resolve(env["store"], key, {**q, "view": "agent"}, PUBLIC)


def test_pull_request_lists_every_failure_suspect_and_selection(env):
    c = ctx(env, pr="7")
    d = A.pr_data(c)
    json.dumps(d)                                  # JSON as it is: no default= needed for the export
    f, t = d["facts"], d["tables"]
    assert (f["verdict"], f["gate"], f["gate_failed"]) == ("no_ship", "FAILED", ["new_failed_journeys"])
    assert (f["checked"], f["total"], f["broke"], f["pass"], f["fail"]) == (3, 5, ["comment.add"], 1, 2)
    assert f["reference"] == "main@3333333" and f["reference_id"] == env["mains"][-1] and f["duration_s"] == 90
    assert f["links"]["brief"] == f"{PUBLIC}/api/analyses/{env['pr7']}/brief"

    new, old = t["failures"]
    assert (new["journey"], new["status"], new["step"], new["check"], new["paths"]) == ("comment.add", "new", 3, "state", ["comments.body"])
    assert (new["expected"], new["actual"]) == (EXPECTED, EXPECTED[:18])
    assert new["signature"] == "checks 0:state(comments)"
    assert new["screenshot"] == f"{PUBLIC}/a/{env['pr7']}/cases/comment.add/final.png"
    assert new["brief"].endswith(f"/api/analyses/{env['pr7']}/brief?case=comment.add&full=1")
    assert (old["journey"], old["status"]) == ("feed.load", "on_target")

    assert [(s["rank"], s["lines"], s["score"]) for s in t["suspects"]] == [(1, "384-391", 10), (2, "393-399", 4)]
    sel = {r["journey"]: r for r in t["selection"]}
    assert set(sel) == {d["id"] for d in DATASET} and [r["ran"] for r in t["selection"]] == [True] * 3 + [False] * 2
    assert sel["admin.audit"]["reason"] is None and sel["feed.load"]["reason"] == "failed on the target branch"
    assert sel["search.find"]["group"] == "discovery" and sel["feed.load"]["group"] == "feed"

    j = {r["journey"]: r for r in t["journeys"]}
    assert (j["comment.add"]["ref_duration_s"], j["comment.add"]["delta_s"]) == (2.3, 7.2)
    assert (j["comment.add"]["console_errors"], j["comment.add"]["failed_requests"], j["comment.add"]["a11y"]) == (1, 1, 2)
    assert j["comment.add"]["lcp_ms"] == 210 and j["feed.load"]["console_errors"] is None      # no evidence recorded
    assert j["post.like"]["flaky_on_target"] is True and j["comment.add"]["flaky_on_target"] is False
    assert [i["rule"] for i in t["issues"]] == ["A11Y-color-contrast", "A11Y-link-name"]     # new first, no CASE-FAIL
    assert t["weakened"] == [{"journey": "post.like", "change": "removed 1 `state` check from `post.like`"}]

    html = A.pr_view(c)
    assert html.startswith('<div class="page agent">')
    for want in ('id="t-failures"', 'id="t-suspects"', 'id="t-selection"', 'id="t-journeys"', 'id="t-issues"', 'id="t-weakened"',
                 f"assay brief {PUBLIC}/api/analyses/{env['pr7']}/brief", "view=agent&amp;format=csv&amp;table=suspects",
                 "<pre class=hunk>", "preview = d.body.slice(0, 20)", "server/posts.ts:384-391", "+7.2", "&lt;b&gt;sampled&lt;/b&gt;"):
        assert want in html, want
    assert "<b>sampled" not in html
    assert "journeys/admin.audit" not in html and "journeys/comment.add?pr=7" in html     # only journeys that ran have a page


def test_health_has_a_matrix_of_the_history_and_the_open_problems(env):
    c = ctx(env, branch="main")
    d = A.health_data(c)
    json.dumps(d)
    f, t = d["facts"], d["tables"]
    m1, m4 = env["mains"][0], env["mains"][-1]
    assert (f["journeys"], f["ran"], f["kept"], f["broken"], f["flaky"], f["not_checked"], f["history"]) == (5, 4, 2, 2, 1, 1, 4)
    assert f["p50_journey_s"] == 1.5 and f["uncovered_screens"] == 2
    mx = {r["journey"]: r for r in t["matrix"]}
    assert [r["journey"] for r in t["matrix"]][:2] == ["feed.load", "post.like"]           # failing now first
    assert [mx["post.like"][f"#{i}"] for i in env["mains"]] == ["PASS", "FAIL", "PASS", "FAIL"]
    assert (mx["post.like"]["flips"], mx["post.like"]["pass_rate"]) == (3, 0.5)
    assert mx["search.find"][f"#{m1}"] is None and mx["search.find"]["runs"] == 2 and mx["admin.audit"]["runs"] == 0
    probs = {p["journey"]: p for p in t["problems"]}
    assert probs["feed.load"]["since"].startswith("2026-11-01") and probs["feed.load"]["fails"] == 4
    assert probs["post.like"]["flaky"] is True and probs["feed.load"]["phrase"] == "`feed` is empty"
    assert [r["analysis"] for r in t["runs"]] == env["mains"] and t["runs"][0]["pass_rate"] == 0.667
    assert {r["dimension"]: r["measured"] for r in t["measured"]}["functional"] is True
    assert t["uncovered"] == [{"screen": "/admin"}, {"screen": "/settings/billing"}]
    mp = {r["journey"]: r for r in t["map"]}
    assert (mp["comment.add"]["routes"], mp["comment.add"]["writes"], mp["comment.add"]["tables"]) == (2, 1, 2)
    assert mp["comment.add"]["write_routes"] == ["POST /api/posts/:id/comments"] and mp["feed.load"]["routes"] is None

    html = A.health_view(c)
    assert html.count("<svg class=chart") == 2
    assert f"journeys/post.like?branch=main&amp;analysis={m4}" in html and 'class="FAIL"' in html
    for want in ('id="t-matrix"', 'id="t-problems"', 'id="t-map"', 'id="t-uncovered"', "/settings/billing", "assay brief "):
        assert want in html, want
    # an older analysis: the history stops there
    old = A.health_data(ctx(env, analysis=str(env["mains"][1])))
    assert len(old["tables"]["runs"]) == 2 and f"#{m4}" not in old["tables"]["matrix"][0]


def test_journeys_table_covers_the_whole_dataset_with_groups(env):
    d = A.journeys_data(ctx(env, branch="main"))
    json.dumps(d)
    rows = {r["journey"]: r for r in d["tables"]["journeys"]}
    assert list(rows) == [x["id"] for x in DATASET]
    assert rows["comment.add"]["group"] == "core" and rows["search.find"]["group"] == "discovery"
    assert rows["comment.add"]["prs_selected"] == 1 and rows["post.like"]["prs_selected"] == 2   # latest analysis per PR
    assert (rows["comment.add"]["p50_s"], rows["comment.add"]["p95_s"], rows["comment.add"]["must"]) == (2.1, 2.3, 1)
    assert rows["admin.audit"]["now"] is None and rows["admin.audit"]["ai"] is True and rows["admin.audit"]["routes"] is None
    groups = {g["group"]: g for g in d["tables"]["groups"]}
    assert groups["core"] == {"group": "core", "journeys": 2, "critical": 1, "ran": 2, "pass": 1, "broken": 1, "unsure": 0,
                              "time_s": 3.3, "pass_rate": 0.5}
    assert groups["admin"]["ran"] == 0 and groups["admin"]["pass_rate"] is None
    assert d["facts"]["prs_considered"] == 2 and d["facts"]["not_run"] == 1
    html = A.journeys_view(ctx(env, branch="main"))
    assert 'id="t-groups"' in html and "&lt;script&gt;" in html and "<script>" not in html


def test_pull_request_list_has_one_row_per_pull_request(env):
    d = A.prs_data(ctx(env))
    json.dumps(d)
    rows = d["tables"]["prs"]
    assert [r["pr"] for r in rows] == ["8", "7"]
    pr8, pr7 = rows
    assert (pr8["status"], pr8["analysis"], pr8["checked"], pr8["broke"]) == ("PASSED", env["pr8"], 2, 0)
    assert (pr7["top_suspect"], pr7["weakened"], pr7["broke"], pr7["new_issues"], pr7["duration_s"]) == ("server/posts.ts:384-391", 1, 1, 1, 90)
    assert pr7["page"] == f"{PUBLIC}/gh/acme/morrow?pr=7" and pr7["brief"] == f"{PUBLIC}/api/analyses/{env['pr7']}/brief"
    assert (d["facts"]["prs"], d["facts"]["failed"], d["facts"]["passed"]) == (2, 1, 1)
    html = A.prs_view(ctx(env))
    assert f'<a href="{PUBLIC}/gh/acme/morrow?pr=7">Comment previews</a>' in html and f'?pr=7">7</a>' in html


def test_old_analyses_and_missing_ones_render(env):
    """No dataset, journey map, scope, measured record, structured failure or suspects; and no analysis at all."""
    bare = analysis([case("legacy.flow", "FAIL"), case("legacy.ok", "PASS")], "FAILED")
    for meta in ({"branch": "legacy"}, {"branch": "old-pr", "pr": "99", "target": "legacy"}):
        upload(env["srv"], env["tmp"], "legacy" + meta.get("pr", ""), bare, project="gh/acme/legacy", **meta)
    for q, kinds in (({"branch": "legacy"}, ("health", "journeys", "prs")), ({"pr": "99"}, ("pr", "journeys"))):
        c = ctx(env, "gh/acme/legacy", **q)
        for kind in kinds:
            d = getattr(A, f"{kind}_data")(c)
            json.dumps(d)
            assert getattr(A, f"{kind}_view")(c).startswith('<div class="page agent">'), kind
    d = A.pr_data(ctx(env, "gh/acme/legacy", pr="99"))
    assert [r["journey"] for r in d["tables"]["failures"]] == ["legacy.flow"] and d["tables"]["suspects"] == []
    assert d["tables"]["failures"][0]["status"] == "broken" and d["facts"]["reference"] is None
    assert [r["ran"] for r in d["tables"]["selection"]] == [True, True]
    nothing = ctx(env, "gh/acme/legacy", branch="no-such-branch")
    assert nothing.a is None and "no analysis" in A.health_view(nothing) and A.health_data(nothing)["tables"]["matrix"] == []


def test_exports_are_the_page_data(env):
    def get(path):
        with urllib.request.urlopen(env["host"] + path) as r:
            return r.status, r.read().decode()
    st, body = get("/gh/acme/morrow?pr=7&view=agent&format=json")
    data = json.loads(body)
    assert st == 200 and set(data["tables"]) == set(A.pr_data(ctx(env, pr="7"))["tables"])
    assert data["tables"]["failures"][0]["journey"] == "comment.add"
    st, csv = get("/gh/acme/morrow?branch=main&view=agent&format=csv&table=matrix")
    head = csv.splitlines()[0].split(",")
    assert st == 200 and head[:3] == ["journey", "now", "pass_rate"] and f"#{env['mains'][0]}" in head
    st, page = get("/gh/acme/morrow/journeys?branch=main&view=agent")
    assert st == 200 and 'class="page agent"' in page and "agent.css" in page


def test_private_project_links_carry_the_view_key(env):
    with env["store"].db() as c:
        c.execute("update projects set visibility='private' where key='gh/acme/morrow'")
    try:
        cx = ctx(env, pr="7")
        k = cx.p["view_key"]
        row = A.pr_data(cx)["tables"]["failures"][0]
        assert row["screenshot"].endswith(f"final.png?k={k}") and f"k={k}" in row["brief"]
        assert all(f"k={k}" in r["brief"] for r in A.prs_data(cx)["tables"]["prs"])
    finally:
        with env["store"].db() as c:
            c.execute("update projects set visibility='public' where key='gh/acme/morrow'")
