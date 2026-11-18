"""Benchmark: the same journeys, repeated, to answer "is B better than A, and is my gate trustworthy?"

The separate workflow for products with AI in them: a model answers differently every time, so one green run
proves little. The benchmark repeats every journey and reports rates with intervals instead of one verdict.

  arms      what to compare; the first is the baseline. An arm is
              name=<project folder>      another build with its own .assay profile (e.g. the base branch)
              name=<url>                 a build that is already running
              name=env:KEY=VALUE[,K=V]   the same app started with other settings: a model, a prompt version, a flag
            With no arms the baseline is run twice (A/A): the difference between two identical builds is the
            noise floor of the gate.
  attempts  every case × arm × attempt is a full journey with its own evidence and verdict.
  stats     pass@1 per arm with a 95% bootstrap CI over cases; pass^k (all attempts pass);
            paired difference vs baseline with a bootstrap CI; flaky cases (mixed verdicts in one arm).
            INCONCLUSIVE attempts are dropped for that case-attempt in every arm, so arms compare on the
            same set.
  latency   per arm, the median journey and the median AI turn (`ask` steps): a cheaper model that is
            as good but twice as slow is a trade-off someone has to see.

Arms, attempts and the journeys to repeat can live in assay.yaml (`bench:`); the command line overrides them.
"""
from __future__ import annotations

import json
import os
import random
import time
from dataclasses import replace
from pathlib import Path

from .analyze import analyze
from .config import load_cases, load_plugins
from .judge import Judge
from .runner import Browsers, run_case


def _boot(values: list[float], n: int = 2000, seed: int = 7) -> tuple[float, float]:
    if not values:
        return (float("nan"), float("nan"))
    rnd = random.Random(seed)
    k = len(values)
    means = sorted(sum(values[rnd.randrange(k)] for _ in range(k)) / k for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def stats(verdicts: dict[str, dict[str, list[str]]]) -> dict:
    """verdicts[arm][case] = [verdict per attempt]."""
    arms = list(verdicts)
    base = arms[0]
    cases = sorted(set().union(*[set(v) for v in verdicts.values()]))
    attempts = max(len(a) for arm in verdicts.values() for a in arm.values())
    # drop case-attempts that were INCONCLUSIVE in any arm
    keep = {c: [i for i in range(attempts)
                if all(i < len(verdicts[a].get(c, [])) and verdicts[a][c][i] != "INCONCLUSIVE" for a in arms)]
            for c in cases}
    out: dict = {"arms": {}, "baseline": base, "attempts": attempts, "cases": len(cases), "per_case": {}}
    per_arm_case: dict[str, dict[str, float]] = {}
    for a in arms:
        rates, all_pass, flaky = {}, 0, []
        for c in cases:
            vs = [verdicts[a][c][i] for i in keep[c]]
            if not vs:
                continue
            rates[c] = sum(v == "PASS" for v in vs) / len(vs)
            all_pass += all(v == "PASS" for v in vs)
            if 0 < rates[c] < 1:
                flaky.append(c)
        per_arm_case[a] = rates
        vals = list(rates.values())
        lo, hi = _boot(vals)
        out["arms"][a] = {"pass@1": round(sum(vals) / len(vals), 3) if vals else None, "ci95": [round(lo, 3), round(hi, 3)],
                          "pass^k": round(all_pass / len(vals), 3) if vals else None, "flaky": flaky,
                          "decided_cases": len(vals)}
    for a in arms[1:]:
        common = sorted(set(per_arm_case[a]) & set(per_arm_case[base]))
        diffs = [per_arm_case[a][c] - per_arm_case[base][c] for c in common]
        lo, hi = _boot(diffs)
        d = sum(diffs) / len(diffs) if diffs else float("nan")
        out["arms"][a]["diff_vs_baseline"] = {"mean": round(d, 3), "ci95": [round(lo, 3), round(hi, 3)],
                                             "significant": bool(diffs) and (lo > 0 or hi < 0)}
    for c in cases:
        out["per_case"][c] = {a: verdicts[a].get(c, []) for a in arms}
    return out


def resolve_bench(profile, args) -> tuple[list[str], int, list[str] | None]:
    """What a benchmark compares, how often and on which journeys: the command line's --arm, --attempts and -k win,
    then the `bench:` section of assay.yaml (so CI needs no arguments), then A/A, 3 attempts and the journeys marked
    `ai: true` (selectors None). Profile arms are `name: target` in the --arm syntax; a folder there is looked up
    relative to the .assay folder first, like every other path in the profile."""
    cfg = profile.raw.get("bench") or {}
    if not isinstance(cfg, dict) or not isinstance(cfg.get("arms") or {}, dict):
        raise ValueError("bench in assay.yaml: {attempts: 3, select: [is:ai], arms: {name: env:KEY=VALUE | folder | URL}}")
    specs = list(args.arm or [])
    for name, target in ([] if specs else (cfg.get("arms") or {}).items()):
        target = "" if target is None else str(target)
        local = profile.root / target
        specs.append(f"{name}={local if target and (local / 'assay.yaml').exists() else target}")
    attempts = args.attempts if args.attempts is not None else cfg.get("attempts", 3)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
        raise ValueError(f"attempts must be a whole number of at least 1, got {attempts!r}")
    sel = cfg.get("select")
    selectors = list(args.k or []) or ([sel] if isinstance(sel, str) else list(sel or [])) or None
    return specs, attempts, selectors


def bench(profile, args, log=print) -> int:
    load_plugins(profile)
    specs, attempts, selectors = resolve_bench(profile, args)
    cases = load_cases(profile, selectors)
    if not selectors and any(c.ai for c in cases):    # journeys marked `ai: true` are what a benchmark repeats
        cases = [c for c in cases if c.ai]
    # an arm is a project folder (its own app build, ports and golden data) or just a URL of a running build
    from .config import load_profile
    arms: list[tuple[str, object]] = []
    arm_env: dict[str, dict] = {}
    for spec in specs:
        name, _, target = spec.partition("=")
        tp = Path(target) if target and not target.startswith("env:") else None
        if target.startswith("env:"):
            arm_prof, arm_env[name] = with_env(profile, target[4:])
        elif tp is not None and (tp / "assay.yaml").exists():
            arm_prof = load_profile(tp)
        else:
            arm_prof = replace(profile, base_url=target or profile.base_url)
        arms.append((name, arm_prof))
    if not arms:
        arms = [("A", profile), ("A'", profile)]
    bid = args.run_id or time.strftime("bench-%Y%m%d-%H%M%S")
    root = Path(args.out or profile.root / "runs") / bid
    root.mkdir(parents=True, exist_ok=True)
    log(f"assay bench · {len(cases)} cases × {len(arms)} arms × {attempts} attempts → {root}")
    verdicts: dict[str, dict[str, list[str]]] = {a: {} for a, _ in arms}
    timing: dict[str, list[float]] = {a: [] for a, _ in arms}
    turns: dict[str, list[float]] = {a: [] for a, _ in arms}       # the AI's turns (`ask` steps): what a model change moves
    agent = None
    if any(c.goal and not c.steps for c in cases):
        from .agent import GoalAgent
        agent = GoalAgent(profile, cache_dir=profile.root / ".assay-cache", log=log)   # one trajectory cache for all arms
    from .runner import AppProcess
    for attempt in range(attempts):
        for name, prof in arms:
            # every attempt of every arm starts from its golden state (reset → start), so repeated attempts
            # measure the build and the cases, not state leaking from the previous attempt
            proc = AppProcess(prof, root / f"app-{_safe(name)}.log")
            managed = bool(prof.app.get("start")) and not args.no_start
            fresh = managed and bool(prof.app.get("reset")) and not args.no_reset
            if managed and (fresh or not proc.is_up()):
                _wait_down(proc)
                try:
                    if fresh:
                        proc.reset()
                    proc.start()
                except RuntimeError as e:
                    log(f"  ✗ environment ({name}): {e}")
                    return 2
            elif not proc.is_up():
                log(f"  ✗ environment ({name}): app not reachable at {prof.base_url}")
                return 2
            run_dir = root / f"{_safe(name)}-{attempt + 1}"
            try:
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "run.json").write_text(json.dumps({"id": run_dir.name, "app": prof.name, "arm": name,
                                                              "attempt": attempt + 1, "case_order": [c.id for c in cases]}))
                load_plugins(prof)
                with Browsers(prof, headed=args.headed) as browsers:
                    for c in cases:
                        ev = run_case(c, prof, browsers, run_dir, agent=agent, auth_cache={})
                        timing[name].append(ev.get("duration_s", 0))
                        turns[name] += [x["latency_s"] for x in ev.get("answers") or [] if x.get("latency_s") is not None]
            finally:
                if managed:
                    proc.stop()
            judge = Judge({**profile.judge, **({"backend": "none"} if args.no_judge else {})},
                          cache_path=root / "judge-cache.json")
            a = analyze(run_dir, prof, judge=judge, log=log)
            for r in a["cases"]:
                verdicts[name].setdefault(r["case_id"], []).append(r["verdict"])
            s = a["summary"]
            log(f"  attempt {attempt + 1} · {name:<10} PASS {s['PASS']} FAIL {s['FAIL']} INCONCLUSIVE {s['INCONCLUSIVE']}")
    res = stats(verdicts)
    res["arms_meta"] = {n: {"title": p.title or p.name, "base_url": p.base_url, "env": arm_env.get(n, {}),
                            "median_journey_s": _median(timing[n]), "median_answer_s": _median(turns[n])}
                        for n, p in arms}
    res["app"] = profile.title or profile.name
    res["generated"] = time.strftime("%Y-%m-%d %H:%M")
    res["case_titles"] = {c.id: c.title for c in cases}
    (root / "bench.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    (root / "bench.md").write_text(_md(res, profile))
    from .bench_report import write_html
    write_html(root, res)
    log("")
    for a, r in res["arms"].items():
        d = r.get("diff_vs_baseline")
        log(f"  {a:<10} pass@1 {r['pass@1']} CI95 {r['ci95']}  pass^k {r['pass^k']}  flaky {len(r['flaky'])}"
            + (f"  Δ {d['mean']:+.3f} {d['ci95']}{' *' if d['significant'] else ''}" if d else ""))
    log(f"  report    {root / 'bench.html'}")
    host = getattr(args, "host", None) or os.environ.get("ASSAY_HOST_URL")
    if host:
        from .ci import publish_bench
        publish_bench(root, res, profile, host, getattr(args, "project_key", None), log)
    return 0


def with_env(profile, assignments: str):
    """The same app with other settings: `KEY=VALUE,KEY2=VALUE2` merged into the start command's environment."""
    kv = {}
    for pair in assignments.split(","):
        if "=" not in pair:
            raise ValueError(f"arm settings must be KEY=VALUE pairs, got {pair!r}")
        k, v = pair.split("=", 1)
        kv[k.strip()] = v.strip()
    start = dict(profile.app.get("start") or {})
    if not start:
        raise ValueError("env: arms need app.start in assay.yaml (Assay starts the app with these settings)")
    start["env"] = {**(start.get("env") or {}), **kv}
    return replace(profile, app={**profile.app, "start": start}), kv


def _wait_down(proc, timeout: float = 15) -> None:
    t0 = time.time()
    while proc.is_up() and time.time() - t0 < timeout:
        time.sleep(0.3)


def _safe(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in s)


def _median(xs: list[float]) -> float | None:
    return round(sorted(xs)[len(xs) // 2], 2) if xs else None


def _md(res: dict, profile) -> str:
    lines = [f"## Benchmark — {profile.title or profile.name}", "",
             f"{res['cases']} cases × {res['attempts']} attempts · baseline **{res['baseline']}**", "",
             "| Arm | pass@1 | 95% CI | pass^k | Δ vs baseline | flaky cases | median journey | median answer |",
             "|---|---|---|---|---|---|---|---|"]
    secs = lambda v: "—" if v is None else f"{v} s"
    for a, r in res["arms"].items():
        d = r.get("diff_vs_baseline")
        dd = f"{d['mean']:+.3f} [{d['ci95'][0]:+.3f}, {d['ci95'][1]:+.3f}]{' ✱' if d['significant'] else ''}" if d else "—"
        m = (res.get("arms_meta") or {}).get(a) or {}
        lines.append(f"| {a} | {r['pass@1']} | {r['ci95'][0]}–{r['ci95'][1]} | {r['pass^k']} | {dd} | {', '.join(r['flaky']) or '—'} "
                     f"| {secs(m.get('median_journey_s'))} | {secs(m.get('median_answer_s'))} |")
    aa = list(res["arms"]) == ["A", "A'"]
    lines += ["", "✱ the 95% interval excludes zero: the difference is real." + (
        " A vs A' is the same build twice, so any difference there is the noise floor (flakiness of the app or the "
        "journeys), not a real change." if aa else " Without ✱ it is within noise at this size: more rows tighten it."), ""]
    lines += ["| Case | " + " | ".join(res["arms"]) + " |", "|---|" + "---|" * len(res["arms"])]
    icon = {"PASS": "✅", "FAIL": "❌", "INCONCLUSIVE": "⚠️"}
    for c, per in res["per_case"].items():
        lines.append(f"| `{c}` | " + " | ".join("".join(icon[v] for v in per[a]) for a in res["arms"]) + " |")
    return "\n".join(lines) + "\n"
