# The file: `pricing.json`

Everything a program reading `pricing.json` needs: its shape, what each field means,
how entries are keyed, and the markers that say a price is no longer a live quote.

```text
https://raw.githubusercontent.com/Simon-LM/ai-pricing/main/pricing.json
https://raw.githubusercontent.com/Simon-LM/ai-pricing/main/pricing.schema.json
```

## Shape

```json
{
  "schema_version": 2,
  "providers": {
    "mistral": {
      "checked_utc": "2026-11-10T09:33:44Z",
      "updated": "2026-11-10",
      "source": "https://docs.mistral.ai/models",
      "sources": ["https://docs.mistral.ai/models", "..."],
      "currency": "USD",
      "models": {
        "mistral small 4": {
          "in_per_mtok": 0.15,
          "out_per_mtok": 0.6,
          "in_per_mtok_batch": 0.075,
          "out_per_mtok_batch": 0.3,
          "batch_derived": "50% off the stated rate, as Mistral's batch API documentation states",
          "display_name": "mistral small 4",
          "api_ids": ["mistral-small-2603", "mistral-small-latest"]
        },
        "voxtral mini transcribe 2": {
          "per_audio_minute": 0.003,
          "display_name": "voxtral mini transcribe 2",
          "api_ids": ["voxtral-mini-2602", "voxtral-mini-latest"],
          "absent_since": "2026-11-02"
        }
      }
    }
  }
}
```

| field | meaning |
| --- | --- |
| `schema_version` | A consumer that does not recognise this number must ignore the file and fall back, rather than misread it. Bumped on any breaking change; see [The schema](#the-schema). |
| `providers` | Keyed by provider (`mistral`, `openai`...). Never flatten it: the same model at two providers is two different prices, not a collision to resolve. |
| `providers.<p>.checked_utc` | When that provider's figures were last committed as verified: the day a figure changed, and every Monday. The check itself runs every day. |
| `providers.<p>.updated` | When that provider's figures last actually **changed**. |
| `providers.<p>.source` | The page the prices come from, so a human can check them in one click. Always present. |
| `providers.<p>.sources` | Every page the block is built from, `source` first. Several where a provider's prices are spread over pages: Mistral, Anthropic and Perplexity today. |
| `providers.<p>.currency` | Never assume it, and never assume it matches another provider's. Nothing here converts currencies. |
| `providers.<p>.models` | The entries, keyed by what identifies them at that source; see [Keys](#keys). |
| `display_name` | A human-readable label, kept so that a diff reads well. Never match on it. |
| `api_ids` | The strings to pass **as the model** when calling that provider, most specific first. Present only where the source states them; see [api_ids](#api_ids-what-to-call). |
| `kind` | Absent on a model, the normal case. `"product"` marks a billable thing that is not a model: a tool such as web search, a storage price, a session. |
| `free` | Present, and always `true`, when the provider gives the model away. The entry then carries no price field at all. |
| `absent_since` | The provider stopped offering this entry on that day; every price beside it is the last one observed. See [Entries that are not live quotes](#entries-that-are-not-live-quotes). |
| `unpriced_since` | The provider still offers this entry, still calls it paid, and states no price, as of that day. |
| `disputed_since` | The source lists this entry twice at two different prices, as of that day; the price beside it is the last one published, until a human picks the real one. |
| `batch_derived` | Present when a `_batch` figure was **worked out** from a rule the source states in words, rather than read. Its text says the rule. |
| `long_context_threshold` | Present exactly when the entry carries a `_long_context` price: the prompt size, in tokens, from which a whole request is billed at those prices. |

Every other field is a price, a plain number, in the unit its name says.

## Keys

**The key is whatever identifies the entry at its source**, which is not the same thing
at every provider:

| provider | key | example |
| --- | --- | --- |
| `anthropic` | the API id the model deprecations page states; a tool's name | `claude-opus-5-5`, `claude-opus-4-5-20251101`, `web_search` |
| `openai` | the API id the pricing page states; `<id> (flex)`, `(fast)`, `(ultrafast)` for another speed; a tool's name | `gpt-6-sol`, `gpt-6-sol (flex)`, `web_search` |
| `google` | the API id the pricing page states; `<id> (flex)`, `(priority)` for another speed; grounding by model generation | `gemini-2.5-flash`, `gemini-2.5-flash (flex)`, `google_search (gemini 2.5)` |
| `googlecloud` | the row the pricing page states, lowercased, with its volume tier: the page names no API id per price | `speech-to-text v2 recognition standard (0 to 500,000 min/month)` |
| `mistral` | the name its documentation states, lowercased | `mistral small 4`, `ocr 4.0` |
| `ovh` | the API model id, the catalog's own `name` | `Qwen3.6-27B` |
| `scaleway` | the API model id, the catalogue's own `product` | `glm-5.3` |
| `xai` | the API model id; `<id> (<resolution>[, <quality>])` for an image or video price; a tool's id | `grok-4.7`, `grok-imagine-image-2.0 (1K, low)`, `x_search` |
| `perplexity` | the id the Agent API takes, `provider/model`; a tool's `type` | `xai/grok-4.7`, `web_search` |
| `edenai` | Eden AI's model id, upstream prefix included | `mistral/codestral-latest` |
| `huggingface` | the exact string the router takes, `<model>:<partner>` | `openai/gpt-oss-120b:ovhcloud` |

To price a call, you need both the provider you called and the model, and read
`providers.<provider>.models.<key>`. There is no provider-less lookup, on purpose.

### `api_ids`: what to call

**These are model identifiers, not endpoints.** The identifier goes in the request body,
as the `model` field; this file publishes no URLs.

```json
"api_ids": ["mistral-ocr-4-1", "mistral-ocr-4", "mistral-ocr-latest"]
```

**Order is meaning: most specific first.** The first pins a version and keeps costing
what this file says. The last is often a `-latest` alias, which resolves to another
model, at another price, whenever the provider decides. Pin the first.

The field is absent when there is nothing more to say:

- on a model keyed by the exact string its API takes (every provider but `mistral` and
  `googlecloud`), the key is the identifier. `api_ids` appears only when the source
  states more: xAI's aliases (`grok-code-fast-1` is `grok-build-0.1`), the shorter alias
  Anthropic accepts for a dated id, the model behind a speed entry (`gpt-6-sol (flex)`
  is called as `gpt-6-sol`);
- on a `"kind": "product"` entry, there is nothing to call.

Mistral publishes it on every model because its keys are names for people, not for
callers.

**One id can appear in several entries of a block.** A model's speeds (`gpt-6-sol`,
`gpt-6-sol (flex)`) and its image resolutions (`grok-imagine-image-2.0 (1K, low)`,
`(2K, medium)`...) are entries of their own, all called with the same id, each at its
own price. The key says which price applies; the id alone does not.

## Units

The unit is part of the field name, so a per-token price cannot be applied to a per-page
model by mistake. There is no generic `price` field.

| field | billed |
| --- | --- |
| `in_per_mtok` | per million input tokens (text) |
| `out_per_mtok` | per million output tokens (text) |
| `per_mtok` | per million tokens, with no input/output split |
| `cache_read_per_mtok` | per million tokens read back from a prompt cache |
| `cache_write_5m_per_mtok`, `cache_write_1h_per_mtok` | per million tokens written to a prompt cache kept 5 minutes or 1 hour (Anthropic) |
| `cache_write_30m_per_mtok`, `cache_write_12h_per_mtok` | per million tokens written to a prompt cache kept 30 minutes or 12 hours (OpenAI) |
| `cache_storage_per_mtok_hour` | per million tokens kept in a context cache, per hour (Google) |
| `audio_in_per_mtok`, `audio_out_per_mtok`, `audio_cache_read_per_mtok` | the same three rates for audio tokens, where a model prices them apart from text |
| `image_in_per_mtok`, `image_out_per_mtok`, `image_cache_read_per_mtok` | the same for image tokens |
| `video_in_per_mtok`, `video_out_per_mtok`, `video_cache_read_per_mtok` | the same for video tokens |
| `index_per_mtok` | per million tokens indexed |
| `train_per_mtok` | per million tokens trained on, one-off |
| `per_1k_pages` | per thousand pages |
| `per_1k_annotated_pages` | per thousand pages that are also annotated (OCR bills these higher) |
| `per_1k_chars` | per thousand characters |
| `per_mchars` | per million characters |
| `per_audio_minute`, `per_audio_second` | per minute or second of audio sent |
| `per_conversation_minute` | per minute of conversation with a voice agent |
| `per_text_input` | per text input sent into a real-time voice conversation (xAI) |
| `per_image` | per image |
| `per_input_image` | per image sent in (xAI's image and video models) |
| `per_1k_images` | per thousand images |
| `per_video_second`, `per_input_video_second` | per second of video made, or sent in |
| `per_song` | per song generated (Google's Lyria) |
| `per_call`, `per_1k_calls` | per call, per thousand calls |
| `per_1k_posts`, `per_1k_profiles` | per thousand posts, or user profiles, returned (xAI's X search) |
| `per_session` | per session (a code sandbox or container, billed by session) |
| `per_session_hour`, `per_container_hour` | per hour an agent session, or a code execution container, runs |
| `per_gb_day` | per gigabyte stored, per day |
| `per_model_month` | per month, per stored model |

**A model can carry several.** Voxtral Small is billed per minute of audio and per
million tokens of text; a real-time model per audio token and per text token. Reading
only one undercounts a bill without looking wrong: read every field you find.

**A `_batch` suffix is the same unit, in batch**: deferred, usually cheaper.
`in_per_mtok_batch` is per million input tokens in batch. Half is the usual discount but
not a rule.

**A `_long_context` suffix is the rate past a prompt-size threshold.** Some providers
bill a request whose prompt reaches a size at a higher rate, for every token in it. The
entry then carries both rates and the size:

```json
"in_per_mtok": 1.25,
"in_per_mtok_long_context": 2.5,
"long_context_threshold": 200001
```

The threshold is the first prompt size billed at the higher rate: 200001 means a prompt
of 200,000 tokens is still billed at the lower one. The two suffixes combine in that
order: `in_per_mtok_batch_long_context`.

**`batch_derived` says a `_batch` figure was worked out.** Most sources state their batch
rates and they are read as stated. Mistral states one sentence, "We offer a 50% discount
on batch API", and xAI one discount per model in a table; their `_batch` figures are this
repository's arithmetic on that rule, and every entry carrying one says so in
`batch_derived`. The rule is read on every run, so the figures follow it if it changes.

**A free model is `"free": true`, never a price of 0.** Zero is also what a broken parser
reads off a page whose layout moved, and this repository refuses a figure of 0 for that
reason. Treat an entry with `free` as costing nothing, not as missing data.

## Entries that are not live quotes

| marker | the provider | the price beside it |
| --- | --- | --- |
| `absent_since` | no longer offers it | the last one observed, frozen |
| `unpriced_since` | still offers it, still charges for it, states no price | the last one observed, or none at all |
| `disputed_since` | lists it twice at two prices | the last one published, until a human picks |

**An entry that goes away is kept a year**, with the last prices actually observed and
the day it was first missed. Deleting it on sight would leave a project that still names
it with no price and no way to learn what it used to cost. If the provider offers it
again, the marker goes.

**An entry sold without a price is published, not dropped.** "Sold, not free, and nobody
says what it costs" is not "does not exist": a program can tell its user to check the
cost before calling it. Mistral's tools (web search, code execution, image generation...)
are in this state since Mistral retired the page that priced them in October 2026.

**The date does not move while the marker stays**, and each marker is reported once,
by email, on the run that first sees it.

**A marker at one provider says nothing about another.** A model withdrawn under
`mistral` may still be sold, and callable, under `edenai`: a reseller has its own
catalogue and its own contracts. Read the block of the provider you actually call.

What to do with them: leave them out of a list of models a user can pick, and show the
date when you display a price found under one.

## The trap: aliases

**An id can keep its name and change its price.** `mistral-small-latest`, `grok-latest`
and every other alias resolves to one model today and to another tomorrow, at another
price. Eden AI's `xai/grok-latest` moved from Grok 4.5 to Grok 4.6 in one week, and in
this file that showed as a price change on an unchanged key, which is exactly what it
was. Nothing here can tell you that the model behind an alias is not the one you tested.

## The schema

The schema is published beside the file, so that what a `schema_version` means can be
read without reading Python. **It is generated**, from `scripts/pricing_validate.py`,
which stays the authority, and a test fails if the two drift apart.

Validating against it checks the shape, the units, and that every figure lies in the
plausible range for its unit. It cannot check two rules this repository also enforces,
because neither is a property of one document:

| rule | why the schema cannot state it |
| --- | --- |
| a figure may not move by more than a factor of 5 in one run | it compares with the **previous** published file |
| a block's currency matches its mapping | the mapping is not published |

**Within a `schema_version`, no field is ever removed.** Fields are added, as every unit
above was. When something must break in a way an addition cannot cover, the version is
bumped.

## Consuming it

- Ship a copy of the file with your program, so a machine with no network still has
  dated figures.
- Refresh it at most once a day, cache it, never block on it, and let the user turn it
  off.
- Convert currencies, if you must, at display time only.
- Always show the date beside a price: `checked_utc` of its block, or the marker's date.
