<!-- @format -->

# Mistral

Publishes `providers.mistral` in `pricing.json` from **two** public sources, because
neither one is complete.

| what | source | why it has to be that one |
| --- | --- | --- |
| the models | <https://docs.mistral.ai/models> | states a machine-readable price object and an `isRetired` flag per model, and lists models the pricing page does not |
| the billable non-models | <https://mistral.ai/pricing/api> | the only place web search, code execution, images, libraries, data capture and the two classifiers are priced at all |

34 entries today: 27 on sale, 7 kept with `absent_since`.

## Why two sources, and not just the pricing page

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
the source that actually knows.

## The keys are names; the identifiers are a field of their own

**The keys are the names the sources state, lowercased**: `ocr 4.0`,
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

The eight products carry no `api_ids`: web search and image generation are billed, but
there is nothing to call. A model whose page states no identifier is still published
with its price, and the run reports it — a missing id is not a reason to withhold a
correct figure, but a consumer has nothing to pass.

Cross-checked against a second source before publishing: Eden AI's own model list
independently names `mistral-medium-3-5`, `mistral-medium-3`, `mistral-medium-latest`,
`codestral-2508`, `mistral-large-2512` and the rest, which is what pins these as real
callable ids rather than documentation slugs.

## What the mapping still holds

No model list, and there must not be one — the docs index says which models exist,
every week. What `mapping.json` holds is the translation for the two things the
sources state in prose rather than in data.

**`denominators`** — the docs state a unit per figure, split into `input` and
`output`:

```json
"input":  { "/M Tokens": "in_per_mtok", "/1000 Pages": "per_1k_pages" },
"output": { "/M Tokens": "out_per_mtok" }
```

`labels` overrides it when a row names itself: GLM 5.2 states two input figures both
denominated `/M Tokens`, ordinary and `Cached input`, which would otherwise collide.

**`rows`** — the same idea for the pricing page's products, which put their unit in a
row label. A label is matched exactly first; failing that, the longest entry it
*starts with* wins. That prefix rule exists for two rows only, where Mistral appends
an explanatory sentence to the label — one of which quotes a fee that will itself
change.

A denominator or label missing from those tables is **not guessed at**: the unit is
part of the field name and there is no honest field for an unrecognised figure. That
one figure is skipped, the run reports it by email, and every other price publishes.

## Three things worth knowing about the data

**A figure of `0` means "this side is not billed", not "free".** Voxtral TTS charges
for the audio it generates and nothing for the text it is given. This source states
`free` separately, so a zero is unambiguous here and is simply not published — unlike
OVH's catalog, which has no such flag and where every-unit-zero is the only thing that
can stand in for one.

**OCR bills annotated pages at a higher rate**, so those models carry both
`per_1k_pages` and `per_1k_annotated_pages`. A consumer reading only the first
understates the bill.

**The pricing page still carries a card for every model, at prices of its own.** They
are deliberately ignored; reading them too would give each model two sources of truth
and let one silently win. A test moves the pricing page's own `codestral` figure and
asserts nothing published changes.

## Running it yourself

```sh
python3 scripts/providers/mistral/scrape.py --out-dir .ci-out          # live, ~22 fetches
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
