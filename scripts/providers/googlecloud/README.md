# Google Cloud (Speech-to-Text)

`scrape.py` reads Google Cloud's public Speech-to-Text pricing page every day (02:30 UTC,
`.github/workflows/refresh-googlecloud.yml`) and works out what `providers.googlecloud`
should say. No API key: the page is public.

Google Cloud is a block of its own, apart from `providers.google`: the Gemini API (Google
AI Studio) and Google Cloud are two products, billed apart. This block holds
Speech-to-Text; its Gemini-based transcription models (Gemini 3.5 Transcribe) are priced
in `providers.google`.

## What it reads

<https://cloud.google.com/speech-to-text/pricing> is rendered on the server. Under a
heading per API version, "Speech-to-Text V2 API" and "Speech-to-Text V1 API", each table
is headed Category | Model | Price (USD), then a column per commitment plan. Only the
price column is read. Every price must read "$N / 1 minute"; a price in another unit, a
range of minutes written another way, or a price table under no version heading fails
the run. A table headed another way is reported in the run's notes and not read.

## Volume tiers: one entry per tier

V2's standard recognition is priced by the minutes of audio processed in the month, per
account. Each tier is an entry of its own, holding a single price, its range in the key:

| key | price per minute on 2026-11-10 |
| --- | --- |
| `speech-to-text v2 recognition standard (0 to 500,000 min/month)` | $0.016 |
| `speech-to-text v2 recognition standard (500,000 to 1,000,000 min/month)` | $0.010 |
| `speech-to-text v2 recognition standard (1,000,000 to 2,000,000 min/month)` | $0.008 |
| `speech-to-text v2 recognition standard (2,000,000+ min/month)` | $0.004 |
| `speech-to-text v2 dynamic batch recognition standard` | $0.003 |
| `speech-to-text v1 speech recognition with data logging standard` | $0.016 |
| `speech-to-text v1 speech recognition without data logging standard` | $0.024 |
| `speech-to-text v1 medical dictation` | $0.078 |
| `speech-to-text v1 medical conversation` | $0.078 |

All in `per_audio_minute`. A month's minutes are billed tier by tier: the first 500,000
at $0.016, the next 500,000 at $0.010, and so on. Dynamic batch processes audio "at a
lower level of urgency", for a lower price.

**With or without data logging** (V1): with data logging, Google keeps the audio to
improve its models, and the minute is cheaper.

## What is not published, and why

- **The free minutes.** V1 gives the first 60 minutes of each month free, per account,
  for standard and medical models; a free tier is not an entry.
- **The commitment plans** (Gemini Enterprise Flexible Savings Plan, 1 or 3 years): the
  page's other price columns, about 10% and 20% below the standard price on 2026-11-10.
- **API ids.** The page names none per price. "Standard" covers the models `default`,
  `command_and_search`, `latest_short`, `latest_long`, `phone_call`, `video` and `chirp`
  (V2 only); "Medical", `medical_conversation` and `medical_dictation`. These are request
  parameters billed at the same price, so the keys name the row, not a model.
- **What the page bills apart**: each audio channel is billed separately, a request is
  rounded up to the next second, and Cloud Storage or other services used alongside are
  billed by their own pages.

## Checked on 2026-11-10

Against the page, by a human: every price above. The tests check that the commitment
plans' prices ($0.0144, $0.0128...) are never published.
