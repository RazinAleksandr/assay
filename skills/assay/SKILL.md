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
3. Give it a `promise:` (what users are promised, in their words) and `critical: true` if it must never break.
4. Unknown UI path: write a `goal:` journey first and run it with `--agent`; the agent explores and caches the path
   in `.assay-cache/` (commit it). Then rewrite it as `steps:` if it should be fast and deterministic.
5. Prove it: `assay run .assay -k <id> --no-scan`, then `assay brief`. Run it 3 times; a check in `must` has to hold
   in every run.

## A bug report → a regression journey

Encode the correct behaviour precisely, confirm it FAILS on the current build and name the check that proves it.
Tag it `known-bug` with `severity` by user impact: an expected failure, listed on every analysis, never blocks while it
fails the same way. If it starts failing differently, or a journey that passed gets the tag, the gate counts it.
When the fix lands it passes and the pull request comment says so; then remove the tag so it guards against a comeback.

## A failed gate

The pull request comment says the verdict in four lines and links the brief. To fix it, use the `fix` skill
(`/assay:fix <brief URL>`): it reads the brief (`assay brief <url>`), follows the suspect code, reproduces and re-runs.
To explain it to a person:
- ❌ Don't ship: a journey broke because of the product (the headline says what users would hit), or the change
  weakened checks (a person must approve with the label `assay-approved`).
- ⚠️ Not sure: something could not be checked (the app did not start, login or setup failed, no judge key). Never the
  product's fault: fix the environment or the journey (`assay doctor .assay`).
- Renamed UI is a stale journey: update it to the new behaviour with checks as strict as before.

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
