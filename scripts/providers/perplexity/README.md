# Perplexity

`scrape.py` reads two of Perplexity's public pages every day (06:30 UTC,
`.github/workflows/refresh-perplexity.yml`) and works out what `providers.perplexity`
should say. No API key: both pages are public.

| what | source | read as |
| --- | --- | --- |
| the Agent API's models | <https://docs.perplexity.ai/docs/agent-api/models> | its tables, one per maker |
| tools, sandbox, Search API, embeddings, Decisions API | <https://docs.perplexity.ai/docs/getting-started/pricing> | the rates its pricing calculator carries as data |

## The models: the models page's tables

Each maker has a table headed `Model | Input ($/1M) | Output ($/1M) | Cache read ($/1M)`.
The unit is in the header: a table headed any other way is not read, and a page with no
such table fails the run. A price cell takes one of four forms, and any other fails the
run:

| cell | published as |
| --- | --- |
| `0.25` | the flat rate |
| `2.00 (<200k) 4.00 (≥200k)` | the rate, its `_long_context` twin, and `long_context_threshold` 200000 |
| `0.10 (≤272k) 0.20 (>272k)` | the same, with `long_context_threshold` 272001: the 272000th token is still billed low |
| `90% off input` (cache only) | worked out from the input rate, tier by tier |
| `—` (cache only) | no cache price |

A model whose prices change tier at two different sizes fails the run: the format has
one threshold per model.

## Everything else: the calculator's rates

The pricing page is built with Mintlify, which ships the compiled page as a JSON
string, `compiledSource`. In it, the page's pricing calculator starts from an object,
`const[defaultPricing]=useState(()=>({...}))`, that holds every rate the page uses.
Perplexity's own note in that object says the rates are checked against the API's own
pricing config. The tests check its figures against the tables a human reads on the
same page.

The object states its units in `_meta.units`. `mapping.json` states the units expected,
word for word, and the run fails if a section's unit changes, before a figure is read.

| section | unit as stated | published as |
| --- | --- | --- |
| `agent.tools` | $ per invocation | `per_1k_calls`, × 1000, `"kind": "product"` |
| `agent.webSearchTypes.fast` | $ per invocation of web_search with `search_type: "fast"` | `web_search (fast)`, `per_1k_calls` |
| `agent.sandbox` | $ per session (a 20-minute billing window), then $ per search made from inside it | `sandbox`: `per_session` and `per_1k_calls` |
| `search` | $ per 1,000 requests | `search_api` and `search_api (fast)`, `per_1k_calls` |
| `embeddings` | $ per 1,000,000 tokens | `in_per_mtok` |
| `decisions.models` | $ per 1,000,000 input tokens; output is free | `in_per_mtok` |

## Why the models are not read from the calculator

The object holds model rates too, under `agent.models`, and on 2026-11-10 they agreed
with the models page for every model they listed. But they did not list every model:
`perplexity/glm-5.3-flash` was on the models page, priced, and missing from the object.
The object names the models page as the source of its model rates, so that page is the
inventory. The object's model rates are kept as a cross-check in the tests.

## Checked on 2026-11-10

Against the pricing page's tables: web_search $0.0025 per invocation ($0.001 with Fast
Search), fetch_url $0.0005, sandbox $0.03 per session, the Search API $5.00 per thousand
requests, pplx-embed-v1-4b $0.03 per million tokens, pplx-decider-v1.2 $0.02 per
million input tokens. Against the calculator: all 27 models it lists, thresholds
included.

## The keys

Agent API models are keyed by the id a request names, `provider/model` (`xai/grok-4.7`,
`perplexity/sonar`). Tools are keyed by their `type`; a tool billed two ways takes a
suffix, `web_search (fast)`. The Search API is an endpoint, not a tool, and is keyed
`search_api`. Embeddings and Decisions API models are keyed by their model id.

**A resold model is Perplexity's price, not its maker's.** The Agent API sells
Anthropic, OpenAI, Google and xAI models beside Perplexity's own; `xai/grok-4.7` here
is what Perplexity charges, to compare with `providers.xai`'s `grok-4.7`.

## What is not published, and why

- **The effort levels** (`fast` to `xhigh`, `agent.presets`). Each names a model and a
  number of tokens and tool calls, but those numbers are, in Perplexity's words, "median
  values from representative Agent API runs, NOT billed values". The bill is the
  model's tokens and the tools' calls, which are published.
- **OpenAI's `flex` and `priority` service tiers**, which the models page names for the
  OpenAI models without pricing them.
- **The Orchestrator API**, which neither page prices.
- **The old Sonar API** (Sonar Pro, Sonar Reasoning Pro, Sonar Deep Research, with
  their per-request fees). The calculator still has code for a `sonar` section, but on
  2026-11-10 the rates had none. A section the rates carry and nothing here reads is
  reported in the run's notes, so its return would be seen.
