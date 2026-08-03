<!-- @format -->

# OVH

Reads OVH's [AI Endpoints catalog](https://www.ovhcloud.com/fr/public-cloud/ai-endpoints/catalog/)
and publishes `providers.ovh` in `pricing.json`. First implemented 2026-08-03,
covering 8 of the models a consumer asked for; three more do not exist on the
catalog yet, and four exist but are currently free -- see below.

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

Read `scripts/providers/ovh/mapping.json`'s own `_comment` for the authoritative,
dated account. Summary:

**Published (8 models):** gpt-oss-120b, gpt-oss-20b, Qwen2.5-VL-72B-Instruct,
Qwen3.5-397B-A17B, Qwen3.5-9B, Qwen3.6-27B, whisper-large-v3,
whisper-large-v3-turbo. Confirmed against the live catalog and cross-checked
against the page's own rendered price text before being committed.

**Requested, absent from the live catalog (as of 2026-08-03):**
Qwen3-Coder-30B-A3B-Instruct, Qwen3-32B, Mistral-Small-3.2-24B-Instruct-2506.
None appear anywhere on the page, under any id or alias -- not a parsing
failure, they are simply not there. Add them once OVH actually publishes them;
do not guess at what their price would be.

**Present but excluded (4 models):** nvr-tts-it-it, nvr-tts-en-us,
nvr-tts-de-de, nvr-tts-es-es. The catalog prices all four at `0` and the page
renders their price block as the literal word "Gratuit" (free), not a number --
this is OVH stating a deliberate free tier, not missing data. This repository's
sanity floor (`pricing_validate.MIN_PLAUSIBLE = 0.001`) exists to catch a
parser that misreads a real price as `0`; it would incorrectly also catch a
model that is genuinely, intentionally free. Rather than invent a schema concept
for "free" to route around that, these four are simply left unmapped until OVH
assigns them a real price -- at which point adding them is the same three-line
diff as any other model.

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
