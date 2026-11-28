# Mistral

`scrape.py` reads Mistral's documentation site every day (04:00 UTC,
`.github/workflows/refresh-mistral.yml`) and works out what `providers.mistral` should
say. No API key: every page is public.

| what | source | read as |
| --- | --- | --- |
| the models, and their prices | <https://docs.mistral.ai/models>, then one page per model | the index's model cards *and* the rows of its "Deprecated & retired models" table; each page's model object: its `name`, its `modelIds`, its `status` and its `pricing` list |
| the batch discount | <https://docs.mistral.ai/inference/batch-processing> | one sentence: "We offer a 50% discount on batch API" |
| the Voice Agents API | <https://docs.mistral.ai/inference/pricing> | its "Voice agents" table only, where it is still there |
| the tools and other products | `mapping.json`'s `products` list | no page states their price any more; see below |

## Why the documentation site

Mistral's API has no pricing endpoint, and the one usage endpoint that returns prices
needs an *admin* API key, which an open-source tool must never ask for. The documentation
site does better than a marketing page. Each model's page streams a whole model object --
its `name`, the ids to call in `modelIds`, an `isFree` flag, a `status` of `ga`,
`deprecated` or `retired`, and a `pricing` list with the unit stated on each row
(`{"direction": "input", "priceUsd": 4, "unit": "/1000 Pages"}`). The index lists the
models currently offered, one card each, and the ones Mistral has deprecated or retired
as rows of a table further down the same page. Both shapes are read: a deprecated model is
still sold, so it must keep being visited, and only its own page says whether it is
really gone.

A marketing page is not an inventory: the pricing page this block was once read from
dropped a model Mistral was still selling. Mistral retired that page in October 2026.

## Retired, and deprecated

`status` on the page decides:

- `retired` -- gone. The entry stops being published, and the runner keeps it dated under
  `absent_since` for a year, exactly as it does for a model that leaves the index.
- `deprecated` -- still sold, so it stays published with its **current** prices, plus
  three fields that say a withdrawal is planned: `deprecated` (`true`), `deprecated_since`
  (the day, from `deprecatedAt`) and `retires_on` (the day it goes, from `retiredAt`).
  The two dates are written only where the page states them. When the day named by
  `retires_on` has passed, the entry stops being published, whether or not Mistral has got
  round to removing it from the page -- Mistral announces withdrawal dates and, this
  capture showed, keeps listing models months after them (see `docs/format.md`).
- anything else (`ga`, `publicPreview`) -- an ordinary entry.

## Keys are names; `api_ids` says what to call

Keys are the names the documentation states, lowercased: `mistral small 4`, `ocr 4.0`.
The ids to call are published beside them, in `api_ids`, as each model's page states
them, most specific first:

```json
"ocr 4.1": {
  "per_1k_pages": 4.0,
  "per_1k_annotated_pages": 5.0,
  "display_name": "ocr 4.1",
  "api_ids": ["mistral-ocr-4-1", "mistral-ocr-4", "mistral-ocr-latest"]
}
```

Pin the first; the last, `-latest`, moves to another model whenever Mistral decides. A
model whose page states no id is still published, and the run reports it.

**Read from Mistral's pages and nothing else.** A reseller can confirm that a string is
a real Mistral id, but not that Mistral still sells it, so no other provider's data
feeds this block. The offline test manifest serves only Mistral's own URLs, so a scraper
that started reading another source would fail the tests.

## The batch figures are worked out, and say so

Mistral states its batch discount in a sentence, not beside each price. The `_batch`
figures are therefore **derived** from it, and every entry carrying one says so:

```json
"in_per_mtok": 0.15,
"in_per_mtok_batch": 0.075,
"batch_derived": "50% off the stated rate, as Mistral's batch API documentation states"
```

The percentage is read on every run, so the figures follow it if Mistral changes it.

If the sentence goes, or states two rates, or the page stops answering at all, the run
does **not** stop. It used to: on 2026-11-25 the page went 404, the run failed, and not a
single Mistral price was republished that day over one sentence. The model prices are
Mistral's own stated figures and had been read fine. So the fallback is now the rate this
block last recorded: the figures are derived on it exactly as before, every entry says so
in its marker rather than claiming Mistral states it today,

```json
"batch_derived": "50% off the stated rate, the rate Mistral last stated: this run could not confirm it against the batch API page"
```

and the doubt is reported on its own -- `unconfirmed.txt`, the job summary, and a GitHub
issue assigned to Simon that closes itself on the first run that reads the rate off the
page again. "Il faut que j'aie une alerte, moi. Mais pas bloquer la mise a jour des prix
et des nouveaux modeles."

With no rate to fall back on either -- a block that records none -- no batch figure is
published that week and the alert says which; the model prices still publish.

The discount is applied only to
the units `mapping.json`'s `batch.fields` names: `in_per_mtok`, `out_per_mtok`,
`per_1k_pages` and `per_1k_annotated_pages`, the units of models Mistral's batch
documentation names an endpoint for. Left out on purpose: `per_audio_minute` (two of
those models are realtime, not batchable), `per_mchars` (speech generation is not a batch
endpoint), `cache_read_per_mtok` (nothing says the two discounts stack), and every
product. A missing figure is a gap; a wrong one is a wrong bill.

## The Voice Agents API

Priced on the API pricing page only, in its "Voice agents" table: "Pricing for the Voice
Agents API, billed per minute of conversation", $0.04 a minute with Mistral Small 4 and
$0.08 with "Larger models" on 2026-11-10. Each row is a product, keyed `voice agents
(<row>)`, in `per_conversation_minute`. The page does not say which models "larger
models" covers. Where the section is there, it is read strictly: the run fails if it stops
saying it is billed per minute of conversation, or if a row stops reading as a price per
minute. A new section on that page that lists no model is reported, since a price Mistral
adds there would show in one.

**On 2026-11-26 the section went.** The page now renders its own model-family tables
("Generalist models", "Audio models", ...) and states no per-conversation-minute figure
anywhere -- "Voice agents" survives only as a navigation link -- so the price cannot be
read from anything left, and is not guessed at. Missing the section is not fatal: the rows
`mapping.json` names in `voice_agents.rows` are published **sold-but-unpriced** instead,
the same treatment the products get -- still sold, with the last figure observed kept
beside the marker, and reported once -- rather than dropping entries Mistral still bills
or taking every model price down over one unreadable table. Reading is attempted first, so
a price stated in the old shape again is read rather than missed.

## The products: sold, and no longer priced

Web search, code execution, image generation, libraries, data capture, premium news and
the two classifier models are billed, and since Mistral retired its pricing page in
October 2026, no page states their price. They stay published with `"kind": "product"`
and `unpriced_since`, beside the last prices actually observed: still sold, not to be
called without checking the cost first. `mapping.json`'s `products` is their whole
inventory now, the one list a human keeps; a name added there is published, unpriced, on
the next run. Do not rename an entry: a rename publishes a new product and freezes the
old one.

## What the mapping holds

No model list. It holds the translations for what the documentation states in words:

- `denominators`: each unit string a model page states, per side, and its field
  (`"/M Tokens"` on the input side is `in_per_mtok`). A unit not in it is reported and
  that figure left out; the model's other prices still publish.
- `labels`: a row that names itself states the unit itself, and that overrides `unit`.
  Two shapes: `Cached input` is `cache_read_per_mtok`, and the older pages that write
  `Input (/M tokens)` / `Output (/M tokens)` with no `unit` and both rows marked
  `"direction": "input"` are `in_per_mtok` / `out_per_mtok`.
- `batch`: the page the discount is read from, and the units it applies to.
- `voice_agents`: the API pricing page's section, its stated unit and its price form,
  plus `rows`, the row labels to publish sold-but-unpriced when the section is gone.
- `products`: see above.

## Two things worth knowing

- **A figure of 0 means that side is not billed.** Voxtral TTS bills the audio it makes
  and nothing for the text it is given. The pages state `isFree` separately, so a 0 is not
  ambiguous here: that side is simply left out.
- **OCR bills annotated pages at a higher rate**: those models carry both
  `per_1k_pages` and `per_1k_annotated_pages`.

## What is not published, and why

- **The products' current prices**: no page states them.
- **EUR prices.** The pages state both, and the two are separate prices, not a
  conversion; this block publishes USD.
- **The API pricing page's model sections**: they summarise the model pages, without
  each figure's unit or the `status`, and its batch, EUR and regional switches are
  computed in the browser, not in the page fetched.
- **The Voice Agents price**: no page states it any more; the rows publish
  sold-but-unpriced with the last figure observed, as above.

## Running it yourself

```sh
python3 scripts/providers/mistral/scrape.py --out-dir .ci-out          # live, about 25 pages
python3 scripts/providers/mistral/scrape.py --out-dir .ci-out --offline tests/fixtures/mistral/offline.json
```

`--offline` serves every URL from the committed fixtures and refuses any URL the manifest
does not name, so a test cannot reach the network by accident.
