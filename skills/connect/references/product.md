# Template: `.assay/product.md`

What Assay knows about the product. `/assay:connect` writes it; every Assay skill reads it before it adds journeys,
explores or benchmarks; the user may edit it. Keep it short and factual: tables over prose, names exactly as the
product uses them. Copy everything below the line into `.assay/product.md` and fill it in.

---

# <Product name>

What Assay knows about this product. Written by `/assay:connect` on <date>; edit it freely, Assay skills read it.

## What it is

<Two sentences: who uses it, for what.>

Roles: <admin (account `ada`), member (`mila`, `noah`), visitor>.

## How it runs for testing

| | |
|---|---|
| Where | local: Assay starts it (`npm run dev` on :5310, data in `.assay/.state/`) · or deployed: `<url>` because <needs a GPU / …> |
| Start state | <seed script, fixtures, the accounts it creates> |
| External services | <Stripe: test mode · email: logged to the console · maps: not used in tests> |
| Model | <the env var with the key, the one that picks the model, the default model> (AI products only) |

## Surfaces

| Area | Screens or routes | Who |
|---|---|---|
| <Feed> | `/`, `/p/:id` | everyone |

## Data

What users create and change, and the probe that reads it.

| Entity | Probe | Where it lives |
|---|---|---|
| <comments> | `comments` | SQLite `data/app.db`, table `comments` |

## AI features

| Feature | What it does for the user | Where | Can vary |
|---|---|---|---|
| <Draft reply> | <drafts an answer to a support ticket> | <ticket page, "Draft reply"> | <`ASSISTANT_MODEL`: sonnet, haiku> |

## Risks

What would hurt users most, worst first. Journeys and exploration charters come from here.

1. <A comment is saved cut short while the screen says "Saved".>

## Intended behaviour

What looks odd but is how the product is meant to work. Explorations read it so they don't report it again; add a
line whenever the user says "that's intended".

- <Deleting a post keeps its comments for 30 days.>

## Not covered

- <Payments with real cards: there is no test mode.>
