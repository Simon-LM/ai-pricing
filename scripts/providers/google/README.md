# Google (Gemini API)

`scrape.py` reads Google's public Gemini API pricing page and works out what
`providers.google` should say (`.github/workflows/refresh-google.yml`). No API key: the
page is public. These are the prices of the Gemini Developer API (Google AI Studio), not
those of Vertex AI or Gemini Enterprise Agent Platform, which the page says may differ.

## The source: the page's Markdown

<https://aistudio.google.com/docs/pricing.md> (`ai.google.dev/gemini-api/docs/pricing`
redirects there). The rendered page builds its tables in the browser; the Markdown holds
them all. It is Markdoc: one `##` section per model or family, an italic line naming the
model ids, then one table per tab, each row a feature ("Input price", "Output price",
"Context caching price"...) with a free tier cell and a paid tier cell.

| tab | published as |
| --- | --- |
| Standard | the model's entry, `gemini-2.5-flash` |
| Batch | the same entry, in the `_batch` fields |
| Flex, Priority | an entry of its own, `gemini-2.5-flash (flex)`, whose `api_ids` is the model's id |
| Contributor | not published: Google offers it by invitation only |

A section that names several ids (the Live models) gives each the same prices. A section
whose rows each name a model (Lyria 3) gives each id its row, in order.

## The paid tier cell: prose, read clause by clause

The prices are written as sentences, and each dollar figure is read with what follows it:

| the page says | published as |
| --- | --- |
| `$0.25 (text / image / video) $0.50 (audio)` | `in_per_mtok`, `image_in_per_mtok` and `video_in_per_mtok` 0.25, `audio_in_per_mtok` 0.5 |
| `$1.25, prompts <= 200k tokens` `$2.50, prompts > 200k tokens` | `in_per_mtok` and `in_per_mtok_long_context`, `long_context_threshold` 200001 |
| `$2.00 through November 30, 2026.` `$4.00 starting December 1, 2026.` | the price valid on the day of the run |
| `$1.00 / 1,000,000 tokens per hour (storage price)` | `cache_storage_per_mtok_hour` |
| `$0.039 per image`, `$0.08 per song` | `per_image`, `per_song` |

The kind of token sets the field's prefix: none for text, `image_`, `audio_`, `video_`.
A price with no kind stated is the text price.

**Estimates are never published.** Google gives some prices "or $0.005/min", in a
parenthesis "($0.00012 per image)", or "Equivalent to $0.0336 per 1K image": the page's
own footnotes say these are worked out from the token prices (for Transcribe, from 25
audio tokens a second). They are taken out before reading; the token prices are what is
billed and what is published.

**A figure this cannot place fails the run.** After a price, only its kinds of token,
its prompt length, its dates and the words `mapping.json` lists in
`prose_without_price` may follow ("Implicit caching only (explicit caching is not
supported)."). A kind of token, a row label or a tab the mapping does not know is
reported or fails, never guessed at.

## Dated prices

Several models are sold at a promotional price until a date, then at a higher one:
Gemini 4 Argon at $2.00 per million input tokens through 2026-11-30, $4.00 from
2026-12-01; Gemini 3.8 Flash at $0.75 through 2026-12-31, $1.50 from 2027-01-01. The
price published is the one valid on the day of the run (`provider_runner.run_date()`).
The run fails if the dates leave a day with no price.

## The tools

| key | price on 2026-11-10 | free before it |
| --- | --- | --- |
| `google_search (gemini 2.5)` | $35 per 1,000 grounded prompts | 1,500 requests a day (shared by Flash and Flash-Lite) |
| `google_search (gemini 3 and newer)` | $14 per 1,000 search requests | 5,000 a month, shared by all Gemini 3 and newer models |
| `google_maps (gemini 2.5)` | $25 per 1,000 grounded prompts | 1,500 requests a day; 10,000 for Pro |
| `google_maps (gemini 3 and newer)` | $14 per 1,000 search queries | 5,000 a month |

All in `per_1k_calls`. The free allowances have no field: they are only written here.
Search grounding on Gemini 3 and newer models is also capped at 1,500 requests a day per
project. Code execution, URL context, computer use and file search are billed as tokens
(file search at the embedding model's rate): no price of their own.

## What is not published, and why

- **The Contributor tier**, offered by invitation only, whose content is used to improve
  Google's products.
- **Agents** (Deep Research, managed agents, Antigravity): their tool and token use is
  billed as above, and their compute is not billed during the preview.

**Gemma 4 is published as free** (`"free": true`): the page offers it in the free tier
only, "Free of charge", and not in the paid tier. In the free tier, Google uses what is
sent to improve its products: no confidentiality is promised. The page states no API id
for it; it is keyed `gemma-4`, the section's own anchor.

## Checked on 2026-11-10

Against the page, by a human: the dated prices, the prices by kind of token and by prompt
length, the per-item prices, the speeds, Gemma, the grounding prices. The tests run the
fixture both on 2026-11-10 and on 2026-12-01, when Argon's price doubles.
