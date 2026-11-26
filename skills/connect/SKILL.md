---
name: connect
description: Set up Assay for this product in one prompt. Studies the product and writes what it learned to .assay/product.md, then the Assay profile, 5-10 user journeys proven on the running app, a benchmark for its AI features if it has any, the CI workflows, and the pull request; the first CI run creates the account on the hosted Assay service. Use when the user asks to connect, set up, add or install Assay, or end-to-end testing, a quality gate for pull requests or a benchmark of LLM features for their product.
---

# Set up Assay for this product

**Outcome:** one pull request that adds `.assay/` (what Assay knows about the product, how to run it and read its
data, 5-10 journeys that pass on the running app, a benchmark if the product uses an LLM) and the workflows in
`.github/workflows/`. Its first CI run signs in with GitHub (no tokens), creates the user's project on the Assay service
and comments on the pull request with the verdict and a link. Usually 30-45 minutes, most of it running the app.

Work on your own. Ask the user only for what you cannot find or decide: a secret, the URL of a deployed environment,
which app of a monorepo, whether a behaviour you found is intended. Post one short line when you start each stage.

**Never change the application's code.** Everything you add lives in `.assay/` and `.github/workflows/assay*.yml`.

## Tools

- `assay`: this plugin puts it on the PATH and runs it through `uv`. If the shell cannot find it, call
  `${CLAUDE_PLUGIN_ROOT}/bin/assay`. Without `uv`, ask the user to install it:
  `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- `../../docs/AUTHORING.md` (the plugin's `docs/AUTHORING.md`, two folders up from this file): the format of
  everything you write. Read it before writing YAML.
- `references/ci.md`: workflow templates for common stacks, deployed environments and the benchmark.
- `references/product.md`: the template of `.assay/product.md`.

## 0. Preflight (2 min)

1. `.assay/assay.yaml` already exists: the product is connected. Switch to the `assay` skill.
2. `git remote get-url origin` on GitHub: the hosted service works with no setup. Another host (GitLab, none): say
   that local testing, exploration and benchmarks work as is, and the pull request gate needs a self-hosted
   `assay server` (docs/SELF_HOSTING.md); carry on without stage 7's pull request unless the user has one.
3. `assay --version`, then `assay browsers` (downloads Chromium once, about a minute).
4. `git status`: leave the user's uncommitted changes alone. Create the branch `assay/connect` from the default branch.

## 1. Study the product (10 min) → `.assay/product.md`

This is what makes Assay fit any product: every other skill reads this file. Read the README, manifests
(package.json, pyproject, go.mod, Gemfile, docker-compose), `.env.example`, the router or pages, the API, the data
layer, recent git history and open issues. Fill in `references/product.md`:

- **What it is and who uses it**, in two sentences; the roles (admin, member, visitor, another tenant).
- **How it runs for testing.** The exact install, build and start commands a clean CI runner needs, the port, a URL
  that answers once it is ready. Where the data lives (SQLite file, Postgres, JSON files, localStorage, IndexedDB, an
  external API) and how to start from a clean, seeded, isolated copy (an env var for the data folder or database URL,
  a seed or migrate script). External services (payments, email, OAuth, third-party APIs): their test or mock mode.
- **Can it run on a CI runner at all?** A GPU, devices, a large backend, a licence, data that cannot leave a network
  mean no. Then it is tested where it is already deployed: ask the user for the URL of a staging or preview
  environment, test accounts, and read access to its data (an admin API, a read-only database role); see
  AUTHORING.md, "Testing a deployed app". A self-hosted runner on the machine with the hardware is the other way.
- **Surfaces:** the main screens and routes, which role reaches them.
- **Data:** the entities users create and change, and where each one can be read (these become probes).
- **AI features:** model SDKs (openai, @anthropic-ai/sdk, ai, langchain, llamaindex, google-genai and others), what
  each feature does for the user, the env var with the key and the one that picks the model or prompt.
- **Risks:** what would hurt users most (losing data or money, another user seeing private data, the core action
  silently not saved, the AI feature doing the wrong thing). These choose the journeys and the exploration charters.
- **Not covered**, and why (no test mode for payments, needs a real phone…).

Tell the user in at most 5 lines: the product, how it will run for testing, where its data lives, the AI features,
the journeys you will cover.

## 2. Profile (10 min)

1. `assay init` writes `.assay/assay.yaml`, `.assay/cases/smoke.yaml`, `.assay/.gitignore` and
   `.github/workflows/assay.yml`. They are guesses; edit them.
2. In `assay.yaml` (AUTHORING.md §1):
   - **Runs locally:** data under `{root}/.state/` through `env:`, so runs never touch the developer's data;
     `app.reset` rebuilds it (copy the seed, run migrations); `app.start` runs from the repo (`cwd: ".."`) on a
     dedicated port with a strict port flag; `app.ready` waits for a URL that proves the app (and its model, for an AI
     product) answers. A database server: locally the developer's (Docker is fine) with a dedicated database that
     `app.reset` creates; in CI a `services:` container (references/ci.md).
   - **Deployed:** no `app.start`; `base_url: "{env.ASSAY_BASE_URL}"`, accounts from `{env.…}` secrets, probes that
     read through an API or a read-only role, every record a journey creates marked with `{vars.uid}`.
   - `accounts` and `auth` when the product has login.
   - `probes`: one per entity users change (a SQL query, `http: GET /api/...`, `postgres`, `js` for localStorage).
   - `volatile` for timestamps and sessions. `ignore` only for noise you verified is not a bug.
   - `scan.routes`: the main screens, `as: <account>` for screens behind login, `route_patterns` for id routes.
   - `judge: {backend: claude-cli, model: sonnet}`. In CI it switches to the API when `ANTHROPIC_API_KEY` is set;
     without it judge checks stay undecided, so keep them in `should` unless the repository will have that secret.
3. `assay doctor .assay` until every line is ✓.
4. **First findings, before any journey:** `assay run .assay --scan-only` crawls the screens in `scan.routes` with every
   sensor on. Tell the user the top 3-5 problems it found (console errors, failed requests, accessibility, broken
   images), in one line each, while you carry on.

## 3. Journeys (10-20 min)

Write 5-10 journeys in `.assay/cases/<area>.yaml` (AUTHORING.md §2-3), chosen from the risks in product.md. Replace
the generic smoke case with a real one or keep it tagged `smoke`.

- `promise:` one sentence of what the product promises the user ("A comment is saved in full"). People read it in
  the verdict instead of the id, so write it in the product's words.
- `critical: true` on the 1-3 journeys that must never break (sign-in, paying, losing data): they run on every pull
  request even when Assay picks only the journeys a change can affect.
- Steps are what a person does, with targets a person perceives (role, label, text); CSS only when nothing else works.
- Every journey that changes data has a `state` check that proves it was saved and `allowed_changes` (nothing else
  changed). A journey that starts from seeded data proves it with `expect_before`.
- `severity` by user impact. Tag 2-3 fast journeys `smoke`. `must` holds facts decided by code; `judge` only for
  meaning code cannot check.

Prove each one: `assay run .assay -k <id> --no-scan`, then `assay brief` (the newest run as JSON: what failed,
expected against what the app saved, the step) and, for a failed step, its screenshot. Iterate until it passes, or
until you are sure the product is wrong:

- Wrong target, timing or start state: fix the journey.
- The product really misbehaves (not saved, the wrong item deleted, an error): keep the journey with the precise
  check, tag it `known-bug` (an expected failure: listed on every analysis, never blocks) and tell the user with the
  evidence. Never weaken a check to get green.

Then run everything twice with `assay run .assay --scan` (as CI does). Verdicts must match between the runs; fix
flaky journeys (waits, `clock`, isolated data). Mention the top issues the scan found.

**Prove the gate bites (5 min).** A journey that cannot fail protects nothing. Take the 1-3 most important journeys
that change data and break the product on purpose, silently, in a throwaway copy:

1. `git worktree add ../assay-bite HEAD`, copy `.assay/` into it (`cp -R .assay ../assay-bite/`).
2. In `../assay-bite`, plant one quiet data bug per journey where its data is written: cut a saved text short
   (`body.slice(0, 20)`), drop a field, write to the wrong record, insert twice. The screen should still say it worked.
3. `assay run ../assay-bite/.assay -k <id> --no-scan`: it must FAIL on its `state` or `allowed_changes` check, and
   `assay brief ../assay-bite/.assay` must list the planted line among the suspects. A journey that still passes has a
   check too weak to catch it: tighten the check, not the bug.
4. `git worktree remove --force ../assay-bite`. Nothing of it is committed; the application's code stays untouched.

Tell the user in one line: "Assay caught N of N planted silent data bugs and pointed at the line; a test that only
looks at the screen would have passed."

## 4. AI features → a benchmark (10 min, only if the product uses a model)

Follow the `bench` skill's stages 2-4 in short: 1-3 journeys per AI feature marked `ai: true`, each checking the
outcome through the product (the record the feature created, the answer's facts, one judge criterion per item), a
dataset (`each:`) of 8-15 realistic inputs including hard ones, and `bench:` in `assay.yaml` with the arms the
product can switch (the model env var: the current model against a cheaper or newer one). Prove it with
`assay bench .assay --attempts 1` and tell the user the per-arm pass rates. Model calls cost the user's key: say how
many a full run makes (journeys × rows × arms × attempts).

## 5. CI (5 min)

Edit `.github/workflows/assay.yml` (references/ci.md) so a clean `ubuntu-latest` runner installs, builds and starts
the app exactly as your local runs did: language and version (from .nvmrc, engines, .python-version), package manager
(npm, pnpm, yarn, bun, uv, pip), build step, `services:` for databases, env vars (plain values inline, secrets as
`${{ secrets.NAME }}`). A deployed product: the "Deployed app" section instead. Keep the three `permissions`:
`id-token: write` is the sign-in to Assay.

AI features: also add `.github/workflows/assay-bench.yml` (references/ci.md), weekly and on demand. The model key must
be a repository secret: give the user the exact `gh secret set NAME` command. The same applies to `ANTHROPIC_API_KEY`
if judge checks are in `must`.

## 6. Ship (5-15 min, mostly waiting)

1. Stage only `.assay/` and the workflow files (`.assay/.gitignore` keeps `runs/` and `.state/` out). Commit
   "Connect the Assay quality gate", push `assay/connect`.
2. Open the pull request: `gh pr create --fill`. Without `gh`, give the user the compare URL from the push output.
3. Wait for the Assay check (`gh pr checks --watch`), 5-10 minutes. Its comment starts with the verdict
   (✅ Ship it, ❌ Don't ship, ⚠️ Not sure) and one sentence why. The first pull request has no analysis of the
   default branch to compare with, so problems show as warnings and block nothing.
4. CI inconclusive or failing while local runs pass: that is the CI environment. Read `gh run view --log-failed`
   and the `assay-ci-evidence` artifact, fix the workflow, push again.

## 7. Report (at most 12 lines)

- The pull request link and the analysis link from its Assay comment. The project lives at
  `http://46.225.104.43:8080/gh/<owner>/<repo>` (a private repository's opens through the link in the comment).
- The journeys covered, by title, and every `known-bug` in one line each; the benchmark's arms and first pass rates.
- The planted bugs the journeys caught (stage 3), in one line.
- What the user can do from now on, in their own words:
  - **Test the product now:** "run the Assay journeys" (the `assay` skill).
  - **Every pull request:** the gate runs by itself; "fix the Assay gate" turns a ❌ into a fix (`/assay:fix`).
  - **Benchmark the AI:** "is the cheaper model good enough?" (`/assay:bench`).
  - **Let an agent loose:** "explore the checkout and find bugs" (`/assay:explore`): an agentic freedom test that
    watches the data, confirms what it finds and keeps it as journeys.

## Rules

- Facts by code, meaning by the judge. One condition per judge item.
- Never run against production data or real accounts. Never commit secrets, `.assay/runs/` or `.assay/.state/`.
- Don't edit the app to make a journey pass. A missing `data-testid` is not a reason to change the app.
- Commit `.assay/.assay-cache/` when goal journeys created it: CI replays it without model calls.
