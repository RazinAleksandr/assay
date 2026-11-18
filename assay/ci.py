"""`assay ci` — the step an app's pipeline runs, modelled on SonarQube's scanner.

    assay ci                      # in the app repo; reads .assay/assay.yaml

1. Reads the CI context: provider, branch, commit, pull/merge request and its target branch.
2. Asks the Assay server for the reference analysis (the target branch for a pull request, the previous analysis
   for a branch) and for the project's gate if the server defines one.
3. Starts the app, scans its screens, runs the journeys, grades them against the reference: only *new* problems block.
   A journey whose failure blocks runs once more from a fresh app (`gate.confirm`, at most `gate.confirm_max`): only a
   failure that reproduces blocks; one that passes the second time is flaky, listed and never blocking.
4. Uploads the evidence. The server answers with the analysis URL.
5. Prints the gate and the link, writes the job summary and the PR/MR comment, and exits
   0 PASSED · 1 FAILED · 2 INCONCLUSIVE · 3 configuration error.

Without ASSAY_HOST_URL it is a local run with the same gate; the reports stay in the job's artifacts.
On GitHub Actions the job signs in with its OIDC token (`permissions: id-token: write`): no ASSAY_TOKEN, and the server
files the analysis under the repository's own project, gh/<owner>/<repo>, creating the account on the first upload.
Environment: ASSAY_HOST_URL, ASSAY_TOKEN (other CI), optionally ASSAY_PROJECT_KEY, GITHUB_TOKEN / ASSAY_GITLAB_TOKEN for PR comments.
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from argparse import Namespace
from pathlib import Path


MARK = "<!-- assay-quality-gate:{key} -->"


# ---- CI context ------------------------------------------------------------------------------------------------

def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def detect_context(env: dict | None = None) -> dict:
    """Branch, commit and pull request of the current pipeline, from GitHub Actions, GitLab CI or plain git."""
    env = dict(os.environ if env is None else env)
    ctx: dict = {"provider": "local", "branch": None, "sha": None, "pr": None, "pr_title": None, "target": None,
                 "default_branch": None, "repo": None, "run_url": None, "base_sha": None, "labels": []}
    if env.get("GITHUB_ACTIONS") == "true":
        ev = {}
        if env.get("GITHUB_EVENT_PATH") and Path(env["GITHUB_EVENT_PATH"]).exists():
            ev = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text() or "{}")
        pr = ev.get("pull_request") or {}
        ctx.update(provider="github", repo=env.get("GITHUB_REPOSITORY"), sha=env.get("GITHUB_SHA"),
                   default_branch=(ev.get("repository") or {}).get("default_branch"),
                   run_url=f'{env.get("GITHUB_SERVER_URL", "https://github.com")}/{env.get("GITHUB_REPOSITORY")}/actions/runs/{env.get("GITHUB_RUN_ID")}'
                   if env.get("GITHUB_RUN_ID") else None)
        if env.get("GITHUB_EVENT_NAME", "").startswith("pull_request"):
            ref = env.get("GITHUB_REF", "")
            ctx.update(pr=str(pr.get("number") or (ref.split("/")[2] if ref.startswith("refs/pull/") else "")) or None,
                       pr_title=pr.get("title"), branch=env.get("GITHUB_HEAD_REF"), target=env.get("GITHUB_BASE_REF"),
                       sha=(pr.get("head") or {}).get("sha") or env.get("GITHUB_SHA"),
                       base_sha=(pr.get("base") or {}).get("sha"), labels=[x.get("name") for x in pr.get("labels") or []])
        else:
            ctx["branch"] = env.get("GITHUB_REF_NAME")
    elif env.get("GITLAB_CI") == "true":
        ctx.update(provider="gitlab", repo=env.get("CI_PROJECT_PATH"), sha=env.get("CI_COMMIT_SHA"),
                   default_branch=env.get("CI_DEFAULT_BRANCH"), run_url=env.get("CI_PIPELINE_URL"))
        if env.get("CI_MERGE_REQUEST_IID"):
            ctx.update(pr=env["CI_MERGE_REQUEST_IID"], pr_title=env.get("CI_MERGE_REQUEST_TITLE"),
                       branch=env.get("CI_MERGE_REQUEST_SOURCE_BRANCH_NAME"),
                       target=env.get("CI_MERGE_REQUEST_TARGET_BRANCH_NAME"),
                       base_sha=env.get("CI_MERGE_REQUEST_DIFF_BASE_SHA"),
                       labels=[x for x in (env.get("CI_MERGE_REQUEST_LABELS") or "").split(",") if x])
        else:
            ctx["branch"] = env.get("CI_COMMIT_REF_NAME")
    else:
        ctx.update(branch=_git("rev-parse", "--abbrev-ref", "HEAD") or None, sha=_git("rev-parse", "HEAD") or None)
    # explicit overrides win (any CI, or a laptop)
    for k in ("branch", "sha", "pr", "pr_title", "target", "default_branch", "repo", "base_sha"):
        if env.get(f"ASSAY_{k.upper()}"):
            ctx[k] = env[f"ASSAY_{k.upper()}"]
    if ctx["pr"] and not ctx["target"]:
        ctx["target"] = ctx["default_branch"] or "main"
    return ctx


# ---- server client ------------------------------------------------------------------------------------------------

def github_oidc_token(audience: str, env: dict | None = None) -> str | None:
    """A fresh GitHub Actions OIDC token for the Assay server (needs `permissions: id-token: write`), or None outside it."""
    env = os.environ if env is None else env
    url, tok = env.get("ACTIONS_ID_TOKEN_REQUEST_URL"), env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not tok:
        return None
    req = urllib.request.Request(url + ("&" if "?" in url else "?") + "audience=" + urllib.parse.quote(audience, safe=""),
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())["value"]


class Server:
    def __init__(self, host: str, token):
        """token: a string, or a callable that returns one per request (OIDC tokens are short-lived)."""
        self.host, self.token = host.rstrip("/"), token

    def _req(self, method: str, path: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 60):
        h = {"Accept": "application/json", **(headers or {})}
        tok = self.token() if callable(self.token) else self.token
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        req = urllib.request.Request(self.host + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            if e.code == 404:
                return 404, None
            raise RuntimeError(f"Assay server {method} {path}: HTTP {e.code} {body}") from None

    def project(self, key: str) -> dict:
        st, body = self._req("GET", f"/api/projects/{urllib.parse.quote(key)}")
        return body or {}

    def reference(self, key: str, branch: str) -> dict | None:
        st, body = self._req("GET", f"/api/projects/{urllib.parse.quote(key)}/analyses/latest?branch={urllib.parse.quote(branch)}")
        return body if st == 200 else None

    def upload(self, run_dir: Path, meta: dict) -> dict:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=5) as tar:
            for f in sorted(run_dir.rglob("*")):
                if f.is_file() and not f.name.endswith((".webm", ".zip")):
                    tar.add(f, arcname=str(f.relative_to(run_dir)))
        hdr = {"Content-Type": "application/gzip",
               "X-Assay-Meta": base64.b64encode(json.dumps(meta).encode()).decode()}
        st, body = self._req("POST", "/api/analyses", data=buf.getvalue(), headers=hdr, timeout=300)
        return body

    def upload_bench(self, bench_dir: Path, meta: dict) -> dict:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name in ("bench.json", "bench.md", "bench.html"):
                if (bench_dir / name).exists():
                    tar.add(bench_dir / name, arcname=name)
        hdr = {"Content-Type": "application/gzip", "X-Assay-Meta": base64.b64encode(json.dumps(meta).encode()).decode()}
        st, body = self._req("POST", "/api/benchmarks", data=buf.getvalue(), headers=hdr, timeout=120)
        return body


def connect(host: str | None, ctx: dict, key: str, log=print) -> tuple["Server | None", str]:
    """The server client and the project key. On GitHub Actions the job signs in with its OIDC token and the
    project is gh/<repository>; elsewhere ASSAY_TOKEN (the server's admin token) and the given key."""
    if not host:
        return None, key
    token = os.environ.get("ASSAY_TOKEN")
    if not token and ctx["provider"] == "github" and ctx.get("repo"):
        if os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL"):
            return Server(host, lambda: github_oidc_token(host.rstrip("/"))), "gh/" + ctx["repo"]
        log("  ! the job cannot sign in to Assay: add `permissions: id-token: write` to it (no token needed); "
            "the results stay in the job artifacts")
        return None, key
    return Server(host, token), key


# ---- PR comment / job summary -------------------------------------------------------------------------------------

def comment_markdown(a: dict, url: str | None, ctx: dict, key: str, brief_url: str | None = None) -> str:
    """The pull request comment: the verdict in four lines (see report.verdict_markdown), one comment per project."""
    from .report import verdict_markdown
    return MARK.format(key=key) + "\n" + verdict_markdown(a, url=url, brief_url=brief_url)


def _http_json(method: str, url: str, headers: dict, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


def pr_labels(ctx: dict, env: dict | None = None) -> list[str]:
    """The pull request's labels now (a re-run keeps the event of the first run, whose labels may be stale)."""
    env = dict(os.environ if env is None else env)
    try:
        if ctx["provider"] == "github" and env.get("GITHUB_TOKEN") and ctx.get("repo") and ctx.get("pr"):
            api = env.get("GITHUB_API_URL", "https://api.github.com")
            rows = _http_json("GET", f"{api}/repos/{ctx['repo']}/issues/{ctx['pr']}/labels",
                              {"Authorization": f"Bearer {env['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"})
            return [r.get("name") for r in rows or []]
    except Exception:
        pass
    return []


def post_comment(ctx: dict, text: str, key: str, env: dict | None = None, marker: str | None = None) -> str:
    """Create or update the single Assay comment on the PR/MR. Returns what happened, for the log."""
    env = dict(os.environ if env is None else env)
    marker = marker or MARK.format(key=key)
    try:
        if ctx["provider"] == "github" and env.get("GITHUB_TOKEN"):
            api = env.get("GITHUB_API_URL", "https://api.github.com")
            h = {"Authorization": f"Bearer {env['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}
            base = f"{api}/repos/{ctx['repo']}/issues/{ctx['pr']}/comments"
            old = next((c for c in _http_json("GET", base + "?per_page=100", h) if marker in (c.get("body") or "")), None)
            if old:
                _http_json("PATCH", f"{api}/repos/{ctx['repo']}/issues/comments/{old['id']}", h, {"body": text})
                return "updated the pull request comment"
            _http_json("POST", base, h, {"body": text})
            return "commented on the pull request"
        if ctx["provider"] == "gitlab" and env.get("ASSAY_GITLAB_TOKEN"):
            api = env.get("CI_API_V4_URL", "https://gitlab.com/api/v4")
            h = {"PRIVATE-TOKEN": env["ASSAY_GITLAB_TOKEN"]}
            base = f"{api}/projects/{env.get('CI_PROJECT_ID')}/merge_requests/{ctx['pr']}/notes"
            old = next((n for n in _http_json("GET", base + "?per_page=100", h) if marker in (n.get("body") or "")), None)
            if old:
                _http_json("PUT", f"{base}/{old['id']}", h, {"body": text})
                return "updated the merge request note"
            _http_json("POST", base, h, {"body": text})
            return "commented on the merge request"
    except Exception as e:  # never fail the pipeline because of a comment
        return f"could not comment ({type(e).__name__}: {e})"
    return "no token for comments: the comment is in ci-comment.md and the job summary"


# ---- the command ----------------------------------------------------------------------------------------------------

def find_project(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    for cand in (Path(".assay"), Path(".")):
        if (cand / "assay.yaml").exists():
            return cand
    raise FileNotFoundError("no .assay/assay.yaml here: run `assay init` in the app repo, or pass the project folder")


def cmd_ci(args, log=print) -> int:
    from .cli import EXIT, cmd_run
    from .config import load_profile

    project = find_project(args.project)
    profile = load_profile(project)
    ctx = detect_context()
    mark = args.project_key or os.environ.get("ASSAY_PROJECT_KEY") or profile.name
    server, key = connect(args.host or os.environ.get("ASSAY_HOST_URL"), ctx, mark, log)
    log(f"assay ci · project {key} · {ctx['provider']} · "
        + (f"pull request #{ctx['pr']} ({ctx['branch']} → {ctx['target']})" if ctx["pr"] else f"branch {ctx['branch']}")
        + (f" · commit {ctx['sha'][:7]}" if ctx.get("sha") else ""))

    reference, ref_branch = None, (ctx["target"] if ctx["pr"] else ctx["branch"])
    if server:
        try:
            settings = server.project(key)
            profile.gate.setdefault("new_code", bool(ctx["pr"]))   # PR: only what it changes blocks it
            if settings.get("gate"):
                profile.gate = {**profile.gate, **settings["gate"]}
                log("  gate: defined on the Assay server")
            if ref_branch:
                reference = server.reference(key, ref_branch)
            if ctx["pr"] and not reference:
                profile.gate["first_analysis"] = True   # nothing to compare with: list problems, block nothing
            elif not reference:
                profile.gate["no_reference"] = True     # the branch's first analysis: what it has now is its baseline
            log(f"  reference: {ref_branch} @ {reference['ci']['sha'][:7]} (analysis #{reference['server']['id']})"
                if reference and (reference.get("ci") or {}).get("sha") else
                f"  reference: none on {ref_branch} yet, findings are listed but block nothing" if ctx["pr"] else
                f"  reference: none on {ref_branch} yet: the problems it has now are its baseline (listed), failing journeys block")
        except Exception as e:
            log(f"  ! Assay server unreachable ({e}); continuing without a reference")
            server = None
    else:
        log("  ASSAY_HOST_URL is not set: local analysis, reports stay in the job artifacts")

    diff_text, select = None, None
    if ctx["pr"]:
        from .code import ci_base_sha, ci_diff
        from .config import load_cases
        from .select import select as pick
        ctx["labels"] = sorted(set(ctx.get("labels") or []) | set(pr_labels(ctx)))
        diff_text = ci_diff(ci_base_sha(ctx))
        if not args.k:
            select = pick([c.entry() for c in load_cases(profile)], reference, diff_text, profile.raw.get("select"),
                          seed=ctx.get("sha") or "", groups=profile.raw.get("groups"))
            log(f"  journeys: {select['why']}" if select["ids"] is None else
                f"  journeys: {len(select['ids'])} selected ({select['why']})")
    run_id = time.strftime("%Y%m%d-%H%M%S") + (f"-pr{ctx['pr']}" if ctx["pr"] else "")
    out = Path(args.out) if args.out else profile.root / "runs"
    ns = Namespace(project=str(project), profile_obj=profile, k=args.k, run_id=run_id, out=str(out), baseline=reference,
                   headed=False, no_start=args.no_start, no_reset=False, no_judge=args.no_judge,
                   scan=not args.no_scan and profile.raw.get("ci", {}).get("scan", True), no_scan=args.no_scan,
                   scan_only=False, agent=False, ci=ctx, select=select, diff_text=diff_text,
                   confirm=bool(profile.gate.get("confirm", True)))
    code = cmd_run(ns)
    run_dir = out / run_id
    a = json.loads((run_dir / "analysis.json").read_text())

    url = brief_url = None
    if server:
        meta = {"project": key, "title": profile.title or profile.name, **ctx,
                "reference_id": (reference or {}).get("server", {}).get("id")}
        try:
            res = server.upload(run_dir, meta)
            url, brief_url = res["url"], res.get("brief_url")
            if res.get("account_url"):
                log(f"  account: {res['account_url']}" + (" · this analysis created the project" if res.get("created_project") else ""))
        except Exception as e:
            log(f"  ! upload failed: {e}")
    text = comment_markdown(a, url, ctx, mark, brief_url)
    (run_dir / "ci-comment.md").write_text(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text)
    if ctx["pr"]:
        log(f"  {post_comment(ctx, text, mark)}")
    log("")
    if url:
        log(f"ANALYSIS SUCCESSFUL, you can find the results at: {url}")
    log(f"QUALITY GATE STATUS: {a['gate']['status']}" + (f" - View details on {url}" if url else ""))
    return EXIT[a["gate"]["status"]] if code in EXIT.values() else code


def cmd_upload(args, log=print) -> int:
    """Upload an existing run folder (e.g. history from before CI) as an analysis of a branch or pull request."""
    run_dir = Path(args.run_dir)
    a = json.loads((run_dir / "analysis.json").read_text())
    host = args.host or os.environ.get("ASSAY_HOST_URL")
    if not host:
        raise ValueError("pass --host or set ASSAY_HOST_URL")
    meta = {"project": args.project_key or a["app"]["name"], "title": a["app"].get("title") or a["app"]["name"],
            "provider": "upload", "branch": args.branch, "sha": args.sha, "pr": args.pr, "pr_title": args.pr_title,
            "target": args.target, "created": args.date}
    res = Server(host, os.environ.get("ASSAY_TOKEN")).upload(run_dir, meta)
    log(f"uploaded analysis #{res['id']}: {res['url']}")
    return 0


# ---- benchmark: publish, job summary, PR comment ------------------------------------------------------------------

BENCH_MARK = "<!-- assay-benchmark:{key} -->"


def bench_verdict(res: dict) -> tuple[str, str]:
    """A benchmark in the same words as the gate: is the candidate better, worse, or not distinguishable from the
    baseline at this sample size. ± is half the 95% interval of the difference."""
    base = res["baseline"]
    others = [(a, r["diff_vs_baseline"]) for a, r in res["arms"].items() if a != base and r.get("diff_vs_baseline")]
    if not others:
        r = res["arms"][base]
        lo, hi = r["ci95"]
        rng = f" ({lo * 100:.0f}–{hi * 100:.0f}%)" if lo is not None and hi is not None else ""
        flaky = len(r.get("flaky") or [])
        return "🔁", f"AI features pass {r['pass@1'] * 100:.0f}%{rng} of attempts" + (f", {flaky} journey{'s' if flaky != 1 else ''} flaky" if flaky else "")

    def pp(d):
        lo, hi = d["ci95"]
        half = (hi - lo) / 2 * 100 if lo is not None and hi is not None else None
        return f"{d['mean'] * 100:+.0f} pp" + (f" (±{half:.0f})" if half is not None else "")
    worse = [(a, d) for a, d in others if d["significant"] and d["mean"] < 0]
    better = [(a, d) for a, d in others if d["significant"] and d["mean"] > 0]
    if worse:
        a, d = min(worse, key=lambda x: x[1]["mean"])
        return "❌", f"AI features: `{a}` is worse than `{base}`, {pp(d)}"
    if better:
        a, d = max(better, key=lambda x: x[1]["mean"])
        return "✅", f"AI features: `{a}` is better than `{base}`, {pp(d)}"
    a, d = others[0]
    return "➖", f"AI features: no significant difference, `{a}` {pp(d)} against `{base}` (not significant)"


def bench_markdown(res: dict, url: str | None, ctx: dict, key: str) -> str:
    where = (f"pull request #{ctx['pr']}" if ctx.get("pr") else f"branch `{ctx.get('branch') or '—'}`")
    icon, line = bench_verdict(res)
    rates = " · ".join(f"`{a}` {r['pass@1'] * 100:.0f}%" for a, r in res["arms"].items() if r.get("pass@1") is not None)
    flaky = sorted({c for r in res["arms"].values() for c in r.get("flaky") or []})
    lines = [BENCH_MARK.format(key=key), f"### {icon} {line}",
             f"Pass rate on one attempt: {rates}." + (f" Flaky: {', '.join(f'`{c}`' for c in flaky[:6])}." if flaky else ""),
             f"<sub>{res['cases']} journeys × {res['attempts']} attempts per arm · {res['app']} · {where}"
             + (f" · commit `{ctx['sha'][:7]}`" if ctx.get("sha") else "") + "</sub>"]
    if url:
        lines.append(f"**[See the benchmark →]({url})**")
    table = ["| Arm | Pass rate | 95% interval | Δ vs " + res["baseline"] + " | Flaky journeys |", "|---|---|---|---|---|"]
    for a, r in res["arms"].items():
        d = r.get("diff_vs_baseline")
        pct = lambda v: "—" if v is None else f"{v * 100:.0f}%"
        dd = "baseline" if d is None else (f"{d['mean'] * 100:+.0f} pp [{d['ci95'][0] * 100:+.0f}, {d['ci95'][1] * 100:+.0f}]"
                                          + (" ✱" if d["significant"] else ""))
        env = ", ".join(f"{k}={v}" for k, v in (res.get("arms_meta", {}).get(a, {}).get("env") or {}).items())
        table.append(f"| **{a}**" + (f" `{env}`" if env else "") + f" | {pct(r['pass@1'])} | {pct(r['ci95'][0])}–{pct(r['ci95'][1])} | {dd} | "
                     + (", ".join(f"`{c}`" for c in r["flaky"]) or "none") + " |")
    lines += ["", "<details><summary>Arms</summary>", "", *table, "",
              "✱ the difference is beyond chance: its 95% interval excludes zero.", "", "</details>"]
    return "\n".join(lines) + "\n"


def publish_bench(root: Path, res: dict, profile, host: str, project_key: str | None = None, log=print) -> str | None:
    """Upload a finished benchmark, write the job summary and, on a pull request, the comment. Returns the URL."""
    ctx = detect_context()
    mark = project_key or os.environ.get("ASSAY_PROJECT_KEY") or profile.name
    server, key = connect(host, ctx, mark, log)
    url = None
    if server:
        try:
            r = server.upload_bench(root, {"project": key, "title": profile.title or profile.name, **ctx})
            url = r["url"]
            log(f"  benchmark on Assay: {url}")
        except Exception as e:
            log(f"  ! benchmark upload failed: {e}")
    text = bench_markdown(res, url, ctx, mark)
    (root / "bench-comment.md").write_text(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text)
    if ctx.get("pr"):
        log(f"  {post_comment(ctx, text, mark, marker=BENCH_MARK.format(key=mark))}")
    return url
