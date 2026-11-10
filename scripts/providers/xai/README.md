# xAI

`scrape.py` reads <https://docs.x.ai/developers/pricing> once a week (Mondays,
07:30 UTC, `.github/workflows/refresh-xai.yml`) and works out what `providers.xai`
should say. No API key: the page is public.

## What it reads

The page is a Next.js document. Besides the tables a human reads, it carries xAI's
whole public catalogue as data, in a script that sets
`globalThis.__XAI_PUBLIC_MODELS__`: per cluster, every language model with its prices,
its aliases and its long-context threshold, and every audio model with its prices.
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
| tools table, "$5 / 1k calls" and the like | dollars per thousand | `per_1k_calls`, `per_1k_posts`, `per_1k_profiles`, `"kind": "product"` |

Every unit was checked against the page's own human-readable table on 2026-11-09:
grok-4.7 at $2.00 / $6.00 / $0.50 cached, $4.00 / $12.00 / $1.00 past 200k tokens;
speech to text at $0.10 per hour ($0.20 streaming); text to speech at $15.00 per
million characters.

## What is not published, and why

- **Other clusters.** Prices differ by cluster: on 2026-11-09 `us-central-1` charged
  10% more for grok-4.6 and grok-4.7. One cluster is published, `us-east-1`, the one
  every language model is listed in (`cluster` in `mapping.json`).
- **Image and video generation, real-time voice.** Billed per image, per second of
  video, per audio token or per minute of phone line: units this file does not
  represent yet.
- **Tools billed as tokens or elsewhere** ("Token-based", "Imagine API rates"):
  there is no per-call price to publish.
- **The batch discount** (`batchDiscountPercent`, 20% on 2026-11-09): stated per
  model, to be published as derived `_batch` figures in a later change.
