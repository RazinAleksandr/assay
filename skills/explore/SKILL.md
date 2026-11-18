---
name: explore
description: Agentic freedom test. You use the product freely, like a curious, skeptical person with a charter, through Assay's browser, which after every action shows what changed in the app's saved data, new errors and failed requests. Finds bugs no journey was written for, confirms each one by reproducing it as a journey, and keeps it as a regression test. Use when the user asks to explore, poke at, try to break, smoke out bugs in, or freely test the product or an area of it, or wants an exploratory or agentic test.
---

# Agentic freedom test

**Outcome:** a short report of what you found in the product, each bug **confirmed** (its journey fails twice on a
fresh app for the reason you state) and kept in `.assay/cases/` as a `known-bug` journey; flows nobody covered kept as
new passing journeys; UX problems and open questions listed with evidence. Usually 15-30 minutes per charter.

Needs Assay set up (`.assay/assay.yaml`). If it is missing, run the `connect` skill's stages 0-2 first (profile and
probes only), or ask the user.

## Tools

The `assay` MCP server of this plugin:

| tool | what it does |
|---|---|
| `assay_explore_start(charter, as?, viewport?, url?, max_actions?, minutes?)` | starts the app from its golden state and one browser, opens the first page; returns the page and the data you can watch. The budget (default 60 steps, 20 minutes) is enforced: every observation shows `budget_left`, and acting is refused once it is spent |
| `assay_explore_act(steps, why?, look?, audit?)` | runs steps in the journey language (`{"click": {"role": "button", "name": "Post"}}`, `{"fill": {"label": "Comment", "value": "…"}}`, `{"goto": "/x"}`, `{"press": "Enter"}`, `{"back": true}`, `{"reload": true}`); returns the page (URL, title, accessibility tree), `data` (what changed in the saved state), new `errors`, `failed_requests`, the API `calls`. `look`: the screenshot as an image. `audit`: accessibility and layout checks |
| `assay_explore_note(title, detail, kind, severity, step?)` | notes a finding at a step: kind `bug` / `ux` / `question` / `idea` |
| `assay_explore_finish()` | closes; writes `session.json`, `findings.md`, `trail.yaml` (the steps that worked) |

And the `assay` command to prove findings (`assay run .assay -k <id> --no-scan`, `assay brief`). The step and check
format is `../../docs/AUTHORING.md` (two folders up from this file).

## 1. Charter (2 min)

Read `.assay/product.md` (risks, surfaces, data, roles, intended behaviour) and the journeys in `.assay/cases/`
(what is covered already).
Write one charter per area the user asked about, or, if they asked for none, for the top 1-3 risks nobody covers:
*"Explore <area> with <account, data, inputs> to discover <the kind of problem>"*, e.g. "Explore comments as mila with
long, multilingual and edited text to discover text that is saved differently from what was typed". Tell the user the
charters in one line each.

## 2. Explore (10-20 min per charter, at most ~60 actions)

`assay_explore_start(charter=…, as=…)`. Act in small batches (1-4 steps), read what comes back, decide the next move.

**The data is the oracle the screen is not.** After every action compare `data` with what the screen claims:
"Saved" with nothing saved, a value saved cut short, rounded, in the wrong field or on the wrong record, a record that
another user's action changed, more records than one click should make (a double submit), a deleted record still
there or the wrong one gone. That is where this test finds what screenshot-based testers cannot.

Tours, in this order, as far as the budget goes:
1. **The main path** once, as a person would, so you know the normal `data` and `calls`.
2. **Inputs:** empty, whitespace only, very long (1000+ characters), emoji and other scripts, `%`, `_`, quotes,
   `<b>tags</b>`, numbers at limits (0, -1, 1e9), dates at month and year ends.
3. **Interruptions:** submit twice fast, back and reload in the middle of a flow, a second tab (`{"new_tab": "/"}`),
   leave a form and return.
4. **People:** another account (`{"login": "noah"}`) on mila's private things; a visitor on pages behind sign-in.
5. **Screens:** `viewport: mobile` (start a new exploration), `audit: true` on the key screens.

Note a finding the moment you see it (`assay_explore_note`): what you did, what you expected, what happened, and the
evidence (the `data` line, the error, the call that failed). Use `look: true` when the screen itself matters. Not
every oddity is a bug: what product.md lists as intended is not a finding, and a behaviour that might be intended is a
`question`. When the user answers that one is intended, add it to product.md's "Intended behaviour". Stop a charter when its area is covered or
the budget is spent, then `assay_explore_finish()`.

## 3. Confirm every bug (5 min each)

A finding is only reported as a bug once it reproduces:

1. Write it as a journey in `.assay/cases/<area>.yaml`: the shortest steps from `findings.md` ("Steps that led
   here"), on a record it creates itself (with `{vars.uid}` in its text), and the precise `must` check that fails
   because of the bug (`state` on the saved value, `allowed_changes`, `no_errors`, `count`, `visible`). Tags
   `known-bug` and `explore`, `severity` by user impact, a `promise` in the product's words.
2. `assay run .assay -k <id> --no-scan` twice (each run starts the app from its golden state). Keep it if it FAILS both
   times on that check (`assay brief` shows the check and the saved value). It passes, or fails for another reason:
   the finding was not what you thought; drop the journey and keep the finding as a `question`.

Flows nobody covered that work: turn the useful part of `trail.yaml` into a normal journey with a `state` check and
`allowed_changes`, and prove it passes twice.

## 4. Report (at most 15 lines)

- Per charter: steps taken, data the product changed, findings.
- **Confirmed bugs**, worst first: the promise broken, the evidence in one line, the journey id.
- UX problems and questions in one line each; the user decides the questions.
- Journeys added (known-bug and passing), and the folder `.assay/runs/explore-…/` with screenshots.
- Next: commit the new journeys on a branch and open a pull request. `known-bug` journeys never block; each one turns
  green, and says so on the pull request, the day the bug is fixed.

## Rules

- Never explore production or anything with real users, money or email. Local or staging only.
- Never change the application's code; only `.assay/` changes.
- One exploration at a time. Always finish it: the files are the record.
- Report only what the evidence shows. A bug you could not reproduce is not reported as a bug.
