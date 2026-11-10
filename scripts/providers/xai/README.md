# xAI

`scrape.py` reads <https://docs.x.ai/developers/pricing> once a week (Mondays,
07:30 UTC, `.github/workflows/refresh-xai.yml`) and works out what `providers.xai`
should say. No API key: the page is public.

## What it reads

The page is a Next.js document. Besides the tables a human reads, it carries xAI's
whole public catalogue as data, in a script that sets
`globalThis.__XAI_PUBLIC_MODELS__`: per cluster, every language model with its prices,
its aliases and its long-context threshold, every audio model, and every image and
video generation model, each with its prices.
That object is read through `scripts/nextjs_flight.py`, not the rendered tables, so a
change of layout does not move a figure.

The server-side tools are the exception: they are priced only in the table under
"server-side tool invocations", so that table is read too.

## Units

| source field | unit as stated | published as |
| --- | --- | --- |
| `promptTextTokenPrice`, `completionTextTokenPrice`, `cachedPromptTokenPrice` and their `LongContext` twins | US cents per 100 million tokens (xAI's API reference, `GET /v1/language-models`) | `in_per_mtok`, `out_per_mtok`, `cache_read_per_mtok`, and the `_long_context` fields, divided by 10 000 |
| `longContextThreshold` | prompt tokens | `long_context_threshold` |
| `perAudioSecond`, `perAudioSecondStreaming` (`grok-voice-transcribe-2.0`) | 1e-10 dollars per second | `per_audio_second`, the streaming rate under `grok-voice-transcribe-2.0 (streaming)` |
| `perCharacter` (`grok-tts`) | 1e-10 dollars per character | `per_mchars` |
| `realtimeAudioSecondPrice` (`grok-voice-3.0`, `grok-voice-2.0`) | 1e-10 dollars per second of conversation | `per_conversation_minute`, times 60 |
| `realtimeTextInputPrice` (the same) | 1e-10 dollars per text input | `per_text_input` |
| `resolutionPricing[].pricePerImage` (image models) | 1e-10 dollars per image made, per resolution and quality | `per_image`, one entry per row: `grok-imagine-image-2.0 (1K, low)` |
| `resolutionPricing[].pricePerSecond` (video models) | 1e-10 dollars per second of video made, per resolution | `per_video_second`, one entry per row: `grok-imagine-video-1.5 (480p)` |
| `pricePerInputImage`, `pricePerInputVideoSecond` | 1e-10 dollars per image, per second of video, sent in | `per_input_image`, `per_input_video_second`, on each of the model's entries |
| tools table, "$5 / 1k calls" and the like | dollars per thousand | `per_1k_calls`, `per_1k_posts`, `per_1k_profiles`, `"kind": "product"` |

Every unit was checked against the page's own human-readable tables on 2026-11-09:
grok-4.7 at $2.00 / $6.00 / $0.50 cached, $4.00 / $12.00 / $1.00 past 200k tokens;
speech to text at $0.10 per hour ($0.20 streaming); text to speech at $15.00 per
million characters. And on 2026-11-10, every image, video and real-time voice figure
published: grok-imagine-image-2.0 at $0.04 to $0.08 an image, $0.01 per image sent in;
grok-imagine-video-1.5 at $0.08, $0.14 and $0.25 a second in 480p, 720p and 1080p;
grok-voice-3.0 at $0.08 a minute and $0.004 per text input.

A figure scaled from the source's unit keeps six significant digits. The catalogue
states real-time voice at 13333333 a second, which is $0.0799999980 a minute; the page
states $0.08, and $0.08 is what is published.

**Images and video are published one entry per resolution**, and per quality where
the model states one, because the price depends on both. Each entry also carries what
the model charges for what it is given, so that one entry prices a call:
`grok-imagine-image-2.0 (1K, low)` is $0.04 for the image made and $0.01 for each image
sent in. Every entry of a model lists the model's id and aliases in `api_ids`; the
resolution and quality are request parameters.

## What is not published, and why

- **Other clusters.** Prices differ by cluster: on 2026-11-09 `us-central-1` charged
  10% more for grok-4.6 and grok-4.7. One cluster is published, `us-east-1`, the one
  every language model is listed in (`cluster` in `mapping.json`).
- **Real-time voice per audio token** (`realtimeAudioTokenPrice`). It is the
  per-minute rate again: 13333333 a second over 266667 a token is exactly 50 tokens a
  second, so publishing both would count one conversation twice.
- **The phone line** (`pstnMinutePrice`, $0.01 a minute in the catalogue). No page xAI
  shows a customer states it (pricing, voice, speech to speech and SIP phone calls,
  read on 2026-11-10), so nothing confirms its unit.
- **`imagePrice`**, one figure per image model that the page's table does not show;
  the table prices per resolution and quality, and so does this block.
- **Batch rates for images and video.** The page says they are billed at standard
  rates in the Batch API.
- **Tools billed as tokens or elsewhere** ("Token-based", "Imagine API rates"):
  there is no per-call price to publish.
- **Long-context rates in batch.** The discount applies to every token type, but the
  format has no field for a long-context batch rate.

## Batch

The page states a batch discount per model in a table, "Batch discounts by model", and
says it applies to every token type. For each model listed there, `in_per_mtok_batch`,
`out_per_mtok_batch` and `cache_read_per_mtok_batch` are worked out from it, and the
entry says so in `batch_derived`: 20% on 2026-11-09, so grok-4.7's $2.00 input is $1.60
in batch. The table wins over the catalogue's own `batchDiscountPercent`: on 2026-11-09
the catalogue gave grok-build-0.1 20%, while the table left it out under the sentence
"Models not listed above have no batch discount". A page without the table still
publishes every other price, with no batch figure, and the run says so in a note.
