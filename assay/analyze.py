"""Analysis: a run folder (evidence only) → analysis.json (verdicts, issues, what was measured, gate, screens).

Pure function of the evidence + judge cache, so `assay grade <run>` reproduces it exactly. A failure that blocked was run
once more from a fresh app (`assay ci`, `assay run --confirm`): that run's evidence is in `confirm/` and what it found
in `confirm.json`, which this applies (`case.confirm`), so re-grading reaches the same gate.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .config import Profile
from .code import journey_entry, map_files, repo_root, stamp
from .explain import failure_of
from .grade import grade_case
from .guard import APPROVE_LABEL, weakened
from .judge import Judge
from .model import FAIL, INCONCLUSIVE, PASS
from .quality import case_changes, flaky, gate, known_bugs, load_baseline, mark_new, measured, ratings, screens
from .rules import case_issues, merge, route_of, sensor_issues


def analyze(run_dir: Path, profile: Profile, *, judge: Judge | None, baseline: Path | None = None,
            log=print) -> dict:
    run_dir = Path(run_dir)
    run_meta = json.loads((run_dir / "run.json").read_text()) if (run_dir / "run.json").exists() else {}
    evs = []
    for f in sorted((run_dir / "cases").glob("*/evidence.json")) if (run_dir / "cases").exists() else []:
        evs.append((f.parent, json.loads(f.read_text())))
    order = {cid: i for i, cid in enumerate(run_meta.get("case_order", []))}
    evs.sort(key=lambda x: order.get(x[1]["case_id"], 10_000))

    results, issues = [], []
    case_routes: list[str] = []
    again = second_run(run_dir)
    for case_dir, ev in evs:
        res = grade_case(ev, case_dir, volatile=profile.volatile, judge=judge)
        shot = f"cases/{case_dir.name}/{ev['final_screenshot']}" if ev.get("final_screenshot") else ""
        d = res.to_dict()
        if (ev.get("final_url") or "about:blank") == "about:blank":
            shot = ""          # API-only journey: there is no screen to show
        raw = ev.get("case") or {}
        d.update(screenshot=shot, final_url=ev.get("final_url"), account=ev.get("account"), viewport=ev.get("viewport"), browser=ev.get("browser"),
                 steps=[{k: s.get(k) for k in ("i", "action", "summary", "ok", "error", "ms", "screenshot")}
                        for s in ev.get("steps", [])],
                 tags=raw.get("tags", []), error=ev.get("error"), promise=raw.get("promise"), critical=bool(raw.get("critical")),
                 goal=bool(ev.get("goal")))
        d["failure"] = failure_of(d, ev, profile.probes)
        if d["verdict"] == FAIL and d["case_id"] in again:
            d["confirm"] = again[d["case_id"]]
        results.append(d)
        # a failure that did not reproduce is not a finding: its journey still lists it, the issues do not
        issues += [i for i in case_issues(res, shot) if not (i.rule == "CASE-FAIL" and flaky([d]))]
        issues += sensor_issues(ev, source=ev["case_id"], evidence_path=shot)
        case_routes += [route_of(u) for u in (ev.get("sensors") or {}).get("urls", [])]
        if judge:
            judge.save()

    pages = []
    pages_file = run_dir / "scan" / "pages.json"
    if pages_file.exists():
        pages = json.loads(pages_file.read_text())
        for p in pages:
            issues += sensor_issues(p, source=f"scan:{p['viewport']}:{p['route']}", evidence_path=p.get("screenshot", ""),
                                    page_mode=True)

    import fnmatch
    ignored = profile.ignore.get("rules", [])
    issues = [i for i in issues if not any(fnmatch.fnmatch(i.rule, pat) for pat in ignored)]
    merged = merge(issues)
    base = load_baseline(baseline) if baseline else None
    mark_new(merged, base)
    known = known_bugs(results, base)
    ci = run_meta.get("ci") or {}
    weak = weakened(base, run_meta.get("dataset") or [], run_meta.get("settings"))
    approved = APPROVE_LABEL in (ci.get("labels") or [])
    # whether weakening blocks is the target branch's setting: a pull request must not switch off its own guard
    ref_gate = ((base or {}).get("settings") or {}).get("gate")
    block_cfg = (ref_gate or {}).get("block_on_weakened", True) if ref_gate is not None else profile.gate.get("block_on_weakened", True)
    blocks = bool(weak) and bool(ci.get("pr")) and not approved and block_cfg
    extra = [{"id": "weakened", "name": "Weakened checks", "actual": len(weak), "op": "=", "threshold": 0, "ok": not blocks,
              "soft": not blocks}] if weak else []
    dims_measured = measured(results, [ev for _, ev in evs], pages)
    r_all, r_new = ratings(merged, measured_dims=dims_measured), ratings(merged, only_new=True, measured_dims=dims_measured)
    cfg = {**profile.gate, **({"no_reference": True} if run_meta.get("no_reference") else {})}
    g = gate(cfg, results, merged, r_new if base else r_all, baseline=base, extra=extra)
    by_id = {ev["case_id"]: ev for _, ev in evs}
    jmap = map_files({cid: journey_entry(ev, profile.probes) for cid, ev in by_id.items() if ev.get("sensors") is not None},
                     repo_root(profile.root))
    change = run_dir / "change.diff"                       # the pull request's diff, saved by `assay ci`
    changes = case_changes(results, base)
    stamp(results, by_id, change.read_text() if change.exists() else None, jmap,
          set(changes["regressions"]) if base else None)

    n = len(results)
    counts = {v: sum(r["verdict"] == v for r in results) for v in (PASS, FAIL, INCONCLUSIVE)}
    sev_counts: dict[str, int] = {}
    for i in merged:
        sev_counts[i.severity] = sev_counts.get(i.severity, 0) + 1
    dims: dict[str, dict] = {}
    for i in merged:
        d = dims.setdefault(i.dimension, {"issues": 0, "new": 0})
        d["issues"] += 1
        d["new"] += int(i.new)
    analysis = {
        "run": run_meta, "ci": run_meta.get("ci"), "app": {"name": profile.name, "title": profile.title, "base_url": profile.base_url},
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"), "baseline": _baseline_label(baseline, base),
        "summary": {"cases": n, **counts, "pass_rate": round(counts[PASS] / max(1, n - counts[INCONCLUSIVE]), 3) if n else None,
                    "flaky": len(flaky(results)), "issues": len(merged), "new_issues": sum(i.new for i in merged), "by_severity": sev_counts,
                    "pages_scanned": len(pages), "routes_scanned": len({p["route"] for p in pages})},
        "gate": g, "ratings": r_all, "ratings_new": r_new if base else None, "dimensions": dims, "measured": dims_measured,
        "cases": results, "issues": [i.to_dict() for i in merged], "known_bugs": sorted(known),
        "scope": run_meta.get("scope") or {"mode": "all", "checked": n, "total": n},
        "dataset": run_meta.get("dataset") or [], "settings": run_meta.get("settings"), "journey_map": jmap,
        "weakened": {"items": weak, "approved": approved, "blocking": blocks} if weak else None,
        "reference_ids": sorted({d.get("id") for d in (base or {}).get("dataset") or []}
                                | {c["case_id"] for c in (base or {}).get("cases", [])}) if base else [],
        "pages": [{k: p.get(k) for k in ("url", "route", "viewport", "browser", "status", "screenshot", "load_s", "error")}
                  | {"a11y": len(((p.get("audit") or {}).get("a11y") or {}).get("violations", [])),
                     "vitals": (p.get("audit") or {}).get("vitals"), "timing": (p.get("audit") or {}).get("timing")}
                  for p in pages],
        "screens": screens([p["route"] for p in pages], case_routes) if pages else None,
        "changes": changes,
        "judge": {"backend": judge.backend if judge else None, "model": judge.model if judge else None,
                  "calls": judge.calls if judge else 0, "cost_usd": round(judge.cost_usd, 4) if judge else 0},
    }
    (run_dir / "analysis.json").write_text(json.dumps(analysis, indent=1, ensure_ascii=False, default=str))
    return analysis


def second_run(run_dir: Path) -> dict[str, dict]:
    """What the second run of the failures that blocked found, from confirm.json: {case_id: {verdict, run, phrase?}}.
    A journey the second run did not record counts as not checked there: its first failure stands."""
    f = Path(run_dir) / "confirm.json"
    if not f.exists():
        return {}
    c = json.loads(f.read_text())
    return {cid: {"verdict": x.get("verdict") or INCONCLUSIVE, "run": c.get("run") or "confirm",
                  **({"phrase": x["phrase"]} if x.get("phrase") else {})} for cid, x in (c.get("cases") or {}).items()}


def _baseline_label(baseline, base: dict | None) -> str | None:
    """Short name of what this analysis was compared with: a run folder, or a server analysis (branch @ commit)."""
    if not baseline:
        return None
    if isinstance(baseline, dict):
        ci = (base or {}).get("ci") or {}
        ref = ci.get("branch") or (base or {}).get("run", {}).get("id", "")
        return f"{ref} @ {ci['sha'][:7]}" if ci.get("sha") else str(ref)
    return str(baseline)
