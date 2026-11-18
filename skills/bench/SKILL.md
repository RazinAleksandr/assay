---
name: bench
description: Benchmark the product's LLM features through the product itself. Finds the AI features, writes journeys that check what each feature actually did (the record it created, the facts in its answer, a judge criterion) over a dataset of realistic inputs, compares arms (models, prompts, settings, builds) with repeated attempts, pass rates, 95% intervals and pass^k, and says which difference is real. Use when the user asks to benchmark, evaluate or compare models, prompts or versions of an AI feature, asks whether a cheaper or newer model is good enough, or wants evals for their LLM features.
---

# Benchmark the AI features

**Outcome:** a benchmark the user can re-run any time (and weekly in CI): journeys marked `ai: true` over datasets of
real inputs, arms in `.assay/assay.yaml`, a first result, and a clear answer to the user's question ("haiku passes 81%
[72-89] against sonnet's 92% [86-97]: the gap is real on refunds, within noise elsewhere").

Why through the product: a model's answer can read well while the feature fails (the task was created with the wrong
date, the refund was refused). Assay checks the outcome the user gets, repeated, with intervals. Format:
`../../docs/AUTHORING.md` §2 (datasets: `each:`) and §5 (benchmark). Needs Assay set up (`connect` skill otherwise).

## 1. What to compare (5 min)

Read `.assay/product.md` ("AI features") and the code: model SDK calls, prompts, the env vars that pick the model,
the prompt version, temperature, tools. Ask the user what decision the benchmark serves, if they did not say:
a cheaper model, a new model, a prompt change, a new build. Each **arm** is one way to run the product:
`env:MODEL=haiku` (the same app started with other settings), a folder with another checkout's `.assay`, or the URL of
a running build. The first arm is the baseline (what runs today).

## 2. Journeys and datasets (15 min)

For each AI feature, 1-3 journeys marked `ai: true` that use it the way a person does and check the **outcome**:

- the record or side effect it made: `state` (the task exists with the right fields), `allowed_changes`;
- facts in the answer, by code: `answer: {contains / not_contains / regex}`, `text`, `visible`;
- meaning, by the judge, one condition per item: `judge: The reply offers a refund for the late delivery`;
- `no_errors`, and `perf: {step_ms: …}` when latency matters.

Give each journey a dataset with `each:` (inline rows or a CSV/JSON/YAML file in `.assay/fixtures/`): 10-30 realistic
inputs from the product's domain (its docs, seed data, support examples the user can share), including the hard ones:
ambiguous requests, missing information, other languages, adversarial prompts, inputs where the right answer is to
refuse or ask. Use `{row.<field>}` in steps and checks, and an expected value per row where code can check it.

## 3. Arms and readiness (5 min)

```yaml
bench:
  attempts: 3
  arms:
    sonnet: env:ASSISTANT_MODEL=sonnet     # baseline: what runs today
    haiku: env:ASSISTANT_MODEL=haiku
```

`app.ready` must prove the model answers (e.g. `/api/health?model=1`): a missing key then makes the run inconclusive
instead of failing every journey. Judge items cost judge calls; the product's calls cost the user's key. Before a full
run, tell the user the size: journeys × rows × arms × attempts model-backed journeys.

## 4. Run (time: the size above × the median journey)

1. Smoke: `assay bench .assay --attempts 1 -k '<one journey>*'`. Fix journeys until every inconclusive one is gone
   and failures are about the model, not the journey.
2. Full: `assay bench .assay` (the profile's arms and attempts). The report is `.assay/runs/bench-…/bench.html`;
   `bench.json` has the numbers.

## 5. Read it (2 min)

- **pass@1** per arm with its **95% interval**: the share of journeys that pass on one attempt.
- **pass^k**: journeys that passed every attempt. What users feel; a feature that works 2 times in 3 is broken for
  one user in three.
- **Δ vs baseline** with its interval; ✱ marks a difference whose interval excludes zero. Without ✱ it is noise at
  this size: say so, and how to tighten it (more rows beat more attempts).
- **Flaky** journeys and the per-journey table: where the arms differ (one arm fails refunds) matters more than the
  average. Median journey and answer time per arm, when speed matters.

Answer the user's question in 3-5 lines with the numbers, then the evidence (the failing journeys of the losing arm,
`assay brief .assay/runs/bench-…/<arm>-<n>` for one of them).

## 6. In CI

`.github/workflows/assay-bench.yml` (connect skill, `references/ci.md`): weekly and on demand, `command: bench`; the
model key as a repository secret. Results land on the Assay service under the project's Benchmarks, with history; run on
a pull request, it leaves one comment with the table there.

## Rules

- Check what the feature did, not only how its text reads. Facts by code, meaning by the judge, one condition per item.
- Never benchmark against production data or real users. Never put secrets in datasets.
- Don't change the product's prompts or code to win a benchmark; report, and let the user decide.
