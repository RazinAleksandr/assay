"""The agentic freedom test: what the agent is told after each action, and a whole session through the MCP tools."""
import json
from pathlib import Path

import pytest
import yaml

from assay.explore import changes, overview

HELLO = Path(__file__).resolve().parents[1] / "examples" / "hello" / ".assay"


def _chromium() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


def test_changes_are_told_record_by_record():
    before = {"comments": [{"id": 1, "body": "hi"}, {"id": 2, "body": "old"}], "settings": {"theme": "light"}}
    after = {"comments": [{"id": 2, "body": "new"}, {"id": 3, "body": "QA: the light is lovely"}], "settings": {"theme": "dark"}}
    lines = changes(before, after)
    assert '+ comments id=3 {"body": "QA: the light is lovely"}' in lines
    assert "- comments id=1" in lines
    assert '~ comments id=2: body old → new' in lines
    assert any(x.startswith("~ settings:") and "dark" in x for x in lines)
    assert changes(before, before) == []


def test_changes_without_ids_and_the_limit():
    assert changes({"notes": []}, {"notes": ["Buy oat milk"]}) == ['~ notes: [] → ["Buy oat milk"]']
    many = changes({"t": [{"id": i} for i in range(40)]}, {"t": []}, limit=5)
    assert len(many) == 6 and many[-1] == "… and 35 more changes"


def test_overview_names_what_the_agent_can_watch():
    lines = overview({"posts": [{"id": 1, "caption": "Slow start"}], "me": {"name": "mila"}, "count": 3})
    assert lines == ["count: 3", "me: 1 fields", 'posts: 1 records, e.g. {"id": 1, "caption": "Slow start"}']


@pytest.mark.skipif(not _chromium(), reason="Chromium is not installed (assay browsers)")
def test_a_session_through_the_mcp_tools(tmp_path):
    from assay import mcp
    call = lambda name, args: mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                          "params": {"name": name, "arguments": args}})["result"]
    text = lambda r: json.loads(r["content"][0]["text"])
    assert call("assay_explore_act", {"steps": []})["isError"]          # nothing open yet
    try:
        r = text(call("assay_explore_start", {"project": str(HELLO), "charter": "add and delete notes"}))
        assert r["data"] == ["notes: 0 records"] and 'button "Add note"' in r["tree"]
        r = call("assay_explore_act", {"steps": [{"fill": {"label": "New note", "value": "Buy oat milk"}},
                                                 {"click": {"role": "button", "name": "Add note"}}], "look": True})
        assert text(r)["data"] == ['~ notes: [] → ["Buy oat milk"]'] and r["content"][1]["type"] == "image"
        r = text(call("assay_explore_act", {"steps": [{"click": {"role": "button", "name": "No such button"}, "timeout": 800},
                                                      {"goto": "/never"}]}))
        assert r["ran"][0]["ok"] is False and len(r["ran"]) == 1             # stops at the first failing step
        assert r["budget_left"]["actions"] == 57 and r["budget_left"]["minutes"] > 19
        assert text(call("assay_explore_note", {"title": "A note survives a reload?", "kind": "question", "severity": "minor", "step": 2}))["noted"] == 1
        assert call("assay_explore_note", {"title": "x", "kind": "rant"})["isError"]
        mcp._SESSION["s"].budget["actions"] = 3                             # spent: acting is refused, noting is not
        assert call("assay_explore_act", {"steps": [{"reload": True}]})["isError"]
        done = text(call("assay_explore_finish", {}))
    finally:
        mcp._SESSION.pop("s", None) and None
    folder = Path(done["folder"])
    try:
        assert done["steps"] == 4 and done["findings"] == 1
        trail = yaml.safe_load((folder / "trail.yaml").read_text())[0]
        assert trail["steps"] == [{"goto": "/"}, {"fill": {"label": "New note", "value": "Buy oat milk"}},
                                  {"click": {"role": "button", "name": "Add note"}}]
        session = json.loads((folder / "session.json").read_text())
        assert session["steps"][2]["observation"]["data"] == ['~ notes: [] → ["Buy oat milk"]']
        assert "A note survives a reload?" in (folder / "findings.md").read_text()
    finally:
        import shutil
        shutil.rmtree(folder.parent, ignore_errors=True)
