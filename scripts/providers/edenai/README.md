# Eden AI

`scrape.py` reads Eden AI's public model list every day (06:00 UTC,
`.github/workflows/refresh-edenai.yml`) and works out what `providers.edenai` should say.
No API key: the list is public.

| what | source | read as |
| --- | --- | --- |
| every model Eden AI sells, with its price | <https://api.edenai.run/v3/models> | JSON, explicit field names per model |

`mapping.json` names the upstreams covered (Mistral, xAI, Scaleway, OVHcloud and
Perplexity) and the price fields read; it holds no list of models. A model Eden AI adds
is published on the next run.

## Eden AI's prices, not the upstream's

Eden AI is an aggregator: one API, forwarding each call to a provider underneath. A price
here is what **Eden AI** charges to do that, which can equal the upstream's or not:
`mistral/codestral-latest` is $0.30 / $0.90 per million tokens, as at Mistral, while
`scaleway/deepseek-v4.1-flash-1017` is $0.67 / $2.01, Eden AI's conversion of Scaleway's
€0.60 / €1.80. Both blocks are correct; `providers.edenai` is not a cross-check of
`providers.mistral` or `providers.scaleway`.

Keys are Eden AI's own ids, upstream prefix included: `mistral/codestral-latest`. An
alias such as `mistral/mistral-large-4-0` or a region such as `...@eu` is an entry of its
own, as Eden AI lists it.

## How the figures are read

```json
"pricing": { "input_cost_per_token": 1.5e-06, "output_cost_per_token": 7.5e-06 }
```

- **Per token, published per million**, with exact decimal arithmetic: a plain float
  multiplication turns `4e-07` into `0.39999999999999997`. Long decimals that are really
  there are kept: Eden AI's conversions from EUR carry fifteen of them, and rounding would
  invent a price.
- **USD.** The API states no currency; Eden AI's model page renders the same figures as
  dollars, under a column headed "Input $".
- **The price paid, `pricing`, not the list price, `list_pricing`**, when Eden AI runs a
  promotion it states in the same public response (`"discount": 0.5`), and only if every
  figure in `pricing` is exactly the list price less that discount, in Eden AI's own
  floating-point arithmetic. A difference the stated discount does not account for, or a
  shape the comparison cannot check, fails the run.
- **`cache_read_per_mtok`** for every model that states `cache_read_input_token_cost`.
- **Long context**: the rates Eden AI states past a prompt size
  (`input_cost_per_token_above_128k_tokens`, `_above_200k_tokens`, and their output and
  cache twins) go in the `_long_context` fields, the size in `long_context_threshold`.

## A model listed twice

Eden AI sometimes lists one id twice at two prices: `mistral/mistral-large-4` from
2026-10-06, at $0.68 / $2.09 and at $0.70 / $2.10. That model alone keeps its published
prices, marked `disputed_since`, and the run asks a human to pick; every other model is
published as usual. The pick goes in `mapping.json`'s `duplicate_choices`, by the
`created` stamp of the real entry, with the reason. For Mistral Large 4 it is the
$0.70 / $2.10 entry, the one that matches Mistral's own price, picked on 2026-11-09. An
entry there can go once Eden AI lists the id once.

## What is not published, and why

- **A second long-context tier.** The format holds one threshold per entry: a model that
  states two publishes neither, and the run says so, since picking one would understate
  the bill past the other.
- **`search_context_cost_per_query`**: a per-query web search charge, stated as three
  sizes rather than one figure.
- **`output_cost_per_reasoning_token`** and **`citation_cost_per_token`**, stated by one
  model each.
- **Upstreams not covered**: Eden AI's list also holds other makers' models (Google,
  Anthropic and Together AI among them), which `mapping.json` does not name. Google and
  Anthropic are published directly, under their own blocks.

## Running it yourself

```sh
python3 scripts/providers/edenai/scrape.py --out-dir .ci-out          # live
python3 scripts/providers/edenai/scrape.py --out-dir .ci-out --html tests/fixtures/edenai/models_ok.json
```
