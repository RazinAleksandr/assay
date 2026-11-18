"""Selection at scale: groups of journeys, files no journey is mapped to (group paths, reverse imports), a time budget."""
import subprocess
from pathlib import Path

import pytest

from assay.code import Repo
from assay.config import Profile, _select, load_cases
from assay.select import select


def _tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return root


def _diff(path: str, old: str = "a", new: str = "b") -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{old}\n+{new}\n"


def _ds(*ids, **extra):
    return [{"id": i, "must": [], "should": [], "tags": [], **extra.get(i.replace(".", "_"), {})} for i in ids]


JMAP = {"post.view": {"routes": ["GET /api/posts/p1"]},
        "post.comment": {"routes": ["POST /api/posts/p1/comments"], "tables": ["comments"]},
        "feed.scroll": {"routes": ["GET /api/feed"], "files": ["web/src/Feed.tsx"]},
        "profile.edit": {"routes": ["PUT /api/me"], "files": ["web/src/Profile.tsx"]}}
IDS = list(JMAP)
REF = {"journey_map": JMAP, "dataset": _ds(*IDS), "cases": [{"case_id": i, "verdict": "PASS"} for i in IDS]}
ROUTES = 'import { view } from "./serialize.js";\nr.get("/posts/:id", (q, s) => s.json(view(q)));\n'


# ---- groups ------------------------------------------------------------------------------------------------------------------

def test_a_group_is_the_file_inside_the_suite_unless_named(tmp_path):
    _tree(tmp_path, {"cases/feed.yaml": "- {id: feed.scroll, steps: [{goto: /}]}\n",
                     "cases/social/follow.yaml": "- {id: follow, steps: [{goto: /}]}\n"
                                                 "- {id: block, group: safety, steps: [{goto: /}]}\n"})
    prof = Profile(name="t", root=tmp_path, base_url="http://x", suites=["cases"])
    groups = {c.id: c.group for c in load_cases(prof)}
    assert groups == {"feed.scroll": "feed", "follow": "social/follow", "block": "safety"}
    assert [c.entry()["group"] for c in load_cases(prof)] == ["feed", "social/follow", "safety"]
    assert [c.id for c in load_cases(prof, ["group:social/*"])] == ["follow"]
    assert [c.id for c in load_cases(prof, ["group:feed", "group:safety"])] == ["feed.scroll", "block"]


def test_the_group_field_does_not_make_every_journey_look_edited(tmp_path):
    sel = select([{**d, "group": "core", "source": "cases/core.yaml"} for d in _ds(*IDS)], REF,   # REF: before groups
                 _diff("README.md"), {"sample": 0}, root=tmp_path)
    assert sel["ids"] == [] and sel["reasons"] == {}


def test_a_group_owns_a_file_no_journey_is_mapped_to(tmp_path):
    ds = _ds(*IDS, post_view={"group": "posts"}, post_comment={"source": "cases/posts.yaml"}, feed_scroll={"group": "feed"})
    groups = {"posts": {"paths": ["server/posts/*.ts"]}, "feed": {"paths": ["web/src/Feed*"]}}
    sel = select(ds, REF, _diff("server/posts/format.ts"), {"sample": 0}, root=tmp_path, groups=groups)
    assert sel["ids"] == ["post.view", "post.comment"]           # an entry without `group` falls back to its file
    assert sel["reasons"]["post.view"] == "group `posts` owns `server/posts/format.ts`"
    assert select(ds, REF, _diff("server/other.ts"), {}, root=tmp_path, groups=groups)["ids"] is None


# ---- reverse imports ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("spec,target", [
    ("./serialize.js", "server/serialize.ts"),      # TypeScript's ESM style names the .ts file .js
    ("./serialize", "server/serialize.ts"),
    ("../lib/fmt", "lib/fmt/index.ts"),
])
def test_a_helper_reaches_the_journeys_through_the_files_that_import_it(tmp_path, spec, target):
    _tree(tmp_path, {target: "export const view = (p) => p;\n",
                     "server/posts.ts": f'import {{ view }} from "{spec}";\nr.get(\n  "/posts/:id",\n  (q, s) => s.json(view(q)));\n',
                     "server/posts.test.ts": f'import {{ view }} from "{spec}";\nr.post("/me", x);\n'})
    sel = select(_ds(*IDS), REF, _diff(target), {"sample": 0}, root=tmp_path)
    assert sel["ids"] == ["post.view"]                           # not profile.edit: tests do not run in the app
    assert sel["reasons"]["post.view"] == f"uses `{target}` through `server/posts.ts` (`GET /posts/:id`)"


def test_require_dynamic_import_and_frontend_importers_count(tmp_path):
    _tree(tmp_path, {"web/src/format.ts": "export const f = 1;\n",
                     "web/src/Feed.tsx": 'const F = await import("./format");\n',
                     "server/me.js": 'const { f } = require("../web/src/format.ts");\napp.put("/me", h);\n'})
    sel = select(_ds(*IDS), REF, _diff("web/src/format.ts"), {"sample": 0}, root=tmp_path)
    assert sel["ids"] == ["feed.scroll", "profile.edit"]
    assert sel["reasons"]["feed.scroll"] == "uses `web/src/format.ts` through `web/src/Feed.tsx`, which ran in it"


def test_python_imports_relative_and_absolute(tmp_path):
    _tree(tmp_path, {"src/app/__init__.py": "", "src/app/util/__init__.py": "", "src/app/util/text.py": "def f(): pass\n",
                     "src/app/views.py": "from .util.text import f\n@router.get('/posts/{id}')\ndef view(): ...\n",
                     "src/app/feed.py": "import app.util.text as t\n@app.get('/feed')\ndef feed(): ...\n",
                     "src/app/me.py": "from app.util import (\n    text,  # helpers\n)\n@app.put('/me')\ndef me(): ...\n"})
    sel = select(_ds(*IDS), REF, _diff("src/app/util/text.py"), {"sample": 0}, root=tmp_path)
    assert sel["ids"] == ["post.view", "feed.scroll", "profile.edit"]
    assert "through `src/app/views.py`" in sel["reasons"]["post.view"]


def test_two_levels_of_importers_and_no_further(tmp_path):
    _tree(tmp_path, {"server/serialize.ts": "export const view = 1;\n",
                     "server/views.ts": 'export * from "./serialize";\n',              # a barrel: no routes of its own
                     "server/posts.ts": 'import { view } from "./views";\nr.get("/posts/:id", h);\n'})
    sel = select(_ds(*IDS), REF, _diff("server/serialize.ts"), {"sample": 0}, root=tmp_path)
    assert sel["reasons"] == {"post.view": "uses `server/serialize.ts` through `server/views.ts` and `server/posts.ts` "
                                           "(`GET /posts/:id`)"}
    _tree(tmp_path, {"server/base.ts": "export const b = 1;\n", "server/serialize.ts": 'import { b } from "./base";\n'})
    sel = select(_ds(*IDS), REF, _diff("server/base.ts"), {}, root=tmp_path)    # three levels up: unknown impact
    assert sel["ids"] is None and "no journey is known to use `server/base.ts`" in sel["why"]


def test_nobody_imports_it_runs_every_journey(tmp_path):
    _tree(tmp_path, {"server/util.ts": "export const u = 1;\n", "server/posts.ts": ROUTES})
    sel = select(_ds(*IDS), REF, _diff("server/util.ts"), {"budget_min": 0}, root=tmp_path)
    assert sel["ids"] is None and sel["why"] == "every journey: no journey is known to use `server/util.ts` (line 1)"


def test_the_file_list_comes_from_git_when_there_is_one(tmp_path):
    _tree(tmp_path, {"server/serialize.ts": "export const view = 1;\n", "server/posts.ts": ROUTES,
                     "server/scratch.ts": 'import { view } from "./serialize";\napp.put("/me", h);\n'})
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, capture_output=True, check=True)
    run("init", "-q")
    run("add", "server/serialize.ts", "server/posts.ts")              # scratch.ts is not part of the repository
    assert Repo(tmp_path).importers("server/serialize.ts") == ["server/posts.ts"]
    assert select(_ds(*IDS), REF, _diff("server/serialize.ts"), {"sample": 0}, root=tmp_path)["ids"] == ["post.view"]


# ---- a time budget -----------------------------------------------------------------------------------------------------------

def _big(n: int = 40) -> tuple[list[dict], dict]:
    """n journeys of 30 s: j0 critical, j1 failing, j2 edited, j3 calls the changed route, the rest untouched."""
    ids = [f"j{i}" for i in range(n)]
    ds = _ds(*ids, j0={"critical": True}, j2={"must": [{"no_errors": True}]})
    jmap = {i: {"routes": [f"GET /api/{i}"]} for i in ids}
    ref = {"journey_map": jmap, "dataset": _ds(*ids),
           "cases": [{"case_id": i, "verdict": "FAIL" if i == "j1" else "PASS", "duration_s": 30} for i in ids]}
    return ds, ref


def test_a_budget_never_drops_critical_edited_or_failing_journeys(tmp_path):
    _tree(tmp_path, {"server/a.ts": 'r.get("/j3", h);\n'})
    ds, ref = _big()
    sel = select(ds, ref, _diff("server/a.ts"), {"sample": 1.0, "budget_min": 2}, root=tmp_path, seed="pr-1")
    assert sel["ids"] == ["j0", "j1", "j2", "j3"]                   # 4 × 30 s fit; the 36 sampled ones do not
    assert sel["skipped"] == [] and "over the" not in sel["why"]    # a smaller sample is not a skipped journey
    sel = select(ds, ref, _diff("server/a.ts"), {"sample": 0, "budget_min": 1}, root=tmp_path)
    assert sel["ids"] == ["j0", "j1", "j2"] and sel["skipped"] == ["j3"]
    assert sel["why"] == "1 critical, 1 new or edited, 1 failing on the target, 1 over the 1-min budget"


def test_a_budget_ranks_every_journey_when_everything_would_run(tmp_path):
    _tree(tmp_path, {"server/a.ts": 'r.get("/j3", h);\n'})
    ds, ref = _big()
    d = _diff("package-lock.json") + _diff("server/a.ts")
    assert select(ds, ref, d, {"budget_min": 60}, root=tmp_path)["ids"] is None      # it all fits: every journey
    a = select(ds, ref, d, {"budget_min": 5}, root=tmp_path, seed="pr-1")
    b = select(ds, ref, d, {"budget_min": 5}, root=tmp_path, seed="pr-2")
    assert len(a["ids"]) == 10 and a["ids"][:4] == ["j0", "j1", "j2", "j3"] and len(a["skipped"]) == 30
    assert a["why"] == ("1 touched by this change, 1 critical, 1 new or edited, 1 failing on the target, "
                        "6 more since `package-lock.json` affects the whole app, 30 over the 5-min budget")
    assert a["reasons"][a["ids"][-1]] == "every journey: `package-lock.json` affects the whole app"
    assert set(a["ids"][4:]) != set(b["ids"][4:])                   # another pull request, other journeys dropped


def test_the_scope_says_what_the_budget_dropped(tmp_path):
    from assay.cli import _scope
    from assay.model import Case
    from assay.report import scope_line
    _tree(tmp_path, {"server/a.ts": 'r.get("/j3", h);\n'})
    ds, ref = _big()
    sel = select(ds, ref, _diff("package-lock.json"), {"budget_min": 5}, root=tmp_path, seed="x")
    cases, scope = _scope([Case.from_dict({"id": d["id"], "steps": [{"goto": "/"}]}) for d in ds], None, sel)
    assert len(cases) == 10 and scope["checked"] == 10 and scope["total"] == 40 and len(scope["skipped"]) == 30
    line = scope_line({"scope": scope, "cases": [], "summary": {}, "gate": {"status": "PASSED", "conditions": []}},
                      {"scope": scope})
    assert line == ("Checked 10 of 40 journeys (1 critical, 1 new or edited, 1 failing on the target, "
                    "7 more since `package-lock.json` affects the whole app, 30 over the 5-min budget)")
