# OVHcloud AI Endpoints

`scrape.py` reads OVHcloud's public AI Endpoints catalog API every day (05:00 UTC,
`.github/workflows/refresh-ovh.yml`) and works out what `providers.ovh` should say. No
API key: the catalog API is public and keyless.

| what | source | read as |
| --- | --- | --- |
| every model the catalog lists, with its prices | <https://catalog.endpoints.ai.ovh.net/rest/v1/models_v2> | JSON, one entry per model, `metadata.usage_information.pricing` inside each |

`mapping.json` names pricing units, not models: a model OVHcloud adds to the catalog is
published on the next run.

## What it reads

The API answers with a JSON array of models. Each entry carries a machine-readable
`metadata.usage_information.pricing` array whose figures are numbers with an explicit
unit (`price_unit`: `million_input_tokens`, `per_image`, `audio_duration_seconds`...)
rather than the formatted string the catalog page renders. `mapping.json`'s `units`
translates each `price_unit` into a published field; a unit it does not know is reported
and that figure left out, never guessed at. If the response's shape changes, the run
fails.

## Why the API and not the catalog page

The page is a rendering of the same data and lags it. On 2026-11-17 OVHcloud published
nine models to the API at 10:23 and 10:53 UTC, and the page still did not list them hours
later: the scraper read the page, saw the same twenty-one models as the day before, and
reported the catalog unchanged. Those nine prices went unpublished. Reading the API
removes that delay, and it also states the same figures for the models the page does
list -- the twenty-one already published on 2026-11-17 had identical prices in both.

## The key is the API model id, not the catalog's `id`

Each entry carries an `id`, a slug for the catalog's own URLs, and a `name`, what OVH's
API answers to: `qwen-3-6-27b` is called as `Qwen3.6-27B`. Entries are keyed by `name`.
A renamed `name` publishes the new id and keeps the old one, frozen and dated with
`absent_since`.

## EUR

The published figure is the entry's `price`, which is the euro amount; `price_usd`,
`price_aud`, `price_mad` and their siblings restate the same amount in other currencies,
and none of them is read. `currency: EUR` states which one is published. No conversion is
performed anywhere.

## Free models

A model whose every price is `0` is one OVHcloud gives away. It is published as
`"free": true`, with no price field, never as a price of 0, and the marker is read again
on every run, so the day OVHcloud charges for it, the price appears and the marker goes.
A single `0` beside a real price is not published: a giveaway of one side of a token
price has no field, and it could be a misread. The run reports it.

## What is not published, and why

- **A price in a unit `mapping.json` does not know**, until the unit is added to its
  `units` table and to `pricing_validate.KNOWN_PRICE_FIELDS`. The other models in the
  run are unaffected.
- **A price of 0 in one unit beside a real price in another**: reported, not published.

## Reading the catalog by eye

Ask the API, which is what this scraper reads:

```sh
curl -s https://catalog.endpoints.ai.ovh.net/rest/v1/models_v2 \
  | python3 -c 'import json, sys; print(len(json.load(sys.stdin)))'
```

The catalog page's own "N résultats disponibles" count is a rendering of that same list
and can trail it; comparing the two is what made the nine models of 2026-11-17 look
absent rather than late.

## Running it yourself

```sh
python3 scripts/providers/ovh/scrape.py --out-dir .ci-out          # live
python3 scripts/providers/ovh/scrape.py --out-dir .ci-out --html tests/fixtures/ovh/catalog_api.json
```
