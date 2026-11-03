<!-- @format -->

# Scaleway

Reads Scaleway's [public product catalogue][api] and publishes `providers.scaleway`
in `pricing.json`. First implemented 2026-11-03, covering the 17 models Scaleway then
offered. How many it offers now follows the source and is not written down here, so
nothing in this directory has to be edited when it moves.

[api]: https://api.scaleway.com/product-catalog/v2alpha1/public-catalog/products?product_types=generative_apis&page_size=500

## Why the API and not the pricing page

A human reads [scaleway.com/en/pricing/model-as-a-service/][page]. This job does not.
The page sits behind a Bunny Shield challenge: a plain fetch is answered with HTTP
403 unless the request happens to carry browser headers, which is exactly the kind of
luck this repository tries not to depend on. The catalogue endpoint answers the
scraper's own user agent, keyless, with JSON.

[page]: https://www.scaleway.com/en/pricing/model-as-a-service/

Both carry the same figures. Checked by hand on 2026-11-03: all 17 models then on the
page, same numbers to the nanosecond. So the page is still worth a look when a figure
here is surprising -- it is what a customer is shown -- but it is not what the weekly
job reads.

Reading the page instead would also mean parsing formatted, French-language price
strings off rendered cards, which is the least robust thing either source offers.

## One model is several catalogue entries

The catalogue is flat. Every entry is one sku, and one model is several of them: one
per token type, per consumption mode, per region. `glm-5.3` alone arrives as four
entries (`input` and `output`, each in `realtime` and `batch`).

Each entry carries everything needed to place it:

```json
{
  "sku": "/ai/generative_apis/consumption/glm_5_3_input/fr-par",
  "product": "glm-5.3",
  "variant": "glm-5.3 - input token - realtime - fr-par",
  "price": { "retail_price": { "currency_code": "EUR", "units": 0, "nanos": 1800000 } },
  "unit_of_measure": { "unit": "token", "size": 1000 },
  "properties": { "generative_apis": { "consumption_mode": "realtime", "token_type": "input_token" } },
  "status": "preview"
}
```

`product` is the callable model id, so the key published here is that string as-is --
nothing is translated by hand, not even from the sku, which spells the same id with
underscores. `scrape.py` groups the entries by `product` and reassembles each model.

## Prices are `units` + `nanos`, and are scaled in Decimal

0.0018 EUR arrives as `units: 0, nanos: 1800000`. `units + nanos / 1e9` in `Decimal`
is exact. The catalogue quotes per 1000 tokens and this file publishes per million,
so the figure is multiplied by `per / size` from the mapping -- again in `Decimal`,
because `0.003 * 60` is `0.18000000000000002` as a float, and that string of noise
would publish a wrong figure and churn in a diff a human reads for real movements.

## Batch rates are published, under their own keys

Scaleway sells every model twice: served immediately at the listed rate, or deferred
and discounted in batch. Both rates are stated in the catalogue, both are published,
and neither is derived from the other.

The batch rate of a unit is a field of its own, named after the realtime one with a
`_batch` suffix: `in_per_mtok_batch`, `out_per_mtok_batch`,
`cache_read_per_mtok_batch`, `per_audio_minute_batch`. The suffix rather than a
separate section is what keeps the unit in the key name -- `in_per_mtok_batch` is per
million input tokens, in batch.

**Do not compute a batch rate by halving the realtime one.** It usually is half -- 31
of the 32 batch rates on 2026-11-03 -- but not always: `glm-5.2`'s output token is
5.50 realtime and 2.70 in batch, where `glm-5.3`'s is 5.50 and 2.75. Halving would
have published a figure Scaleway never wrote, in the one case where it matters. The
`batch` entries are read like any other row, so a model that stops being sold in batch
simply loses the field on the next run.

## What is deliberately not published

**Regions other than `fr-par`.** All prices were `fr-par` when this was written, and
this file holds one price per model, not one per region. Another region is reported
once per run rather than published.

**`tasks` and `provider_name`.** The catalogue states both (`chat`, `code`, `vision`,
`ocr`, `audio_transcription`, `embeddings`, and the upstream lab behind each model).
Useful labels, but this file classifies by billing unit and by nothing else: OVH
states no task list, Hugging Face states none, and Eden AI states its own, so a task
field would be filled for one provider in five and would invite filtering on
something that is mostly missing. Decided with Simon on 2026-11-03.

## Statuses

`published_statuses` in `mapping.json` is what Scaleway is willing to sell:

| Status | Published | Why |
| --- | --- | --- |
| `general_availability` | yes | on sale |
| `preview` | yes | on sale, and priced |
| `end_of_new_features` | yes | still sold; `glm-5.2` carries it and is on the pricing page |
| `end_of_life` | no | withdrawn |
| `retired` | no | withdrawn |

An entry that stops being published does not vanish from the file: the entry already
published for it keeps the last prices actually observed and gains an `absent_since`
date, and is dropped after a year of absence. That is `reconcile_inventory()` in
`scripts/provider_runner.py`, shared with every other provider -- and it is why
`end_of_life` and `retired` are excluded here rather than deleted by hand.

A catalogue entry for a model that is still sold but has no price at all (the retired
ones have neither a price nor a unit) is published carrying `unpriced_since`, the
shared marker for "offered, not free, and no figure this file can stand behind".

## Running it

```sh
scripts/providers/scaleway/scrape.py --out-dir .ci-out    # read the live catalogue
scripts/providers/scaleway/scrape.py --out-dir .ci-out \
  --html tests/fixtures/scaleway/catalog_ok.json          # offline, from the fixture
python3 -m unittest tests.scraper_tests.test_scaleway
```

The weekly job is `.github/workflows/refresh-scaleway.yml`. It never writes
`pricing.json` itself: it writes candidates into `.ci-out/`, and the workflow decides
which one, if any, to promote.

## Adding a unit

Add the (unit, size, token type, consumption mode) descriptor to the mapping's `fields`
table with the field it publishes as, and make sure that field is in
`KNOWN_PRICE_FIELDS` in `scripts/pricing_validate.py` -- the unit is part of the field
name, so an unknown unit is skipped and reported rather than guessed at. A price row
whose descriptor is not in the table is skipped for that model alone; the run still
publishes everything else and says so in its notes.
