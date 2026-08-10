<!-- @format -->

# Hugging Face

Reads the [Inference Providers router listing](https://router.huggingface.co/v1/models)
and publishes `providers.huggingface` in `pricing.json`. First implemented
2026-08-10, covering the 15 routes served by the two partners asked for: OVHcloud
(7) and Scaleway (8).

## The unit here is a route, not a model

This is what makes this provider different from every other one in the repository.
Hugging Face does not serve models itself; it routes a call to a partner that does,
and the same model is served by several partners at several prices.
`openai/gpt-oss-120b` is offered by **eleven** of them, from `$0.05` to `$0.35` per
million input tokens.

So a key here is a `<model id>:<partner>` pair, which is exactly the string the
router takes — the form Hugging Face's own documentation uses
(`"openai/gpt-oss-120b:groq"`). A key without the suffix would name eleven different
prices at once, and there would be no honest way to pick one.

That also means one model legitimately appears more than once:

```json
"meta-llama/Llama-3.3-70B-Instruct:ovhcloud":  { "in_per_mtok": 0.74,  "out_per_mtok": 0.74  },
"meta-llama/Llama-3.3-70B-Instruct:scaleway":  { "in_per_mtok": 1.026, "out_per_mtok": 1.026 }
```

Not a duplicate to collapse. Two prices, because it is two bills.

## These are prices *through the router*

A figure here is what the partner charges when reached through Hugging Face, which
is not the price of calling that partner directly. OVHcloud's own catalog prices
`Qwen3.6-27B` at `0.40 EUR`; the router quotes `0.47 USD` for the same model on the
same partner. Both are correct, and both are in this file — the direct price under
`providers.ovh`, this one here. `providers.huggingface` is not a cross-check of
`providers.ovh`.

## Currency and unit, pinned by two independent sources

**USD, already per million tokens** — no scaling is applied, unlike Eden AI's
per-token source.

That is not taken on faith. This repository reads OVH's own catalog independently,
in EUR, and every one of the seven `ovhcloud` routes tracks it at the USD/EUR
conversion rate: `0.40 → 0.47`, `0.91 → 1.01`, `0.60 → 0.71`, `0.04 → 0.05`. Two
sources this project fetches separately agreeing to a single exchange rate is what
pins both the unit and the currency here, and a test enforces the band so that a
silent change to either breaks the build rather than the file.

## What is refused

- **A route that is not `live`.** A price for a call that cannot be served is worse
  than no price.
- **A route appearing or disappearing** on a mapped partner. A new one is not an
  error in the data — it is a price nobody has read, and it must reach a person
  through `mapping.json` before it reaches consumers. A vanished one must not drop
  out of the file silently.
- **A price of `0` with `is_free` false.** The router does carry such routes. That
  is not a declared free tier, it is a figure nothing vouches for, and the shared
  sanity floor catches it. A route Hugging Face genuinely marks `is_free` is
  published with the shared `"free": true` marker and no price field, exactly as
  OVH's free models are.

Routes on the other twelve partners are ignored, not refused — a partner this block
does not cover adding a model is none of its business and must not fail the weekly
run.

## Widening the set

`mapping.json` pins `partners` and the exact routes. Adding `together` or `groq` is
a one-line change to `partners` plus their routes in `models` — and reading every
one of those prices first.

## Running it yourself

```sh
python3 scripts/providers/huggingface/scrape.py --out-dir .ci-out          # live
python3 scripts/providers/huggingface/scrape.py --out-dir .ci-out --html tests/fixtures/huggingface/models_ok.json
```

Same rules as every other provider here: no API key, ever; never writes
`pricing.json` directly; touches only `providers.huggingface`, carrying every other
provider's block through byte-for-byte.
`.github/workflows/refresh-huggingface.yml` runs it weekly and on demand, sharing
the `pricing-json-writes` concurrency group with the other providers' workflows so
they queue rather than race.
