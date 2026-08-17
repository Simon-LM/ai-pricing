<!-- @format -->

# Mistral

Reads Mistral's [public pricing page](https://mistral.ai/pricing/api) and publishes
`providers.mistral` in `pricing.json`. Covers every card on the page that states a
price — 23 entries today, and the count follows the page.

## Why there is no API model id in this block

**The keys here are page card names**: `mistral medium 3.5`, `codestral`,
`ministral 3 (3b)`. Not `mistral-medium-latest`. This is a deliberate change from an
earlier version of this repository, and it is the thing most likely to surprise you.

Mistral publishes no machine-readable pricing at all. There is no pricing endpoint in
the API, and the one usage endpoint returning a `prices` field requires an **admin**
API key that an ordinary user of an open-source tool does not have and must never be
asked for. So the numbers come from reading a marketing page.

That page states a card name and a price. It does not state an API model id, and
nothing else on it implies one — `devstral 2` is `devstral-medium-latest` and
`voxtral tts` is `voxtral-mini-tts-latest`, neither derivable from the other. An id in
this file could therefore only ever be a human's translation, re-checked by hand every
time Mistral renames a card.

Mistral renames cards. In the week of 2026-08-17 it renamed six of them (`ocr 4` →
`ocr 4.1`, `ministral 3 - 3b` → `ministral 3 (3b)`, and four more) and retired eight
others. The version of this scraper that held a hand-written name-to-id table did the
only thing it could: it refused to publish anything, and 22 correct prices sat blocked
behind a translation problem that had nothing to do with them.

So the translation is not made here any more. This block is a price list keyed the way
its source keys it. **A consumer that needs to call the model resolves the id itself**,
from `docs.mistral.ai/models/model-cards/`, where each card states its versioned id and
its aliases together.

## What the mapping still holds

`mapping.json` names **row labels**, not models:

```json
"Input (/M tokens)":  { "field": "in_per_mtok" },
"Audio generation":   { "field": "per_1k_chars", "expect_suffix": "per 1k characters" }
```

A label is matched exactly first; failing that, the longest table entry the label
starts with wins. The prefix rule exists for two rows only — training cost and storage
cost — where Mistral appends an explanatory sentence to the label, one of which quotes
a fee that will itself change. The unit is always in the leading segment, which is what
makes a prefix enough.

`expect_suffix` is checked byte-for-byte where the unit is **not** in the label. The
OCR rows read `$4` with the unit stated separately as `/ 1000 pages`; if that suffix
changes, `per_1k_pages` stops meaning what it says, so the row is dropped and reported
rather than published.

A label missing from the table is not guessed at — there is no honest way to name the
unit of a figure whose label this file does not recognise. That one row is skipped, the
run reports it by email, and every other price still publishes.

## Two decoys the tests exist to catch

Prices are read **per card**, never by searching the page for a label. Two things on
the page would go wrong immediately otherwise:

- the `libraries` card carries a row labelled `OCR (per 1K pages)` at `$3`, which is
  not the OCR model's own price of `$4`;
- `voxtral mini transcribe realtime` carries a row labelled `Audio Input/min`, a label
  that has appeared on more than one card at different prices.

## The one card that becomes two entries

`ocr 4.1` prices two different things in the same unit: OCR at `$4` per 1000 pages and
Document AI at `$5`. One entry has exactly one `per_1k_pages` field, so they cannot
share one. Both get their own entry, keyed by row label:

```json
"ocr 4.1 / ocr":         { "per_1k_pages": 4.0 },
"ocr 4.1 / document ai": { "per_1k_pages": 5.0, "kind": "product" }
```

Both, rather than the first keeping the plain card name — so that the two cannot swap
the day Mistral reorders the rows.

## Products, and three cards with no price

`mapping.json`'s `products` list marks the cards that are billable but are not models:
web search, code execution, images, and so on. It is an **annotation and gates
nothing** — a card missing from it is published as an ordinary model, and a name in it
that matches no card does nothing at all. It can never block a price.

Three cards state no price at all (`leanstral`, `mistral moderation 2`, `agent api`).
They are absent from the file rather than published at `0`: the page states no price,
which is not the same statement as a price of zero. They are not reported either —
"still not priced" is not news, and an alert channel that repeats itself gets ignored.

## Currency

**USD.** The page states both `priceUsd` and `priceEur`, and they are genuinely
different prices rather than a conversion of one another — `1.25 EUR` against
`1.50 USD` for the same row. `priceUsd` is read; no conversion is performed anywhere
in this repository.

## Running it yourself

```sh
python3 scripts/providers/mistral/scrape.py --out-dir .ci-out          # live
python3 scripts/providers/mistral/scrape.py --out-dir .ci-out --html tests/fixtures/mistral/page_ok.html
```

Same rules as every other provider here: no API key, ever; never writes `pricing.json`
directly; touches only `providers.mistral`, carrying every other provider's block
through byte-for-byte. `.github/workflows/refresh-mistral.yml` runs it weekly and on
demand, sharing the `pricing-json-writes` concurrency group with the other providers'
workflows so they queue rather than race.
