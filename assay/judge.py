"""LLM / VLM judge: decides meaning, one criterion per call.

Design rules:
  * one condition per item, explicit `cannot_tell` → UNDECIDED (never a silent FAIL);
  * judge `must` items vote (majority of N) — a 90%-TPR judge on 78 criteria produced
    ~8 false FAILs per run; 3 votes cut a 10% single-call error to ~3%;
  * every verdict is cached by (criterion, evidence hash, model) so re-grading is hermetic and free;
  * the judge sees the final screenshot (it can look at canvases, games, layouts) and the page text.

Backends: claude-cli (uses the local Claude Code login, no API key), anthropic (API key), none.
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

from .model import NOK, OK, UNDECIDED

PROMPT = """You are a strict QA judge for a software product. Decide ONE criterion about the final
state of a user journey, using only the evidence below. Do not guess: if the evidence does not
show it, answer cannot_tell.

## Journey
{journey}

## Criterion
{criterion}

## Evidence
- Final URL: {url}
- Visible page text (truncated):
\"\"\"
{text}
\"\"\"
{answer}{image}

Reply with ONLY a JSON object, no prose:
{{"verdict": "pass" | "fail" | "cannot_tell", "reason": "<one short sentence citing what you saw>"}}
"""


class Judge:
    def __init__(self, cfg: dict, cache_path: Path | None = None):
        self.backend = os.environ.get("ASSAY_JUDGE") or cfg.get("backend", "claude-cli")
        # a CI runner has no Claude Code login: with an API key the same profile judges through the API
        if self.backend == "claude-cli" and not shutil.which("claude") and os.environ.get("ANTHROPIC_API_KEY"):
            self.backend = "anthropic"
        self.model = cfg.get("model", "sonnet")
        self.must_votes = int(cfg.get("must_votes", 3))
        self.should_votes = int(cfg.get("should_votes", 1))
        self.cache_path = cache_path
        self.cache: dict[str, dict] = {}
        self.calls = 0
        self.cost_usd = 0.0
        if cache_path and cache_path.exists():
            self.cache = json.loads(cache_path.read_text())

    @property
    def available(self) -> bool:
        if self.backend == "none":
            return False
        if self.backend == "claude-cli":
            return shutil.which("claude") is not None
        if self.backend == "anthropic":
            return bool(os.environ.get("ANTHROPIC_API_KEY"))
        return False

    def save(self) -> None:
        if self.cache_path:
            self.cache_path.write_text(json.dumps(self.cache, indent=1, ensure_ascii=False))

    def decide(self, criterion: str, ev: dict, case_dir: Path, level: str, on: str = "auto",
               screenshot: str | None = None) -> tuple[str, str]:
        votes = self.must_votes if level == "must" else self.should_votes
        shot = f"{screenshot}.png" if screenshot else ev.get("final_screenshot")
        image = case_dir / shot if shot and on in ("auto", "screenshot") else None
        if image is not None and not image.exists():
            image = None
        journey = _journey(ev)
        text = (ev.get("page_text") or "")[:6000] if on in ("auto", "text") else "(not provided)"
        answer = ""
        if ev.get("answers"):
            answer = "- Assistant answer to the last message:\n\"\"\"\n" + ev["answers"][-1].get("answer", "")[:6000] + "\n\"\"\"\n"
        prompt = PROMPT.format(journey=journey, criterion=criterion, url=ev.get("final_url", ""), text=text,
                               answer=answer, image=f"- Screenshot of the final screen: {image}\n  (open it with the Read tool and look at it carefully)" if image else "")
        img_hash = hashlib.sha1(image.read_bytes()).hexdigest()[:16] if image else ""
        key = hashlib.sha1(json.dumps([criterion, journey, text, answer, img_hash, self.model, self.backend]).encode()).hexdigest()
        results = list(self.cache.get(key, {}).get("votes", []))
        while len(results) < votes:
            r = self._call(prompt, image)
            if r is None:
                break
            results.append(r)
        if results:
            self.cache[key] = {"criterion": criterion, "votes": results}
        if len(results) < votes and not results:
            return UNDECIDED, "judge unavailable"
        tally = {"pass": 0, "fail": 0, "cannot_tell": 0}
        for r in results:
            tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
        n = len(results)
        if tally["pass"] * 2 > n:
            status = OK
        elif tally["fail"] * 2 > n:
            status = NOK
        else:
            status = UNDECIDED
        reasons = [r["reason"] for r in results if (r["verdict"] == "pass") == (status == OK)] or [results[0]["reason"]]
        vote_str = f"[{tally['pass']}✓ {tally['fail']}✗" + (f" {tally['cannot_tell']}?" if tally["cannot_tell"] else "") + "] " if n > 1 else ""
        return status, vote_str + reasons[0][:300]

    # -- backends -------------------------------------------------------------
    def _call(self, prompt: str, image: Path | None) -> dict | None:
        self.calls += 1
        try:
            if self.backend == "claude-cli":
                raw = self._claude_cli(prompt, image)
            elif self.backend == "anthropic":
                raw = self._anthropic(prompt, image)
            else:
                return None
        except Exception as e:  # backend down → undecided, never FAIL
            self.last_error = str(e)[:200]
            return None
        return _parse(raw)

    def _claude_cli(self, prompt: str, image: Path | None) -> str:
        cmd = ["claude", "-p", prompt, "--model", self.model, "--output-format", "json", "--max-turns", "4"]
        if image:
            cmd += ["--allowedTools", "Read"]
        cwd = str(image.parent) if image else None
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=240, cwd=cwd)
        if out.returncode != 0:
            raise RuntimeError(out.stderr[-300:] or out.stdout[-300:])
        data = json.loads(out.stdout)
        self.cost_usd += float(data.get("total_cost_usd") or 0)
        return data.get("result", "")

    def _anthropic(self, prompt: str, image: Path | None) -> str:
        import base64
        import urllib.request
        content: list[dict[str, Any]] = []
        if image:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                        "data": base64.b64encode(image.read_bytes()).decode()}})
        content.append({"type": "text", "text": prompt.replace("(open it with the Read tool and look at it carefully)", "(attached above)")})
        model = {"sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5", "opus": "claude-opus-5-5"}.get(self.model, self.model)
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({"model": model, "max_tokens": 400, "messages": [{"role": "user", "content": content}]}).encode(),
            headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        return "".join(b.get("text", "") for b in data.get("content", []))


def _parse(raw: str) -> dict | None:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    v = str(d.get("verdict", "")).lower().strip()
    if v not in ("pass", "fail", "cannot_tell"):
        return None
    return {"verdict": v, "reason": str(d.get("reason", ""))}


def _journey(ev: dict) -> str:
    lines = []
    if ev.get("title"):
        lines.append(f"Case: {ev['title']}")
    if ev.get("goal"):
        lines.append(f"Goal: {ev['goal']}")
    for s in ev.get("steps", [])[:25]:
        mark = "✓" if s.get("ok") else "✗"
        lines.append(f"{mark} {s.get('action')} {s.get('summary', '')}".rstrip())
    return "\n".join(lines) or "(none)"
