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

### Action inputs

| input | default | |
|---|---|---|
| `project` | `.assay` | folder with `assay.yaml` |
| `browsers` | `chromium webkit` | Playwright browsers to install; `chromium` alone is faster |
| `args` | | extra arguments, e.g. `-k tag:smoke` |
| `anthropic-api-key` | | enables judge checks |
| `command` | `ci` | `bench` runs the benchmark |

## Benchmark workflow (apps with AI features): `.github/workflows/assay-bench.yml`

A model answers differently every time, so one green run proves little. The benchmark repeats every journey and
compares **arms**: the same app started with other settings. The first arm is the baseline. The result goes to the
project's Benchmarks page, the job summary and, on a pull request, one comment.

```yaml
# Assay benchmark: repeats the journeys and compares arms (models, prompts) with intervals.
name: Assay benchmark
on:
  workflow_dispatch:
    inputs:
      attempts:
        description: Attempts per journey and arm
        default: "3"
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
          args: >-
            --attempts ${{ inputs.attempts || '3' }}
            -k 'ai.*'
            --arm current=env:MODEL=gpt-5
            --arm candidate=env:MODEL=gpt-5-mini
```

- Arms: `name=env:VAR=value[,VAR2=value]` starts the app with those env vars. Use the variable the app already reads
  to pick the model or prompt. If there is none, leave out `--arm`: the benchmark then measures how stable the
  current build is (A/A).
- `-k 'ai.*'`: benchmark only the AI journeys (give them ids that start with `ai.`).
- The readiness check should prove the model answers (for example a health URL that calls the model). A missing key
  then makes the run inconclusive instead of failing journeys.
