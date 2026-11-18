"""`assay mcp`: the agent's interface to Assay as an MCP server (stdio, JSON-RPC 2.0, one message per line).

Coding agents (Claude Code, Codex, Cursor, Copilot) ask for data, not pages:
  assay_status(pr?)              the verdict of a pull request (from its Assay comment) and the link to its brief
  assay_brief(source?, case?, full?)  the brief: failures, the check that failed, suspect code, how to reproduce
  assay_run(select?, full?)      run journeys locally on the agent's change and return the new brief
  assay_artifact(ref)            one screenshot (as an image) or evidence file from a brief's links

The agentic freedom test (assay/explore.py): the agent uses the product freely while Assay shows it what changed.
  assay_explore_start(charter?, as?, viewport?, url?, fresh?)   start the app and a browser, open the first page
  assay_explore_act(steps, why?, look?, audit?)                 act in the step language; the page, the data that
                                                                changed, new errors and API calls come back
  assay_explore_note(title, detail?, kind?, severity?, step?)   note a finding at a step
  assay_explore_finish()                                        close; session.json, findings.md, trail.yaml

No SDK: the protocol subset tools need is small, and the scanner should not pull a web framework into every CI job.
Logs go to stderr; stdout carries only protocol messages.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from . import __version__

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

TOOLS = [
    {"name": "assay_status",
     "description": "The Assay verdict of a pull request (ship / don't ship / not sure), its headline and the links to the "
                    "evidence and to the brief. Reads the Assay comment of the pull request with the `gh` CLI. "
                    "Without `pr`: the pull request of the current branch.",
     "inputSchema": {"type": "object", "properties": {"pr": {"type": ["integer", "string"], "description": "pull request number"}}}},
    {"name": "assay_brief",
     "description": "What to fix: the verdict, every failing journey with the check that failed (expected vs what the app "
                    "actually saved), the step, the suspect code of the change, how to reproduce it, links to screenshots. "
                    "`source`: the analysis or brief URL from the pull request comment, or a local run folder; default: "
                    "the newest local run. `case` + `full`: everything one journey recorded.",
     "inputSchema": {"type": "object", "properties": {
         "source": {"type": "string"}, "case": {"type": "string"}, "full": {"type": "boolean"}}}},
    {"name": "assay_run",
     "description": "Run Assay journeys locally (starts the app from .assay/assay.yaml) and return the brief of that run, "
                    "with suspects from this branch's changes. `select`: journey ids or globs (`comment.*`), `tag:<name>`, "
                    "`is:critical`; default: all. Use it after a fix, until the verdict is ship.",
     "inputSchema": {"type": "object", "properties": {
         "select": {"type": "array", "items": {"type": "string"}}, "full": {"type": "boolean"},
         "project": {"type": "string", "description": "folder with assay.yaml (default .assay)"}}}},
    {"name": "assay_artifact",
     "description": "One file a brief links to: a screenshot comes back as an image, evidence.json as text.",
     "inputSchema": {"type": "object", "properties": {"ref": {"type": "string", "description": "URL or local path"}},
                     "required": ["ref"]}},
    {"name": "assay_explore_start",
     "description": "Start an agentic freedom test: Assay starts the app from .assay/assay.yaml (from its golden state "
                    "unless fresh=false), opens one browser (signed in as `as`, an account of the profile) and returns "
                    "the first observation. One exploration at a time; finish it with assay_explore_finish. The budget (60 "
                    "steps, 20 minutes by default) is enforced: every observation says what is left.",
     "inputSchema": {"type": "object", "properties": {
         "charter": {"type": "string", "description": "what this exploration is after, in one sentence"},
         "as": {"type": "string"}, "viewport": {"type": "string", "description": "desktop (default), mobile, …"},
         "url": {"type": "string", "description": "first page (default /)"}, "fresh": {"type": "boolean"},
         "max_actions": {"type": "integer", "description": "budget of steps (default 60)"},
         "minutes": {"type": "number", "description": "budget of time (default 20)"},
         "project": {"type": "string", "description": "folder with assay.yaml (default .assay)"}}}},
    {"name": "assay_explore_act",
     "description": "Do something in the app, in the journey step language (one-key objects, as in .assay/cases): "
                    "{\"goto\": \"/p/1\"}, {\"click\": {\"role\": \"button\", \"name\": \"Post\"}}, "
                    "{\"fill\": {\"label\": \"Comment\", \"value\": \"…\"}}, {\"press\": \"Enter\"}, {\"back\": true}, … "
                    "Steps run in order and stop at the first that fails. Returns the page (URL, title, accessibility "
                    "tree), `data`: what changed in the app's saved state since before these steps (read from the "
                    "profile's probes: compare it with what the screen claims), new `errors`, `failed_requests`, the "
                    "API `calls` made, and the screenshot path. `look`: also return the screenshot as an image. "
                    "`audit`: also run accessibility and layout checks on this screen.",
     "inputSchema": {"type": "object", "properties": {
         "steps": {"type": "array", "items": {"type": "object"}}, "why": {"type": "string"},
         "look": {"type": "boolean"}, "audit": {"type": "boolean"}}, "required": ["steps"]}},
    {"name": "assay_explore_note",
     "description": "Note a finding of the current exploration at a step (default: the last one): kind bug | ux | "
                    "question | idea, severity blocker | critical | major | minor. Say what you expected, what happened "
                    "and the evidence (the data change, the error, the screen).",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string"}, "detail": {"type": "string"}, "kind": {"type": "string"},
         "severity": {"type": "string"}, "step": {"type": "integer"}}, "required": ["title"]}},
    {"name": "assay_explore_finish",
     "description": "End the exploration: close the browser, stop the app if Assay started it, and write "
                    ".assay/runs/explore-<time>/ (session.json, findings.md, trail.yaml: the steps that worked as a "
                    "draft journey). Returns the paths and what changed in the data overall.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _log(msg: str) -> None:
    print(f"assay mcp: {msg}", file=sys.stderr, flush=True)


def _text(obj) -> dict:
    return {"content": [{"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj, indent=1, ensure_ascii=False, default=str)}]}


def _gh(*args: str) -> str | None:
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        return out.stdout if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def status(pr=None) -> dict:
    raw = _gh("pr", "view", *([str(pr)] if pr else []), "--json", "number,title,comments,url")
    if raw is None:
        return {"error": "could not read the pull request with the `gh` CLI (install it and run `gh auth login`), "
                         "or pass the analysis URL from the pull request comment to assay_brief"}
    data = json.loads(raw)
    body = next((c.get("body", "") for c in reversed(data.get("comments") or []) if "<!-- assay-quality-gate:" in c.get("body", "")), None)
    if body is None:
        return {"pr": data.get("number"), "verdict": None, "note": "no Assay comment yet: the Assay job has not finished, "
                                                                  "or the repository is not connected (/assay:connect)"}
    lines = [x for x in body.split("<details>")[0].splitlines() if x and not x.startswith("<!--")]
    links = dict(re.findall(r"\[(Evidence|Brief for agents)[^\]]*\]\((https?://[^)\s]+)\)", body))
    state = "ship" if "✅" in lines[0] else "no_ship" if "❌" in lines[0] else "unsure"
    return {"pr": data.get("number"), "verdict": state, "summary": "\n".join(lines[:4]),
            "evidence": links.get("Evidence"), "brief": links.get("Brief for agents"),
            "next": "call assay_brief with the brief URL" if links.get("Brief for agents") else None}


def brief(source=None, case=None, full=False) -> dict:
    from . import brief as B
    from .ci import find_project
    from .code import local_diff
    if source and str(source).startswith(("http://", "https://")):
        return B.fetch(source, case=case, full=bool(full))
    p = Path(source) if source else None
    root = p if p and (p / "analysis.json").exists() else B.latest_run(find_project(source))
    return B.build(root, case=case, full=bool(full), diff_text=local_diff(root))


def run(select=None, full=False, project=None) -> dict:
    import time
    from . import brief as B
    from .ci import find_project
    from .code import local_diff
    proj = find_project(project)
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-mcp"
    cmd = [sys.executable, "-m", "assay", "run", str(proj), "--no-scan", "--run-id", run_id]
    for s in select or []:
        cmd += ["-k", s]
    _log("running " + " ".join(cmd[2:]))
    # the child must not read the protocol's stdin (dev servers such as nodemon read their stdin)
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, stdin=subprocess.DEVNULL)
    root = Path(proj) / "runs" / run_id
    if not (root / "analysis.json").exists():          # never answer with an older run
        return {"error": f"the run did not finish (exit code {out.returncode})", "output": (out.stdout[-3000:] + out.stderr[-2000:])}
    b = B.build(root, full=bool(full), diff_text=local_diff(root))
    b["run"] = {"folder": str(root), "exit_code": out.returncode, "log_tail": out.stdout[-1200:]}
    return b


def artifact(ref: str) -> dict:
    if ref.startswith(("http://", "https://")):
        req = urllib.request.Request(ref, headers={"User-Agent": "assay-mcp"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data, ctype = r.read(), (r.headers.get("Content-Type") or "").split(";")[0]
    else:
        f = Path(ref)
        data = f.read_bytes()
        ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(f.suffix.lower(), "text/plain")
    if ctype.startswith("image/"):
        return {"content": [{"type": "image", "data": base64.b64encode(data).decode(), "mimeType": ctype}]}
    return _text(data.decode(errors="replace")[:60000])


_SESSION: dict = {}


def explore_start(a: dict) -> dict:
    from .ci import find_project
    from .explore import Session
    if _SESSION.get("s"):
        _SESSION.pop("s").finish()                   # one exploration at a time: the previous one is saved first
    s = Session(find_project(a.get("project")), account=a.get("as"), viewport=a.get("viewport"),
                charter=a.get("charter") or "", fresh=a.get("fresh", True) is not False, start_url=a.get("url") or "/",
                actions=a.get("max_actions"), minutes=a.get("minutes"))
    try:
        obs = s.start()
    except BaseException:
        s.finish()
        raise
    _SESSION["s"] = s
    return _observation({"exploration": str(s.dir), "app": s.note_on_app, **obs})


def _current():
    if not _SESSION.get("s"):
        raise RuntimeError("no exploration is open: call assay_explore_start first")
    return _SESSION["s"]


def _observation(obs: dict) -> dict:
    image = obs.pop("_image", None)
    out = _text(obs)
    if image and Path(image).is_file():
        out["content"].append({"type": "image", "data": base64.b64encode(Path(image).read_bytes()).decode(), "mimeType": "image/png"})
    return out


def explore_finish() -> dict:
    s = _current()
    _SESSION.pop("s", None)
    return _text(s.finish())


HANDLERS = {"assay_status": lambda a: _text(status(a.get("pr"))),
            "assay_brief": lambda a: _text(brief(a.get("source"), a.get("case"), a.get("full"))),
            "assay_run": lambda a: _text(run(a.get("select"), a.get("full"), a.get("project"))),
            "assay_artifact": lambda a: artifact(a["ref"]),
            "assay_explore_start": explore_start,
            "assay_explore_act": lambda a: _observation(_current().act(a.get("steps") or [], why=a.get("why") or "",
                                                                       look=bool(a.get("look")), audit=bool(a.get("audit")))),
            "assay_explore_note": lambda a: _text(_current().note(a["title"], a.get("detail") or "", a.get("kind") or "bug",
                                                                  a.get("severity") or "major", a.get("step"))),
            "assay_explore_finish": lambda a: explore_finish()}


def handle(msg) -> dict | None:
    """One JSON-RPC message → the response (None for notifications)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                "error": {"code": -32600, "message": "invalid request"}}
    mid, method, params = msg.get("id"), msg["method"], msg.get("params")
    if "id" not in msg:                  # notifications/initialized, notifications/cancelled, …
        return None
    params = params if isinstance(params, dict) else {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "assay", "version": __version__},
                   "instructions": "Assay checks the running app on every pull request. When its gate fails, call "
                                   "assay_status (or assay_brief with the URL from the pull request comment), fix the "
                                   "product code the brief points to, then assay_run the failing journeys until the "
                                   "verdict is ship. Never weaken a journey's checks to get green. To explore the "
                                   "product freely and find bugs, use assay_explore_start / _act / _note / _finish: "
                                   "after every action they show what changed in the app's saved data."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments")
        if name not in HANDLERS:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name!r}"}}
        try:
            return ok(HANDLERS[name](args if isinstance(args, dict) else {}))
        except Exception as e:           # a tool error is a result the agent can read, not a protocol error
            return ok({"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def _reply(msg):
    try:
        return handle(msg)
    except Exception as e:               # the server must survive any message
        return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}


def _close_exploration(*_) -> None:
    """The client went away or the server is told to stop: keep what the exploration found, stop the app it started."""
    s = _SESSION.pop("s", None)
    if s:
        s.finish()


def serve(stdin=None, stdout=None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    import signal
    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is not None:
            try:
                signal.signal(sig, lambda *a: (_close_exploration(), sys.exit(0)))
            except ValueError:                # not the main thread (tests): the caller manages the session
                pass

    def send(obj) -> None:               # ASCII JSON: safe on any console encoding, one message per line
        stdout.write(json.dumps(obj, ensure_ascii=True, default=str) + "\n")
        stdout.flush()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        if isinstance(msg, list):        # a batch is answered with one array
            out = [r for r in (_reply(m) for m in msg) if r is not None]
            if out:
                send(out)
            elif not msg:
                send({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "empty batch"}})
            continue
        resp = _reply(msg)
        if resp is not None:
            send(resp)
    _close_exploration()
    return 0
