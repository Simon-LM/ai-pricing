<!-- @format -->

# Eden AI

Reads Eden AI's [public model list](https://api.edenai.run/v3/models) and publishes
`providers.edenai` in `pricing.json`. First implemented 2026-08-10, covering 103
models across the five upstreams asked for: Mistral (39), xAI (33), Scaleway (15),
OVHcloud (12) and Perplexity (4).

## These are Eden's prices, not the upstream provider's

Eden AI is an aggregator: you call one API and it forwards to a provider
underneath. What is published here is what **Eden** charges to do that, which is
not the price of calling that provider directly. `codestral-latest` is
`$1.00`/`$3.00` through Eden and `$0.30`/`$0.90` straight from Mistral. Both are
correct, and both are in this file under their own provider.

So `providers.edenai` is a provider in its own right, not a cross-check of
`providers.mistral` or `providers.ovh`. A difference between them is expected. Do
not "reconcile" it.

## How this source differs from the others

Mistral's and OVH's scrapers read marketing pages -- card markup and a Next.js
data chunk respectively -- where a renamed label silently changes what a figure
means. Eden publishes a real JSON API with explicit field names per model:

```json
"pricing": { "input_cost_per_token": 1.5e-06, "output_cost_per_token": 7.5e-06 }
```

There is nothing to string-match and no layout to latch onto, which makes this the
sturdiest source in the repository and `scrape.py` the shortest scraper in it.

`mapping.json` is shaped differently for the same reason. It does not name a page
element per model, because there is none; a per-model spec would be 103 copies of
the same three lines. What it pins instead is the **set**: which upstreams are
covered, and exactly which model ids are expected. The scraper fails if a listed
model disappears **and** if the mapped upstreams start offering one the mapping does
not list. Both are routine and both are meant to reach a human -- a price nobody
reviewed must never reach the file, and a model consumers may depend on must not
vanish from it silently.

## `list_pricing`, not `pricing`

Each model carries both. `list_pricing` is the public list price; `pricing` is what
the caller of the endpoint would actually pay, which is lower when a discount
applies to the account asking. This scraper is unauthenticated, so the two agree
today -- but only the list price means the same thing to every consumer of this
file, so that is the one read, and a divergence stops the run rather than being
silently resolved.

## Currency

**USD.** There is no currency field anywhere in the API. It is confirmed the same
way OVH's EUR was, by comparing a number against the page that renders it: the
endpoint states `4e-07` per token for `mistral/mistral-medium-2508`, and
<https://app.edenai.run/models> renders that exact model as `$0.40` under a column
headed "Input $". If that ever needs revisiting, it starts here.

## Per-token to per-million

Eden states a price per single token; this file publishes per million. `raw *
1_000_000` is wrong often enough to matter -- it turns `4e-07` into
`0.39999999999999997`, on 48 of this provider's ~245 figures -- so the scaling goes
through `Decimal` seeded from `repr()`. Genuinely long decimals are preserved, not
rounded: Scaleway's figures really do carry fifteen of them, because Eden converts
them from EUR, and rounding would invent a price.

## What is deliberately not published

The API states these and this file does not carry them:

- **Tiered prices** — `input_cost_per_token_above_128k_tokens` (9 models) and the
  `_above_200k_tokens` variants (6 models). A price that depends on how long the
  context is has no representation in this schema, and publishing only the first
  tier as though it were *the* price would understate a long-context bill without
  ever looking wrong.
- **`search_context_cost_per_query`** (10 models) — a per-query web-search charge,
  and itself an object of three sizes rather than one figure.
- **`output_cost_per_reasoning_token`** and **`citation_cost_per_token`** (one model
  each).

`cache_read_per_mtok` **is** published, for the 39 models that state
`cache_read_input_token_cost`.

## Running it yourself

```sh
python3 scripts/providers/edenai/scrape.py --out-dir .ci-out          # read the live endpoint
python3 scripts/providers/edenai/scrape.py --out-dir .ci-out --html tests/fixtures/edenai/models_ok.json
```

Same rules as every other provider here: no API key, ever; never writes
`pricing.json` directly; touches only `providers.edenai`, carrying every other
provider's block through byte-for-byte.
`.github/workflows/refresh-edenai.yml` runs it weekly and on demand, sharing the
`pricing-json-writes` concurrency group with the other providers' workflows so they
queue rather than race.
