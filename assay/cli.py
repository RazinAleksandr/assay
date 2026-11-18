"""assay — the quality gate for the running app.

  assay scan --url http://localhost:3000          zero-config: crawl + sensors, report in minutes
  assay run  projects/social                      start app, scan, run journeys, grade, gate
  assay run  projects/social -k 'post.*' --baseline runs/<prev>
  assay run  projects/social --confirm            run a failure that blocks once more before it counts (as CI does)
  assay grade projects/social runs/<id>           re-grade evidence (no app needed)
  assay brief [<analysis URL> | runs/<id>]        what a coding agent needs to fix the gate, as JSON
  assay mcp                                       the same for agents over MCP (stdio)
  assay doctor projects/social                    preflight: app, accounts, probes, judge, browsers
  assay bench projects/social [--attempts 3]      noise / flakiness / A-B of builds (pass@1 + 95% CI)
  assay portfolio projects/* -o portfolio.html     one page over all apps (gate, ratings, issues)

In an app's CI (SonarQube-style):
  assay init                                      add .assay/ and the CI step to an app repo
  assay browsers                                  download the browser the scanner drives (once per machine)
  assay ci                                        run, compare with the reference branch, upload, gate + link
  assay server --data ./assay-data                the site CI links to: projects, branches, pull requests, history
  assay upload runs/<id> --branch main            upload an existing run as an analysis

Exit codes: 0 gate PASSED · 1 FAILED · 2 INCONCLUSIVE · 3 usage/config error.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from argparse import Namespace
from pathlib import Path

from .config import Profile, load_cases, load_profile
from .model import now_iso

EXIT = {"PASSED": 0, "FAILED": 1, "INCONCLUSIVE": 2}
CONFIRM_DIR = "confirm"     # the second run of the failures that blocked, inside the run folder
SECOND_RUN = {"FAIL": "reproduced", "PASS": "passed: flaky, not blocking", "INCONCLUSIVE": "could not be checked: the failure stands"}


def _log(msg: str) -> None:
    print(msg, flush=True)


def _judge(profile: Profile, run_dir: Path, enabled: bool):
    from .judge import Judge
    cfg = dict(profile.judge)
    if not enabled:
        cfg["backend"] = "none"
    return Judge(cfg, cache_path=run_dir / "judge-cache.json")


def _finish(run_dir: Path, profile: Profile, judge, baseline) -> int:
    from .analyze import analyze
    from .report import write_all
    a = analyze(run_dir, profile, judge=judge, baseline=baseline, log=_log)
    write_all(run_dir, a)
    s, g = a["summary"], a["gate"]
    _log("")
    _log(f"  journeys  PASS {s['PASS']} · FAIL {s['FAIL']}" + (f" ({s['flaky']} passed on a second run)" if s.get("flaky") else "")
         + f" · INCONCLUSIVE {s['INCONCLUSIVE']}")
    _log(f"  issues    {s['issues']} ({s['new_issues']} new) · " +
         " ".join(f"{k}:{v}" for k, v in sorted(s['by_severity'].items())))
    _log("  ratings   " + " ".join(f"{k}={v or '—'}" for k, v in (a.get('ratings_new') or a['ratings']).items()))
    _log(f"  gate      {g['status']}")
    _log(f"  report    {run_dir / 'report.html'}")
    return EXIT[g["status"]]


def cmd_run(args) -> int:
    from .config import load_plugins
    from .runner import Browsers, app_running, run_case
    from .scan import scan
    profile = getattr(args, "profile_obj", None) or load_profile(args.project)
    load_plugins(profile)
    if args.scan_only:
        profile.gate["min_cases"] = 0
    dataset = [] if args.scan_only else load_cases(profile)
    cases, scope = _scope(dataset, args.k, getattr(args, "select", None))
    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.out or profile.root / "runs") / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "confirm.json").unlink(missing_ok=True)          # a reused run id: a second run of other evidence
    meta = {"id": run_id, "app": profile.name, "base_url": profile.base_url, "started": now_iso(),
            "case_order": [c.id for c in cases], "argv": sys.argv[1:], "ci": getattr(args, "ci", None),
            "project": _rel(profile.root), "scope": scope, "dataset": [c.entry() for c in dataset],
            "settings": {"ignore": profile.ignore, "volatile": profile.volatile, "gate": profile.raw.get("gate") or {}}}
    if getattr(args, "ci", None) is None and not args.baseline:
        profile.gate.setdefault("no_reference", True)        # a local run: what the product has now is its baseline
    if profile.gate.get("no_reference"):
        meta["no_reference"] = True                           # so `assay grade` reaches the same gate
    (run_dir / "run.json").write_text(json.dumps(meta, indent=1))
    if getattr(args, "diff_text", None):
        (run_dir / "change.diff").write_text(args.diff_text)     # suspects for failing journeys (assay/code.py)
    _log(f"assay · {profile.title or profile.name} · {len(cases)} of {len(dataset)} journeys · run {run_id}"
         if len(cases) != len(dataset) else f"assay · {profile.title or profile.name} · {len(cases)} journeys · run {run_id}")
    agent = None
    if args.agent or any(c.goal and not c.steps for c in cases):
        from .agent import GoalAgent
        agent = GoalAgent(profile, cache_dir=profile.root / ".assay-cache", log=_log)
    with app_running(profile, run_dir, start=not args.no_start, reset=not args.no_reset) as down:
        if down:
            _log(f"  ✗ environment: {down}")
            for c in cases:
                d = run_dir / "cases" / c.id
                d.mkdir(parents=True, exist_ok=True)
                (d / "evidence.json").write_text(json.dumps({"case_id": c.id, "title": c.title, "case": c.raw,
                                                             "cause": "app_unreachable", "error": down}))
        else:
            with Browsers(profile, headed=args.headed) as browsers:
                if (args.scan or args.scan_only) and not args.no_scan:
                    _log("  scanning screens…")
                    scan(profile, browsers, run_dir, log=_log)
                auth_cache: dict = {}
                for c in cases:
                    ev = run_case(c, profile, browsers, run_dir, agent=agent, auth_cache=auth_cache)
                    mark = "✗" if ev.get("cause") else "·"
                    _log(f"  {mark} {c.id:<44} {ev.get('duration_s', 0):>5.1f}s  {(ev.get('error') or '')[:90]}")
    meta["finished"] = now_iso()
    (run_dir / "run.json").write_text(json.dumps(meta, indent=1))
    judge = _judge(profile, run_dir, not args.no_judge)
    code = _finish(run_dir, profile, judge, args.baseline)
    if getattr(args, "confirm", False) and code == EXIT["FAILED"]:
        again = _confirm(args, profile, run_dir, judge)
        code = code if again is None else again
    return code


def _confirm(args, profile: Profile, run_dir: Path, judge) -> int | None:
    """Run the journeys that block once more, from a fresh app, before they block anything: a failure that does not
    reproduce is flaky and blocks nothing. Journeys share the app and its data, so a failure may need what the journeys
    before it did (the feed breaks past 20 posts, which earlier journeys created): the second run replays the first
    run's journeys in their order up to the last one that blocked, and only those that blocked are judged. It runs into
    <run>/confirm/; confirm.json records what it found and the run is analysed again, which applies it (`analyze`), so
    `assay grade` on the run folder reaches the same gate. Over `gate.confirm_max` failures nothing runs again: the rest
    would block anyway, and a change that breaks that many journeys is not flaky. None: nothing ran again."""
    import shutil
    from .model import INCONCLUSIVE
    from .quality import CONFIRM_MAX, blocking_failures
    from .runner import AppProcess
    ids = blocking_failures(json.loads((run_dir / "analysis.json").read_text()))
    cap = int(profile.gate.get("confirm_max", CONFIRM_MAX))
    if not ids or len(ids) > cap:
        if ids:
            _log(f"  {len(ids)} journeys fail, more than gate.confirm_max ({cap}): no second run")
        return None
    _log("")
    _log(f"  {len(ids)} journey{'s' if len(ids) != 1 else ''} block{'' if len(ids) != 1 else 's'}: a second run from a fresh app "
         "before blocking anything")
    if (run_dir / "app.log").exists():          # Assay started the first run's app: let it stop, the second run starts its own
        proc, t0 = AppProcess(profile, run_dir / "app.log"), time.time()
        while proc.is_up() and time.time() - t0 < 15:
            time.sleep(0.3)
    sub = run_dir / CONFIRM_DIR
    shutil.rmtree(sub, ignore_errors=True)
    order = json.loads((run_dir / "run.json").read_text()).get("case_order") or [] if (run_dir / "run.json").exists() else []
    last = max((order.index(i) for i in ids if i in order), default=-1)
    replay = order[:last + 1] if last >= 0 and set(ids) <= set(order) else ids
    cmd_run(Namespace(**{**vars(args), "run_id": CONFIRM_DIR, "out": str(run_dir), "k": None, "baseline": None,
                         "select": {"ids": replay, "why": "a second run, in order, up to the journeys that blocked"}, "scan": False,
                         "no_scan": True, "scan_only": False, "confirm": False, "diff_text": None}))
    found = {c["case_id"]: c for c in json.loads((sub / "analysis.json").read_text()).get("cases", [])} \
        if (sub / "analysis.json").exists() else {}
    cases = {}
    for cid in ids:                         # one the second run did not record could not be checked there
        c = found.get(cid) or {}
        cases[cid] = {"verdict": c.get("verdict") or INCONCLUSIVE,
                      **({"phrase": c["failure"].get("phrase")} if (c.get("failure") or {}).get("phrase") else {})}
    (run_dir / "confirm.json").write_text(json.dumps({"run": CONFIRM_DIR, "cases": cases}, indent=1, ensure_ascii=False))
    _log("")
    _log("  second run: " + " · ".join(f"{cid} {SECOND_RUN[c['verdict']]}" for cid, c in cases.items()))
    return _finish(run_dir, profile, judge, args.baseline)


def _scope(dataset: list, k: list[str] | None, select: dict | None) -> tuple[list, dict]:
    """Which journeys run and why: all of them, the ones `-k` names, or a selection for a pull request (assay/select.py)."""
    from .config import _select
    total = len(dataset)
    if select and select.get("ids") is None and not k:
        return dataset, {"mode": "all", "checked": total, "total": total, "why": select.get("why") or "every journey"}
    if select and select.get("ids") is not None:
        ids = set(select["ids"])
        cases = [c for c in dataset if c.id in ids]
        return cases, {"mode": "auto", "checked": len(cases), "total": total, "why": select.get("why", ""),
                       "reasons": select.get("reasons", {}), "skipped": list(select.get("skipped") or [])}
    if k:
        cases = [c for c in dataset if any(_select(c, s) for s in k)]
        return cases, {"mode": "filter", "checked": len(cases), "total": total, "why": "-k " + " ".join(k)}
    return dataset, {"mode": "all", "checked": total, "total": total, "why": "every journey"}


def _rel(root: Path) -> str:
    """The project folder as the repository names it (`.assay`), for commands in briefs and comments."""
    try:
        return str(Path(root).resolve().relative_to(Path.cwd().resolve())) or "."
    except ValueError:
        return str(root)


def cmd_grade(args) -> int:
    from .config import load_plugins
    profile = load_profile(args.project)
    load_plugins(profile)
    run_dir = Path(args.run_dir)
    judge = _judge(profile, run_dir, not args.no_judge)
    return _finish(run_dir, profile, judge, args.baseline)


def cmd_scan(args) -> int:
    if args.url:
        profile = Profile(name=args.name or "app", root=Path.cwd(), base_url=args.url, title=args.name or args.url)
        profile.gate.update(fail_on_case_fail=False, min_cases=0)     # a scan has no journeys to execute
        if args.routes:
            profile.scan["routes"] = args.routes
        args.project = None
        run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
        run_dir = Path(args.out or "runs") / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(json.dumps({"id": run_id, "app": profile.name, "base_url": args.url,
                                                      "case_order": []}))
        from .runner import Browsers
        from .scan import scan
        _log(f"assay scan · {args.url}")
        with Browsers(profile, headed=args.headed) as b:
            scan(profile, b, run_dir, max_pages=args.max_pages, log=_log)
        return _finish(run_dir, profile, None, args.baseline)
    args.scan_only, args.scan, args.k, args.agent = True, True, None, False
    args.no_scan = False
    return cmd_run(args)


def cmd_doctor(args) -> int:
    from .doctor import doctor
    return doctor(load_profile(args.project, strict=False), log=_log)    # doctor names every missing variable itself


def cmd_bench(args) -> int:
    from .bench import bench
    return bench(load_profile(args.project), args, log=_log)


def cmd_report(args) -> int:
    """Re-render reports from analysis.json; --embed writes one self-contained HTML file (images inlined)."""
    from .report import write_all, dashboard
    from .theme import embed_images
    run_dir = Path(args.run_dir)
    a = json.loads((run_dir / "analysis.json").read_text())
    write_all(run_dir, a)
    if args.output:
        html_text = dashboard(a)
        if args.embed:
            html_text = embed_images(html_text, run_dir, width=args.width)
        Path(args.output).write_text(html_text)
        _log(f"  {Path(args.output).resolve()}  {Path(args.output).stat().st_size / 1024:.0f} KB")
    return 0


def cmd_portfolio(args) -> int:
    from .portfolio import build
    res = build([Path(p) for p in args.projects], Path(args.output), args.run_id)
    _log(json.dumps(res))
    _log(f"  portfolio {Path(args.output).resolve()}")
    return 0


def cmd_brief(args) -> int:
    """The brief for a coding agent (JSON on stdout): from an analysis URL, a run folder, or the newest local run."""
    from . import brief as B
    src = args.source
    if src and src.startswith(("http://", "https://")):
        out = B.fetch(src, case=args.case, full=args.full)
    else:
        from .ci import find_project
        root = Path(src) if src and (Path(src) / "analysis.json").exists() else B.latest_run(find_project(src))
        from .code import local_diff
        out = B.build(root, case=args.case, full=args.full, diff_text=local_diff(root))
    print(json.dumps(out, indent=1, ensure_ascii=False, default=str))
    return 0


def cmd_mcp(args) -> int:
    from .mcp import serve
    return serve()


def cmd_ci(args) -> int:
    from .ci import cmd_ci as run
    return run(args, log=_log)


def cmd_upload(args) -> int:
    from .ci import cmd_upload as run
    return run(args, log=_log)


def cmd_hash_password(args) -> int:
    import getpass
    from .access import hash_password
    print(hash_password(args.password or getpass.getpass("password: ")))
    return 0


def cmd_server(args) -> int:
    from .server import serve
    return serve(args, log=_log)


def cmd_init(args) -> int:
    from .init import init
    return init(args, log=_log)


def cmd_browsers(args) -> int:
    """Download the browsers the scanner drives (the Playwright build it is pinned to)."""
    import subprocess
    cmd = [sys.executable, "-m", "playwright", "install", *(["--with-deps"] if args.with_deps else []), *args.names]
    return subprocess.call(cmd)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="assay", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    from . import __version__
    ap.add_argument("--version", action="version", version=f"assay {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common_run(p):
        p.add_argument("project", nargs="?", help="project dir with assay.yaml")
        p.add_argument("--run-id")
        p.add_argument("--out", help="runs directory (default <project>/runs)")
        p.add_argument("--baseline", help="previous run dir: only new issues count, regressions are flagged")
        p.add_argument("--headed", action="store_true")
        p.add_argument("--no-start", action="store_true", help="do not start the app (it must already be running)")
        p.add_argument("--no-reset", action="store_true", help="skip the app reset command before start")
        p.add_argument("--no-judge", action="store_true", help="judge items become undecided")

    p = sub.add_parser("run", help="run journeys (and optionally scan) and apply the quality gate")
    common_run(p)
    p.add_argument("-k", action="append", help="select cases: glob on id, tag:<name>, group:<glob>, is:critical (repeatable)")
    p.add_argument("--scan", action="store_true", help="also crawl and audit every screen")
    p.add_argument("--no-scan", action="store_true")
    p.add_argument("--scan-only", action="store_true")
    p.add_argument("--agent", action="store_true", help="enable the goal agent for `goal` cases")
    p.add_argument("--confirm", action="store_true",
                   help="run the journeys that fail the gate once more from a fresh app, as `assay ci` does: "
                        "one that passes then is flaky and does not block")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("scan", help="zero-config crawl + sensors (works on any URL)")
    common_run(p)
    p.add_argument("--url")
    p.add_argument("--name")
    p.add_argument("--routes", nargs="*")
    p.add_argument("--max-pages", type=int)
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("grade", help="re-grade an existing run from its evidence")
    p.add_argument("project")
    p.add_argument("run_dir")
    p.add_argument("--baseline")
    p.add_argument("--no-judge", action="store_true")
    p.set_defaults(fn=cmd_grade)

    p = sub.add_parser("doctor", help="preflight checks for a project")
    p.add_argument("project")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("bench", help="repeat journeys: flakiness, noise floor, A/B of builds")
    common_run(p)
    p.add_argument("-k", action="append", help="default: bench.select in assay.yaml, else the journeys marked ai: true")
    p.add_argument("--attempts", type=int, help="per journey and arm (default: bench.attempts in assay.yaml, else 3)")
    p.add_argument("--arm", action="append",
                   help="name=<project dir> | name=<url> | name=env:KEY=VALUE[,K=V] (first is the baseline); "
                        "default: bench.arms in assay.yaml, else A/A")
    p.add_argument("--host", help="Assay server to publish to (default $ASSAY_HOST_URL; none: the report stays local)")
    p.add_argument("--project-key", help="default: gh/<repository> on GitHub Actions, else $ASSAY_PROJECT_KEY or the profile name")
    p.set_defaults(fn=cmd_bench)

    p = sub.add_parser("report", help="re-render a run's reports; --embed -o file.html for a shareable single file")
    p.add_argument("run_dir")
    p.add_argument("-o", "--output")
    p.add_argument("--embed", action="store_true")
    p.add_argument("--width", type=int, default=900)
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("portfolio", help="one overview page over several projects' latest runs")
    p.add_argument("projects", nargs="+")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--run-id")
    p.set_defaults(fn=cmd_portfolio)

    p = sub.add_parser("brief", help="what a coding agent needs to fix a failed gate, as JSON (≈4 000 tokens)")
    p.add_argument("source", nargs="?", help="analysis URL (from the pull request comment), run folder, or project (default: newest run in .assay)")
    p.add_argument("--case", help="one journey")
    p.add_argument("--full", action="store_true", help="with --case: steps, checks, data changes, errors, API calls")
    p.set_defaults(fn=cmd_brief)

    p = sub.add_parser("mcp", help="MCP server (stdio) for coding agents: status, brief, run, artifact")
    p.set_defaults(fn=cmd_mcp)

    p = sub.add_parser("ci", help="the CI step: run, compare with the reference branch, upload to the server, gate + link")
    p.add_argument("project", nargs="?", help="default: .assay/ in the current directory")
    p.add_argument("--host", help="Assay server URL (default $ASSAY_HOST_URL)")
    p.add_argument("--project-key", help="default $ASSAY_PROJECT_KEY or the profile name")
    p.add_argument("--out", help="runs directory (default .assay/runs)")
    p.add_argument("-k", action="append")
    p.add_argument("--no-scan", action="store_true")
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--no-judge", action="store_true")
    p.set_defaults(fn=cmd_ci)

    p = sub.add_parser("upload", help="upload an existing run folder to the Assay server")
    p.add_argument("run_dir")
    p.add_argument("--host")
    p.add_argument("--project-key")
    p.add_argument("--branch", default="main")
    p.add_argument("--sha")
    p.add_argument("--pr")
    p.add_argument("--pr-title")
    p.add_argument("--target")
    p.add_argument("--date", help="ISO time of the analysis (default: the run's start)")
    p.set_defaults(fn=cmd_upload)

    p = sub.add_parser("server", help="the Assay server: dashboards, history, branches and pull requests, API for CI")
    p.add_argument("--data", default="assay-data", help="storage folder (SQLite index + analyses)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--public-url", help="URL CI links should use (default http://host:port)")
    p.add_argument("--config", help="server.yaml: gate for all projects or per project")
    p.set_defaults(fn=cmd_server)

    p = sub.add_parser("hash-password", help="the admin password hash for server.yaml (admin: {user: admin, password: <hash>})")
    p.add_argument("password", nargs="?")
    p.set_defaults(fn=cmd_hash_password)

    p = sub.add_parser("init", help="add Assay to an app repo: .assay/assay.yaml, a smoke journey, the CI step")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--ci", choices=["github", "gitlab"], default="github")
    p.add_argument("--url", help="where the app listens once started (default: guessed from package.json)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("browsers", help="download the browsers the scanner drives (default: chromium)")
    p.add_argument("names", nargs="*", default=["chromium"])
    p.add_argument("--with-deps", action="store_true", help="also the system libraries (Linux CI)")
    p.set_defaults(fn=cmd_browsers)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (FileNotFoundError, ValueError, KeyError, RuntimeError, __import__("yaml").YAMLError) as e:
        print(f"assay: {type(e).__name__}: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
