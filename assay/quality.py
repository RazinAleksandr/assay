"""Quality model: what was measured, ratings per dimension, the quality gate, baseline diff, scanned screens.

Ratings use SonarQube's familiar rule (A = no issues · B ≥1 minor · C ≥1 major · D ≥1 critical · E ≥1 blocker),
but only for a dimension something actually measured: a dimension nothing checked has no rating (None), never A.
The gate is three-valued like a journey: PASSED / FAILED / INCONCLUSIVE. It fails closed: no journeys executed, a
condition on an unmeasured dimension or too many journeys that could not be checked make it INCONCLUSIVE, never PASSED.
"""
from __future__ import annotations

import json
from pathlib import Path

from .model import DIMENSIONS, FAIL, INCONCLUSIVE, NOK, OK, PASS, SEV_RANK, Issue

RATING_ORDER = "ABCDE"
KNOWN_BUG = "known-bug"   # tag of a journey that encodes a bug not fixed yet
CONFIRM_MAX = 10          # gate.confirm_max: at most this many failures get a second run before they block


def rating(issues: list[Issue]) -> str:
    worst = min((SEV_RANK[i.severity] for i in issues if i.severity != "info"), default=None)
    if worst is None:
        return "A"
    return {0: "E", 1: "D", 2: "C", 3: "B"}[worst]


def measured(results: list[dict], evidence: list[dict], pages: list[dict]) -> dict[str, list[str]]:
    """What measured each dimension in this analysis: decided checks of journeys and sensors that actually ran.
    A dimension missing here was not checked at all."""
    out: dict[str, list[str]] = {}

    def add(dim: str, what: str) -> None:
        if what not in out.setdefault(dim, []):
            out[dim].append(what)
    for r in results:
        if r.get("verdict") in (PASS, FAIL):            # the journey ran to a product verdict
            add("functional", "journeys")
        for it in r.get("items", []):
            if it.get("status") in (OK, NOK):
                add(it.get("dimension") or "functional", "journey checks")
    for ev in [*evidence, *pages]:
        if ev.get("cause") in ("app_unreachable",):
            continue
        if ev.get("sensors") is not None or "console" in ev:
            add("reliability", "console and network")
        audit = ev.get("audit") or {}
        a11y = audit.get("a11y") or {}
        if a11y and not a11y.get("error"):
            add("accessibility", "axe audit")
        if "overflowX" in audit:
            add("ux", "layout audit")
        if audit.get("vitals") or audit.get("timing"):
            add("performance", "web vitals")
        if ev.get("cookies") is not None or "storageTokens" in audit:
            add("security", "cookie flags and tokens in storage")
    return out


def ratings(issues: list[Issue], only_new: bool = False, measured_dims: dict | None = None) -> dict[str, str | None]:
    """Rating per dimension; None for a dimension nothing measured (not checked ≠ A)."""
    out: dict[str, str | None] = {}
    for dim in DIMENSIONS:
        sel = [i for i in issues if i.dimension == dim and (i.new or not only_new)]
        out[dim] = rating(sel) if (measured_dims is None or dim in measured_dims or sel) else None
    return out


def load_baseline(path: Path | dict | None) -> dict | None:
    if not path:
        return None
    if isinstance(path, dict):          # a reference analysis fetched from the Assay server
        return path
    path = Path(path)
    f = path / "analysis.json" if path.is_dir() else path
    if not f.exists():
        raise FileNotFoundError(f"baseline analysis not found: {f}")
    return json.loads(f.read_text())


def mark_new(issues: list[Issue], baseline: dict | None) -> None:
    if baseline is None:
        return
    known = {i["fingerprint"] for i in baseline.get("issues", [])}
    for i in issues:
        i.new = i.fingerprint not in known


def failure_signature(result: dict) -> tuple:
    """How a journey fails: whether a step broke (and which), and which `must` checks failed: position, kind and the
    data paths that went wrong (`comments.body`). Values are left out on purpose: they carry run-specific data
    (generated ids, counts, timings), and the same bug must keep the same signature from run to run. A goal journey's
    path depends on how the agent explored, so only its failed checks count. A different signature is a different failure."""
    import re
    if not result or result.get("verdict") != FAIL:
        return ()
    musts = [i for i in result.get("items", []) if i.get("level") == "must"]
    failed = tuple((n, i.get("kind", ""), tuple(sorted(set(re.findall(r"(?:^|; )([\w.\[\]=-]+): ", i.get("detail") or "")))))
                   for n, i in enumerate(musts) if i.get("status") == NOK)
    if result.get("goal"):
        return "goal", failed
    step = next((s.get("i") for s in result.get("steps", []) if s.get("ok") is False), None)
    return ("step", step, failed) if str(result.get("cause") or "").startswith("step_failed") else ("checks", failed)


def flaky(results: list[dict]) -> set[str]:
    """Journeys that failed and then passed when run a second time from a fresh app (`case.confirm`, see
    `blocking_failures`): the failure did not reproduce, so it blocks nothing. The first verdict stays FAIL, because that
    is what its evidence shows."""
    return {r["case_id"] for r in results if r.get("verdict") == FAIL and (r.get("confirm") or {}).get("verdict") == PASS}


def _reference(baseline: dict) -> dict[str, dict]:
    """The target branch's journeys by id. One that failed there and passed on its second run counts as passing: a pull
    request that makes it fail again for real broke it, and one where it passes fixed nothing."""
    fl = flaky(baseline.get("cases", []))
    return {r["case_id"]: ({**r, "verdict": PASS} if r["case_id"] in fl else r) for r in baseline.get("cases", [])}


def _newly_failing(r: dict, prev: dict | None) -> bool:
    """FAIL now and not the same failure before (a journey new to the suite counts as newly failing)."""
    return r["verdict"] == FAIL and (prev is None or failure_signature(r) != failure_signature(prev))


def case_changes(results: list[dict], baseline: dict | None) -> dict:
    """What the change did to journeys against the target branch. A flaky failure (see `flaky`) is not a regression."""
    if baseline is None:
        return {}
    prev, fl = _reference(baseline), flaky(results)
    reg = [r["case_id"] for r in results if _newly_failing(r, prev.get(r["case_id"])) and r["case_id"] not in fl]
    fixed = [r["case_id"] for r in results if r["verdict"] == PASS and (prev.get(r["case_id"]) or {}).get("verdict") == FAIL]
    return {"regressions": reg, "fixed": fixed}


def known_bugs(results: list[dict], baseline: dict | None) -> set[str]:
    """Journeys whose failure is the expected one: tagged `known-bug` and failing the way they failed in the reference.
    A journey new to the suite (a bug report turned into a journey) is expected to fail. A tag never hides a different
    failure, or a journey that passed in the reference and fails now: that is a regression."""
    tagged = [r for r in results if KNOWN_BUG in (r.get("tags") or []) and r.get("verdict") == FAIL]
    if baseline is None:
        return {r["case_id"] for r in tagged}
    prev = _reference(baseline)
    in_ref = set(prev) | {d.get("id") for d in baseline.get("dataset") or []}
    return {r["case_id"] for r in tagged
            if r["case_id"] not in in_ref or failure_signature(r) == failure_signature(prev.get(r["case_id"]))}


def gate(cfg: dict, results: list[dict], issues: list[Issue], new_ratings: dict[str, str | None],
         baseline: dict | None = None, extra: list[dict] | None = None) -> dict:
    """`new_code: true` (what `assay ci` sets for pull requests) makes the journey condition look at *new code* only,
    like SonarQube: a journey that already failed the same way on the target branch does not block the change, a journey
    that starts failing (or fails differently) does. On a branch (release readiness) every failing journey counts.
    Issue conditions (`max_new`, `min_rating`) count new issues when there is a baseline. `no_reference: true` (the
    server answered that the branch has no analysis yet, or a local run): nothing tells a new problem from one the
    product always had, so, as SonarQube's first analysis does, what is there becomes the baseline: those conditions are
    listed and block nothing; failing journeys do. A missing reference for any other reason (no server, the server down)
    is not that: every finding counts, the gate fails closed.
    `first_analysis: true` (a pull request whose target branch has no analysis yet, e.g. the one that connects Assay):
    nothing to compare with, so problems are listed as warnings and block nothing.
    Journeys tagged `known-bug` that fail the expected way (see `known_bugs`) are listed but count in no condition, and so
    do flaky ones: failed, then passed on a second run from a fresh app (see `flaky`), and so do the problems only those
    journeys hit. Only a failure that reproduces blocks.
    Fail-closed: fewer than `min_cases` journeys executed (default 1), a `min_rating` on a dimension nothing measured,
    or more than `max_inconclusive` of the journeys undecided → INCONCLUSIVE, never PASSED.
    `extra`: more conditions computed elsewhere (weakened checks)."""
    conds = []
    n = len(results)
    quiet = known_bugs(results, baseline) | flaky(results)        # expected failures and failures that did not reproduce
    # a problem only quiet journeys hit is their symptom (the known bug answers 500): it counts no more than they do
    issues = [i for i in issues if not (i.sources and set(i.sources) <= quiet)]
    counted = [r for r in results if r.get("case_id") not in quiet]
    n_fail = sum(r["verdict"] == FAIL for r in counted)
    n_inc = sum(r["verdict"] == INCONCLUSIVE for r in results)
    undecided = False

    if cfg.get("fail_on_case_fail", True):
        if baseline is not None and cfg.get("new_code", False):
            prev = _reference(baseline)
            newly = sum(_newly_failing(r, prev.get(r["case_id"])) for r in counted)
            conds.append({"id": "new_failed_journeys", "name": "New failed journeys", "actual": newly, "op": "=",
                          "threshold": 0, "ok": newly == 0})
        else:
            conds.append({"id": "failed_journeys", "name": "Failed journeys", "actual": n_fail, "op": "=", "threshold": 0,
                          "ok": n_fail == 0})
    for sev, limit in (cfg.get("max_new") or {}).items():
        cnt = sum(1 for i in issues if i.new and i.severity == sev)
        conds.append({"id": f"new_issues:{sev}", "name": f"New {sev} issues", "actual": cnt, "op": "≤", "threshold": limit,
                      "ok": cnt <= limit})
    for dim, worst in (cfg.get("min_rating") or {}).items():
        r = new_ratings.get(dim)
        if r is None:                     # nothing measured it: the condition cannot be met, nor failed
            undecided = True
            conds.append({"id": f"rating:{dim}", "name": f"{DIMENSIONS.get(dim, dim)} rating (new)", "actual": "not measured", "op": "≤",
                          "threshold": worst, "ok": False, "soft": True})
            continue
        conds.append({"id": f"rating:{dim}", "name": f"{DIMENSIONS.get(dim, dim)} rating (new)", "actual": r, "op": "≤", "threshold": worst,
                      "ok": RATING_ORDER.index(r) <= RATING_ORDER.index(worst)})
    min_cases = int(cfg.get("min_cases", 1))
    if n < min_cases:
        undecided = True
        conds.append({"id": "min_cases", "name": "Journeys executed", "actual": n, "op": "≥", "threshold": min_cases, "ok": False, "soft": True})
    share = (n_inc / n) if n else 0.0
    max_inc = float(cfg.get("max_inconclusive", 0.2))
    too_many = n > 0 and share > max_inc
    conds.append({"id": "inconclusive", "name": "Inconclusive journeys", "actual": f"{share:.0%}", "op": "≤", "threshold": f"{max_inc:.0%}",
                  "ok": not too_many, "soft": True})
    if baseline is None and cfg.get("no_reference"):
        for c in conds:
            if c["id"].startswith(("new_issues:", "rating:")):
                c.update(soft=True, baseline=True)
    conds += [dict(c) for c in extra or []]          # e.g. weakened checks (assay/guard.py)
    if cfg.get("first_analysis"):
        for c in conds:
            c["soft"] = True
    hard_fail = any(not c["ok"] for c in conds if not c.get("soft"))
    status = "FAILED" if hard_fail else ("INCONCLUSIVE" if (too_many or undecided) else "PASSED")
    return {"status": status, "conditions": conds, **({"first_analysis": True} if cfg.get("first_analysis") else {})}


def blocking_failures(a: dict) -> list[str]:
    """The journeys whose failure fails this analysis' gate: failing, not a known bug, not run a second time yet and, on a
    pull request, new (`changes.regressions`). `assay ci` runs exactly these once more from a fresh app before it blocks
    anything (`gate.confirm`); what that run found goes to confirm.json, which `analyze` applies. Pure function of
    analysis.json, in the order the journeys ran."""
    g = a.get("gate") or {}
    conds = {c.get("id"): c for c in g.get("conditions", [])}
    journeys = conds.get("new_failed_journeys") or conds.get("failed_journeys")
    if g.get("status") != "FAILED" or g.get("first_analysis") or not journeys or journeys.get("ok"):
        return []
    new = set((a.get("changes") or {}).get("regressions") or []) if journeys.get("id") == "new_failed_journeys" else None
    known = set(a.get("known_bugs") or [])
    return [c["case_id"] for c in a.get("cases", []) if c.get("verdict") == FAIL and not c.get("confirm")
            and c["case_id"] not in known and (new is None or c["case_id"] in new)]


def screens(scan_routes: list[str], case_routes: list[str]) -> dict:
    """Which scanned routes the journeys reached. A count, not a quality metric: the crawler follows links, it does not
    open menus, dialogs or editor modes, so `reached` says nothing about how much of the product was checked."""
    disc = sorted(set(scan_routes))
    hit = sorted(set(case_routes) & set(disc))
    return {"scanned": len(disc), "discovered": disc, "covered": hit, "uncovered": sorted(set(disc) - set(hit))}
