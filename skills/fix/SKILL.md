---
name: fix
description: Fix what a failed Assay gate found. Gets the brief of the analysis (each failing journey, the check that failed with the expected value against what the app actually saved, the step, the suspect code of the change, how to reproduce it), fixes the product code and re-runs the journeys until the verdict is "Ship it". Use when an Assay comment on a pull request says "Don't ship" or "Not sure", when the user asks to fix the Assay gate or a failing journey, or gives an Assay analysis or brief URL.
---

# Fix a failed Assay gate

**Outcome:** the product does what the failing journeys promise, proven by `assay run`, and the pull request's Assay
check turns green. The journeys are the specification: fix the product, not the checks.

Arguments: the analysis or brief URL from the pull request comment, if the user gave one.

## Tools

The plugin provides the `assay` MCP server and the `assay` command (on the PATH, or `${CLAUDE_PLUGIN_ROOT}/bin/assay`):

| need | MCP tool | command |
|---|---|---|
| the verdict of a pull request | `assay_status(pr?)` | `gh pr view --comments` (the Assay comment) |
| the brief | `assay_brief(source, case?, full?)` | `assay brief <url or run folder> [--case ID --full]` |
| re-run journeys on your change | `assay_run(select)` | `assay run .assay -k <id> --no-scan` then `assay brief` |
| a screenshot or evidence file | `assay_artifact(ref)` | open the path, or `curl` the URL |

First use on a machine: `assay browsers` (downloads Chromium once).

## 1. Get the brief (1 min)

- A URL from the user or the comment: `assay_brief(source=<url>)`. The link of a private repository carries its key;
  use it as is.
- No URL: `assay_status()` for the current branch's pull request; it returns the brief link.
- No pull request yet (a local run): `assay_brief()` reads the newest run in `.assay/runs/`.

Read `verdict`, `headline`, `action`:
- `no_ship`: the product broke. Go on.
- `unsure`: nothing was decided (the app did not start, login or setup failed, no judge key). `not_checked` says what;
  it is never a product bug. Fix the environment (`assay doctor .assay`) and run again.
- `action: decide` with `weakened_checks`: the change made checks weaker. That is the user's call; tell them, never
  approve it yourself.

## 2. Each failure, most important first (5-20 min each)

1. `phrase` says what broke for the user. `failed_check` has the facts: `expected` against `actual`, for records the
   differing `fields`, the data `address` (probe, table, fields), the `step` that failed and its error.
2. `suspects` are the hunks of the change ranked by the data: one that writes the wrong field, sits in the handler of
   a route the journey called, touches the table, changes the text the step looks for. Open the first suspect, check
   it against `failed_check`, then the others. No suspects (a run outside a pull request): follow `address` (search the
   code for the table and field writes) and the API routes in `assay_brief(case=<id>, full=true)` → `api`.
3. Reproduce before you change anything: `assay_run(select=[<case_id>])` (or the `repro` command). Same failure:
   go on. It passes locally: compare the CI environment (data, env vars, build), not the product.
4. Fix the product code at the root cause. Keep the change small and in the spirit of the pull request.
5. `assay_run(select=[<case_id>])` until that journey passes, then the whole suite once
   (`assay_run()`): the verdict must be `ship` and nothing else may break.

## 3. Finish

1. Commit the fix on the pull request's branch with a message that names the journey and the cause; push. CI re-runs
   the gate; `assay_status()` shows the new verdict in about as long as the last run took.
2. Report in at most 6 lines: what users would have hit (the phrase), the cause (`file:line`), the fix, the verdict now.

## Rules

- **Never edit `.assay/` to get green**: no removed or softened checks, no new `known-bug` tags, no `ignore` or
  `volatile` entries. The gate compares the journeys with the target branch, lists every weakened check and blocks
  until a person approves it.
- The journey is wrong only when the change deliberately alters the behaviour it checks (a renamed button, a new
  required field). Then update the journey to the new behaviour, keep every check as strict, and say so in the pull
  request description.
- A `known-bug` journey that now passes: remove its tag so it guards against the bug coming back.
- Never point a run at production data or real accounts.
