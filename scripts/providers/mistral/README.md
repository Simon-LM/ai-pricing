<!-- @format -->

# Mistral

Publishes `providers.mistral` in `pricing.json` from **one** public source:
Mistral's own documentation site.

| what | source | why it has to be that one |
| --- | --- | --- |
| the models | <https://docs.mistral.ai/models> | states a machine-readable price object and an `isRetired` flag per model, and a unit per figure rather than implying one from a label |
| the billable non-models | `mapping.json`'s `products` list | no page prices them any more — Mistral retired the one that did in October 2026 — so their inventory is held by hand, and every name in it is published sold-but-unpriced |

Every model the docs index lists, plus the billable products the list names. How
many that is follows the source and the list; count them in `pricing.json`. The
products currently carry `unpriced_since`: Mistral still sells them and states no
price for them anywhere.

## Why the docs site, and not just the pricing page

An earlier version read only `mistral.ai/pricing/api`. It silently missed four priced
models the docs site lists and that page does not:

| model | price the docs state |
| --- | --- |
| OCR 4.0 | `$4` / 1000 pages, `$5` / 1000 annotated pages |
| OCR 3 | `$2` / `$3` |
| Leanstral 1.5 | free |
| **Voxtral Mini Transcribe 2** | `$0.003` / min |

The last one is the one that mattered. The pricing page dropped its card while
Mistral was still selling it — the docs page says `isRetired: false` and states a
price — so this repository published a model that was on sale as withdrawn. **A
marketing page is not an inventory.** Exactly the same thing happened on OVH's
catalog, which is why that provider has a coverage check; here the fix was to read
the source that actually knows. Mistral went on to retire the pricing page itself —
in October 2026, it began redirecting to the docs site — and nothing of it is read
any more.

## The products' page was retired, and they stay published

In October 2026 `mistral.ai/pricing/api` began redirecting to the docs site, which
prices models only; no page Mistral publishes states a product price any more. The
products — web search, code execution, image generation, libraries, data capture,
premium news and the two classifier models — are still sold and still billed, so
they stay listed and are never dropped. `mapping.json`'s `products` is their whole
inventory now, the one place a human adds a name, and every name in it is published
`unpriced_since` — a dated warning, not a withdrawal; `absent_since` would claim
Mistral stopped offering them, which is false — with the last prices actually
observed kept beside the marker, and the transition reported once.

Nothing is read for them any more, not even as a control pass: the page is gone, and
a weekly look to confirm it is still gone would only add a failure path with nothing
to learn from. A name added to the list is published on the next run,
sold-but-unpriced, and the run reports the addition once.

The run for 2026-10-12 treated the retirement as a layout failure, and the failure
took every model price down with it: nothing in the block was republished, including
figures Mistral had changed. Nothing in this design can repeat that: the products
half reads nothing, so there is no failure on it left to hold the models back.

## The keys are names; the identifiers are a field of their own

**The keys are the names the source states, lowercased**: `ocr 4.0`,
`mistral medium 3.5`, `web search`. Those are what a human reads on the page, and they
are not what you call.

What you call is published beside them, in `api_ids`, read from the same docs page —
which streams it as a `names` array and renders it as copy-to-clipboard badges:

```json
"ocr 4.1": {
  "per_1k_pages": 4.0,
  "per_1k_annotated_pages": 5.0,
  "display_name": "ocr 4.1",
  "api_ids": ["mistral-ocr-4-1", "mistral-ocr-4", "mistral-ocr-latest"]
}
```

**These are model identifiers, not endpoints.** The endpoint is Mistral's own URL and
is the same for every model; the identifier goes in the request body's `model` field.

**Order is meaning and is preserved: most specific first.** `mistral-ocr-4-1` pins a
version and will keep costing what this file says. `mistral-ocr-latest` resolves to
whatever Mistral points it at next, at a different price, with no change anywhere in
this file. Pin the first; treat the last as a convenience you cannot price.

The products carry `"kind": "product"` and no `api_ids`: web search and image
generation are billed, but there is nothing to call. A model whose page states no
identifier is still published with its price, and the run reports it — a missing id
is not a reason to withhold a correct figure, but a consumer has nothing to pass.

**Read from Mistral's own docs and from nowhere else.** Before these were first
published they were sanity-checked by hand against Eden AI's model list, which
independently names `mistral-medium-3-5`, `codestral-2508`, `mistral-large-2512` and
the rest — enough to establish that they are callable ids rather than documentation
slugs. That check was a one-off and is deliberately wired into nothing, because a
reseller can only testify to half the question:

- it *can* confirm a string is a genuine Mistral id, since it resells against that id;
- it *cannot* confirm Mistral still sells it. A reseller lags a retirement by however
  long its own catalogue takes to notice, and may hold a contract that keeps a model
  callable through the reseller for months after Mistral has dropped it from the
  public API.

So the inventory — which models exist, and which are dated with `absent_since` —
comes from the docs index alone. Eden still listing `mistral/<something>` that this
file marks absent is not a contradiction anyone needs to reconcile: it is two
different sellers, and only one of them is Mistral.

That is structural rather than a promise. The offline test manifest serves 21 URLs,
every one of them Mistral's, and a run that asks for any URL it does not name fails
instead of reaching the network — so a scraper here that started reading somebody
else's catalogue would break the test suite rather than quietly publish from it. It
does not serve the retired products page, so that page cannot slip back into a run
by accident either.

## What the mapping still holds

No model list, and there must not be one — the docs index says which models exist,
every week. What `mapping.json` holds is the translation for the things the source
states in prose rather than in data, plus the one list no page states any more.

**`denominators`** — the docs state a unit per figure, split into `input` and
`output`:

```json
"input":  { "/M Tokens": "in_per_mtok", "/1000 Pages": "per_1k_pages" },
"output": { "/M Tokens": "out_per_mtok" }
```

`labels` overrides it when a row names itself: GLM 5.2 states two input figures both
denominated `/M Tokens`, ordinary and `Cached input`, which would otherwise collide.

**`products`** — the inventory of the billable non-models, held by hand since the
page that priced them was retired. Adding a name is what publishes a new product:
sold-but-unpriced, reported once, on the next run. Do not rename an entry — a rename
would publish a new product and freeze the old one, and the name is the key a
consumer already reads.

A denominator or label missing from those tables is **not guessed at**: the unit is
part of the field name and there is no honest field for an unrecognised figure. That
one figure is skipped, the run reports it by email, and every other price publishes.

## Two things worth knowing about the data

**A figure of `0` means "this side is not billed", not "free".** Voxtral TTS charges
for the audio it generates and nothing for the text it is given. This source states
`free` separately, so a zero is unambiguous here and is simply not published — unlike
OVH's catalog, which has no such flag and where every-unit-zero is the only thing that
can stand in for one.

**OCR bills annotated pages at a higher rate**, so those models carry both
`per_1k_pages` and `per_1k_annotated_pages`. A consumer reading only the first
understates the bill.

## Running it yourself

```sh
python3 scripts/providers/mistral/scrape.py --out-dir .ci-out          # live, ~21 fetches
python3 scripts/providers/mistral/scrape.py --out-dir .ci-out --offline tests/fixtures/mistral/offline.json
```

`--offline` serves every URL from committed fixtures and **refuses** any URL the
manifest does not name, so a test that forgets a fixture fails loudly instead of
quietly reaching the network and passing for the wrong reason.

Same rules as every other provider here: no API key, ever; never writes `pricing.json`
directly; touches only `providers.mistral`, carrying every other provider's block
through byte-for-byte. `.github/workflows/refresh-mistral.yml` runs it weekly and on
demand, sharing the `pricing-json-writes` concurrency group with the other providers'
workflows so they queue rather than race.
