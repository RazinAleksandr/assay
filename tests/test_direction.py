"""The human verdict, the agent brief, product → code, selection for pull requests, weakened checks, MCP."""
import json
import os
from pathlib import Path

import pytest

from assay import explain
from assay.brief import build, tokens
from assay.code import enclosing_routes, parse_diff, route_regex, suspects_for
from assay.guard import weakened
from assay.quality import gate
from assay.select import select

# a real analysis (Excalidraw, #34) kept outside the repository: $ASSAY_EXCALIDRAW, else docs/ next to the checkout
EXCALIDRAW = Path(os.environ.get("ASSAY_EXCALIDRAW")
                  or Path(__file__).resolve().parents[2] / "docs/Excalidraw-Analysis-34/data/analyses/34")
needs_excalidraw = pytest.mark.skipif(not EXCALIDRAW.exists(), reason="the Excalidraw analysis is not on this machine")

POSTS_TS = """import { Router } from "express";
export function postRoutes(s) {
  const r = Router();
  r.get("/posts/:id", (req, res) => res.json(getPost(req.params.id)));
  r.post("/posts/:id/comments", (req, res) => {
    const d = parse(req.body);
    const cid = id();
    const preview = d.body.length > 20 ? d.body.slice(0, 20) : d.body;
    s.run("INSERT INTO comments(id,post_id,body) VALUES(?,?,?)", cid, req.params.id, preview);
    res.json({ id: cid });
  });
  r.post("/users/:id/follow", (req, res) => follow(req));
  return r;
}
"""
DIFF = """diff --git a/server/posts.ts b/server/posts.ts
--- a/server/posts.ts
+++ b/server/posts.ts
@@ -6,3 +6,4 @@ export function postRoutes(s) {
     const d = parse(req.body);
     const cid = id();
-    s.run("INSERT INTO comments(id,post_id,body) VALUES(?,?,?)", cid, req.params.id, d.body);
+    const preview = d.body.length > 20 ? d.body.slice(0, 20) : d.body;
+    s.run("INSERT INTO comments(id,post_id,body) VALUES(?,?,?)", cid, req.params.id, preview);
diff --git a/web/src/Profile.tsx b/web/src/Profile.tsx
--- a/web/src/Profile.tsx
+++ b/web/src/Profile.tsx
@@ -1,1 +1,1 @@
-export const Title = () => <h1>Profile</h1>;
+export const Title = () => <h1>Your profile</h1>;
"""


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "server").mkdir()
    (tmp_path / "server/posts.ts").write_text(POSTS_TS)
    return tmp_path


# ---- phrases ----------------------------------------------------------------------------------------------------------------

def test_a_cut_short_value_is_said_as_such():
    ev = {"state_before": {"comments": []},
          "state_after": {"comments": [{"id": 9, "body": "QA kind words 1a2b3c"}]}}
    want = "QA kind words 1a2b3c: the light in the second frame is lovely, where was this taken?"
    r = {"case_id": "comment.add", "title": "Mila comments", "verdict": "FAIL", "cause": "must:state", "steps": [],
         "items": [{"kind": "state", "level": "must", "status": "fail", "spec": {"comments": {"contains": {"body": want}}}}]}
    f = explain.failure_of(r, ev, {"comments": {"sqlite": "x.db", "sql": "select id, body from comments"}})
    assert f["phrase"] == f"`comments.body` was saved cut short in the database: 20 of {len(want)} characters"
    assert f["address"] == {"probe": "comments", "store": "database", "tables": ["comments"], "fields": ["body"]}
    assert f["fields"]["body"]["actual"] == "QA kind words 1a2b3c"


def test_a_step_that_timed_out_says_what_could_not_be_done():
    r = {"case_id": "x", "title": "Post a photo", "verdict": "FAIL", "cause": "step_failed", "items": [],
         "steps": [{"i": 2, "action": "click", "ok": False,
                    "error": "Locator.click: Timeout 10000ms exceeded.\nCall log:\n  - waiting for get_by_role(...)"}]}
    ev = {"case": {"steps": [{"goto": "/"}, {"fill": {"label": "Caption", "value": "x"}}, {"click": {"role": "button", "name": "Share"}}]}}
    assert explain.failure_of(r, ev)["phrase"] == "could not click button “Share”: nothing matched within 10 s (step 3)"


def test_not_checked_is_never_the_products_fault():
    r = {"case_id": "x", "verdict": "INCONCLUSIVE", "cause": "login_failed", "error": "login as mila: HTTP 500", "items": []}
    f = explain.failure_of(r, {})
    assert f["kind"] == "not_checked" and "could not log in" in f["phrase"] and f["phrase"].endswith("Not a product bug")


@needs_excalidraw
def test_excalidraw_verdict_in_words():
    a = json.loads((EXCALIDRAW / "analysis.json").read_text())
    explain.fill_failures(a, lambda cid: json.loads((EXCALIDRAW / "cases" / cid / "evidence.json").read_text()))
    v = explain.verdict(a)
    assert v["state"] == "no_ship" and v["title"] == "4 journeys fail"     # the known console bug does not count
    assert v["headline"].startswith("Library controls have accessible names and usable contrast: 6 buttons without a name")


# ---- the brief ---------------------------------------------------------------------------------------------------------------

@needs_excalidraw
def test_excalidraw_brief_fits_the_budget_and_names_every_failure():
    b = build(EXCALIDRAW)
    assert tokens(b) < 4000
    named = {f["case_id"] for f in b["failures"]} | {k["case_id"] for k in b["known_bugs"]}
    assert named == {"accessibility.library", "accessibility.preferences", "accessibility.export", "accessibility.fonts",
                     "compat.reload_console"}
    one = build(EXCALIDRAW, case="accessibility.export", full=True)["case"]
    assert one["steps"] and one["checks"] and one["accessibility"]["violations"]


def test_brief_finds_suspects_in_a_local_diff(tmp_path, repo, monkeypatch):
    run = tmp_path / "run"
    (run / "cases/comment.add").mkdir(parents=True)
    case = {"case_id": "comment.add", "title": "Mila comments", "verdict": "FAIL", "cause": "must:state", "items": [], "steps": [],
            "failure": {"kind": "state", "phrase": "…", "address": {"probe": "comments", "tables": ["comments"], "fields": ["body"]}}}
    a = {"app": {"name": "s"}, "gate": {"status": "FAILED", "conditions": []}, "cases": [case], "issues": [], "run": {"project": ".assay"}}
    (run / "analysis.json").write_text(json.dumps(a))
    (run / "cases/comment.add/evidence.json").write_text(json.dumps(
        {"api": [{"method": "POST", "path": "/api/posts/p1/comments", "status": 200, "step": 2}]}))
    monkeypatch.chdir(repo)
    b = build(run, diff_text=DIFF)
    top = b["failures"][0]["suspects"][0]
    assert top["file"] == "server/posts.ts" and "`body`" in top["why"] and "POST /posts/:id/comments" in top["why"]


# ---- product → code ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("decl,path,ok", [
    ("/posts/:id/comments", "/api/posts/p-1/comments", True),
    ("/posts/{post_id}/comments", "/posts/7/comments", True),
    ("posts/<int:pk>/comments/", "/api/v2/posts/7/comments", True),
    ("/api/posts/[id]", "/api/posts/7", True),
    ("/posts/:id/comments", "/api/posts/7/likes", False),
])
def test_route_declarations_match_the_paths_journeys_call(decl, path, ok):
    assert bool(route_regex(decl).search(path)) is ok


def test_a_route_without_a_literal_segment_matches_nothing():
    assert route_regex("/:id") is None


def test_the_hunk_sits_in_the_handler_of_its_route(repo):
    h = parse_diff(DIFF)[0]
    assert enclosing_routes(h, repo) == [("POST", "/posts/:id/comments")]


def test_suspects_put_the_writer_of_the_wrong_field_first(repo):
    case = {"case_id": "comment.add", "failure": {"address": {"probe": "comments", "tables": ["comments"], "fields": ["body"]}}}
    ev = {"api": [{"method": "GET", "path": "/api/posts/p1", "step": 0}, {"method": "POST", "path": "/api/posts/p1/comments", "step": 2}]}
    s = suspects_for(case, ev, DIFF, None, repo)
    assert s[0]["file"] == "server/posts.ts" and s[0]["lines"] == "6-9"
    assert "writes `body`" in s[0]["why"] and "at step 3" in s[0]["why"] and s[0]["score"] > s[1]["score"]
    assert s[1]["file"] == "web/src/Profile.tsx" and "no direct link" in s[1]["why"]


# ---- selection for a pull request ---------------------------------------------------------------------------------------------

def _ds(*ids, **flags):
    return [{"id": i, "must": [], "should": [], "tags": [], **flags.get(i, {})} for i in ids]


REF = {"journey_map": {"comment.add": {"routes": ["POST /api/posts/p1/comments"], "tables": ["comments"], "files": []},
                       "profile.edit": {"routes": ["GET /api/me"], "files": ["web/src/Profile.tsx"], "tables": ["users"]},
                       "feed.scroll": {"routes": ["GET /api/feed"], "files": ["web/src/Feed.tsx"]},
                       "follow": {"routes": ["POST /api/users/u1/follow"]}},
       "cases": [{"case_id": "feed.scroll", "verdict": "PASS"}], "dataset": _ds("comment.add", "profile.edit", "feed.scroll", "follow")}


def test_a_pull_request_runs_the_journeys_its_change_can_break(repo):
    sel = select(_ds("comment.add", "profile.edit", "feed.scroll", "follow", "pay", pay={"critical": True}), REF, DIFF,
                 {"sample": 0}, root=repo)
    assert sel["ids"] == ["comment.add", "profile.edit", "pay"]
    assert "POST /posts/:id/comments" in sel["reasons"]["comment.add"] and sel["reasons"]["pay"] == "critical"
    assert "web/src/Profile.tsx" in sel["reasons"]["profile.edit"]


@pytest.mark.parametrize("diff,why", [
    ("diff --git a/package-lock.json b/package-lock.json\n--- a/package-lock.json\n+++ b/package-lock.json\n@@ -1 +1 @@\n-a\n+b\n",
     "package-lock.json"),
    ("diff --git a/server/util.ts b/server/util.ts\n--- a/server/util.ts\n+++ b/server/util.ts\n@@ -1 +1 @@\n-a\n+b\n",
     "no journey is known to use `server/util.ts`"),
])
def test_unknown_or_global_impact_runs_everything(repo, diff, why):
    sel = select(_ds("comment.add", "follow"), REF, diff, {}, root=repo)
    assert sel["ids"] is None and why in sel["why"]


def test_no_map_or_select_all_runs_everything(repo):
    assert select(_ds("a"), {"cases": []}, DIFF, {}, root=repo)["ids"] is None
    assert select(_ds("a"), REF, DIFF, {"mode": "all"}, root=repo)["ids"] is None


def test_docs_only_change_runs_the_safety_sample(repo):
    d = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n"
    jmap = {f"j{i}": {"routes": [f"GET /api/j{i}"]} for i in range(20)}
    sel = select(_ds(*[f"j{i}" for i in range(20)]), {**REF, "journey_map": jmap, "dataset": _ds(*[f"j{i}" for i in range(20)])},
                 d, {}, root=repo, seed="abc")
    assert len(sel["ids"]) == 2 and set(sel["reasons"].values()) == {"safety sample"}


# ---- weakened checks --------------------------------------------------------------------------------------------------------

def test_weakened_checks_are_listed():
    ref = {"dataset": [
        {"id": "c", "must": [{"state": {"x": 1}}, {"allowed_changes": ["comments"]}, {"no_errors": True}], "should": [], "tags": []},
        {"id": "gone", "title": "Pay", "must": [], "should": [], "tags": []}],
        "settings": {"ignore": {"rules": []}, "volatile": [], "gate": {}}}
    cur = [{"id": "c", "must": [{"allowed_changes": ["comments", "users"]}, {"no_errors": {"allow": ["unload"]}}],
            "should": [{"state": {"x": 1}}], "tags": ["known-bug"]}]
    got = [w["phrase"] for w in weakened(ref, cur, {"ignore": {"rules": ["A11Y-*"]}, "volatile": [], "gate": {"max_inconclusive": 0.5}})]
    assert got == ["removed journey `gone` (Pay)", "moved 1 `state` check of `c` from must to should",
                   "tagged `c` as a known bug (its failure stops blocking)", "allowed more errors in `c`",
                   "allowed `c` to change more data (`users`)", "added ignored findings in assay.yaml",
                   "allowed more journeys to go unchecked (gate.max_inconclusive)"]
    assert weakened(None, cur, {}) == [] and weakened(ref, ref["dataset"], ref["settings"]) == []


def test_weakened_checks_block_a_pull_request_until_approved(tmp_path):
    from assay.analyze import analyze
    from assay.config import Profile
    run = tmp_path / "run"
    (run / "cases/c").mkdir(parents=True)
    must = [{"no_errors": True}]
    (run / "cases/c/evidence.json").write_text(json.dumps({"case_id": "c", "title": "C", "case": {"id": "c", "must": must},
                                                           "sensors": {}, "steps": []}))
    ref = {"cases": [{"case_id": "c", "verdict": "PASS"}], "issues": [],
           "dataset": [{"id": "c", "must": must + [{"state": {"x": 1}}], "should": [], "tags": []}]}

    def verdict(labels):
        (run / "run.json").write_text(json.dumps({"id": "r", "case_order": ["c"], "ci": {"pr": "5", "labels": labels},
                                                  "dataset": [{"id": "c", "must": must, "should": [], "tags": []}]}))
        prof = Profile(name="t", root=tmp_path, base_url="http://x")
        prof.gate["new_code"] = True
        return analyze(run, prof, judge=None, baseline=ref)
    a = verdict([])
    assert a["gate"]["status"] == "FAILED" and a["weakened"]["blocking"]
    v = explain.verdict(a)
    assert v["action"] == "decide" and "removed 1 `state` check from `c`" in v["action_text"]
    assert verdict(["assay-approved"])["gate"]["status"] == "PASSED"


# ---- benchmark and MCP ---------------------------------------------------------------------------------------------------------

def _bench(mean, lo, hi, sig):
    return {"baseline": "sonnet", "cases": 4, "attempts": 3, "app": "Relay",
            "arms": {"sonnet": {"pass@1": 0.8, "ci95": [0.7, 0.9], "flaky": []},
                     "haiku": {"pass@1": 0.8 + mean, "ci95": [0.6, 0.9], "flaky": ["ai.refund"],
                               "diff_vs_baseline": {"mean": mean, "ci95": [lo, hi], "significant": sig}}}}


@pytest.mark.parametrize("res,icon,words", [
    (_bench(-0.25, -0.35, -0.15, True), "❌", "`haiku` is worse than `sonnet`, -25 pp (±10)"),
    (_bench(0.06, 0.02, 0.10, True), "✅", "`haiku` is better than `sonnet`, +6 pp (±4)"),
    (_bench(-0.02, -0.10, 0.06, False), "➖", "no significant difference"),
])
def test_a_benchmark_says_better_worse_or_no_difference(res, icon, words):
    from assay.ci import bench_markdown, bench_verdict
    i, line = bench_verdict(res)
    assert i == icon and words in line
    assert bench_markdown(res, None, {"pr": "4"}, "relay").splitlines()[1].startswith(f"### {icon} AI features")


@needs_excalidraw
def test_mcp_speaks_the_protocol():
    from assay.mcp import handle
    init = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["protocolVersion"] == "2025-06-18" and "tools" in init["result"]["capabilities"]
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = [t["name"] for t in handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
    assert names == ["assay_status", "assay_brief", "assay_run", "assay_artifact", "assay_explore_start",
                     "assay_explore_act", "assay_explore_note", "assay_explore_finish"]
    got = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "assay_brief", "arguments": {"source": str(EXCALIDRAW)}}})
    assert json.loads(got["result"]["content"][0]["text"])["verdict"] == "no_ship"
    bad = handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "assay_brief", "arguments": {"source": "/nope"}}})
    assert bad["result"]["isError"]
    assert handle({"jsonrpc": "2.0", "id": 5, "method": "resources/list"})["error"]["code"] == -32601


def test_ci_context_carries_the_base_commit_and_labels(tmp_path):
    from assay.ci import detect_context
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps({"pull_request": {"number": 7, "head": {"sha": "h1"}, "base": {"sha": "b1"},
                                               "labels": [{"name": "assay-approved"}]}}))
    ctx = detect_context({"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF": "refs/pull/7/merge",
                          "GITHUB_BASE_REF": "main", "GITHUB_REPOSITORY": "o/r", "GITHUB_EVENT_PATH": str(ev)})
    assert ctx["base_sha"] == "b1" and ctx["labels"] == ["assay-approved"]
    mr = detect_context({"GITLAB_CI": "true", "CI_MERGE_REQUEST_IID": "3", "CI_MERGE_REQUEST_DIFF_BASE_SHA": "b2",
                         "CI_MERGE_REQUEST_LABELS": "x,assay-approved"})
    assert mr["base_sha"] == "b2" and "assay-approved" in mr["labels"]


def test_not_sure_says_it_is_not_the_products_fault():
    from assay.report import verdict_markdown
    a = {"gate": {"status": "INCONCLUSIVE", "conditions": []}, "run": {}, "cases": [
        {"case_id": "x", "title": "Sign in", "verdict": "INCONCLUSIVE", "cause": "app_unreachable",
         "failure": {"kind": "not_checked", "phrase": "could not be checked: the app did not start or did not answer. Not a product bug"}}]}
    head = verdict_markdown(a).splitlines()
    assert head[0] == "### ⚠️ Not sure: 1 of 1 journey could not be checked"
    assert head[1] == "Sign in: could not be checked: the app did not start or did not answer. Not a product bug."
    assert head[2] == "**From you:** look: the check did not complete. This is not a product bug."


def test_a_case_lists_itself_in_the_dataset_as_written():
    from assay.model import Case
    c = Case.from_dict({"id": "x", "steps": [{"goto": "/"}], "promise": "The app opens", "critical": True,
                        "must": [{"state": {"n": "{vars.uid}"}}]}, source="cases/a.yaml")
    assert c.entry() == {"id": "x", "title": "", "promise": "The app opens", "critical": True, "ai": False, "tags": [],
                         "group": "a", "source": "cases/a.yaml", "must": [{"state": {"n": "{vars.uid}"}}], "should": [],
                         "journey": {"steps": [{"goto": "/"}]}}


def test_a_phrase_names_the_matcher_that_failed_not_the_first_one():
    want = "QA kind words 1a2b3c: the light in the second frame"
    ev = {"state_before": {"comments": []}, "state_after": {"comments": [{"id": 9, "post_id": "p0", "body": "QA kind words 1a2b3c"}]}}
    r = {"case_id": "c", "verdict": "FAIL", "cause": "must:state", "steps": [], "items": [
        {"kind": "state", "level": "must", "status": "fail", "spec": {"comments": {"delta": 1, "contains": {"post_id": "p0", "body": want}}}}]}
    f = explain.failure_of(r, ev, {"comments": {"sqlite": "x", "sql": "select * from comments"}})
    assert f["phrase"].startswith("`comments.body` was saved cut short") and f["address"]["fields"] == ["body"]


def test_the_change_of_a_pull_request_starts_where_its_branch_left_the_target(tmp_path):
    import subprocess
    from assay.code import ci_diff, changed_files, parse_diff
    g = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t"), g("config", "user.name", "t")
    (tmp_path / "a.ts").write_text("a\n"); g("add", "."); g("commit", "-qm", "a")
    fork = g("rev-parse", "HEAD")
    g("checkout", "-qb", "feature"); (tmp_path / "b.ts").write_text("b\n"); g("add", "."); g("commit", "-qm", "b")
    g("checkout", "-q", "main"); (tmp_path / "c.ts").write_text("c\n"); g("add", "."); g("commit", "-qm", "c")
    base = g("rev-parse", "HEAD")
    g("checkout", "-q", "feature")
    text = ci_diff(base, cwd=tmp_path)
    assert changed_files(parse_diff(text), text) == ["b.ts"] and fork


@needs_excalidraw
def test_excalidraw_comment_says_it_in_four_lines_without_engine_terms():
    from assay.report import verdict_markdown
    a = json.loads((EXCALIDRAW / "analysis.json").read_text())
    explain.fill_failures(a, lambda cid: json.loads((EXCALIDRAW / "cases" / cid / "evidence.json").read_text()))
    head = verdict_markdown(a, url="http://x/a/34").split("<details>")[0].strip().splitlines()
    assert len(head) == 4 and head[0] == "### ❌ Don't ship: 4 journeys fail"
    for word in ("Condition", "issue", "critical", "rating", "INCONCLUSIVE", "a11y", "must"):
        assert word not in "\n".join(head), word


def test_a_probe_can_be_read_only_in_signed_in_journeys():
    from assay.config import Profile
    from assay.probes import snapshot
    prof = Profile(name="t", root=Path("."), base_url="http://x", probes={"badges": {"cmd": "echo '{\"n\": 1}'", "when_as": "*"}})
    assert snapshot(prof, account=None) == ({}, {})
    assert snapshot(prof, account="mila")[0] == {"badges": {"n": 1}}


def test_a_journey_without_a_map_entry_always_runs(repo):
    sel = select(_ds("comment.add", "follow", "brand.new"), REF, DIFF, {"sample": 0}, root=repo)
    assert sel["reasons"]["brand.new"] == "new in this change" or sel["reasons"]["brand.new"] == "not mapped yet"


def test_every_hunk_must_link_to_a_journey(repo):
    """One file, two hunks: a known handler and a renamed route nobody maps → every journey, not just the known one."""
    (repo / "server/feed.ts").write_text('r.get("/timeline", list);\nr.post("/posts", create);\n')
    d = ("diff --git a/server/feed.ts b/server/feed.ts\n--- a/server/feed.ts\n+++ b/server/feed.ts\n"
         "@@ -1,1 +1,1 @@\n-r.get(\"/feed\", list);\n+r.get(\"/timeline\", list);\n")
    ref = {**REF, "journey_map": {**REF["journey_map"], "feed.scroll": {"routes": ["GET /api/feed"]}}}
    sel = select(_ds("comment.add", "feed.scroll", "follow"), ref, d, {"sample": 0}, root=repo)
    assert sel["ids"] == ["feed.scroll"] and "/feed" in sel["reasons"]["feed.scroll"]


def test_templates_and_locales_count_as_product_files(repo):
    from assay.code import is_source
    assert is_source("app/views/posts/show.html.erb") and is_source("locales/en.json") and is_source("public/logo.svg")
    assert not is_source("docs/intro.md") and not is_source("web/src/Feed.test.tsx") and not is_source("README.md")


def test_diff_paths_survive_quotes_deletions_and_plus_lines():
    from assay.code import changed_files, parse_diff
    d = ('diff --git "a/src/caf\\303\\251.ts" "b/src/caf\\303\\251.ts"\n--- "a/src/caf\\303\\251.ts"\n+++ "b/src/caf\\303\\251.ts"\n'
         "@@ -1 +1 @@\n-a\n+++b\n"
         "diff --git a/old.ts b/old.ts\ndeleted file mode 100644\n--- a/old.ts\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x\n-y\n")
    hs = parse_diff(d)
    assert [h.file for h in hs] == ["src/café.ts", "old.ts"] and hs[0].added == ["++b"] and hs[1].removed == ["x", "y"]
    assert changed_files(hs, d) == ["src/café.ts", "old.ts"]


def test_mcp_survives_odd_messages():
    import io
    from assay.mcp import serve
    out = io.StringIO()
    serve(io.StringIO('5\n{"jsonrpc":"2.0","id":1,"method":"tools/call","params":[]}\nnot json\n'
                      '[{"jsonrpc":"2.0","id":2,"method":"ping"},{"jsonrpc":"2.0","method":"notifications/x"}]\n'
                      '{"jsonrpc":"2.0","id":3,"method":"tools/list"}\n'), out)
    msgs = [json.loads(x) for x in out.getvalue().splitlines()]
    assert msgs[0]["error"]["code"] == -32600 and msgs[1]["error"]["code"] == -32602 and msgs[2]["error"]["code"] == -32700
    assert msgs[3] == [{"jsonrpc": "2.0", "id": 2, "result": {}}] and msgs[4]["id"] == 3
    assert out.getvalue().isascii()


def test_a_pull_request_cannot_switch_off_its_own_guard(tmp_path):
    from assay.analyze import analyze
    from assay.config import Profile
    run = tmp_path / "run"
    (run / "cases/c").mkdir(parents=True)
    (run / "cases/c/evidence.json").write_text(json.dumps({"case_id": "c", "case": {"id": "c", "must": []}, "sensors": {}, "steps": []}))
    loose = {"block_on_weakened": False, "fail_on_case_fail": False}
    (run / "run.json").write_text(json.dumps({"id": "r", "case_order": ["c"], "ci": {"pr": "5"},
                                              "dataset": [{"id": "c", "must": [], "should": [], "tags": []}],
                                              "settings": {"gate": loose}}))
    ref = {"cases": [{"case_id": "c", "verdict": "PASS"}], "issues": [], "settings": {"gate": {}},
           "dataset": [{"id": "c", "must": [], "should": [], "tags": []}]}
    prof = Profile(name="t", root=tmp_path, base_url="http://x")
    prof.gate.update(new_code=True, **loose)
    a = analyze(run, prof, judge=None, baseline=ref)
    phrases = [w["phrase"] for w in a["weakened"]["items"]]
    assert a["gate"]["status"] == "FAILED" and "stopped blocking on weakened checks" in phrases
    assert "made failing journeys not block (gate.fail_on_case_fail: false)" in phrases


def test_steps_and_edited_checks_need_a_person():
    ref = {"dataset": [{"id": "pub", "must": [{"state": {"posts": {"len": 1}}}], "should": [], "tags": [],
                        "journey": {"steps": [{"click": {"role": "button", "name": "Publish"}}]}}]}
    cur = [{"id": "pub", "must": [{"state": {}}], "should": [], "tags": [],
            "journey": {"steps": [{"http": {"method": "POST", "path": "/api/posts"}}]}}]
    assert [w["phrase"] for w in weakened(ref, cur, None)] == ["changed 1 `state` check of `pub`",
                                                               "changed the steps or start state of `pub`"]


def test_a_known_bug_does_not_hide_a_new_failure_in_the_same_check():
    from assay.quality import failure_signature
    def failing(detail):
        return {"case_id": "x", "verdict": "FAIL", "cause": "must:state", "items": [
            {"kind": "state", "level": "must", "status": "fail", "detail": detail}]}
    posts, comments = failing("posts.0.caption: expected 'a', got 'b'"), failing("comments: changed by 0, expected 1")
    assert failure_signature(posts) != failure_signature(comments)
    assert failure_signature(posts) == failure_signature(failing("posts.0.caption: expected 'c', got 'd'"))


def test_a_passing_known_bug_is_not_counted_as_failing():
    from assay.quality import known_bugs
    assert known_bugs([{"case_id": "k", "verdict": "PASS", "tags": ["known-bug"]}], None) == set()


def test_a_speed_check_without_measurements_is_undecided():
    from assay.checks import d_perf
    assert d_perf({"lcp_ms": 2500}, {"audit": {"error": "audit failed"}}, None, {})[0] == "undecided"
    assert d_perf({"step_ms": 1500}, {"audit": {}, "steps": []}, None, {})[0] == "pass"
