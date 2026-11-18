"""Two levels of every project page: Human (the verdict and its evidence) and Agent (every fact on one page, exportable),
under one navigation: Health, Journeys, Pull requests, Branches."""
import csv
import io
import json
import urllib.request

from assay.ci import Server
from test_pages import FAILURE, SUSPECT, analysis, case, folder, get, host  # noqa: F401  (host is a fixture)

COND_FAIL = [{"id": "new_failed_journeys", "name": "New failed journeys", "actual": 1, "op": "=", "threshold": 0, "ok": False}]
COND_OK = [{"id": "failed_journeys", "name": "Failed journeys", "actual": 0, "op": "=", "threshold": 0, "ok": True}]
DATASET = [{"id": "comment.add", "title": "Comment", "source": "cases/posts.yaml", "tags": ["posts"]},
           {"id": "post.like", "title": "Like", "source": "cases/posts.yaml"},
           {"id": "auth.login", "title": "Log in", "source": "cases/core.yaml", "group": "auth", "critical": True}]


def project(host, tmp_path):
    srv = Server(host, "tok")
    main = analysis([case("comment.add", "PASS"), case("post.like", "PASS"), case("auth.login", "PASS")], "PASSED", COND_OK,
                    dataset=DATASET)
    m = srv.upload(folder(tmp_path, "main", main), {"project": "gh/acme/morrow", "title": "Morrow", "branch": "main", "sha": "a" * 40})
    pr = analysis([case("comment.add", "FAIL", failure=FAILURE, suspects=[SUSPECT]), case("post.like", "PASS")], "FAILED", COND_FAIL,
                  dataset=DATASET, baseline="main @ aaaaaaa", changes={"regressions": ["comment.add"], "fixed": []},
                  scope={"mode": "auto", "checked": 2, "total": 3, "why": "1 touched by this change, 1 sampled",
                         "reasons": {"comment.add": "calls `POST /posts/:id/comments`", "post.like": "safety sample"}})
    p = srv.upload(folder(tmp_path, "pr", pr), {"project": "gh/acme/morrow", "title": "Morrow", "branch": "feat", "pr": "3",
                                               "target": "main", "sha": "b" * 40, "reference_id": m["id"]})
    return m, p


def fetch(url: str, cookie: str = "") -> tuple[int, str, dict]:
    req = urllib.request.Request(url, headers={"Cookie": cookie} if cookie else {})
    with urllib.request.urlopen(req) as r:
        return r.status, r.read().decode(), dict(r.headers)


def test_navigation_is_health_journeys_pull_requests_branches(host, tmp_path):
    project(host, tmp_path)
    st, body, _ = get(f"{host}/gh/acme/morrow")
    assert st == 200
    nav = body[body.index("<aside class=snav"):body.index("</aside>")]
    assert [x for x in ("Health", "Journeys", "Pull requests", "Branches", "Set up CI") if x in nav] == \
           ["Health", "Journeys", "Pull requests", "Branches", "Set up CI"]
    assert "view=agent" in body and "view=human" in body                     # the toggle
    st, body, _ = get(f"{host}/gh/acme/morrow?pr=3")                          # a pull request lives under Pull requests
    nav = body[body.index("<aside class=snav"):body.index("</aside>")]
    assert 'class="it on" href="/gh/acme/morrow/pull-requests"' in nav
    assert "Brief for agents" not in body and "assay brief " not in body      # agent tooling is on the agent level
    st, body, _ = get(f"{host}/gh/acme/morrow/branches")
    assert "view=agent" not in body                                          # branches: one level only


def test_pull_requests_list_says_why_in_one_line(host, tmp_path):
    project(host, tmp_path)
    st, body, _ = get(f"{host}/gh/acme/morrow/pull-requests")
    assert st == 200 and "Don&#x27;t ship" in body and "was saved cut short" in body and "2 of 3 journeys" in body


def test_journeys_are_grouped_by_group_or_file(host, tmp_path):
    project(host, tmp_path)
    st, body, _ = get(f"{host}/gh/acme/morrow/journeys")
    assert st == 200
    assert "<h2>posts</h2>" in body and "<h2>auth</h2>" in body and "<h2>core</h2>" not in body
    assert "in 2 groups" in body and "critical" in body


def test_the_agent_level_is_remembered_and_exports_its_data(host, tmp_path):
    project(host, tmp_path)
    st, body, headers = fetch(f"{host}/gh/acme/morrow?pr=3&view=agent")
    assert st == 200 and "page agent" in body and "assay_view=agent" in headers.get("Set-Cookie", "")
    st, body, _ = fetch(f"{host}/gh/acme/morrow/journeys", cookie="assay_view=agent")   # the next page keeps the level
    assert "page agent" in body
    st, body, headers = fetch(f"{host}/gh/acme/morrow/journeys?view=human", cookie="assay_view=agent")
    assert "page agent" not in body and "assay_view=human" in headers.get("Set-Cookie", "")
    for page in ("?pr=3", "", "/journeys", "/pull-requests"):
        sep = "&" if "?" in page else "?"
        st, body, headers = fetch(f"{host}/gh/acme/morrow{page}{sep}view=agent&format=json")
        data = json.loads(body)
        assert st == 200 and headers["Content-Type"].startswith("application/json") and data["tables"], page
        table = next(iter(data["tables"]))
        st, body, headers = fetch(f"{host}/gh/acme/morrow{page}{sep}view=agent&format=csv&table={table}")
        assert st == 200 and headers["Content-Type"].startswith("text/csv") and len(list(csv.reader(io.StringIO(body)))) == len(data["tables"][table]) + 1, page


def test_the_whole_analysis_as_json(host, tmp_path):
    _, p = project(host, tmp_path)
    st, body, _ = get(f"{host}/api/analyses/{p['id']}")
    a = json.loads(body)
    assert st == 200 and a["cases"][0]["case_id"] == "comment.add" and a["scope"]["checked"] == 2
    assert get(f"{host}/api/analyses/999")[0] == 404
