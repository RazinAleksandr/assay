"""The human level of the server: a pull request says ship it or not with the evidence; a branch says how healthy it is
and what nobody checked. No QA-engineer furniture (condition tables, ratings, severities, issue counters)."""
import json
import os
import shutil
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from assay.ci import Server
from assay.server import Store, make_handler

# a real analysis (Excalidraw, #34) kept outside the repository: $ASSAY_EXCALIDRAW, else docs/ next to the checkout
EXCALIDRAW = Path(os.environ.get("ASSAY_EXCALIDRAW")
                  or Path(__file__).resolve().parents[2] / "docs/Excalidraw-Analysis-34/data/analyses/34")
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


@pytest.fixture()
def host(tmp_path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Store(tmp_path / "data"), "tok", None, {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def get(url: str, follow: bool = True) -> tuple[int, str, str]:
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url) as r:
            return r.status, r.read().decode(), r.headers.get("Location") or ""
    except urllib.error.HTTPError as e:
        return e.code, "", e.headers.get("Location") or ""


def case(cid, verdict, **kw):
    return {"case_id": cid, "title": cid.replace(".", " ").capitalize(), "verdict": verdict, "cause": None, "owner": None,
            "summary": "", "items": [{"kind": "state", "level": "must", "status": "pass" if verdict == "PASS" else "fail",
                                       "spec": {}, "detail": "", "by": "code", "dimension": "functional"}],
            "score": 1, "duration_s": 2.0, "severity": "major", "dimension": "functional", "browser": "chromium",
            "viewport": "desktop", "screenshot": f"cases/{cid}/final.png",
            "steps": [{"i": 0, "action": "click", "summary": "Post", "ok": True, "screenshot": "step-00.png"}], **kw}


def analysis(cases, status, conds, **extra):
    return {"app": {"name": "morrow", "title": "Morrow", "base_url": "http://x"},
            "run": {"id": "r", "started": "2026-11-12T10:00:00", "finished": "2026-11-12T10:03:00"},
            "ci": {}, "generated": "now", "baseline": None,
            "summary": {"cases": len(cases), "PASS": sum(c["verdict"] == "PASS" for c in cases),
                        "FAIL": sum(c["verdict"] == "FAIL" for c in cases), "INCONCLUSIVE": 0, "issues": 0, "new_issues": 0,
                        "by_severity": {}, "pages_scanned": 0, "routes_scanned": 0},
            "gate": {"status": status, "conditions": conds}, "ratings": {}, "ratings_new": None, "dimensions": {},
            "measured": {"functional": ["journeys"], "reliability": ["console and network"],
                         "security": ["cookie flags and tokens in storage"]},
            "cases": cases, "issues": [], "pages": [], "changes": {}, "judge": {"backend": None, "model": None, "calls": 0},
            "known_bugs": [], **extra}


def folder(tmp: Path, name: str, a: dict) -> Path:
    run = tmp / name
    for c in a["cases"]:
        (run / "cases" / c["case_id"]).mkdir(parents=True, exist_ok=True)
        (run / "cases" / c["case_id"] / "final.png").write_bytes(PNG)
        (run / "cases" / c["case_id"] / "step-00.png").write_bytes(PNG)
        (run / "cases" / c["case_id"] / "evidence.json").write_text("{}")
    (run / "analysis.json").write_text(json.dumps(a))
    return run


EXPECTED = "QA kind words 1a2b: the light in the second frame is lovely, where was this taken?"
FAILURE = {"kind": "state", "probe": "comments", "path": "comments", "store": "database", "record_is_new": True,
           "phrase": "`comments.body` was saved cut short in the database: 18 of 82 characters",
           "fields": {"body": {"expected": EXPECTED, "actual": EXPECTED[:18]}},
           "address": {"probe": "comments", "tables": ["comments"], "fields": ["body"]}}
SUSPECT = {"file": "server/posts.ts", "lines": "384-391", "score": 10,
           "why": "changes code that writes `body`; in the handler of `POST /posts/:id/comments`, which the journey called",
           "hunk": "@@ -384,6 +384,8 @@\n+    const preview = d.body.slice(0, 20);\n-        d.body,\n+        preview,"}


def test_pull_request_page_says_ship_or_not_with_the_evidence(host, tmp_path):
    srv = Server(host, "tok")
    main = analysis([case("comment.add", "PASS"), case("post.like", "PASS")], "PASSED",
                    [{"id": "failed_journeys", "name": "Failed journeys", "actual": 0, "op": "=", "threshold": 0, "ok": True}],
                    dataset=[{"id": "comment.add", "title": "Comment"}, {"id": "post.like", "title": "Like"}])
    m = srv.upload(folder(tmp_path, "main", main), {"project": "gh/acme/morrow", "title": "Morrow", "branch": "main", "sha": "a" * 40})
    pr = analysis([case("comment.add", "FAIL", promise="A comment is saved in full", failure=FAILURE, suspects=[SUSPECT]),
                   case("post.like", "PASS")], "FAILED",
                  [{"id": "new_failed_journeys", "name": "New failed journeys", "actual": 1, "op": "=", "threshold": 0, "ok": False},
                   {"id": "new_issues:critical", "name": "New critical issues", "actual": 0, "op": "≤", "threshold": 0, "ok": True}],
                  baseline="main @ aaaaaaa", changes={"regressions": ["comment.add"], "fixed": []},
                  scope={"mode": "auto", "checked": 2, "total": 40},
                  weakened={"items": [{"case_id": "post.like", "phrase": "removed 1 `state` check from `post.like`"}],
                            "approved": False, "blocking": True})
    p = srv.upload(folder(tmp_path, "pr", pr), {"project": "gh/acme/morrow", "title": "Morrow", "branch": "feat", "pr": "3",
                                               "target": "main", "sha": "b" * 40, "reference_id": m["id"]})
    st, body, _ = get(host + p["url"][len(host):] if p["url"].startswith(host) else p["url"])
    assert st == 200
    for want in ("Don&#x27;t ship", "1 journey broke", "A comment is saved in full:", "<code>comments.body</code> was saved cut short",
                 "Decide:", "2 of 40 journeys",
                 "the light in the second frame", "cut at 18 of 82 characters", "server/posts.ts:384-391",
                 "Checks weakened", "assay-approved", f"/a/{m['id']}/cases/comment.add/", "Quality change"):
        assert want in body, want
    # under the verdict, SonarQube's structure: the gate and each condition with its value and threshold, the measures
    gate = body[body.index("id=gate-h"):body.index("id=meas-h")]
    assert "Quality gate" in gate and "1 of 2 conditions failed" in gate
    assert "<th scope=row>New failed journeys</th><td class=\"r num\">1</td><td class=\"r num\">required 0</td>" in gate
    assert "<th scope=row>New critical issues</th><td class=\"r num\">0</td><td class=\"r num\">≤ 0</td>" in gate
    assert "Fix it before merging" not in body and "Nothing to do" not in body           # decide: a person must
    assert "role=tab id=mt-new" in body and "Compared with main @ aaaaaaa." in body         # new code, against the target
    assert "coverage" not in body
    # the journey's own page: the same evidence, its steps, its checks in words
    st, jp, _ = get(f"{host}/gh/acme/morrow/journeys/comment.add?pr=3")
    assert st == 200 and "Broken" in jp and "server/posts.ts" in jp and "What it checks" in jp
    # the dataset index
    st, idx, _ = get(f"{host}/gh/acme/morrow/journeys?branch=main")
    assert st == 200 and "Comment" in idx and "Like" in idx
    # the health of main: what is not checked, said plainly
    st, health, _ = get(f"{host}/gh/acme/morrow?branch=main")
    assert st == 200 and "Healthy" in health and "Not checked" in health and "AI features: nothing measured it" in health
    assert "Security: only cookie flags and tokens in browser storage" in health
    # pages for QA engineers are gone; old links land on the verdict (Issues is back, SonarQube-style: see below)
    assert get(f"{host}/gh/acme/morrow/issues?pr=3", follow=False)[0] == 200
    for old in ("screens", "activity", "quality-gate"):
        st, _, loc = get(f"{host}/gh/acme/morrow/{old}?pr=3", follow=False)
        assert st == 302 and loc == "/gh/acme/morrow?pr=3", old
    for old in ("/rules", "/quality-gates"):
        st, _, loc = get(host + old, follow=False)
        assert st == 302 and loc == "/docs"


@pytest.mark.skipif(not EXCALIDRAW.exists(), reason="the Excalidraw analysis is not on this machine")
def test_an_analysis_from_before_the_verdict_model_still_renders(host, tmp_path):
    run = tmp_path / "excalidraw"
    shutil.copytree(EXCALIDRAW, run)
    up = Server(host, "tok").upload(run, {"project": "gh/viktor/qa-excalidraw", "title": "Excalidraw", "branch": "master",
                                          "sha": "c" * 40, "default_branch": "master"})
    st, body, _ = get(f"{host}/gh/viktor/qa-excalidraw?branch=master")
    assert st == 200 and "4 of 19 journeys broken" in body and "buttons without a name for screen readers" in body
    assert "known bug" in body and "Not checked" in body and "AI features: nothing measured it" in body
    for page in ("journeys", "journeys/accessibility.library", "journeys/compat.reload_console", "pull-requests", "branches"):
        assert get(f"{host}/gh/viktor/qa-excalidraw/{page}?branch=master")[0] == 200, page
    st, b, _ = get(f"{host}/api/analyses/{up['id']}/brief")
    assert st == 200


ISSUES = [{"rule": "REL-003", "title": "Server error", "dimension": "reliability", "severity": "critical", "where": "PATCH /api/links/:id",
           "message": "HTTP 500", "count": 1, "new": True, "sources": ["links.edit"], "fingerprint": "f1"},
          {"rule": "REL-001", "title": "Uncaught error", "dimension": "reliability", "severity": "critical", "where": "",
           "message": "TypeError: x is null", "count": 8, "new": True, "sources": ["links.edit", "comment.add"], "fingerprint": "f2"},
          {"rule": "UX-002", "title": "Touch target smaller than 24×24 px", "dimension": "ux", "severity": "minor", "where": "/settings",
           "message": "2+ targets", "count": 1, "new": True, "sources": ["scan:mobile:/settings"], "fingerprint": "f3"}]
COND_ISSUES = [{"id": "failed_journeys", "name": "Failed journeys", "actual": 0, "op": "=", "threshold": 0, "ok": True},
               {"id": "new_issues:critical", "name": "New critical issues", "actual": 2, "op": "≤", "threshold": 0, "ok": False},
               {"id": "inconclusive", "name": "Inconclusive journeys", "actual": "0%", "op": "≤", "threshold": "20%", "ok": True, "soft": True}]
OK_GATE = [{"id": "failed_journeys", "name": "Failed journeys", "actual": 0, "op": "=", "threshold": 0, "ok": True}]


def test_a_broken_branch_lists_every_problem_its_verdict_counts(host, tmp_path):
    """Red because of sensor problems, no journey broken: the page says what is broken, worst first, where and how often,
    and the numbers agree with the label."""
    a = analysis([case("comment.add", "PASS"), case("links.edit", "PASS")], "FAILED", COND_ISSUES, issues=ISSUES)
    Server(host, "tok").upload(folder(tmp_path, "main", a), {"project": "gh/acme/kutt", "title": "Kutt", "branch": "main", "sha": "a" * 40})
    st, body, _ = get(f"{host}/gh/acme/kutt")
    assert st == 200 and "Broken" in body and "2 new problems" in body
    assert "Nothing to do" not in body and "Fix what is broken." in body and "/assay:fix" in body
    assert '<dt>journeys broken</dt><dd class=" num">0</dd>' in body and '<dt>new problems</dt><dd class="bad num">2</dd>' in body
    broken = body[body.index("What is broken"):body.index("Not checked")]
    assert broken.index("TypeError: x is null") < broken.index("answered HTTP 500")          # worst first, then the most frequent
    assert "8 times, in 2 journeys" in broken and "Critical" in broken and "Touch target" not in broken   # what the verdict counts
    assert "/gh/acme/kutt/issues?branch=main&amp;rule=REL-003" in broken                                   # each one leads to its issue
    # the gate's conditions under it, and the measures: a quality nothing measured says so, never A
    assert '<th scope=row>New critical issues</th><td class="r num">2</td><td class="r num">≤ 0</td>' in body
    assert "No new code to show" in body and "not measured" in body
    # the project list agrees: the problems are counted next to the journeys
    _, plist, _ = get(f"{host}/projects")
    assert '<dt>New problems</dt><dd class="bad">2</dd>' in plist and "on <span class=mono>main</span>" in plist


def test_the_first_analysis_of_a_branch_sets_the_baseline_instead_of_blocking(host, tmp_path):
    conds = [OK_GATE[0], {"id": "new_issues:critical", "name": "New critical issues", "actual": 2, "op": "≤", "threshold": 0,
                          "ok": False, "soft": True, "baseline": True}]
    a = analysis([case("comment.add", "PASS"), case("links.edit", "PASS")], "PASSED", conds, issues=ISSUES)
    Server(host, "tok").upload(folder(tmp_path, "main", a), {"project": "gh/acme/kutt", "title": "Kutt", "branch": "main", "sha": "a" * 40})
    _, body, _ = get(f"{host}/gh/acme/kutt")
    gate = body[body.index("id=gate-h"):body.index("id=meas-h")]
    assert "<span class=\"cres base\">" in gate and "Baseline" in gate and "1 only set the baseline" in gate
    assert "does not block" in body and "Fix what is broken" not in body                   # listed, not blocking


def test_the_issues_page_filters_by_facets_kept_in_the_address(host, tmp_path):
    a = analysis([case("comment.add", "PASS"), case("links.edit", "PASS")], "FAILED", COND_ISSUES, issues=ISSUES)
    Server(host, "tok").upload(folder(tmp_path, "main", a), {"project": "gh/acme/kutt", "title": "Kutt", "branch": "main", "sha": "a" * 40})
    st, body, _ = get(f"{host}/gh/acme/kutt/issues")
    assert st == 200 and "<b>3</b> of 3 issues" in body
    nav = body[body.index("<aside class=snav"):body.index("</aside>")]
    assert 'class="it on" href="/gh/acme/kutt/issues"' in nav and "<span class=cnt>3</span>" in nav
    for facet in ("<h2>Quality</h2>", "<h2>Severity</h2>", "<h2>Rule</h2>", "<h2>Screen</h2>", "<h2>Journey</h2>"):
        assert facet in body, facet
    assert "<h2>New code</h2>" not in body and "New code appears once there is an earlier analysis" in body
    assert body.index("answered HTTP 500") < body.index("Touch target")                  # worst first
    st, body, _ = get(f"{host}/gh/acme/kutt/issues?dimension=reliability&severity=critical")
    assert "<b>2</b> of 3 issues" in body and "Touch target" not in body and "Clear filters" in body
    assert 'href="/gh/acme/kutt/issues?branch=main&amp;severity=critical"' in body                      # removing one filter keeps the other
    st, body, _ = get(f"{host}/gh/acme/kutt/issues?screen=/settings")
    assert "<b>1</b> of 3 issues" in body and "UX-002" in body
    # a second analysis of the branch: what is new since the one before
    b = analysis([case("comment.add", "PASS"), case("links.edit", "PASS")], "FAILED", COND_ISSUES,
                 issues=ISSUES + [{**ISSUES[0], "where": "DELETE /api/links/:id", "fingerprint": "f4"}])
    Server(host, "tok").upload(folder(tmp_path, "main2", b), {"project": "gh/acme/kutt", "title": "Kutt", "branch": "main", "sha": "b" * 40})
    st, body, _ = get(f"{host}/gh/acme/kutt/issues?new=new")
    assert "<b>1</b> of 4 issues" in body and "DELETE /api/links/:id" in body and "<h2>New code</h2>" in body
    st, health, _ = get(f"{host}/gh/acme/kutt")
    assert "role=tab id=mt-new" in health and "Since the previous analysis of main" in health


def test_a_project_opens_on_the_branch_it_is_analysed_on(host, tmp_path):
    """No default branch from CI: the server guesses main; a project analysed on master opens on master, not empty."""
    a = analysis([case("comment.add", "PASS")], "PASSED", OK_GATE)
    Server(host, "tok").upload(folder(tmp_path, "m", a), {"project": "gh/acme/fork", "title": "Fork", "branch": "master", "sha": "a" * 40})
    st, body, _ = get(f"{host}/gh/acme/fork")
    assert st == 200 and "Healthy" in body and "No analysis of" not in body and "Main branch · " in body
    nav = body[body.index("<aside class=snav"):body.index("</aside>")]
    assert 'href="/gh/acme/fork/journeys"' in nav and "Benchmarks" in nav                     # no ?branch=: master is home
    st, body, _ = get(f"{host}/gh/acme/fork?branch=main")                                     # asked for: said, with the way out
    assert "No analysis of main yet" in body and 'href="/gh/acme/fork?branch=master"' in body
    st, body, _ = get(f"{host}/gh/acme/fork/benchmarks")                                      # always in the nav, says how to add one
    assert st == 200 and "No benchmarks yet" in body and "/assay:bench" in body


def test_a_failure_that_passed_on_a_second_run_is_flaky_not_broken(host, tmp_path):
    flaky = case("post.like", "FAIL", confirm={"verdict": "PASS", "run": "confirm"}, failure={"phrase": "the like count did not change"})
    real = case("comment.add", "FAIL", confirm={"verdict": "FAIL", "run": "confirm"}, failure=FAILURE)
    a = analysis([flaky, real], "FAILED",
                 [{"id": "failed_journeys", "name": "Failed journeys", "actual": 1, "op": "=", "threshold": 0, "ok": False}],
                 dataset=[{"id": "post.like", "title": "Like"}, {"id": "comment.add", "title": "Comment"}])
    Server(host, "tok").upload(folder(tmp_path, "m", a), {"project": "gh/acme/morrow", "title": "Morrow", "branch": "main", "sha": "a" * 40})
    st, body, _ = get(f"{host}/gh/acme/morrow")
    assert "1 of 2 journeys broken" in body and "Reproduced on a second run." in body
    broken = body[body.index("What is broken"):body.index("Not checked")]
    assert 'chip warn">flaky</span>' in broken and "Failed once, passed on a second run: it blocks nothing." in broken
    st, body, _ = get(f"{host}/gh/acme/morrow/journeys")
    assert '<span class="vd FLAKY">Flaky</span>' in body
    st, body, _ = get(f"{host}/gh/acme/morrow/journeys/post.like")
    assert st == 200 and ">Flaky</h2>" in body


def test_dataset_journeys_are_linked_with_their_ids_encoded(host, tmp_path):
    a = analysis([case("ai.draft[refund-on-day-29]", "FAIL", failure={"phrase": "the draft refused the refund"})], "FAILED",
                 [{"id": "failed_journeys", "name": "Failed journeys", "actual": 1, "op": "=", "threshold": 0, "ok": False}])
    Server(host, "tok").upload(folder(tmp_path, "m", a), {"project": "gh/acme/relay", "title": "Relay", "branch": "main", "sha": "a" * 40})
    _, body, _ = get(f"{host}/gh/acme/relay")
    assert "/gh/acme/relay/journeys/ai.draft%5Brefund-on-day-29%5D" in body
    assert get(f"{host}/gh/acme/relay/journeys/ai.draft%5Brefund-on-day-29%5D")[0] == 200
