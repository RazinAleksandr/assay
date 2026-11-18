---
name: assay
description: Work with a product's Assay setup (the .assay/ folder) day to day. Run the journeys now and say what is broken, add or fix a journey, turn a bug report into a regression journey, explain a failed or inconclusive gate on a pull request. Use in repositories that have .assay/assay.yaml, or when the user mentions Assay, a journey, the gate, an analysis, or asks to test the product.
---

# Assay: test the product, grow the journeys, explain the gate

The product's Assay setup is `.assay/`: `product.md` (what Assay knows about the product), `assay.yaml` (how to run
it, read its data, the gate) and `cases/*.yaml` (journeys). The format is `../../docs/AUTHORING.md` (two folders up
from this file); read it before writing YAML. Not set up yet (no `.assay/assay.yaml`)? Use the `connect` skill.

`assay` is on the PATH through this plugin (or `${CLAUDE_PLUGIN_ROOT}/bin/assay`); it needs `uv`. First use on a
machine: `assay browsers`.

Other jobs have their own skills: a failed gate to fix → `fix`; comparing models or prompts → `bench`; letting an agent
loose on the product to find bugs → `explore`.

**Never change the application's code to make a journey pass, and never weaken a check to get green.**

## Test the product now (5-15 min)

1. `assay run .assay --scan` (everything, plus every screen's sensors), or `-k '<glob>' | -k tag:smoke |
   -k group:<area> | -k is:critical` for a part. It starts the app from its golden state and stops it after.
2. `assay brief` (the newest run as JSON) for what failed: the promise broken, the check with expected against what the
   app saved, the step, and how to reproduce. Open the screenshots it links when the screen matters.
3. Tell the user in at most 8 lines: ✅ / ❌ / ⚠️, what is broken for users in their words, `known-bug` journeys still
   failing (expected), what the scan found (errors, accessibility, speed), and the report path
   (`.assay/runs/<id>/report.html`). ⚠️ Not sure is never the product's fault: `assay doctor .assay` says what to fix.

## Add a journey (10 min)

1. Find what the user does and what proves it worked: the saved row, the API state, the visible result. The data and
   risks in `product.md` say where to look; update `product.md` when you learn something new about the product.
2. Write it in `.assay/cases/<area>.yaml`: steps with targets a person perceives (role, label, text); a `state` check
   plus `allowed_changes` for anything that changes data; `expect_before` when it starts from seeded data. Many inputs
   for the same flow: one journey with a dataset (`each:`).
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

## Explain a gate

The pull request comment says the verdict in four lines and links the brief (`assay brief <url>`):
- ❌ Don't ship: a journey broke because of the product (the headline says what users would hit), or the change
  weakened checks (a person must approve with the label `assay-approved`). To fix it: the `fix` skill.
- ⚠️ Not sure: something could not be checked (the app did not start, login or setup failed, no judge key). Never the
  product's fault: fix the environment or the journey (`assay doctor .assay`).
- Renamed UI is a stale journey: update it to the new behaviour with checks as strict as before.

## Rules

- Facts by code, meaning by the judge. One condition per judge item; judge items in `must` need `ANTHROPIC_API_KEY`
  in CI.
- Every journey that changes data has `allowed_changes`. Every seeded start has `expect_before`.
- Never point a run at production data or real accounts.
