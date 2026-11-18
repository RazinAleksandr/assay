"""Datasets (`each:` on a case: one journey over many rows) and the `bench:` section of assay.yaml."""
import fnmatch
import json
from argparse import Namespace
from pathlib import Path

import pytest

import assay.bench as B
from assay import cli
from assay.bench import _md, resolve_bench, stats
from assay.config import Profile, load_cases, load_profile
from assay.guard import weakened
from assay.uploads import check_analysis, check_bench
from test_access import _analysis

TEMPLATE = """
- id: draft.reply
  title: The assistant drafts a reply to {row.ticket}
  promise: The draft offers what the customer is owed
  ai: true
  critical: true
  group: assistant
  tags: [ai, "{row.expect}"]
  each: %s
  setup:
    - http: {method: POST, path: /api/tickets, json: {ref: "{row.ticket}", note: "{vars.uid}"}}
  expect_before: {tickets: {contains: {ref: "{row.ticket}"}}}
  steps:
    - goto: /?t={row.ticket}&from={env.ORIGIN}
    - click: {role: button, name: Draft reply}
  must:
    - judge: The draft offers a {row.expect}
    - answer: {contains: "{row.expect}"}
  should:
    - text: "{row.ticket}"
"""
INLINE = "[{id: late-delivery, ticket: T-1001, expect: refund}, {id: wrong-size, ticket: T-1002, expect: exchange}]"


def _project(tmp_path: Path, cases: str, files: dict[str, str] | None = None) -> Profile:
    (tmp_path / "cases").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cases" / "ai.yaml").write_text(cases)
    for name, text in (files or {}).items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    return Profile(name="relay", root=tmp_path, base_url="http://127.0.0.1:5410", suites=["cases"])


# ---- each: ------------------------------------------------------------------------------------------------------------------

def test_inline_rows_make_one_journey_each(tmp_path):
    a, b = load_cases(_project(tmp_path, TEMPLATE % INLINE))
    assert [a.id, b.id] == ["draft.reply[late-delivery]", "draft.reply[wrong-size]"]
    assert a.title == "The assistant drafts a reply to T-1001"                    # names a row field: as written
    assert a.promise == "The draft offers what the customer is owed (late-delivery)"   # names none: the row tells them apart
    assert a.must == [{"judge": "The draft offers a refund"}, {"answer": {"contains": "refund"}}]
    assert b.must[0] == {"judge": "The draft offers a exchange"} and b.should == [{"text": "T-1002"}]
    assert a.tags == ["ai", "refund"] and a.expect_before == {"tickets": {"contains": {"ref": "T-1001"}}}
    assert (a.ai, a.critical, a.group, a.source) == (True, True, "assistant", "cases/ai.yaml")
    assert a.raw["row"] == {"id": "late-delivery", "ticket": "T-1001", "expect": "refund"} and "each" not in a.raw
    e = a.entry()                                     # what the guard and the selection compare
    assert e["id"] == a.id and e["must"] == a.must and e["journey"]["steps"][0] == {"goto": "/?t=T-1001&from={env.ORIGIN}"}


def test_run_time_placeholders_are_left_for_run_time(tmp_path):
    a = load_cases(_project(tmp_path, TEMPLATE % INLINE))[0]
    assert a.setup[0]["http"]["json"] == {"ref": "T-1001", "note": "{vars.uid}"}
    assert a.steps[0]["goto"] == "/?t=T-1001&from={env.ORIGIN}"


def test_a_goal_and_nested_fields(tmp_path):
    cases = """
- id: ask
  goal: Ask the assistant about {row.customer.name}'s order
  each: [{customer: {name: Mila}}]
"""
    (c,) = load_cases(_project(tmp_path, cases))
    assert c.id == "ask[1]" and c.goal == "Ask the assistant about Mila's order"


def test_rows_from_csv_json_and_yaml_files(tmp_path):
    cases = "".join(f"""
- id: {kind}
  title: Reply to {{row.ticket}}
  each: fixtures/tickets.{kind}
  steps: [{{goto: "/?t={{row.ticket}}&n={{row.n}}"}}]
""" for kind in ("csv", "json", "yaml"))
    files = {"fixtures/tickets.csv": "﻿id,ticket,n\nlate,T-1,1\n,T-2,2\n",          # a spreadsheet's BOM; a blank id
             "fixtures/tickets.json": json.dumps([{"id": "late", "ticket": "T-1", "n": 1}, {"ticket": "T-2", "n": 2}]),
             "fixtures/tickets.yaml": "- {id: late, ticket: T-1, n: 1}\n- {ticket: T-2, n: 2}\n"}
    got = {c.id: c for c in load_cases(_project(tmp_path, cases, files))}
    assert list(got) == ["csv[late]", "csv[2]", "json[late]", "json[2]", "yaml[late]", "yaml[2]"]
    assert got["csv[2]"].steps == got["json[2]"].steps == got["yaml[2]"].steps == [{"goto": "/?t=T-2&n=2"}]
    assert got["csv[late]"].raw["row"] == {"id": "late", "ticket": "T-1", "n": "1"}     # csv values are text
    assert got["json[late]"].raw["row"]["n"] == 1 and got["yaml[2]"].title == "Reply to T-2"


def test_rows_without_ids_are_numbered_from_one(tmp_path):
    cases = "- {id: t, title: Same title, each: [{q: a}, {q: b}, {id: 7, q: c}], steps: [{ask: '{row.q}'}]}\n"
    assert [(c.id, c.title) for c in load_cases(_project(tmp_path, cases))] == \
        [("t[1]", "Same title (1)"), ("t[2]", "Same title (2)"), ("t[7]", "Same title (7)")]


def test_k_selects_one_row_exactly_or_all_rows(tmp_path):
    prof = _project(tmp_path, TEMPLATE % INLINE + "- {id: draft.reply_v2, steps: [{goto: /}]}\n")
    assert not fnmatch.fnmatch("draft.reply[late-delivery]", "draft.reply[late-delivery]")   # why the exact match comes first
    assert [c.id for c in load_cases(prof, ["draft.reply[late-delivery]"])] == ["draft.reply[late-delivery]"]
    assert [c.id for c in load_cases(prof, ["draft.reply"])] == ["draft.reply[late-delivery]", "draft.reply[wrong-size]"]
    assert [c.id for c in load_cases(prof, ["draft.reply*"])] == ["draft.reply[late-delivery]", "draft.reply[wrong-size]",
                                                                 "draft.reply_v2"]
    assert [c.id for c in load_cases(prof, ["tag:exchange"])] == ["draft.reply[wrong-size]"]


@pytest.mark.parametrize("each, files, match", [
    ("[{id: a, ticket: T, expect: x}, {id: a, ticket: T, expect: y}]", {}, "duplicate case id 'draft.reply\\[a\\]'"),
    ("[{id: '2', ticket: T, expect: x}, {ticket: T, expect: y}]", {}, "duplicate case id"),      # the second row is number 2
    ("[{ticket: T, expect: x}, just-a-string]", {}, "row 2 of `each` must be a mapping"),
    ("[]", {}, "non-empty list"),
    ("fixtures/rows.json", {"fixtures/rows.json": '{"rows": []}'}, "non-empty list"),
    ("fixtures/rows.csv", {"fixtures/rows.csv": "ticket,expect\n"}, "non-empty list"),
    ("fixtures/rows.txt", {"fixtures/rows.txt": "x"}, "a .csv, .json or .yaml file"),
    ("[{ticket: T}]", {}, "no field 'expect' for {row.expect}"),
    ("[{id: a/b, ticket: T, expect: x}]", {}, "must be a plain name"),
])
def test_bad_datasets_are_configuration_errors(tmp_path, each, files, match):
    with pytest.raises(ValueError, match=match):
        load_cases(_project(tmp_path, TEMPLATE % each, files))


def test_a_missing_rows_file_is_named(tmp_path):
    with pytest.raises(FileNotFoundError, match="tickets.csv"):
        load_cases(_project(tmp_path, TEMPLATE % "fixtures/tickets.csv"))


def test_the_guard_sees_rows_as_journeys(tmp_path):
    two = [c.entry() for c in load_cases(_project(tmp_path, TEMPLATE % INLINE))]
    one, three = two[:1], two + [{**two[0], "id": "draft.reply[damaged]"}]
    ref = {"dataset": two}
    assert [w["phrase"] for w in weakened(ref, one, None)] == \
        ["removed journey `draft.reply[wrong-size]` (The draft offers what the customer is owed (wrong-size))"]
    assert weakened(ref, three, None) == []                                    # a new row is a new journey: nothing weaker
    edited = [{**two[0], "must": [{"judge": "The draft is polite"}, two[0]["must"][1]]}, two[1]]
    assert [w["phrase"] for w in weakened(ref, edited, None)] == ["changed 1 `judge` check of `draft.reply[late-delivery]`"]


def test_a_rows_brief_can_be_pasted_into_a_shell_and_curl(tmp_path):
    from assay.brief import build
    cid = "draft.reply[late-delivery]"
    (tmp_path / "cases" / cid).mkdir(parents=True)
    (tmp_path / "cases" / cid / "evidence.json").write_text("{}")
    case = {"case_id": cid, "title": "Reply", "verdict": "FAIL", "cause": "must:judge", "items": [], "steps": [],
            "screenshot": f"cases/{cid}/final.png"}
    (tmp_path / "analysis.json").write_text(json.dumps({"app": {"name": "relay"}, "gate": {"status": "FAILED", "conditions": []},
                                                        "cases": [case], "issues": [], "run": {"project": ".assay"}}))
    f = build(tmp_path, base="http://assay/a/1", link_query="?k=v")["failures"][0]
    assert f["repro"] == "assay run .assay -k 'draft.reply[late-delivery]' --no-scan"
    assert f["artifacts"]["screenshot"] == "http://assay/a/1/cases/draft.reply%5Blate-delivery%5D/final.png?k=v"


def test_the_server_accepts_rows_ids_and_their_files():
    a = _analysis()
    a["cases"] = [{"case_id": "draft.reply[late-delivery]", "verdict": "PASS", "screenshot": "cases/draft.reply[late-delivery]/final.png"}]
    a["dataset"] = [{"id": "draft.reply[late-delivery]", "must": [], "should": []}]
    out = check_analysis(a)
    assert out["cases"][0]["screenshot"] == "cases/draft.reply[late-delivery]/final.png"
    a["cases"][0]["screenshot"] = "cases/[x]/../../etc/passwd"
    with pytest.raises(ValueError):
        check_analysis(a)


# ---- bench: -----------------------------------------------------------------------------------------------------------------

BENCH = """
bench:
  attempts: 2
  select: [is:ai]
  arms:
    sonnet: env:ASSISTANT_MODEL=sonnet
    haiku: env:ASSISTANT_MODEL=haiku
"""


def _profile(tmp_path: Path, extra: str = BENCH) -> Profile:
    (tmp_path / "assay.yaml").write_text("name: relay\nbase_url: http://127.0.0.1:5410\n"
                                         "app: {start: {cmd: node server.mjs}}\n" + extra)
    return load_profile(tmp_path)


def _args(**kw):
    return Namespace(**{"arm": None, "attempts": None, "k": None, **kw})


def test_the_profile_bench_section_is_the_default(tmp_path):
    assert resolve_bench(_profile(tmp_path), _args()) == \
        (["sonnet=env:ASSISTANT_MODEL=sonnet", "haiku=env:ASSISTANT_MODEL=haiku"], 2, ["is:ai"])


def test_the_command_line_overrides_the_profile(tmp_path):
    prof = _profile(tmp_path)
    got = resolve_bench(prof, _args(arm=["a=env:M=1"], attempts=5, k=["draft.*"]))
    assert got == (["a=env:M=1"], 5, ["draft.*"])
    assert resolve_bench(prof, _args(attempts=1))[0][0] == "sonnet=env:ASSISTANT_MODEL=sonnet"   # only what is given


def test_without_any_bench_settings(tmp_path):
    assert resolve_bench(_profile(tmp_path, ""), _args()) == ([], 3, None)        # A/A, 3 attempts, the `ai: true` journeys
    assert resolve_bench(_profile(tmp_path, "bench: {select: tag:smoke}"), _args())[2] == ["tag:smoke"]


def test_profile_arms_keep_the_arm_syntax_and_folders_are_relative_to_the_project(tmp_path):
    (tmp_path / "base").mkdir()
    (tmp_path / "base" / "assay.yaml").write_text("base_url: http://127.0.0.1:5411\n")
    prof = _profile(tmp_path, "bench: {arms: {main: base, prod: 'https://app.example.com', same: ''}}")
    specs = resolve_bench(prof, _args())[0]
    assert specs == [f"main={tmp_path.resolve() / 'base'}", "prod=https://app.example.com", "same="]


@pytest.mark.parametrize("extra, args", [("bench: {attempts: 0}", {}), ("bench: {attempts: three}", {}), ("", {"attempts": 0}),
                                         ("bench: {arms: [env:M=1]}", {}), ("bench: true", {})])
def test_bad_bench_settings(tmp_path, extra, args):
    with pytest.raises(ValueError):
        resolve_bench(_profile(tmp_path, extra), _args(**args))


def test_the_command_line_leaves_unset_flags_unset(tmp_path, monkeypatch):
    _profile(tmp_path)
    seen = {}
    monkeypatch.setattr(B, "bench", lambda profile, args, log=print: seen.update(profile=profile, args=args) or 0)
    assert cli.main(["bench", str(tmp_path)]) == 0
    assert (seen["args"].attempts, seen["args"].arm, seen["args"].k) == (None, None, None)
    assert resolve_bench(seen["profile"], seen["args"])[1] == 2
    assert cli.main(["bench", str(tmp_path), "--attempts", "4"]) == 0 and seen["args"].attempts == 4


# ---- a benchmark run, without browsers --------------------------------------------------------------------------------------

def test_bench_runs_the_profile_arms_and_times_the_answers(tmp_path, monkeypatch):
    prof = _project(tmp_path, TEMPLATE % INLINE + "- {id: smoke, steps: [{goto: /}]}\n")
    prof.app = {"start": {"cmd": "node server.mjs"}}
    prof.raw = {"bench": {"attempts": 2, "arms": {"sonnet": "env:M=sonnet", "haiku": "env:M=haiku"}}}
    latency = {"sonnet": 3.0, "haiku": 1.0}
    calls = []

    class App:
        def __init__(self, p, log): pass
        def is_up(self): return True

    class Browsers:
        def __init__(self, p, headed=False): self.p = p
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def run_case(c, p, browsers, run_dir, **kw):
        arm = p.app["start"]["env"]["M"]
        calls.append((arm, c.id))
        (run_dir / "cases" / c.id).mkdir(parents=True, exist_ok=True)
        return {"duration_s": 10.0, "answers": [{"latency_s": latency[arm] + len(calls) / 100}]}

    def analyze(run_dir, p, judge=None, log=print):
        ids = [d.name for d in sorted((run_dir / "cases").iterdir())]
        return {"cases": [{"case_id": i, "verdict": "PASS"} for i in ids], "summary": {"PASS": len(ids), "FAIL": 0, "INCONCLUSIVE": 0}}

    monkeypatch.setattr("assay.runner.AppProcess", App)
    monkeypatch.setattr(B, "Browsers", Browsers)
    monkeypatch.setattr(B, "run_case", run_case)
    monkeypatch.setattr(B, "analyze", analyze)
    monkeypatch.delenv("ASSAY_HOST_URL", raising=False)
    args = Namespace(arm=None, attempts=None, k=None, run_id="b1", out=str(tmp_path / "runs"), no_start=True,
                     no_reset=True, headed=False, no_judge=True)
    assert B.bench(prof, args, log=lambda m: None) == 0
    assert len(calls) == 2 * 2 * 2 and {c for _, c in calls} == {"draft.reply[late-delivery]", "draft.reply[wrong-size]"}
    res = json.loads((tmp_path / "runs" / "b1" / "bench.json").read_text())
    assert res["baseline"] == "sonnet" and res["attempts"] == 2
    meta = res["arms_meta"]
    assert meta["sonnet"]["median_journey_s"] == 10.0 and 3.0 < meta["sonnet"]["median_answer_s"] < 3.1
    assert 1.0 < meta["haiku"]["median_answer_s"] < 1.1 and meta["haiku"]["env"] == {"M": "haiku"}
    md = (tmp_path / "runs" / "b1" / "bench.md").read_text()
    assert "| median journey | median answer |" in md and "| 10.0 s |" in md
    assert check_bench(res)["arms_meta"]["haiku"]["median_answer_s"] == meta["haiku"]["median_answer_s"]


def test_bench_md_without_answers(tmp_path):
    res = stats({"A": {"x": ["PASS"]}, "A'": {"x": ["FAIL"]}})
    res["arms_meta"] = {"A": {"median_journey_s": 4.2, "median_answer_s": None}, "A'": {}}
    row = [line for line in _md(res, Profile(name="t", root=tmp_path, base_url="http://x")).splitlines() if line.startswith("| A |")][0]
    assert row.endswith("| 4.2 s | — |")


def test_row_fields_fill_state_paths_in_keys(tmp_path):
    from assay.config import load_cases, load_profile
    (tmp_path / "assay.yaml").write_text("name: x\nbase_url: http://127.0.0.1:1\n")
    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / "ai.yaml").write_text(
        "- id: draft\n  each: [{id: a, ticket: T-1, cat: refund}]\n  steps: [{goto: '/?t={row.ticket}'}]\n"
        "  must:\n    - state: {'tickets.{row.ticket}.draft.category': '{row.cat}'}\n"
        "    - allowed_changes: ['tickets.{row.ticket}.draft']\n")
    c, = load_cases(load_profile(tmp_path))
    assert c.must == [{"state": {"tickets.T-1.draft.category": "refund"}}, {"allowed_changes": ["tickets.T-1.draft"]}]
