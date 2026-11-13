---
name: assay
description: Work with a repository's Assay quality gate (the .assay/ folder). Add or fix a user journey, turn a bug report into a regression journey, explain a failed or inconclusive gate on a pull request, run or read a benchmark of AI features. Use in repositories that have .assay/assay.yaml, or when the user mentions an Assay gate, journey, analysis or benchmark.
---

# Assay: journeys, gates and benchmarks

The repository's Assay project is `.assay/`: `assay.yaml` (how to start the app, read its state, the gate) and
`cases/*.yaml` (journeys). `references/AUTHORING.md` next to this file is the format; read it before writing YAML.
Not connected yet (no `.assay/assay.yaml`)? Use the `connect` skill.

`assay` is on the PATH through this plugin (or `${CLAUDE_PLUGIN_ROOT}/bin/assay`); it needs `uv`. First use on a
machine: `assay browsers`.

**Never change the application's code to make a journey pass, and never weaken a check to get green.**

## Add a journey (10 min)

1. Find what the user does and what proves it worked: the saved row, the API state, the visible result.
2. Write it in `.assay/cases/<area>.yaml`: steps with targets a person perceives (role, label, text); a `state`
   check plus `allowed_changes` for anything that changes data; `expect_before` when it starts from seeded data.
3. Unknown UI path: write a `goal:` journey first and run it with `--agent`; the agent explores and caches the path
   in `.assay-cache/` (commit it). Then rewrite it as `steps:` if it should be fast and deterministic.
4. Prove it: `assay run .assay -k <id> --no-scan`, read `.assay/runs/<newest>/report.md`. Run it 3 times; a check
   in `must` has to hold in every run.

## A bug report → a regression journey

Encode the correct behaviour precisely, confirm it FAILS on the current build and name the check that proves it.
Tag it `known-bug` with `severity` by user impact. It turns green when the fix lands.

## Explain a gate

1. Start from the pull request's Assay comment: the condition that failed and the newly failing journeys.
   The link opens the analysis: each journey's steps with screenshots, checks, and what changed in the data.
2. Locally: `assay run .assay -k <id>` reproduces it; `.assay/runs/<id>/report.md` has the same evidence.
3. Who owns it:
   - FAIL with a missing button or text: look at the screenshot. Renamed UI is a stale journey; update it.
   - FAIL on `state` or `allowed_changes`: the product saved the wrong thing. That is the bug the gate caught.
   - INCONCLUSIVE is never the product's fault: the app did not start, login or setup failed, the start state was
     wrong, or the judge had no key. Fix the environment or the journey.
4. A new issue from the sensors (console error, failed request, accessibility, layout) names its screen and
   evidence. Fix it or, only if it is verified noise, add it to `ignore` in `assay.yaml`.

## Benchmark AI features

`assay bench .assay --attempts 3 --arm a=env:MODEL=x --arm b=env:MODEL=y [-k 'ai.*']` repeats every journey per arm.
Read: pass rate with its 95% interval, all attempts pass, the difference from the baseline arm (✱ = the interval
excludes zero, so it is real), and the flaky journeys. A difference without ✱ is noise at this sample size: add
attempts or journeys before deciding. In CI it is the separate `assay-bench.yml` workflow (`command: bench`).

## Rules

- Facts by code, meaning by the judge. One condition per judge item; judge items in `must` need `ANTHROPIC_API_KEY`
  in CI.
- Every journey that changes data has `allowed_changes`. Every seeded start has `expect_before`.
- Never point a run at production data or real accounts.
