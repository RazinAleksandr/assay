"""The agentic freedom test: a coding agent uses the product freely, like a curious person with a charter, while Assay
records everything and shows the agent what a screen never shows.

The agent is the person's own (Claude Code, Codex, Cursor through `assay mcp`); Assay is the harness. It starts the app
from its golden state, keeps one browser open, runs the agent's actions in the step language journeys use, and after
every action answers with what changed:

  page    the URL, the title and the accessibility tree: what the agent can act on next
  data    what changed in the app's state (every probe of assay.yaml, compared with before the action)
  errors  new console and page errors, failed and 4xx/5xx first-party requests
  calls   the API calls the action made
  shot    the screenshot file (and the image itself when the agent asks to look)

So the agent sees a comment saved cut short even when the screen says "Saved". Findings are noted with the step they
came from. `finish` writes everything to .assay/runs/explore-<time>/: session.json (every step with its observation),
findings.md, and trail.yaml (the steps that worked, as a draft journey). A finding becomes a regression journey only
after it reproduces: the explore skill writes the journey and keeps it, tagged known-bug, once it fails twice on a fresh
app for the stated reason. Nothing here calls a model.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import yaml

from .config import load_plugins, load_profile
from .driver import StepError, WebDriver
from .model import now_iso
from .probes import snapshot

TREE_CHARS = 9000          # the accessibility tree the agent reads after each action
BUDGET = {"actions": 60, "minutes": 20}   # per exploration: enforced here, not left to the agent's discipline
KINDS = ("bug", "ux", "question", "idea")
SEVERITIES = ("blocker", "critical", "major", "minor")


def _short(v: Any, n: int = 120) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[: n - 1] + "…"


def _key(before: list, after: list) -> str | None:
    """The field that names each record in both lists, if one does: present in every record and unique in each list.
    Two comments by the same `name` are two records, not one edited."""
    rows = before + after
    for k in ("id", "uuid", "key", "slug", "name"):
        if rows and all(isinstance(x, dict) and k in x for x in rows) and all(
                len({json.dumps(x[k], default=str) for x in side}) == len(side) for side in (before, after)):
            return k
    return None


def changes(before: dict, after: dict, limit: int = 25) -> list[str]:
    """What changed in the app's data, record by record where records have ids, in lines an agent reads at a glance:
    `+ comments id=7 {"body": "QA…"}`, `~ comments id=3: body "a" → "b"`, `- posts id=2`, `settings: {...} → {...}`."""
    out: list[str] = []
    for name in sorted(set(before or {}) | set(after or {})):
        b, a = (before or {}).get(name), (after or {}).get(name)
        if b == a:
            continue
        key = _key(b, a) if isinstance(b, list) and isinstance(a, list) else None
        if key:
            mb, ma = {f"{key}={x[key]}": x for x in b}, {f"{key}={x[key]}": x for x in a}
            out += [f"+ {name} {k} {_short({f: v for f, v in ma[k].items() if f != key}, 200)}" for k in ma if k not in mb]
            out += [f"- {name} {k}" for k in mb if k not in ma]
            for k in mb.keys() & ma.keys():
                if mb[k] != ma[k]:
                    fields = sorted(f for f in set(mb[k]) | set(ma[k]) if mb[k].get(f) != ma[k].get(f))
                    out.append(f"~ {name} {k}: " + "; ".join(f"{f} {_short(mb[k].get(f), 60)} → {_short(ma[k].get(f), 60)}"
                                                           for f in fields[:6]))
        else:
            out.append(f"~ {name}: {_short(b, 160)} → {_short(a, 160)}")
    return out[:limit] + ([f"… and {len(out) - limit} more changes"] if len(out) > limit else [])


def overview(state: dict) -> list[str]:
    """The data the agent can watch, at the start: one line per probe."""
    out = []
    for name, v in sorted(state.items()):
        size = f"{len(v)} records" if isinstance(v, list) else f"{len(v)} fields" if isinstance(v, dict) else _short(v, 60)
        out.append(f"{name}: {size}" + (f", e.g. {_short(v[0], 140)}" if isinstance(v, list) and v else ""))
    return out


def _yaml(data) -> str:
    """YAML the way journeys are written: one step per line, targets inline."""
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120, default_flow_style=None)


class Session:
    """One exploration: the app, one browser context, the steps taken, what each one changed, the findings."""

    def __init__(self, project: str | Path = ".assay", *, account: str | None = None, viewport: str | None = None,
                 charter: str = "", fresh: bool = True, start_url: str = "/", out: str | Path | None = None,
                 actions: int | None = None, minutes: float | None = None):
        self.profile = load_profile(project)
        load_plugins(self.profile)
        self.account, self.viewport, self.charter = account, viewport, charter.strip()
        self.fresh, self.start_url = fresh, start_url or "/"
        self.dir = Path(out or self.profile.root / "runs") / time.strftime("explore-%Y%m%d-%H%M%S")
        (self.dir / "shots").mkdir(parents=True, exist_ok=True)
        self.steps: list[dict] = []
        self.findings: list[dict] = []
        self.started = now_iso()
        self.budget = {"actions": int(actions or BUDGET["actions"]), "minutes": float(minutes or BUDGET["minutes"])}
        self._t0 = time.time()
        self.note_on_app = ""
        self._proc = self._browsers = self._context = None
        self._seen = {"console": 0, "page_errors": 0, "failed_requests": 0, "bad_responses": 0, "api": 0}

    # -- lifecycle ---------------------------------------------------------------------------------------------------
    def start(self) -> dict:
        from .runner import AppProcess, Browsers, login
        from .sensors import Recorder
        p = self.profile
        self._proc = AppProcess(p, self.dir / "app.log")
        if self._proc.is_up():
            self.note_on_app = (f"used the app already running at {self._proc.ready_url}: its data was not reset"
                                if p.app.get("start") else f"testing the deployed app at {p.base_url}")
        elif p.app.get("start"):
            if self.fresh:
                self._proc.reset()
            self._proc.start()
            self.note_on_app = "started the app" + (" from its golden state (app.reset)" if self.fresh and p.app.get("reset") else "")
        else:
            raise RuntimeError(f"the app is not reachable at {self._proc.ready_url} and assay.yaml has no app.start")
        self._browsers = Browsers(p).__enter__()      # finish() closes it, also when what follows fails
        self._context, self.engine = self._browsers.new_context(self.viewport)
        self._context.set_default_timeout(int(p.browser.get("timeout_ms", 10000)))
        self.recorder = Recorder(p.base_url, p.ignore, p.raw.get("first_party"))
        self.recorder.attach(self._context)
        self.recorder.phase = "act"
        page = self._context.new_page()
        self.drv = WebDriver(p, page, self._context, self.dir / "shots", self.recorder)
        self.drv._login = self._switch                    # a `login` step mid-exploration: probes follow the account
        if self.account:
            login(p, self._context, page, self.account, self.drv)
        self.state = {}
        obs = self.act([{"goto": self.start_url}], why="open the app")
        self.state0 = self.state           # after the first page: probes that read the page (js) need one
        obs["data"] = overview(self.state) or ["no probes in assay.yaml: Assay cannot show data changes"]
        return obs

    def finish(self) -> dict:
        """Close the browser, stop the app if Assay started it, write the session files. Safe to call twice, and after
        a start that failed half way."""
        if getattr(self, "finished", None):
            return self.summary()
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        try:
            if self._browsers is not None:
                self._browsers.__exit__(None, None, None)
        finally:
            self._browsers = None
            if self._proc:
                self._proc.stop()
        self.finished = now_iso()
        self._write()
        return self.summary()

    # -- what the agent does ------------------------------------------------------------------------------------------
    def act(self, steps: list[dict], *, why: str = "", look: bool = False, audit: bool = False) -> dict:
        """Run steps in order (stop at the first that fails), then observe. Refused once the budget is spent."""
        if not isinstance(steps, list) or not steps:
            raise ValueError("steps must be a non-empty list of steps, e.g. [{\"goto\": \"/\"}]")
        left = self.left()
        if self.steps and (left["actions"] <= 0 or left["minutes"] <= 0):
            raise RuntimeError("the exploration's budget is spent: note what you found (assay_explore_note) and call "
                               "assay_explore_finish")
        steps = steps[:max(1, left["actions"])] if self.steps else steps
        ran: list[dict] = []
        before = self.state
        for step in steps:
            i = len(self.steps)
            self.recorder.step = i
            rec: dict[str, Any] = {"i": i, "step": step, "ok": True}
            if why:
                rec["why"] = why[:300]
            try:
                self.drv.run(step)
                self.drv.settle(idle_ms=300, max_ms=3000)
            except StepError as e:
                rec.update(ok=False, error=str(e)[:500])
            except Exception as e:                      # a crashed page is a finding too, never a crashed session
                rec.update(ok=False, error=f"{type(e).__name__}: {e}"[:500])
            rec["shot"] = self.drv.screenshot(f"step-{i:03d}")
            self.steps.append(rec)
            ran.append(rec)
            if not rec["ok"]:
                break
        obs = self.observe(before, audit=audit)
        ran[-1]["observation"] = {k: v for k, v in obs.items() if k not in ("tree",)}
        obs["ran"] = [{"i": r["i"], "ok": r["ok"], **({"error": r["error"]} if not r["ok"] else {})} for r in ran]
        obs["budget_left"] = self.left()
        self._write()                     # every action is on disk: a session killed half way keeps what it found
        obs["_image"] = self.dir / "shots" / ran[-1]["shot"] if look and ran[-1].get("shot") else None
        return obs

    def observe(self, before: dict | None = None, audit: bool = False) -> dict:
        page = self.drv.page
        self.state = self._read_state()
        obs: dict[str, Any] = {"step": len(self.steps) - 1, "url": page.url}
        try:
            obs["title"] = page.title()
        except Exception:
            obs["title"] = ""
        obs["data"] = changes(before if before is not None else self.state, self.state) or ["no data changed"]
        if self.probe_errors:
            obs["data_unreadable"] = self.probe_errors
        obs.update(self._new_signals())
        if audit:
            from .sensors import audit_page
            a = audit_page(page, a11y=True)
            obs["audit"] = {"a11y": [f"{v['impact']}: {v['help']} ({v['nodes']} elements)" for v in (a.get("a11y") or {}).get("violations", [])],
                            "layout": [x for x in (["blank screen"] if a.get("blank") else []) + (a.get("overflowEls") or [])
                                       + [f"small tap target: {t}" for t in (a.get("smallTargets") or [])[:5]]
                                       + [f"broken image: {t}" for t in (a.get("brokenImages") or [])[:5]]]}
        try:
            tree = page.locator("body").aria_snapshot(timeout=5000)
        except Exception as e:
            tree = f"(accessibility tree unavailable: {e})"
        obs["tree"] = tree if len(tree) <= TREE_CHARS else tree[:TREE_CHARS] + f"\n… ({len(tree) - TREE_CHARS} more characters)"
        obs["shot"] = str(self.dir / "shots" / (self.steps[-1].get("shot") or "")) if self.steps else ""
        return obs

    def left(self) -> dict:
        """What remains of the budget: actions (the first page does not count) and minutes."""
        return {"actions": self.budget["actions"] - max(0, len(self.steps) - 1),
                "minutes": round(self.budget["minutes"] - (time.time() - self._t0) / 60, 1)}

    def note(self, title: str, detail: str = "", kind: str = "bug", severity: str = "major", step: int | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
        i = len(self.steps) - 1 if step is None else int(step)
        if not 0 <= i < len(self.steps):
            raise ValueError(f"step {i} does not exist (0..{len(self.steps) - 1})")
        f = {"n": len(self.findings) + 1, "kind": kind, "severity": severity, "title": title.strip()[:200],
             "detail": detail.strip()[:3000], "step": i, "url": (self.steps[i].get("observation") or {}).get("url"),
             "shot": self.steps[i].get("shot")}
        self.findings.append(f)
        self._write()
        return {"noted": f["n"], "step": i, "findings": len(self.findings)}

    # -- internals -----------------------------------------------------------------------------------------------------
    def _switch(self, account: str) -> dict:
        from .runner import login
        saved = login(self.profile, self._context, self.drv.page, account, self.drv)
        self.account = account
        return saved

    def _read_state(self) -> dict:
        state, self.probe_errors = snapshot(self.profile, self._context, self.drv.page if hasattr(self, "drv") else None,
                                            account=self.account)
        return state

    def _new_signals(self) -> dict:
        r, seen, out = self.recorder, self._seen, {}
        con = [c for c in r.console[seen["console"]:] if c.get("type") == "error"]
        errs = [f"console: {c['text'][:300]}" for c in con] + [f"page error: {e['text'][:300]}" for e in r.page_errors[seen["page_errors"]:]]
        net = [f"{x['method']} {x['url'][:200]} failed: {x.get('failure', '')}" for x in r.failed_requests[seen["failed_requests"]:]
               if x.get("first_party")]
        net += [f"{x['method']} {x['url'][:200]} → {x['status']}" for x in r.bad_responses[seen["bad_responses"]:]
                if x.get("first_party") and x.get("type") in ("fetch", "xhr", "document")]
        calls = [f"{c['method']} {c['path']} → {c.get('status') or 'no response'}" for c in r.api[seen["api"]:]]
        seen.update(console=len(r.console), page_errors=len(r.page_errors), failed_requests=len(r.failed_requests),
                    bad_responses=len(r.bad_responses), api=len(r.api))
        if errs:
            out["errors"] = errs[:15]
        if net:
            out["failed_requests"] = net[:15]
        if calls:
            out["calls"] = calls[:20]
        return out

    def summary(self) -> dict:
        bugs = [f for f in self.findings if f["kind"] == "bug"]
        return {"folder": str(self.dir), "steps": len(self.steps), "findings": len(self.findings), "bugs": len(bugs),
                "files": {"session": str(self.dir / "session.json"), "findings": str(self.dir / "findings.md"),
                          "trail": str(self.dir / "trail.yaml")},
                "data_changed_overall": changes(self.state0, self.state) if hasattr(self, "state0") else [],
                "next": "Reproduce each bug as a journey (cases/*.yaml) and run it twice on a fresh app: keep it with the "
                        "tag known-bug when it fails both times for the stated reason; drop it otherwise."}

    def _write(self) -> None:
        s = {"charter": self.charter, "app": self.profile.title or self.profile.name, "base_url": self.profile.base_url,
             "account": self.account, "viewport": self.viewport or self.profile.browser.get("viewport", "desktop"),
             "engine": getattr(self, "engine", None), "app_note": self.note_on_app, "started": self.started,
             "finished": getattr(self, "finished", None), "steps": self.steps, "findings": self.findings,
             "sensors": self.recorder.to_dict() if hasattr(self, "recorder") else {},
             "state_start": getattr(self, "state0", {}), "state_end": self.state if hasattr(self, "state") else {}}
        (self.dir / "session.json").write_text(json.dumps(s, indent=1, ensure_ascii=False, default=str))
        (self.dir / "findings.md").write_text(self.markdown())
        worked = [r["step"] for r in self.steps if r["ok"]]
        draft = [{"id": f"explore.{self.dir.name[8:]}", "title": self.charter[:120] or "Exploration trail",
                  **({"as": self.account} if self.account else {}), **({"viewport": self.viewport} if self.viewport else {}),
                  "steps": worked, "must": [{"no_errors": True}]}]
        (self.dir / "trail.yaml").write_text(
            "# The steps of this exploration that worked, in order: a draft, not a journey. Cut it to one goal, add the\n"
            "# checks the data showed (state, allowed_changes), give it an id, then move it into .assay/cases/.\n"
            + _yaml(draft))

    def markdown(self) -> str:
        lines = [f"# Exploration · {self.profile.title or self.profile.name}", ""]
        if self.charter:
            lines += [f"**Charter:** {self.charter}", ""]
        lines += [f"{len(self.steps)} steps · {len(self.findings)} finding{'' if len(self.findings) == 1 else 's'} · as {self.account or 'a visitor'} · "
                  f"{self.started[:16].replace('T', ' ')} · {self.note_on_app}", ""]
        order = {s: i for i, s in enumerate(SEVERITIES)}
        for f in sorted(self.findings, key=lambda f: (KINDS.index(f["kind"]), order[f["severity"]])):
            lines += [f"## {f['n']}. [{f['kind']} · {f['severity']}] {f['title']}", "",
                      f"Step {f['step']} · {f['url'] or ''} · screenshot `shots/{f['shot']}`", ""]
            if f["detail"]:
                lines += [f["detail"], ""]
            path = [self.steps[i]["step"] for i in range(f["step"] + 1) if self.steps[i]["ok"]]
            lines += ["Steps that led here:", "```yaml", _yaml(path).rstrip(), "```", ""]
        if not self.findings:
            lines += ["No findings noted.", ""]
        sens = self.recorder.to_dict() if hasattr(self, "recorder") else {}
        errs = len([c for c in sens.get("console", []) if c.get("type") == "error"]) + len(sens.get("page_errors", []))
        bad = len([x for x in sens.get("bad_responses", []) + sens.get("failed_requests", []) if x.get("first_party")])
        lines += ["## What the sensors saw", "", f"- {errs} console/page errors, {bad} failed first-party requests",
                  f"- {sens.get('requests', 0)} requests, {len(sens.get('urls', []))} pages visited"]
        lines += [f"- data: {c}" for c in changes(getattr(self, "state0", {}), getattr(self, "state", {}), limit=15)]
        return "\n".join(lines) + "\n"
