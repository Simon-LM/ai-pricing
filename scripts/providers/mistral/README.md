# Mistral

`scrape.py` reads Mistral's documentation site every day (04:00 UTC,
`.github/workflows/refresh-mistral.yml`) and works out what `providers.mistral` should
say. No API key: every page is public.

| what | source | read as |
| --- | --- | --- |
| the models, and their prices | <https://docs.mistral.ai/models>, then one page per model | the index's model cards; each page's price object and `isRetired` flag |
| the batch discount | <https://docs.mistral.ai/studio/batch-processing> | one sentence: "We offer a 50% discount on batch API" |
| the Voice Agents API | <https://docs.mistral.ai/inference/pricing> | its "Voice agents" table only |
| the tools and other products | `mapping.json`'s `products` list | no page states their price any more; see below |

## Why the documentation site

Mistral's API has no pricing endpoint, and the one usage endpoint that returns prices
needs an *admin* API key, which an open-source tool must never ask for. The documentation
site does better than a marketing page: each model's page streams a machine-readable
price object, with the unit stated per figure (`{"price": 4, "denominator": "/1000
Pages"}`), and an `isRetired` flag. The index lists the models currently offered, one
card each; the retired ones are listed further down as plain rows and are not read.

A marketing page is not an inventory: the pricing page this block was once read from
dropped a model Mistral was still selling. Mistral retired that page in October 2026.

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

The percentage is read on every run, so the figures follow it if Mistral changes it; if
the sentence goes, or states two rates, the run fails. The discount is applied only to
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
models" covers. The run fails if the section stops saying it is billed per minute of
conversation, if a row stops reading as a price per minute, or if the section goes. A
new section on that page that lists no model is reported, since a price Mistral adds
there would show in one.

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
- `labels`: a row that names itself overrides its unit (`Cached input` is
  `cache_read_per_mtok`).
- `batch`: the page the discount is read from, and the units it applies to.
- `voice_agents`: the API pricing page's section, its stated unit and its price form.
- `products`: see above.

## Two things worth knowing

- **A figure of 0 means that side is not billed.** Voxtral TTS bills the audio it makes
  and nothing for the text it is given. The pages state `free` separately, so a 0 is not
  ambiguous here: that side is simply left out.
- **OCR bills annotated pages at a higher rate**: those models carry both
  `per_1k_pages` and `per_1k_annotated_pages`.

## What is not published, and why

- **The products' current prices**: no page states them.
- **EUR prices.** The pages state both, and the two are separate prices, not a
  conversion; this block publishes USD.
- **The API pricing page's model sections**: they summarise the model pages, without
  each figure's unit or the retired flag, and its batch, EUR and regional switches are
  computed in the browser, not in the page fetched.

## Running it yourself

```sh
python3 scripts/providers/mistral/scrape.py --out-dir .ci-out          # live, about 25 pages
python3 scripts/providers/mistral/scrape.py --out-dir .ci-out --offline tests/fixtures/mistral/offline.json
```

`--offline` serves every URL from the committed fixtures and refuses any URL the manifest
does not name, so a test cannot reach the network by accident.
