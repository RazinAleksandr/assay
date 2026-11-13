---
name: connect
description: Connect this repository to Assay, the quality gate for the running app. Reads the app, writes its Assay profile and 5-10 user journeys, proves them on the running app, adds the GitHub Actions workflow and opens the pull request; the first CI run creates the Assay account. Use when the user asks to connect, set up, add or install Assay (or an end-to-end quality gate for the running app) in a repository.
---

# Connect a repository to Assay

**Outcome:** one pull request that adds `.assay/` (how to start the app, how to read its state, 5-10 journeys that
pass on the running app) and `.github/workflows/assay.yml`. Its first CI run signs in with GitHub (no tokens),
creates the user's Assay account and project, and comments on the pull request with the gate and a link.
Usually 20-40 minutes, most of it running the app.

Work on your own. Ask the user only for what you cannot find or decide: a secret, which app of a monorepo, whether
a behaviour you found is intended. Post one short line when you start each stage.

**Never change the application's code.** Everything you add lives in `.assay/` and `.github/workflows/assay*.yml`.

## Tools

- `assay`: this plugin puts it on the PATH and runs the bundled scanner through `uv`. If the shell cannot find it,
  call `${CLAUDE_PLUGIN_ROOT}/bin/assay`. Without `uv`, ask the user to install it:
  `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- `references/AUTHORING.md` (next to this file): the format of everything you write. Read it before writing YAML.
- `references/ci.md`: workflow templates for common stacks and for the benchmark.

## 0. Preflight (2 min)

1. `git remote get-url origin` must point to GitHub. Any other host: stop and tell the user that the hosted service
   signs CI in with GitHub; elsewhere it needs a self-hosted `assay server` and its token.
2. `.assay/assay.yaml` already exists: the repo is connected. Switch to the `assay` skill to extend it.
3. `assay --version`, then `assay browsers` (downloads Chromium once, about a minute).
4. `git status`: leave the user's uncommitted changes alone. Create the branch `assay/connect` from the default branch.

## 1. Understand the app (5-10 min)

Read the README, manifests (package.json, pyproject, go.mod, Gemfile, docker-compose), `.env.example`, the router or
pages, the API and the data layer. Find:

- **Install, build, start:** the exact commands a clean CI runner needs, the port, a URL that answers once the app
  is ready. Prefer a production-like start (build, then start or preview) when it is quick.
- **State:** where data lives (SQLite file, Postgres or MySQL, JSON files, localStorage, IndexedDB, an external API)
  and how to start from a clean, seeded, isolated copy: an env var for the data folder or database URL, a seed or
  migrate script, fixtures.
- **Accounts:** seeded users or a sign-up the setup can call; how login works (an API beats the form).
- **External services** (payments, email, OAuth, third-party APIs): find the app's test, mock or dev mode. A journey
  that needs a real secret stays out; tell the user.
- **AI features:** model SDKs (openai, @anthropic-ai/sdk, ai, langchain, llamaindex, google-genai and others), the
  env var with the key and the one that picks the model.
- **What users do:** the 5-10 journeys that matter most. Sign-in, the core object's create, edit and delete, search
  and filters, what another user must not see or change, the paths that lose data or money, the AI feature. Recent
  git history and open issues show what breaks.

Tell the user in at most 5 lines: the app, how it starts, where its state lives, the journeys you will cover.

## 2. Profile (10 min)

1. `assay init` writes `.assay/assay.yaml`, `.assay/cases/smoke.yaml`, `.assay/.gitignore` and
   `.github/workflows/assay.yml`. They are guesses; edit them.
2. In `assay.yaml` (AUTHORING.md §1):
   - Data under `{root}/.state/` through `env:`, so runs never touch the developer's data. `app.reset` rebuilds it
     (copy the seed, run migrations). `app.start` runs from the repo (`cwd: ".."`) on a dedicated port with a strict
     port flag. `app.ready` waits for a URL.
   - A database server: locally use the developer's (Docker is fine) and let `app.reset` create a dedicated
     database; in CI add a `services:` container (references/ci.md).
   - `accounts` and `auth` when the app has login.
   - `probes`: one per entity users change (a SQL query, `http: GET /api/...`, `js` for localStorage). The `state`
     and `allowed_changes` checks read them.
   - `volatile` for timestamps and sessions. `ignore` only for noise you verified is not a bug.
   - `scan.routes`: the main screens, `as: <account>` for screens behind login, `route_patterns` for id routes.
   - `judge: {backend: anthropic, model: claude-sonnet-5-5}`. Without `ANTHROPIC_API_KEY` judge checks stay undecided,
     so keep them in `should` unless the repository will have that secret.
3. `assay doctor .assay` until every line is ✓.

## 3. Journeys (10-20 min)

Write 5-10 journeys in `.assay/cases/<area>.yaml` (AUTHORING.md §2-3). Replace the generic smoke case with a real
one or keep it tagged `smoke`.

- Steps are what a person does, with targets a person perceives (role, label, text); CSS only when nothing else works.
- Every journey that changes data has a `state` check that proves it was saved and `allowed_changes` (nothing else
  changed). A journey that starts from seeded data proves it with `expect_before`.
- `severity` by user impact. Tag 2-3 fast journeys `smoke`.
- `must` holds facts decided by code. `judge` only for meaning code cannot check.

Prove each one: `assay run .assay -k <id> --no-scan`, then read `.assay/runs/<newest>/report.md` and, for a failed
step, its screenshot. Iterate until it passes, or until you are sure the product is wrong:

- Wrong target, timing or start state: fix the journey.
- The app really misbehaves (not saved, the wrong item deleted, an error): keep the journey with the precise check,
  tag it `known-bug` and tell the user with the evidence. Never weaken a check to get green.

Then run everything twice with `assay run .assay` (it also scans the screens). Verdicts must match between the runs;
fix flaky journeys (waits, `clock`, isolated data). Mention the top issues the scan found.

## 4. CI (5 min)

Edit `.github/workflows/assay.yml` (references/ci.md) so a clean `ubuntu-latest` runner installs, builds and starts
the app exactly as your local runs did: language and version (from .nvmrc, engines, .python-version), package manager
(npm, pnpm, yarn, bun, uv, pip), build step, `services:` for databases, env vars (plain values inline, secrets as
`${{ secrets.NAME }}`). Keep the three `permissions`: `id-token: write` is the sign-in to Assay.

AI features: also add `.github/workflows/assay-bench.yml` (references/ci.md). It repeats the AI journeys and compares
models or prompts (arms set env vars the app reads). The model key must be a repository secret: give the user the
exact `gh secret set NAME` command. The same applies to `ANTHROPIC_API_KEY` if judge checks are in `must`.

## 5. Ship (5-15 min, mostly waiting)

1. Stage only `.assay/` and the workflow files (`.assay/.gitignore` keeps `runs/` and `.state/` out). Commit
   "Connect the Assay quality gate", push `assay/connect`.
2. Open the pull request: `gh pr create --fill`. Without `gh`, give the user the compare URL from the push output.
3. Wait for the Assay check (`gh pr checks --watch`), 5-10 minutes. The first pull request has no analysis of the
   default branch to compare with, so problems show as warnings and block nothing.
4. CI inconclusive or failing while local runs pass: that is the CI environment. Read `gh run view --log-failed`
   and the `assay-ci-evidence` artifact, fix the workflow, push again.

## 6. Report (at most 8 lines)

- The pull request link and the analysis link from its Assay comment.
- The journeys covered, by title, and every `known-bug` in one line each.
- Next step: merge. The default branch's first analysis becomes the reference; after that a pull request fails only
  for problems it introduces.
- The project lives at `http://46.225.104.43:8080/gh/<owner>/<repo>`. A private repository's project opens through
  the link in the pull request comment.

## Rules

- Facts by code, meaning by the judge. One condition per judge item.
- Never run against production data or real accounts. Never commit secrets, `.assay/runs/` or `.assay/.state/`.
- Don't edit the app to make a journey pass. A missing `data-testid` is not a reason to change the app.
- Commit `.assay/.assay-cache/` when goal journeys created it: CI replays it without model calls.
