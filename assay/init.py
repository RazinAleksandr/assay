"""`assay init` — install Assay into an app repository: the SonarQube-style onboarding in one command.

Writes .assay/assay.yaml (how to start the app, what the gate is), .assay/cases/smoke.yaml (one journey that works on
any web app), .assay/.gitignore, and the CI step (GitHub Actions or GitLab CI). Nothing else in the repo changes.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PROFILE = """# Assay profile: how to start this app and what the quality gate is.
# Format: https://github.com/RazinAleksandr/assay/blob/v0/docs/AUTHORING.md
name: {name}
title: "{title}"
base_url: {url}

app:
  start: {{cmd: "{start}", cwd: ".."}}
  ready: {{url: "{url}/", timeout_s: 90}}

viewports:
  desktop: {{width: 1366, height: 900}}
  mobile: {{device: "iPhone 13"}}

# zero-config pass on every CI run: these screens on desktop and phone through every sensor
scan: {{routes: ["/"], viewports: [desktop, mobile], max_pages: 20}}

# without an Anthropic key (ANTHROPIC_API_KEY) judge checks are undecided, never guessed
judge: {{backend: anthropic, model: claude-sonnet-5-5}}

gate:
  fail_on_case_fail: true            # on pull requests: no journey may start failing
  max_new: {{blocker: 0, critical: 0}} # no new blocker/critical issues
  max_inconclusive: 0.2
"""

SMOKE = """# The first journey: open the app the way a visitor does. Add your own next to it (see AUTHORING.md).
cases:
  - id: smoke.home
    title: The home screen opens without errors
    promise: The app opens                      # what users are promised; the verdict shows it instead of the id
    critical: true                              # runs on every pull request
    severity: blocker
    tags: [smoke]
    steps:
      - goto: /
      - wait: 1
    must:
      - no_errors: true
      - no_failed_requests: true
"""

GITHUB = """# Assay quality gate: runs the app's journeys, compares with {branch}, posts the result and a link to the Assay service.
# No token: GitHub signs the job in (id-token: write) and the first analysis creates the account and the project.
name: Assay
on:
  push:
    branches: [{branch}]
  pull_request:
jobs:
  assay:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write   # the gate comment
      id-token: write        # sign-in: no token needed
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: {{node-version: 22, cache: npm}}
      - run: npm ci
{build}      - uses: RazinAleksandr/assay@v0
"""

GITLAB = """# Assay quality gate: runs the app's journeys, compares with the target branch, posts the result and a link.
include:
  - remote: https://raw.githubusercontent.com/RazinAleksandr/assay/v0/ci/assay.gitlab-ci.yml
variables:
  ASSAY_HOST_URL: https://assay.example.com   # your server; ASSAY_TOKEN (its admin token) as a masked CI/CD variable
"""


def _guess(repo: Path) -> dict:
    pkg = {}
    if (repo / "package.json").exists():
        pkg = json.loads((repo / "package.json").read_text())
    scripts = pkg.get("scripts", {})
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    port, start, build = 3000, "npm start", ""
    if "vite" in deps:
        if "preview" in scripts and "build" in scripts:
            port, start, build = 4173, "npm run preview -- --host 127.0.0.1 --port 4173 --strictPort", "npm run build"
        else:
            port, start = 5173, "npm run dev -- --host 127.0.0.1 --port 5173 --strictPort"
    elif "next" in deps:
        port, start, build = 3000, "npm run start -- -p 3000", "npm run build"
    elif "dev" in scripts:
        start = "npm run dev"
    return {"title": pkg.get("name") or repo.name, "port": port, "start": start, "build": build}


def init(args, log=print) -> int:
    repo = Path(args.path).resolve()
    g = _guess(repo)
    url = (args.url or f"http://127.0.0.1:{g['port']}").rstrip("/")
    name = re.sub(r"[^\w.-]+", "-", repo.name.lower())
    files = {
        repo / ".assay/assay.yaml": PROFILE.format(name=name, title=g["title"], url=url, start=g["start"]),
        repo / ".assay/cases/smoke.yaml": SMOKE,
        repo / ".assay/.gitignore": "runs/\n.state/\n",
    }
    if args.ci == "github":
        build = f"      - run: {g['build']}\n" if g["build"] else ""
        files[repo / ".github/workflows/assay.yml"] = GITHUB.format(branch="main", build=build)
    else:
        files[repo / ".gitlab-ci.assay.yml"] = GITLAB
    for f, text in files.items():
        if f.exists() and not args.force:
            log(f"  = {f.relative_to(repo)} exists, kept (use --force to overwrite)")
            continue
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        log(f"  + {f.relative_to(repo)}")
    log("")
    log("Next: check the start command in .assay/assay.yaml, run `assay doctor .assay`, then commit and push.")
    log("GitHub Actions needs nothing else: the first analysis creates your account on the Assay service.")
    log("GitLab and other CI: set ASSAY_HOST_URL and ASSAY_TOKEN (your server's admin token).")
    return 0
