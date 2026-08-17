<!-- @format -->

# OVH

Reads OVH's [AI Endpoints catalog](https://www.ovhcloud.com/fr/public-cloud/ai-endpoints/catalog/)
and publishes `providers.ovh` in `pricing.json`. First implemented 2026-08-03.
Covers the catalog in full: all 19 entries it currently lists, twelve priced and
seven free. Five further models were dropped from the catalog page on 2026-08-04
while remaining on sale -- see below.

If you are checking the catalog by eye, **hard-reload it first**
(`Ctrl+Shift+R`) and compare the "N résultats
disponibles" count against what `scrape.py` reads. A cached tab already caused
one round of "the scraper is missing models" that turned out to be a stale
browser copy; nothing distinguishes a stale render from a live one except that
count.

## How this page differs from Mistral's

Mistral's pricing page is server-rendered card markup: `scripts/providers/mistral/scrape.py`
reads `<div class="model-item">` elements and a `data-prices` JSON attribute on
each price row. OVH's catalog is a Next.js app where the entire model list --
id, display name, and a machine-readable `metadata.usage_information.pricing`
array -- is embedded as JSON inside a React Server Components ("Flight") data
chunk: a `<script>self.__next_f.push([1, "..."])</script>` tag whose string
argument, once JS-unescaped, contains a `"models":[...]` array. The rendered
price text a browser shows ("0.08€/Mtoken(entrée)") is derived from that same
JSON at render time.

`scrape.py` reads the JSON directly rather than re-parsing the formatted,
French-language price string: it is more robust (a number stays a number
regardless of locale formatting) and gives an explicit, unambiguous unit per
price (`price_unit`, e.g. `"million_input_tokens"`) rather than a suffix like
Mistral's `"/ 1000 pages"` that has to be string-matched.

This also means a genuinely different failure mode than Mistral's page: the
thing that can break is not the CSS class of a card, but the shape of a
framework-internal data-streaming format. `extract_catalog_models()` in
`scrape.py` documents exactly what it expects and refuses -- never guesses --
the moment that shape changes.

## Currency

**EUR**, confirmed by direct comparison: the catalog's JSON price for
`bge-multilingual-gemma2` (`0.01`) matches the literal "0.01€" rendered on the
page at the `/fr/` locale URL this scraper reads. No currency field exists
elsewhere in the payload -- the number is only meaningfully EUR because of
which locale URL was fetched. `mapping.json`'s `source` is pinned to that exact
`/fr/` URL for this reason; fetching a different locale could plausibly return
different (converted) numbers under the same JSON shape, which this scraper has
no way to detect. If that ever needs revisiting, it starts here.

## What is published, and what is deliberately not

`mapping.json` names **pricing units**, not models: it says that a catalog price
tagged `million_input_tokens` is published as `in_per_mtok`, and so on. A unit
missing from that table is reported and its figure left unpublished, never guessed
at -- the unit is part of the field name, and there is no honest field to put an
unrecognised figure in. Read its own `_comment` for the authoritative account.
Summary:

**Published: every catalog entry, whatever the catalog happens to list.** There is
no hand-written model list here and there must not be one: the catalog states the
callable model id itself, so a model OVH adds is published on the next run and one
it withdraws keeps its last observed prices under an `absent_since` stamp. That is
19 entries today, twelve priced and seven free.
Priced: gpt-oss-120b, gpt-oss-20b, Meta-Llama-3_3-70B-Instruct,
Qwen2.5-VL-72B-Instruct, Qwen3.5-397B-A17B, Qwen3.5-9B, Qwen3.6-27B,
Qwen3-Embedding-8B, bge-m3, bge-multilingual-gemma2, whisper-large-v3,
whisper-large-v3-turbo. Free: Qwen3Guard-Gen-8B, Qwen3Guard-Gen-0.6B,
stable-diffusion-xl-base-v10 and the four nvr-tts voices. Every figure was
confirmed against the live catalog and cross-checked against the page's own
rendered price text before being committed.

## The key is the API model id, and it is not the catalog's `id`

Each catalog entry carries both an `id` and a `name`, and they are different
things. `id` is a CMS slug used in the catalog's own URLs; `name` is what OVH's
API actually answers to. The entry with `id: "qwen-3-6-27b"` is called as
`Qwen3.6-27B`, and `llama-3-3-70b-instruct` is called as
`Meta-Llama-3_3-70B-Instruct`. `pricing.json` is keyed by `name` for that reason,
and nothing is keyed off `id` at all -- so OVH reorganising its own catalog URLs
does not show up in this file, while a rename of `name` does: it publishes the new
id with today's price and keeps the old one, frozen and dated, so a consumer still
calling it finds out rather than getting a plausible price for a model that no
longer answers.

Fifteen of the nineteen names were cross-checked against OVH's public,
keyless OpenAI-compatible model list at
`https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/models`. The four nvr-tts
voices are absent from that list only because text-to-speech is served from its
own per-model endpoint rather than the OpenAI-compatible one.

**Withdrawn from the catalog page between 2026-08-03 and 2026-08-04, but still
sold (5 models):** Qwen3-Coder-30B-A3B-Instruct, Qwen3-32B,
Mistral-Small-3.2-24B-Instruct-2506, Mistral-7B-Instruct-v0.3,
Mistral-Nemo-Instruct-2407. The page listed 24 models on the 3rd and lists 19
now; these five are the difference, and the word "mistral" no longer appears
anywhere in the page's HTML. They have not been retired from the service --
OVH's public, keyless OpenAI-compatible serving API
(`https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/models`) still lists all five
with live non-zero prices. That API is deliberately not used as a source here:
it quotes **USD**, this block publishes **EUR**, and the two are different
numbers rather than the same price in two formats (Qwen3.6-27B: `0.47`/`3.19`
there against `0.40`/`2.70` on the page). Add these five if the catalog page
lists them again. Do not backfill them from the API, and do not convert.

**Free (7 models):** the four nvr-tts voices, both Qwen3Guard models and
stable-diffusion-xl-base-v10. The catalog prices all seven at `0` and the page
renders their price block as the literal word "Gratuit" (free), not a number --
OVH stating a deliberate free tier, not missing data. They are published as
`"free": true` with no price field, never as a price of `0`: this repository's
sanity floor (`pricing_validate.MIN_PLAUSIBLE = 0.001`) exists to catch a parser
that misreads a real price as `0`, and publishing genuine free tiers as numbers
would force that floor open for everything.

The marker is not remembered anywhere: it is read off the catalog on every run,
and it means "every unit this model is priced in is stated at 0". The day OVH
starts charging for one of these, the price simply appears in the file and the
marker simply goes -- nothing keeps announcing "free" for something that now bills.

A single `0` sitting *beside* a real price is a different thing and is not
published at all. It is either a giveaway of one side of a token price, which this
schema has no way to state, or a misparse; the run reports it and publishes the
prices it could read.

## The coverage check

`check_coverage.py` closes a blind spot the scraper cannot close on its own.
`scrape.py` notices when a model it used to publish disappears from the page; it
is silent about a model OVH **sells** that the page never mentions, because to a
scraper reading one source, an absent row is indistinguishable from a model that
does not exist. That is exactly how the five withdrawn models above went unnoticed
until a human compared a stale browser tab against a fresh one.

So once a week, right after the price check, the catalog page is compared
against OVH's own public model list. Three things get reported:

- **sold but not on the catalog page** — OVH serves it, the page does not price
  it, so nothing here can publish it;
- **on the catalog page but not published** — the scraper publishes every model
  the page prices, so this normally means the page states no price for it, or
  states one in a unit `mapping.json` has no field for;
- **on the catalog page but not in the API list** — either the page advertises
  something not yet servable, or a published key is not the callable model id.

It reads **no price** and can never change a figure. Prices there are quoted in
USD while this block publishes EUR, and they are different numbers rather than
the same price in another format; only the sets of model ids are compared.

**A gap never fails the job.** Refusing to publish correct prices for nineteen
models because a twentieth is missing from a page would also suppress the
`checked_utc` stamp — and that stamp is what keeps the schedule from being
switched off. A gap is a `::warning::` and a job-summary entry, nothing more.

The three exit codes are deliberate: `0` no gap, `2` a gap was found, `1` the
check could not run. "I could not look" must never be reported as "I looked and
found nothing", which is why a timeout is not folded into the same code as a
clean result.

`coverage.json` holds the exceptions, each with a reason: `ppl` (in the API,
never a published model), `stabilityai/stable-diffusion-xl-base-1.0` (the
upstream name for weights the catalog lists as `stable-diffusion-xl-base-v10`),
and the four nvr-tts voices (served from their own endpoint, so never in the
OpenAI-compatible list). Without those, this check would report the same
non-problem every Monday and be worth nothing by March.

```sh
python3 scripts/providers/ovh/check_coverage.py     # reads both sources live
python3 scripts/providers/ovh/check_coverage.py \
  --html tests/fixtures/ovh/catalog_ok.html \
  --models-json tests/fixtures/ovh/models_api_ok.json
```

## Running it yourself

```sh
python3 scripts/providers/ovh/scrape.py --out-dir .ci-out                       # read the live catalog
python3 scripts/providers/ovh/scrape.py --out-dir .ci-out --html tests/fixtures/ovh/catalog_ok.html
```

Same rules as every other provider here: no API key, ever; never writes
`pricing.json` directly; touches only `providers.ovh`, carrying every other
provider's block through byte-for-byte. `.github/workflows/refresh-ovh.yml`
runs it weekly and on demand, sharing the `pricing-json-writes` concurrency
group with Mistral's workflow so the two queue rather than race.
