"""Grading: evidence → item results → case verdict. Runs offline; re-grading never re-runs the app.

The verdict rule (one rule for every item, every product):
  1. the environment broke (app down, login, setup, read-back, no model for the goal agent) → INCONCLUSIVE
  2. the case itself is invalid                                               → INCONCLUSIVE (owner: case)
  3. a step of the journey failed / goal not reached                          → FAIL
  4. any `must` item failed                                                   → FAIL
  5. any `must` item undecided (probe/judge unavailable)                      → INCONCLUSIVE
  6. otherwise                                                                → PASS
`should` items never change the verdict; they add to the score and raise minor issues.
"""
from __future__ import annotations

from pathlib import Path

from . import checks as C
from .judge import Judge
from .model import (FAIL, INCONCLUSIVE, NOK, OK, OWNER_CASE, OWNER_ENV, OWNER_JUDGE, OWNER_PRODUCT, PASS,
                    SKIPPED, UNDECIDED, CaseResult, ItemResult)
from .runner import ENV_CAUSES

CAUSE_TEXT = {
    "app_unreachable": "app is not reachable",
    "login_failed": "could not log in the test account",
    "setup_failed": "start state could not be prepared",
    "probe_failed": "state could not be read",
    "precondition_mismatch": "start state read back differently from the case",
    "browser_crashed": "browser or harness crashed",
    "agent_unavailable": "the goal agent could not run",
    "case_invalid": "the case is invalid",
    "step_failed": "a step of the journey failed",
    "goal_not_reached": "the agent could not complete the goal",
}


def grade_case(ev: dict, case_dir: Path, *, volatile: list[str], judge: Judge | None) -> CaseResult:
    case = ev.get("case") or {}
    res = CaseResult(case_id=ev["case_id"], title=ev.get("title", ""), verdict=PASS,
                     severity=case.get("severity", "major"), dimension=case.get("dimension", "functional"),
                     duration_s=ev.get("duration_s", 0.0), healed=bool((ev.get("agent") or {}).get("healed")))
    cause = ev.get("cause")
    env_broken = cause in ENV_CAUSES or cause == "case_invalid"
    ctx = {"volatile": volatile}
    for level in ("must", "should"):
        for i, item in enumerate(case.get(level, [])):
            try:
                name = C.check_name(item)
            except ValueError as e:
                res.items.append(ItemResult("invalid", item, level, UNDECIDED, str(e)))
                continue
            dim = item.get("dimension") or C.DIMENSION.get(name, "functional")
            if env_broken:
                res.items.append(ItemResult(name, item[name], level, SKIPPED, "not checked: environment", dimension=dim))
                continue
            if name == "judge":
                spec = item["judge"]
                criterion = spec if isinstance(spec, str) else spec.get("criterion") or spec.get("text")
                on = "auto" if isinstance(spec, str) else spec.get("on", "auto")
                if judge is None or not judge.available:
                    st, detail = UNDECIDED, "judge disabled"
                else:
                    st, detail = judge.decide(criterion, ev, case_dir, level, on,
                                              screenshot=None if isinstance(spec, str) else spec.get("screenshot"))
                res.items.append(ItemResult(name, criterion, level, st, detail, by="judge",
                                            dimension=item.get("dimension", "ai" if ev.get("answers") else "functional")))
                continue
            if name not in C.DECIDERS:
                res.items.append(ItemResult(name, item[name], level, UNDECIDED, f"unknown check {name!r}", dimension=dim))
                continue
            obs = ev.get("observations", {}).get(f"{level}.{i}")
            try:
                st, detail = C.DECIDERS[name](item[name], ev, obs, ctx)
            except Exception as e:
                st, detail = UNDECIDED, f"check error: {e}"[:300]
            res.items.append(ItemResult(name, item[name], level, st, detail, dimension=dim))

    decided = [i for i in res.items if i.status in (OK, NOK)]
    res.score = round(sum(i.status == OK for i in decided) / len(decided), 3) if decided else 0.0
    must = [i for i in res.items if i.level == "must"]
    failed = [i for i in must if i.status == NOK]
    undecided = [i for i in must if i.status == UNDECIDED]

    if cause in ENV_CAUSES:
        res.verdict, res.cause, res.owner = INCONCLUSIVE, cause, OWNER_ENV
        res.summary = f"{CAUSE_TEXT.get(cause, cause)}: {ev.get('error', '')}"
    elif cause == "case_invalid":
        res.verdict, res.cause, res.owner = INCONCLUSIVE, cause, OWNER_CASE
        res.summary = f"{CAUSE_TEXT[cause]}: {ev.get('error', '')}"
    elif cause in ("step_failed", "goal_not_reached"):
        res.verdict, res.cause, res.owner = FAIL, cause, OWNER_PRODUCT
        err = _first_line(ev.get("error", ""))
        # the data the app actually wrote explains a failed step better than the timeout it caused
        res.summary = f"{failed[0].kind}: {failed[0].detail} (then {err})" if failed and failed[0].kind != "no_errors" else err
    elif failed:
        res.verdict, res.cause, res.owner = FAIL, f"must:{failed[0].kind}", OWNER_PRODUCT
        res.summary = "; ".join(f"{i.kind}: {i.detail}" for i in failed[:3])
    elif undecided:
        res.verdict, res.cause = INCONCLUSIVE, f"undecided:{undecided[0].kind}"
        res.owner = OWNER_JUDGE if undecided[0].by == "judge" else OWNER_ENV
        res.summary = "; ".join(f"{i.kind}: {i.detail}" for i in undecided[:3])
    else:
        res.verdict = PASS
        soft = [i for i in res.items if i.level == "should" and i.status == NOK]
        res.summary = f"{len(must)} must ✓" + (f", {len(soft)} should ✗" if soft else "")
    return res


def _first_line(err: str, n: int = 220) -> str:
    """Playwright errors carry a call log; the first line says what happened."""
    import re
    line = re.split(r"\n|Call log:", err or "")[0].strip()
    line = re.sub(r"wait_for css='[^']*', has_text='([^']{0,40})[^']*'", r"waiting for text '\1…'", line)
    return line if len(line) <= n else line[: n - 1] + "…"
