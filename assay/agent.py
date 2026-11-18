"""Goal agent: a case can say WHAT the user wants (`goal: "Save the second post into a new collection"`)
instead of HOW. An LLM drives the real UI step by step through the same step DSL, reading the page's
accessibility tree.

Explore once, replay for free:
  * the first successful trajectory is cached (.assay-cache/<case>.json) and replayed with zero model calls;
  * accessible names carry volatile text ("Leo Martin · See you Saturday · 23h · 2 unread"): the cache keeps the
    stable part of such a name, and a replayed step that misses retries with it before anything else;
  * if the UI changed and a cached step still fails, the agent re-plans from there (self-healing) and the case is
    flagged `healed` → the gate raises a "needs review" issue instead of silently passing;
  * without a model (no Claude CLI, no ANTHROPIC_API_KEY) a trajectory that cannot be replayed is INCONCLUSIVE
    (owner: environment), never a product FAIL;
  * the agent never decides PASS: it only acts. The case's must/should checks decide, as for any case.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .driver import StepError, WebDriver

PROMPT = """You are operating a web application through a browser to achieve a user's goal, like a careful
manual tester. You see the page as an accessibility tree. Reply with exactly ONE next action as JSON.

## Goal
{goal}

## Actions so far
{history}

## Current page
URL: {url}
Accessibility tree (truncated):
{tree}

## Allowed replies (JSON only, no prose)
{{"action": <step>, "why": "<short>"}}   where <step> is ONE of:
  {{"goto": "/path"}}
  {{"click": <target>}}              {{"fill": {{...<target>, "value": "text"}}}}
  {{"press": "Enter"}}               {{"press": {{...<target>, "key": "Enter"}}}}
  {{"select": {{...<target>, "value": "option"}}}}   {{"check": <target>}}
  {{"upload": {{"css": "input[type=file]", "file": "<one of: {files}>"}}}}
  {{"wait_for": <target>}}           {{"scroll": "bottom"}}       {{"wait": 800}}
  <target> = {{"role": "button", "name": "Exact accessible name", "exact": true}} | {{"label": "Field label"}} |
             {{"placeholder": "..."}} | {{"text": "visible text"}} | {{"css": "selector"}}  (add "nth": i if ambiguous)
{{"done": true, "why": "<what on the page proves the goal is achieved>"}}
{{"give_up": true, "why": "<what blocks you>"}}

Rules: prefer role+name targets copied exactly from the tree; never invent data that the goal does not give;
after an action fails, try a different target; say done only when the page shows the goal is achieved.
"""


# relative times change between runs of the same UI; bare counters (unread badges) usually do too
_TIME = r"\d+\s?(?:s|m|h|d|w|mo|y|min|mins|hr|hrs|sec|secs)\b|just now\b|yesterday\b|\d+\s(?:second|minute|hour|day|week|month|year)s?\s+ago\b"
_TIME_RE = re.compile(r"\s(?:" + _TIME + ")", re.I)
_ANY_RE = re.compile(r"\s(?:" + _TIME + r"|\d+\b)", re.I)


def stable_name(name: str, times_only: bool = False) -> str | None:
    """The part of an accessible name before its first volatile token, if that differs from the name and still says enough."""
    m = (_TIME_RE if times_only else _ANY_RE).search(" " + name)
    if not m:
        return None
    head = (" " + name)[:m.start()].strip()
    return head if len(head) >= 3 and head != name else None


def relaxed(step: dict, times_only: bool = False) -> dict | None:
    """The same step with its role+name target matched by the stable part of the name (substring), or None."""
    if len(step) != 1:
        return None
    (verb, spec), = step.items()
    if not isinstance(spec, dict) or "role" not in spec or not isinstance(spec.get("name"), str):
        return None
    head = stable_name(spec["name"], times_only)
    if not head:
        return None
    return {verb: {**spec, "name": head, "exact": False}}


def _normalise(steps: list[dict]) -> list[dict]:
    """What goes into the cache: names cut before a relative time (bare numbers stay: "Add 2 items" is not volatile)."""
    return [relaxed(s, times_only=True) or s for s in steps]


def backend() -> str | None:
    if shutil.which("claude"):
        return "claude-cli"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


class GoalAgent:
    def __init__(self, profile, cache_dir: Path, log=print):
        self.p = profile
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cfg = profile.agent or {}
        self.model = cfg.get("model", "sonnet")
        self.max_steps = int(cfg.get("max_steps", 15))
        self.files = cfg.get("files", [])
        self.log = log
        self.calls = 0
        self.cost_usd = 0.0

    # -- public ---------------------------------------------------------------
    def achieve(self, case, drv: WebDriver, ev: dict) -> dict:
        key = hashlib.sha1(json.dumps([case.goal, case.setup, case.as_account]).encode()).hexdigest()[:12]
        cache = self.cache_dir / f"{case.id}.json"
        executed: list[dict] = []
        healed = False
        if cache.exists():
            data = json.loads(cache.read_text())
            if data.get("key") == key:
                ok, miss = True, ""
                for s in data["steps"]:
                    rec = self._run(drv, ev, s, by="cache")
                    if not rec["ok"] and (r := relaxed(s)):
                        rec = self._run(drv, ev, r, by="cache", why="cached name had volatile text; matched its stable part")
                        s = r
                    if not rec["ok"]:
                        ok, healed, miss = False, True, rec.get("error", "")
                        break
                    executed.append(s)
                if ok:
                    return {"reached": True, "mode": "replay", "healed": False, "steps": len(executed), "calls": 0}
        if not backend():
            why = f"cached step {len(executed) + 1} failed ({miss.splitlines()[0][:160]}) and " if healed else "no cached trajectory and "
            return {"reached": False, "unavailable": True,
                    "reason": why + "no model to re-plan (Claude CLI not found, ANTHROPIC_API_KEY not set)"}
        history: list[str] = [f"{i}. {json.dumps(s)} → ok (replayed)" for i, s in enumerate(executed)]
        calls0 = self.calls
        for _ in range(self.max_steps):
            reply = self._ask(case.goal, history, drv)
            if reply is None:
                return {"reached": False, "reason": "agent reply unreadable", "healed": healed}
            if isinstance(reply.get("action"), dict) and ("done" in reply["action"] or "give_up" in reply["action"]):
                reply = reply["action"]          # tolerate {"action": {"done": true}}
            if reply.get("done"):
                cache.write_text(json.dumps({"key": key, "goal": case.goal, "steps": _normalise(executed)}, indent=1, ensure_ascii=False))
                return {"reached": True, "mode": "healed" if healed else "explored", "healed": healed,
                        "steps": len(executed), "calls": self.calls - calls0, "why": reply.get("why", "")}
            if reply.get("give_up"):
                return {"reached": False, "reason": f"agent gave up: {reply.get('why', '')}", "healed": healed}
            step = reply.get("action")
            if not isinstance(step, dict):
                history.append(f"{len(history)}. invalid reply {json.dumps(reply)[:120]}")
                continue
            rec = self._run(drv, ev, step, by="agent", why=reply.get("why", ""))
            history.append(f"{len(history)}. {json.dumps(step, ensure_ascii=False)} → "
                           + ("ok" if rec["ok"] else f"FAILED: {rec.get('error', '')[:160]}"))
            if rec["ok"]:
                executed.append(step)
        return {"reached": False, "reason": f"goal not reached in {self.max_steps} steps", "healed": healed}

    # -- internals ------------------------------------------------------------
    def _run(self, drv: WebDriver, ev: dict, step: dict, by: str, why: str = "") -> dict:
        from .runner import _safe_name, _step_summary
        i = len(ev["steps"])
        rec: dict[str, Any] = {"i": i, "action": _safe_name(step), "summary": _step_summary(step), "ok": True, "by": by}
        if why:
            rec["why"] = why[:200]
        try:
            rec.update(drv.run(step))
            drv.settle(idle_ms=300, max_ms=2500)
            rec["screenshot"] = drv.screenshot(f"step-{i:02d}")
        except StepError as e:
            rec.update(ok=False, error=str(e))
        ev["steps"].append(rec)
        return rec

    def _ask(self, goal: str, history: list[str], drv: WebDriver) -> dict | None:
        page = drv.page
        try:
            tree = page.locator("body").aria_snapshot(timeout=5000)
        except Exception as e:
            tree = f"(accessibility tree unavailable: {e})"
        prompt = PROMPT.format(goal=goal, history="\n".join(history[-20:]) or "(none yet)", url=page.url,
                               tree=tree[:14000], files=", ".join(self.files) or "none")
        self.calls += 1
        try:
            if backend() == "claude-cli":
                out = subprocess.run(["claude", "-p", prompt, "--model", self.model, "--output-format", "json", "--max-turns", "1"],
                                     capture_output=True, text=True, timeout=180)
                data = json.loads(out.stdout)
                self.cost_usd += float(data.get("total_cost_usd") or 0)
                raw = data.get("result", "")
            else:                       # a CI runner: the same model through the API
                from .judge import Judge
                raw = Judge({"backend": "anthropic", "model": self.model})._anthropic(prompt, None)
        except Exception:
            return None
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
