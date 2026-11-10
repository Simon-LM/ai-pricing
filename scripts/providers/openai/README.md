# OpenAI

`scrape.py` reads OpenAI's public pricing page once a week (Mondays, 05:30 UTC,
`.github/workflows/refresh-openai.yml`) and works out what `providers.openai` should say.
No API key: the page is public.

## The source: the page's Markdown

OpenAI serves every docs page as Markdown too, at the page's address plus `.md`:
<https://developers.openai.com/api/docs/pricing.md>. It holds every tab of the rendered
page at once, as plain Markdown tables, with each model under the id the API takes. That
is what is read. (`openai.com/api/pricing` refuses automated readers; the docs page does
not.)

The page is a run of sections, each with one table per speed. A table belongs to the
last section and the last speed written above it, and is recognised by both and by its
header row, word for word, as `mapping.json`'s `tables` states them:

| section | speeds | header | published as |
| --- | --- | --- | --- |
| Flagship models | all five | Model, then input, output, cached input, 30m write, 12h write, for short and long context | `in_per_mtok`, `out_per_mtok`, `cache_read_per_mtok`, `cache_write_30m_per_mtok`, `cache_write_12h_per_mtok`, and their `_long_context` twins |
| Flagship models | all five | Model, Input, Output, Cached input | the same three fields |
| Images | Standard, Batch | Model, Modality, Input, Output, Cached input | text tokens in the plain fields, image tokens in `image_in_per_mtok`, `image_out_per_mtok`, `image_cache_read_per_mtok` |
| Realtime & Audio | Standard | the same | text, `audio_*` and `image_*` fields; `tts-1` and `tts-1-hd` in `per_mchars` |
| GPT-Live | Standard | Model, Price per minute | `per_conversation_minute` |
| Transcription | Standard | Model, Use case, Input, Output, Estimated cost | see below |
| Specialized models | Standard, Fast | Category, Model, prices | the same fields as the flagship models |
| Tools | — | Tool, Details, Pricing | per row, see below |

A table the page has and the mapping does not name is reported in the run's notes, and
its prices are not published: a unit is read from a header, and a new header is not a
unit this can trust. A table the mapping requires (`required_tables`) that the page
lacks fails the run, and so does a cell that is not a price (`$N`) or a dash.

## The speeds

OpenAI sells most models at several speeds, each at its own price:

| speed | published as |
| --- | --- |
| Standard | the model's entry, `gpt-6-sol` |
| Batch | the same entry, in the `_batch` fields: `in_per_mtok_batch`, `cache_write_30m_per_mtok_batch_long_context`... |
| Flex, Fast, Ultrafast | an entry of its own, `gpt-6-sol (flex)`, `gpt-6-sol (fast)`, `gpt-6.1-sol (ultrafast)`, whose `api_ids` is the model's id |

Flex, Fast and Ultrafast are the same model, asked for with a setting of the request,
not another model: hence their own entry, as Anthropic's fast mode has. The page says
Fast was called Priority processing until 2026-07-30.

## Long context

The flagship tables price each model twice, "Short context: ≤272K input tokens. Long
context: >272K input tokens." The second set is published in the `_long_context` fields,
with `long_context_threshold` 272001: a prompt of 272,000 tokens is still billed at the
short context rates. K is read as 1,000, as Perplexity's pages use it for the same
OpenAI models. The threshold is read from that sentence on every run; a model name that
carries its own, "gpt-5.5 (<272K context length)", must agree with it, and the note is
not part of the id.

## Tokens that are not text

A model that takes or makes audio or images bills those tokens at their own rates. The
page states them row by row, a modality per row; the entry carries them all, so that one
entry prices a call:

```json
"gpt-realtime-2": {
  "in_per_mtok": 4.0, "out_per_mtok": 24.0, "cache_read_per_mtok": 0.4,
  "audio_in_per_mtok": 32.0, "audio_out_per_mtok": 64.0, "audio_cache_read_per_mtok": 0.4,
  "image_in_per_mtok": 5.0, "image_cache_read_per_mtok": 0.5
}
```

## Transcription

A model priced in tokens (`gpt-4o-transcribe`, $2.50 / $10.00 per million on 2026-11-10)
publishes its token prices in `in_per_mtok` and `out_per_mtok`, as the page states them,
without saying which kind of token the input is; its cost per minute is, in the page's
own words, an estimate, and is not published. A model priced only per minute (Whisper,
`gpt-transcribe`, the live transcription and translation models) publishes that, in
`per_audio_minute`. The page names Whisper "Whisper", not by an id: it is keyed `whisper`.

## The tools

| key | price on 2026-11-10 | field |
| --- | --- | --- |
| `web_search` | $10 per 1,000 calls | `per_1k_calls` |
| `web_search_preview (reasoning models)` | $10 per 1,000 calls | `per_1k_calls` |
| `web_search_preview (other models)` | $25 per 1,000 calls | `per_1k_calls` |
| `containers (1 GB)` ... `containers (64 GB)` | $0.03, $0.12, $0.48, $1.92 per 20-minute session | `per_session` |
| `file_search` | $2.50 per 1,000 calls | `per_1k_calls` |
| `file_search (storage)` | $0.10 per GB per day | `per_gb_day` |
| `chatkit (storage)` | $0.10 per GB per day | `per_gb_day` |

Each row is named in `mapping.json`'s `tools` with the pattern its price must match. A
row named there that the table lacks, or prices another way, fails the run; a row not
named there is reported and not read. The search content tokens the web search tools add
are billed at the model's rates (free with the preview tool on non-reasoning models).

## What is not published, and why

- **Regional processing and FedRAMP endpoints**, charged a 10% uplift for models released
  on or after 2026-03-05. A multiplier, not a price; every figure above is the global
  one.
- **The free storage**: file search and ChatKit storage give the first GB free (ChatKit's,
  per account per month). Only the price past it is published.
- **Containers are billed by the minute**, with a 5-minute minimum per session: the page
  states the price per 20-minute session, and that is what is published.
- **Promotional prices** are published as the page states them, with no end date in the
  file: GPT-5.6 Sol's until 2026-11-30, GPT-6 Astra for Law's at least through
  2026-12-31. The run publishes the new price the week the page changes it.
- **The cost of a generated image.** The page sends to a calculator in the image
  generation guide; the token prices it builds on are published.
- **The models sold only to approved organisations**: the Specialized models whose
  category says "Trusted Access" (on 2026-11-10, Daybreak's cybersecurity models and
  Rosalind's life sciences models). Read from that category, as `restricted_marker`
  names it, not from a list of models.
- **Fine-tuning**, which the page says is closed to new users: its section is not read.
- **The cloud platforms** (Amazon Bedrock, Microsoft Azure), which bill OpenAI's models
  through their own pages.

## Checked on 2026-11-10

Against the page, by a human: the flagship tables at every speed, the image, realtime,
speech and transcription tables, the specialized models, the tools. The tests check every
row of the flagship models' plain Standard table against what is published.
