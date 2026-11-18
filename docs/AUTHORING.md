# Writing an Assay project

A project is the `.assay/` folder in the app's repository: `assay.yaml` (the app profile) and `cases/*.yaml`
(journeys). The engine is generic; everything product-specific lives here, versioned with the app's code.
`assay init` writes a starting point.

```
<app repo>/
  .assay/
    assay.yaml        # how to start the app, log in, read its state, what to ignore, gate
    cases/*.yaml      # journeys: start state + steps (or goal) + must/should checks
    fixtures/         # files to upload, saved games, seed data
    .assay-cache/     # goal-agent trajectories (commit them: CI replays them with zero model calls)
    runs/<id>/        # output (git-ignored): evidence, screenshots, analysis.json, report.html|md, junit.xml
  .github/workflows/assay.yml   # or the GitLab include: the CI step, `assay ci`
```

In CI: `assay ci` (see the product README). Locally, from the app repo:
`assay run .assay [--scan] [--confirm] [-k 'glob*' | -k tag:x | -k group:feed | -k is:critical] [--baseline .assay/runs/<id>]` ·
re-grade without re-running: `assay grade .assay .assay/runs/<id>` · preflight: `assay doctor .assay` ·
what to fix, as JSON for a coding agent: `assay brief [.assay/runs/<id> | <analysis URL>]`.

## 1. Profile (`assay.yaml`)

```yaml
name: drawing
title: Folio — drawing studio
base_url: http://127.0.0.1:5274
env: {DATA_DIR: "{root}/.state/data"}           # {root} = the .assay folder; {env.X} expands everywhere
app:
  reset: {cmd: "rm -rf {env.DATA_DIR} && mkdir -p {env.DATA_DIR}", cwd: ".."}  # before start; cwd is relative to .assay/
  start: {cmd: "npx vite --host 127.0.0.1 --port 5274 --strictPort", cwd: ".."}
  ready: {url: http://127.0.0.1:5274/, timeout_s: 60}
accounts: {mila: {username: mila.chen, password: "..."}}
auth:                                            # how `as: <account>` logs in (http and/or steps)
  http: {method: POST, path: /api/auth/login, headers: {Origin: "{origin}"},
         json: {identity: "{account.username}", password: "{account.password}"}, save: {csrf: csrf}}
http_headers: {Origin: "{origin}", X-CSRF-Token: "{vars.csrf}"}   # added to every `http` step
probes:                                          # the app's state as JSON — read before and after each case
  posts: {sqlite: "{env.DATA_DIR}/app.sqlite", sql: "select id, caption from posts order by id"}
  likes: {postgres: "{env.DATABASE_URL}", sql: "select post_id, count(*) n from likes group by post_id"}   # via psql
  me:    {http: "GET /api/auth/session", select: user, when_as: "*"}   # read only in signed-in journeys
                                                 # (when_as: mila | [mila, noah] | "*"); out of scope = not part of
                                                 # that journey's state. A probe in scope that fails makes `state`,
                                                 # `changed` and `allowed_changes` undecided: no data, no pass
  docs:  {js: "JSON.parse(localStorage.getItem('docs') || '[]')"}   # runs in the page
  saves: {files: "{env.DATA_DIR}/media"}
  build: {cmd: "xcrun simctl listapps booted | plutil -convert json -o - -"}
volatile: ["*.updated_at", "sessions"]           # never counted as a change
ignore: {console: ["Download the React DevTools"], network: ["/favicon.ico"], rules: ["PERF-004"]}   # rules: drop issues by rule id/glob
browser: {timeout_ms: 10000, settle: idle | fixed, settle_ms: 500, screenshots: steps | end}
viewports: {desktop: {width: 1366, height: 860}, mobile: {device: "iPhone 13"}, ipad: {device: "iPad Pro 11"}}
scan: {routes: ["/", "/explore"], as: mila, viewports: [desktop, mobile], max_pages: 20}
#   route_patterns: [/u/:username, /p/:id]       # the app's route templates: one screen = one place for its issues,
#                                                # so an issue keeps its fingerprint and "new" on a PR really means new
judge: {backend: claude-cli, model: sonnet, must_votes: 3, should_votes: 1}   # or anthropic / none
gate:
  fail_on_case_fail: true
  max_new: {blocker: 0, critical: 0}  # new = not in the reference analysis; with none yet (a branch's first analysis,
                                      # a local run) what is there is the baseline: listed, never blocking
  min_rating: {reliability: B}        # a dimension nothing measured has no rating: the gate is then INCONCLUSIVE
  max_inconclusive: 0.2
  min_cases: 1                        # fewer journeys executed → INCONCLUSIVE, never PASSED
  block_on_weakened: true             # a pull request that weakens checks waits for the label `assay-approved`
  confirm: true                       # `assay ci` runs a failure that blocks once more first; passing then = flaky, not blocking
  confirm_max: 10                     # more failing journeys than this: no second run (they would block anyway)
select:                               # pull requests only; the default branch always runs every journey
  mode: auto                          # auto (default) | all
  sample: 0.1                         # safety sample of the journeys the change does not touch
  budget_min: 10                      # hard cap on the estimated minutes (optional; see below what goes first)
  global: ["src/theme/*"]             # more files that affect every journey (lock files, build config, auth,
                                      # middleware, global styles and assay.yaml already do)
groups:                               # optional: who owns code no journey is mapped to (globs, like `global`)
  feed: {paths: ["server/feed.ts", "web/src/Feed*"]}
```

### Which journeys a pull request runs

On a pull request Assay runs the journeys its change can break, not all of them: the `critical: true` ones, the
journeys the change adds or edits, those that failed on the target branch, and those linked to the changed files by
the **journey map** of the target branch's last full analysis: frontend files that ran in a journey (Chromium
coverage, mapped through source maps), API routes whose handlers the change touches, tables whose data a journey
changed, texts its steps aim at. A changed source file none of that links to a journey goes to the journeys of the
**group** whose `paths` match it, else to the journeys that reach it through code that **imports** it, two levels up
(JS/TS relative imports, `require`, `import()`; Python imports): those that ran an importer, and those that call a
route an importer declares — "uses `server/serialize.ts` through `server/posts.ts` (`GET /posts/:id`)". Only when that
finds nothing too, or the change touches something everything depends on, does every journey run. A rotating 10 %
sample of the rest catches gaps in the map. The comment says "Checked 12 of 140 journeys (touched by this change)" and
the brief says why each one ran.

A journey's **group** is its `group:` field, else its file inside `cases/` without the extension (`cases/feed.yaml` →
`feed`, `cases/social/feed.yaml` → `social/feed`); `-k group:feed` runs one group locally.

`select.budget_min` is a hard cap on the estimated time (the target branch's durations; 5 s for a journey it has not
run), also when every journey would run. Over it, Assay drops the safety sample first, then the rest of "every journey"
(a different shuffle on every pull request), journeys not mapped yet, those linked by group or import, and last those
the change touches directly. `critical: true`, new or edited and failing journeys always run, and the comment says
what was dropped: "Checked 40 of 120 journeys (…, 80 over the 10-min budget)".

### What a pull request is told

The comment answers "ship it or not, and why" in four lines: ✅ Ship it / ❌ Don't ship / ⚠️ Not sure (nothing could
be decided: never the product's fault), one sentence built from the failed check's data ("`comments.body` was saved
cut short in the database: 20 of 90 characters"), what the person must do (nothing, decide, look), and how much was
checked. Its brief link gives a coding agent the failures, expected against saved values, the **suspect code** of the
change (hunks ranked by the data: writes the wrong field, sits in the handler of a route the journey called, touches
the table…) and how to reproduce: `assay brief <url>`, or the `assay` MCP server and `/assay:fix` in Claude Code.
A change that weakens the gate (removes a journey or a `must` check, moves one to `should`, adds a `known-bug` tag,
allows more errors or data changes, relaxes a limit, adds `ignore`/`volatile` entries, loosens `gate`) is listed and
blocks until someone adds the label `assay-approved` to the pull request and re-runs the job.

**A failure must reproduce.** Before a journey's failure blocks, `assay ci` runs that journey once more from a fresh
app (reset and started as for the first run). Fails again: the comment says "Reproduced on a second run". Passes: it is
flaky, listed as "failed once, passed on a second run" and blocks nothing, so the gate passes if nothing else blocks.
A second run that could not check the journey leaves the failure standing. Its evidence is in `<run>/confirm/` and what
it found in `<run>/confirm.json`, so `assay grade` reaches the same gate. More than `gate.confirm_max` (10) failing
journeys get no second run: they block. Locally `assay run --confirm` does the same; `gate.confirm: false` turns it off.

### How a run goes (what is shared between journeys)

1. **Start.** If something already answers at `app.ready`, Assay uses it as is: no reset, no start. That is why the
   profile starts the app on a dedicated port, never on the one a developer's own server uses. Otherwise
   `app.reset` runs once, then `app.start`, then Assay waits for `app.ready`. The app is stopped at the end.
2. **Scan** (with `--scan`, and in CI): every route in `scan.routes` on every scan viewport.
3. **Journeys** run one after another in the order of the files (sorted by name) and of the cases inside them,
   **against the same running app and the same data**. Each journey gets a fresh browser context (no cookies,
   no storage). `as: <account>` logs in at the start of the journey; the session is reused by later journeys of the
   same account and viewport (`auth: {cache: false}` turns that off).
4. **Grading** reads only the recorded evidence, so `assay grade` re-grades without the app.

So a journey must not rely on another journey's changes, and must leave nothing that breaks the next one:
check changes with `delta` (not absolute counts), prove the seed with `expect_before`, and when a journey deletes or
edits, let it work on a record it creates in `setup:` (or on one no other journey uses).
`auth.http` sends its request from the browser context, so the session cookie lands in the browser; `auth.steps`
logs in through the UI instead; `auth.check` (`GET /api/me`) proves it worked, otherwise the journey is INCONCLUSIVE.
`assay bench` starts the app from `app.reset` for every attempt.

### Testing a deployed app (staging, preview, special hardware)

Some products cannot start inside a CI job: they need a GPU or devices, a large backend, paid third-party services.
Assay then tests them where they already run: a staging stand, a pull request's preview deployment, or the machine
with the hardware (CI templates for all three: "Deployed app" in the connect skill's `references/ci.md`).

```yaml
base_url: "{env.ASSAY_BASE_URL}"                 # the stand's address, from the CI job
app: {ready: {url: "{base_url}/api/health", timeout_s: 60}}    # optional; no start, no reset
accounts: {qa: {username: "{env.QA_USER}", password: "{env.QA_PASSWORD}"}}   # test accounts from CI secrets
probes:
  orders:   {http: {path: /api/test/orders?owner=qa, headers: {Authorization: "Bearer {env.STAND_API_TOKEN}"}}}
  comments: {postgres: "{env.DATABASE_URL}", sql: "select id, body from comments where author = 'qa' order by id"}
  jobs:     {cmd: "ssh -o BatchMode=yes qa@gpu-01 /opt/app/bin/jobs --json"}
```

- **No `app.start`.** Assay neither starts nor resets the app. It waits up to `app.ready.timeout_s` (default 30 s: a
  preview may be waking up) for `app.ready.url` (default `base_url`) to answer, else the run is INCONCLUSIVE.
- **A protected preview** (Vercel's deployment protection, a stand behind a token) gets its bypass header on the run's
  requests to the app's own origin (pages, API calls, the probes' and logins' requests, the readiness check; never a
  third-party origin, never over a header the page sets itself):
  `browser: {headers: {x-vercel-protection-bypass: "{env.VERCEL_AUTOMATION_BYPASS_SECRET}", x-vercel-set-bypass-cookie: "true"}}`.
  A stand behind basic auth: `browser: {http_credentials: {username: "{env.STAND_USER}", password: "{env.STAND_PASSWORD}"}}`.
- **Addresses and secrets come from the environment.** An unset `{env.ASSAY_BASE_URL}` (or an empty one) stops every
  command with a configuration error that names it (exit 3). Account values expand at login, so the job only has to
  export them; a missing one makes the journey INCONCLUSIVE, never FAILED. `assay doctor` says which deployed app it
  tests, whether it answers, and lists every `{env.X}` the profile reads that is not set, with where it is used.
- **Reading state remotely.** An `http` probe runs in the journey's browser with its cookies: an endpoint the test
  account may read, or a test/admin API of the stand with a token in `headers`. `postgres` runs the query through
  `psql` (GitHub's Ubuntu runners have it; elsewhere `apt-get install postgresql-client`), SELECT/WITH only, `as: rows |
  row | value` like `sqlite`; connect as a read-only role (`grant select` on what the probes read), because that, not
  the SELECT rule, is what keeps a shared database safe. Errors never show the password. `cmd` reads anything that
  prints JSON, for example over `ssh` on the machine with the hardware.
- **Isolation on a shared stand.** Other people, other pull requests' runs and the stand's own jobs write to it while a
  journey runs. Put `{vars.uid}` (unique per journey run) in every record a journey creates (`value: "Q3 report
  {vars.uid}"`) and check that record: `state: {orders: {contains: {title: "Q3 report {vars.uid}"}}}`. Scope probes to
  what the test accounts own (`where author = 'qa'`, `GET /api/me/orders`), not whole tables. Use `contains`, not
  absolute counts; `delta` only on a probe nobody else writes to meanwhile. Never put `allowed_changes` over a probe of
  a table other people write to concurrently, unless the probe is scoped to the test account: their writes would
  count as your journey's side effects.
- **Never production with real users.** Journeys create and change data, and the evidence (screenshots, page text,
  the values probes read) goes to the Assay server. Use a stand made for testing, with test accounts.

## 2. Case

```yaml
- id: post.create_image               # unique; selectable with -k 'post.*'
  title: Mila publishes a photo with a caption
  promise: A new post appears with its photo and caption   # what users are promised; people read it instead of the id
  critical: true                       # runs on every pull request, whatever the selection (optional)
  ai: false                            # true: an AI feature; `assay bench` repeats these by default (optional)
  severity: critical                   # issue severity if it FAILs: blocker|critical|major|minor
  tags: [posts, smoke]
  group: posts                         # default: the file's name inside cases/ (optional; -k group:posts)
  as: mila                             # log in first (optional)
  viewport: mobile                     # desktop (default) | mobile | any profile viewport
  setup:                               # prepare the start state (API/UI/storage). Failure → INCONCLUSIVE
    - http: {method: POST, path: /api/x, json: {...}, save: {post_id: post.id}}   # later: {vars.post_id}
  expect_before: {posts: {len: {gte: 1}}}   # read back the start state. Mismatch → INCONCLUSIVE
  steps:                               # the journey, as a user does it. A failing step → FAIL
    - goto: /create
    - upload: {css: "input[type=file]", file: fixtures/cat.png}
    - fill: {label: Caption, value: "Slow start"}
    - click: {role: button, name: Share moment}
    - wait_for: {text: Moment shared}
  must:                                # any fail → FAIL; any undecided → INCONCLUSIVE
    - no_errors: true
    - state: {posts: {delta: 1, contains: {caption: Slow start}}}
    - allowed_changes: [posts, media, post_media, notifications]
    - judge: The new post shows the photo and the caption "Slow start" at the top of the feed
  should:                              # never change the verdict; add to score, raise minor issues
    - perf: {lcp_ms: 2500, step_ms: 1500}
    - a11y: {max_impact: serious}
```

### Targets (how steps find elements — as a user perceives them)
Role/label/text names match as a **case-insensitive substring** (Playwright semantics): "Like post" also matches
"Unlike post". Add `exact: true` whenever a shorter name is contained in a longer one.

`"Share"` (visible text) · `{role: button, name: Share}` · `{label: Caption}` · `{placeholder: Search}` ·
`{testid: save}` · `{alt: Avatar}` · `{title: Close}` · `{text: Done, exact: true}` · `{css: ".post-card"}`
Modifiers: `nth: 2`, `last: true`, `has_text: "x"`, `within: <target>`.

### Steps
| step | example |
|---|---|
| goto | `goto: /p/1` · `{goto: {url: /, wait: domcontentloaded}}` |
| click / dblclick / hover / tap / check / uncheck | `click: {role: button, name: Save}` · `{click: {target: {css: canvas}, position: [40, 60]}}` |
| fill | `fill: {label: Email, value: a@b.c}` |
| type / press | `type: hello` · `press: Control+Z` · `press: {label: Search, key: Enter}` |
| select / upload | `select: {label: Size, value: L}` · `upload: {css: "input[type=file]", file: fixtures/a.png}` |
| wait_for / wait | `wait_for: {text: Saved}` · `wait_for: {css: .spinner, state: hidden}` · `wait: 500` |
| reload / back / offline | `reload: true` · `offline: true` (and later `offline: false`) |
| viewport | `viewport: {width: 390, height: 844}` |
| eval | `eval: "window.scrollTo(0, 0)"` |
| http | `http: {method: POST, path: /api/x, json: {...}, expect_status: 201, save: {id: data.id}}` |
| draw | `draw: {within: {css: canvas}, points: [[0.2, 0.2], [0.8, 0.8]]}` (0..1 = relative to target) · `strokes: [[...], [...]]` |
| drag | `drag: {within: {css: canvas}, from: [0.1, 0.1], to: [0.5, 0.5]}` · `drag: {from: <target>, to: <target>}` |
| storage | `storage: {set: {"save.v1": {...}}, remove: [k], reload: true}` (localStorage; JSON-encodes objects; `{file: fixtures/save.json}` loads a value from a file) |
| expect | `expect: {not_visible: {role: button, name: More}}` · `{visible: …, text: "…", url: "…", invariant: "js", within_ms: 4000}` — a mid-journey checkpoint; failing it fails the step (→ FAIL) |
| clear_storage | `clear_storage: true` (localStorage, sessionStorage, IndexedDB) |
| clock | `clock: {install: "2026-01-01T09:00:00Z"}` · `clock: {run_for: 5000}` · `{fixed: ...}` · `{fast_forward: 60000}` |
| repeat | `repeat: {times: 5, steps: [{click: End turn}]}` |
| ask | `ask: "Add RSI to the chart"` (chat surfaces; needs `chat:` in the profile) |
| new_tab / switch_tab | `new_tab: /` (second tab, same user; later steps act on it) · `switch_tab: 0` |
| download | `download: {role: button, name: Export}` → saves the file into evidence; sets `{vars.download_name}`, `{vars.download_size}` (check with `vars:`) |
| dialogs | `dialogs: accept` (alert/confirm/beforeunload; default dismiss — every dialog is recorded in evidence) |
| screenshot / login | `screenshot: before-save` · `login: noah` (switch account mid-journey) |

Any step accepts `timeout: <ms>`.

### Checks (must / should items) — one key each
| check | decided by | meaning |
|---|---|---|
| `no_errors: true` | code | no uncaught exceptions / console errors during the journey (`{allow: [regex], console: false}`) |
| `no_failed_requests: true` | code | no first-party 4xx/5xx or failed requests (`{allow_status: [404]}`) |
| `visible: <target>` / `not_visible: <target>` | code | element is (not) visible at the end |
| `count: {css: .post-card, gte: 3}` | code | number of matching elements |
| `text: "Saved"` · `text: {not_contains: "ago ago", regex: "..."}` | code | page text at the end |
| `url: "/p/\\d+$"` | code | final URL matches regex |
| `state: {path: matcher}` | code | probed state after the journey (see matchers) |
| `changed: [paths]` / `unchanged: [paths]` | code | these state paths did / did not change |
| `allowed_changes: [paths]` | code | **nothing else in the probed state changed** (side-effect oracle) |
| `invariant: "js expression"` | code | evaluated in the page at the end; must be true (or `{js: ..., equals: x}`) |
| `a11y: {max_impact: serious}` | code | violations at or above this impact fail (default `serious`: serious + critical) |
| `perf: {lcp_ms, cls, inp_ms, long_task_ms, step_ms}` | code | budgets |
| `no_layout_issues: true` | code | no blank screen, horizontal overflow, broken images |
| `answer: {contains / not_contains / regex / min_len}` | code | last chat answer (AI surfaces) |
| `tools_called: [names]` | code | from the trace adapter (AI surfaces) |
| `judge: "one criterion"` · `{judge: {criterion: ..., on: screenshot|text, screenshot: <step name>}}` | LLM/VLM | meaning; sees the final (or a named `screenshot:` step) screenshot + page text; must items vote 3× |

**State paths:** `posts`, `posts.length`, `posts.0.caption`, `posts.-1.caption`, `me.user.username`, `posts.caption` (plucks the field from every element).
**Matchers:** a literal (equality) or `{eq, ne, gt, gte, lt, lte, len, contains, not_contains, every, exists, regex, any_of, delta, unchanged}` — `delta` compares with the value before the journey (list → length), `contains` on a list matches an element by subset (`{caption: "~slow"}` = case-insensitive substring).
**Diff paths** (for `changed` / `allowed_changes`): list items are keyed by `id`/`uuid`/`key`/`slug`/`name` when present, e.g. `posts.id=42.caption`; otherwise by index `posts[3]`. A pattern `posts` allows anything under `posts`; globs work (`posts.*.likes`).

### Datasets: one journey over many inputs

`each:` runs one journey over a list of rows, as evals tools run one test over a dataset (an AI feature asked many things):

```yaml
- id: draft.reply
  title: The assistant drafts a reply to {row.ticket}
  ai: true
  each:                       # inline rows, or a file relative to .assay/: fixtures/tickets.csv | .json | .yaml
    - {id: late-delivery, ticket: T-1001, expect: refund}
    - {id: wrong-size, ticket: T-1002, expect: exchange}
  steps:
    - goto: /?t={row.ticket}
    - click: {role: button, name: Draft reply}
  must:
    - judge: The draft offers a {row.expect}
```

- Every row is a journey of its own: `draft.reply[late-delivery]` (the row's `id`, else its number: `draft.reply[2]`),
  with everything else of the case (`ai`, `critical`, `group`, tags). Its evidence records the row.
- `{row.<field>}` is filled in every string of the case when the suite loads (`{row.customer.name}` for nested fields),
  as text. A field the row lacks is a configuration error. `{vars.uid}`, `{env.X}` are left for run time.
- A title or promise that names no row field gets the row id in parentheses, `(late-delivery)`, so rows read apart.
- Files: `.csv` (the header names the fields), `.json` or `.yaml` (a list of objects). Keep them out of `cases/`
  (everything there is read as journeys), e.g. in `fixtures/`.
- `-k 'draft.reply[late-delivery]'` runs one row; `-k draft.reply` or `-k 'draft.reply*'` all of them.
- To the gate rows are journeys: a removed row is a removed journey (it waits for `assay-approved`), an edited row an
  edited one.

## 3. Rules for good cases
1. **Facts by code, meaning by the judge.** If code can check it, don't ask the judge. One condition per judge item.
2. **Every mutating journey gets `allowed_changes`.** It finds the bugs nobody wrote an assertion for.
3. **Prove the start state** with `expect_before`; never let a broken fixture look like a product bug.
4. **Check the result, not the path.** `state`/`visible` beat "clicked the right button".
5. **`must` = the release minimum**, `should` = desirable. A judge item in `must` costs 3 calls.
6. **A known bug** → a case that FAILs today with a precise `must`, its `severity` and the tag `known-bug`. It is an
   expected failure: listed on every analysis, never blocks the gate while it fails the same way (same cause, step
   and failed checks as on the target branch; a journey new to the suite is expected to fail). If it starts failing
   differently, or a journey that passed gets the tag, that is a regression. When the fix lands it passes (the pull
   request comment says so); then drop the tag, and it guards against the bug coming back.
7. **Games / canvas:** state hooks (`js` probes, `invariant`) and seeded saves (`storage`) beat pixels; freeze time with `clock`.

## 4. Verdicts and owners
| verdict | when | owner |
|---|---|---|
| PASS | all `must` pass | — |
| FAIL | a step failed, or a `must` failed | product (check the screenshot: a renamed button = stale case) |
| INCONCLUSIVE | app down, login/setup failed, start state not as expected, probe/judge unavailable | environment / case / judge |

Issues come from failed cases **and** from always-on sensors on every journey and scanned screen:
reliability (exceptions, console errors, 5xx/4xx, failed requests), performance (LCP, CLS, INP, long tasks,
slow APIs, heavy pages), accessibility (axe-core WCAG 2.2 AA), UX & mobile (overflow, small touch targets,
broken images, blank screens), security-lite (session cookies without HttpOnly, tokens in localStorage).
The accessibility audit keeps every failing element and what axe could not decide (`incomplete`: a person must look).
`analysis.json` records what measured each dimension (`measured`); a dimension nothing measured has no rating (not
an A) and the product's page lists it under "Not checked". Ratings A–E stay in `analysis.json` for `min_rating`;
people see the verdict and the problems in words instead.

## 5. Benchmark (products with AI)

The gate runs every journey once. For AI features that is not enough: a model answers differently every time.
`assay bench` repeats the journeys and compares **arms**; the first arm is the baseline.

```bash
assay bench .assay --attempts 3 \
  --arm sonnet=env:ASSISTANT_MODEL=sonnet \     # the same app, started with other settings
  --arm haiku=env:ASSISTANT_MODEL=haiku
assay bench .assay --arm main=../base/.assay --arm pr=.assay   # two checkouts (folders with assay.yaml)
assay bench .assay --arm prod=https://app.example.com           # a build that is already running
assay bench .assay                                              # no arms: A/A, the noise floor of the gate
```

The usual settings live in `assay.yaml`, so `assay bench .assay` (and the CI step) needs no arguments; `--arm`,
`--attempts` and `-k` on the command line replace them for one run:

```yaml
bench:
  attempts: 3                 # default 3
  select: [is:ai]             # -k selectors; default: the journeys marked ai: true, else all
  arms:                       # first is the baseline; the --arm syntax; a folder is relative to .assay/
    sonnet: env:ASSISTANT_MODEL=sonnet
    haiku: env:ASSISTANT_MODEL=haiku
```

Per arm: **pass rate** (share of journeys passing on one attempt) with a 95% bootstrap interval over journeys,
**all attempts pass**, the **difference** from the baseline with its interval (✱ when it excludes zero), the
**flaky** journeys (mixed verdicts across attempts) and the **median journey** and **median answer** time (the AI's
turn in `ask` steps): a cheaper model that is as good but slower is a trade-off to see. Inconclusive attempts are
dropped in every arm, so arms compare on the same set. Every attempt starts the app from its reset state.

In CI it is a separate workflow (`command: bench` in the action; see the product README). With `ASSAY_HOST_URL`
the result is published to the server (project → Benchmarks), the job summary gets the table, and a pull request
gets one comment, updated on every run. Readiness checks should prove the model answers (e.g. `/api/health?model=1`):
a missing key or login then makes the run inconclusive instead of failing journeys.

## 6. Agentic freedom test (explore)

Journeys check what someone thought to write down. The freedom test finds the rest: a coding agent uses the product
freely, with a charter ("explore comments with long and multilingual text to discover text saved differently from what
was typed"), while Assay runs the browser and tells it, after every action, what the screen does not: what changed in
the app's data (every probe, record by record), new console errors, failed requests and the API calls made.

The agent is the person's own (Claude Code through `/assay:explore`, or any agent through `assay mcp`); no model runs
inside Assay. The MCP tools:

| tool | |
|---|---|
| `assay_explore_start(charter, as?, viewport?, url?, fresh?)` | starts the app from its golden state (`app.reset`, `app.start`) unless it is already running or deployed, opens one browser signed in as `as`, returns the first page and the data it can watch |
| `assay_explore_act(steps, why?, look?, audit?)` | runs steps of §2 in order, stopping at the first that fails; returns the URL, title, accessibility tree, `data` (changes), `errors`, `failed_requests`, `calls`, the screenshot path; `look` adds the screenshot as an image, `audit` accessibility and layout checks |
| `assay_explore_note(title, detail?, kind?, severity?, step?)` | a finding at a step: `bug`, `ux`, `question` or `idea`; `blocker` … `minor` |
| `assay_explore_finish()` | closes and writes `.assay/runs/explore-<time>/` |

The folder holds `session.json` (every step with what it changed), `findings.md` (each finding with the steps that led
to it and its screenshot) and `trail.yaml` (the steps that worked, as a draft journey). A finding is reported as a bug
only once it reproduces: written as a journey on a record it creates, tagged `known-bug` and `explore`, it must fail
twice on a fresh app on the check that names the bug. It then stays in `cases/` as a regression journey: it never blocks
while the bug is there, and turns green the day it is fixed.

Explorations run on demand, never on every pull request: they cost the agent's time and tokens, and a gate must be
deterministic.
