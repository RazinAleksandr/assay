# CI templates

The Assay step is always the same: `uses: RazinAleksandr/assay@v0` after the steps that install (and build) the app.
It installs the scanner and browsers, starts the app with `app.start` from `.assay/assay.yaml`, runs the journeys and
the screen scan, uploads the analysis, comments on the pull request and sets the check status.

## Test workflow: `.github/workflows/assay.yml`

```yaml
# Assay quality gate: every push to main and every pull request runs the app's journeys and screen scan,
# compares the result with main, comments on the pull request and links to the analysis.
# No token: GitHub signs the job in (id-token: write); the first analysis creates the account and the project.
name: Assay
on:
  push:
    branches: [main]
  pull_request:
jobs:
  assay:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    permissions:
      contents: read
      pull-requests: write   # the gate comment on the pull request
      id-token: write        # GitHub signs the job in to Assay
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: {node-version: 22, cache: npm}
      - run: npm ci
      - run: npm run build          # only when app.start serves a build
      - uses: RazinAleksandr/assay@v0
        # with:
        #   anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}   # judge checks; without it they stay undecided
```

Use the real default branch name in `push.branches`.

### Install steps for other stacks

```yaml
# pnpm
      - uses: pnpm/action-setup@v4
      - uses: actions/setup-node@v4
        with: {node-version: 22, cache: pnpm}
      - run: pnpm install --frozen-lockfile
# yarn
      - uses: actions/setup-node@v4
        with: {node-version: 22, cache: yarn}
      - run: yarn install --frozen-lockfile
# bun
      - uses: oven-sh/setup-bun@v2
      - run: bun install --frozen-lockfile
# Python with uv
      - uses: astral-sh/setup-uv@v6
      - run: uv sync --frozen
# Python with pip
      - uses: actions/setup-python@v5
        with: {python-version: "3.12", cache: pip}
      - run: pip install -r requirements.txt
```

Take the version from `.nvmrc`, `engines`, `.python-version` or `.tool-versions` when the repo has one.

### A database server

```yaml
jobs:
  assay:
    services:
      postgres:
        image: postgres:16
        env: {POSTGRES_PASSWORD: postgres, POSTGRES_DB: app_test}
        ports: ["5432:5432"]
        options: --health-cmd pg_isready --health-interval 5s --health-timeout 5s --health-retries 10
    env:
      DATABASE_URL: postgres://postgres:postgres@localhost:5432/app_test
```

`app.reset` in `assay.yaml` then runs the app's own migrate and seed commands against `DATABASE_URL`, so the same
profile works locally and in CI.

### Env vars and secrets

Plain values go into `env:` of the job. Secrets go in as `${{ secrets.NAME }}`; tell the user to add them with
`gh secret set NAME`. Never write a secret value into a file.

### The app in a subfolder (monorepo)

Keep `.assay/` next to the app and point the step at it:

```yaml
      - run: npm ci
        working-directory: apps/web
      - uses: RazinAleksandr/assay@v0
        with: {project: apps/web/.assay}
```

### What the step does on a pull request

It reads the change (`git diff` against the target's commit; it fetches that commit itself, `fetch-depth` can stay 1),
runs the journeys the change can break (see "Which journeys a pull request runs" in AUTHORING.md), finds the suspect
code of every failure and comments with the verdict and a brief link. A change that weakens checks waits for a person:
they add the label `assay-approved` to the pull request and re-run the Assay job (the step reads the current labels).

### Action inputs

| input | default | |
|---|---|---|
| `project` | `.assay` | folder with `assay.yaml` |
| `browsers` | `chromium webkit` | Playwright browsers to install; `chromium` alone is faster |
| `args` | | extra arguments, e.g. `-k tag:smoke` |
| `anthropic-api-key` | | enables judge checks |
| `command` | `ci` | `bench` runs the benchmark |

## Deployed app

When the app cannot start in a CI job (a GPU, devices, a large backend, paid services), Assay tests it where it is
hosted. The profile has no `app.start`, takes `base_url: "{env.ASSAY_BASE_URL}"` and its test accounts from secrets
(AUTHORING.md, "Testing a deployed app"). In (a) and (b) the job installs nothing of the app: checkout, the address,
the Assay step. `postgres` probes need `psql`: GitHub's Ubuntu runners have it, elsewhere `apt-get install
postgresql-client`.

### (a) A stable staging URL

Staging runs what was deployed, not the pull request's code, so gate it after each deploy, not on pull requests.
Add the job to the workflow that deploys staging:

```yaml
  assay:
    needs: deploy-staging            # the job that deploys main to staging
    runs-on: ubuntu-latest
    timeout-minutes: 30
    concurrency: {group: assay-staging, cancel-in-progress: false}   # one run at a time on the shared stand
    permissions: {contents: read, id-token: write}
    env:
      ASSAY_BASE_URL: ${{ vars.STAGING_URL }}               # repository variable (or a secret)
      QA_USER: ${{ vars.QA_USER }}
      QA_PASSWORD: ${{ secrets.QA_PASSWORD }}
      DATABASE_URL: ${{ secrets.STAGING_DATABASE_URL_RO }}  # a read-only role, for postgres probes
    steps:
      - uses: actions/checkout@v4
      - uses: RazinAleksandr/assay@v0
```

### (b) A preview per pull request (Vercel, Netlify)

The preview runs the pull request's code, so this one gates pull requests. A pull request is compared with the last
analysis of its target branch and blocks nothing until there is one, so run (a) for `main` as well (a staging stand
built from `main`, never production). Wait for the preview of the head commit and export its URL before the Assay
step; hosts such as Vercel report each preview as a GitHub deployment of the commit:

```yaml
on: {pull_request: {}}
jobs:
  assay:
    runs-on: ubuntu-latest
    timeout-minutes: 40
    permissions: {contents: read, deployments: read, pull-requests: write, id-token: write}
    env:
      QA_USER: ${{ vars.QA_USER }}
      QA_PASSWORD: ${{ secrets.QA_PASSWORD }}
    steps:
      - uses: actions/checkout@v4
      - name: Wait for the preview deployment
        env:
          GH_TOKEN: ${{ github.token }}
          SHA: ${{ github.event.pull_request.head.sha }}
        run: |
          for i in $(seq 60); do
            for id in $(gh api "repos/$GITHUB_REPOSITORY/deployments?sha=$SHA" --jq '.[].id'); do
              url=$(gh api "repos/$GITHUB_REPOSITORY/deployments/$id/statuses" \
                --jq '.[0] | select(.state == "success") | .environment_url // empty')
              if [ -n "$url" ]; then echo "ASSAY_BASE_URL=$url" >> "$GITHUB_ENV"; exit 0; fi
            done
            sleep 10
          done
          echo "::error::no successful preview deployment of $SHA after 10 minutes"; exit 1
      - uses: RazinAleksandr/assay@v0
```

When the repository deploys several apps, add `&environment=<name>` (Vercel: `Preview`) to the deployments query.
One option for Vercel instead of the loop is a third-party action:

```yaml
      - uses: patrickedqvist/wait-for-vercel-preview@v1.3.1
        id: preview
        with: {token: "${{ secrets.GITHUB_TOKEN }}", max_timeout: 600}
      - run: echo "ASSAY_BASE_URL=${{ steps.preview.outputs.url }}" >> "$GITHUB_ENV"
```

A protected preview needs its bypass header: in `assay.yaml`,
`browser: {headers: {x-vercel-protection-bypass: "{env.VERCEL_AUTOMATION_BYPASS_SECRET}", x-vercel-set-bypass-cookie: "true"}}`,
and the secret in the job's `env:` (Vercel: Project settings → Deployment Protection → Protection Bypass for Automation).

### (c) A self-hosted runner on the machine with the hardware

Register the machine as a runner with a label (Settings → Actions → Runners), then pick it. The job can build and
start the pull request's code there with `app.start` as usual, or test what already runs on it (no `app.start`,
`base_url: http://127.0.0.1:<port>`):

```yaml
jobs:
  assay:
    runs-on: [self-hosted, gpu]
    timeout-minutes: 60
    permissions: {contents: read, pull-requests: write, id-token: write}
    steps:
      - uses: actions/checkout@v4
      - uses: RazinAleksandr/assay@v0
        with: {browsers: chromium}
```

The action installs the browsers with their system libraries (`apt-get` through `sudo`), so on Linux the runner's
user needs passwordless sudo. A pull request's code runs on that machine: keep self-hosted runners to private
repositories, or require approval for pull requests from forks.

## Benchmark workflow (apps with AI features): `.github/workflows/assay-bench.yml`

A model answers differently every time, so one green run proves little. The benchmark repeats every journey and
compares **arms**: the same app started with other settings. The first arm is the baseline. The result goes to the
project's Benchmarks page, the job summary and, on a pull request, one comment.

What to compare goes into `.assay/assay.yaml`, so the step needs no arguments:

```yaml
bench:
  attempts: 3
  arms:                          # the first is the baseline
    current: env:MODEL=gpt-5
    candidate: env:MODEL=gpt-5-mini
```

```yaml
# Assay benchmark: repeats the journeys and compares arms (models, prompts) with intervals.
# What it compares is `bench:` in .assay/assay.yaml; this workflow says when.
name: Assay benchmark
on:
  workflow_dispatch:
  schedule:
    - cron: "0 6 * * 1"      # every Monday
jobs:
  bench:
    runs-on: ubuntu-latest
    timeout-minutes: 90
    permissions:
      contents: read
      pull-requests: write
      id-token: write
    env:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}       # the key the app itself uses
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: {node-version: 22, cache: npm}
      - run: npm ci
      - uses: RazinAleksandr/assay@v0
        with:
          command: bench
          browsers: chromium
```

To override the profile in one workflow (a manual run with more attempts, another candidate), pass the command-line
flags in `args`. `--arm` replaces all the profile's arms; `--attempts` and `-k` replace only their own setting:

```yaml
on:
  workflow_dispatch:
    inputs:
      attempts: {description: Attempts per journey and arm, default: "5"}
# …
      - uses: RazinAleksandr/assay@v0
        with:
          command: bench
          browsers: chromium
          args: >-
            --attempts ${{ inputs.attempts || '3' }}
            --arm current=env:MODEL=gpt-5
            --arm candidate=env:MODEL=gpt-5.1
```

- Arms: `name: env:VAR=value[,VAR2=value]` starts the app with those env vars. Use the variable the app already reads
  to pick the model or prompt. If there is none, leave out `arms`: the benchmark then measures how stable the current
  build is (A/A).
- Journeys: the ones marked `ai: true` (else all). `select: [tag:assistant]` in `bench:` picks others. To try one AI
  journey on many inputs, give it a dataset (`each:`, see "Datasets" in AUTHORING.md).
- The readiness check should prove the model answers (for example a health URL that calls the model). A missing key
  then makes the run inconclusive instead of failing journeys.
