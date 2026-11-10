# OVHcloud AI Endpoints

`scrape.py` reads OVHcloud's public AI Endpoints catalog every day (05:00 UTC,
`.github/workflows/refresh-ovh.yml`) and works out what `providers.ovh` should say. No
API key: the catalog is public.

| what | source | read as |
| --- | --- | --- |
| every model the catalog lists, with its prices | <https://www.ovhcloud.com/fr/public-cloud/ai-endpoints/catalog/> | the model list the page embeds as data |
| the models OVHcloud serves, for the coverage check only | <https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/models> | JSON, model ids only, no price read |

`mapping.json` names pricing units, not models: a model OVHcloud adds to the catalog is
published on the next run.

## What it reads

The catalog is a Next.js app. Its whole model list, each with a machine-readable
`metadata.usage_information.pricing` array, is embedded as JSON in the page's streamed
data (`self.__next_f.push(...)`), read through `scripts/nextjs_flight.py`. The price a
browser shows ("0.08€/Mtoken(entrée)") is drawn from that same data; reading the data
gives a number and an explicit unit (`price_unit`: `million_input_tokens`...) instead of
a French-formatted string. `mapping.json`'s `units` translates each `price_unit` into a
field; a unit it does not know is reported and that figure left out, never guessed at.
If the data's shape changes, the run fails.

## The key is the API model id, not the catalog's `id`

Each entry carries an `id`, a slug for the catalog's own URLs, and a `name`, what OVH's
API answers to: `qwen-3-6-27b` is called as `Qwen3.6-27B`. Entries are keyed by `name`.
A renamed `name` publishes the new id and keeps the old one, frozen and dated with
`absent_since`.

## EUR, because of the URL

The payload states no currency. The figures are euros because the `/fr/` catalog is
fetched: its JSON price for `bge-multilingual-gemma2`, `0.01`, is the "0.01€" the page
renders. Another locale could return other numbers in the same shape, undetectably, so
`source` is pinned to that URL.

## Free models

A model whose every price is `0` is one OVHcloud gives away: the page renders its price
as "Gratuit". It is published as `"free": true`, with no price field, never as a price of
0, and the marker is read again on every run, so the day OVHcloud charges for it, the
price appears and the marker goes. A single `0` beside a real price is not published: a
giveaway of one side of a token price has no field, and it could be a misread. The run
reports it.

## The coverage check

A scraper sees a model disappear from its page, but not a model the provider sells that
the page never lists. On 2026-08-04 the catalog page dropped five models OVHcloud was
still serving, and nothing in the scrape could notice. So, every day after the price
check, `check_coverage.py` compares the catalog page with OVHcloud's public list of
served models and reports:

- **served but not on the catalog page**: nothing here can publish its price;
- **on the catalog page but not published**: usually a price in a unit the mapping has no
  field for;
- **on the catalog page but not served**: advertised before it can be called, or a key
  that is not the callable id.

It reads no price and never changes a figure. The served list quotes USD prices that are
not the catalog's EUR ones converted (`Qwen3.6-27B`: $0.47 / $3.19 there, €0.40 / €2.70
on the page), so it is never used as a source of prices. A gap is a warning on the run,
never a failure: refusing every correct price for one missing model would also stop the
Monday stamp that keeps the schedule alive. Exit codes: `0` no gap, `2` a gap, `1` the
check could not run. `coverage.json` lists the known exceptions, each with its reason
(text-to-speech voices served from their own endpoint, a model listed under another
name).

## What is not published, and why

- **A model OVHcloud serves but the catalog page does not list.** The coverage check
  reports it; its price is not taken from the served list, which quotes another currency
  and other figures. It is published if the catalog lists it again.
- **A price in a unit `mapping.json` does not know**, until the unit is added.

## Checking the catalog by eye

Hard-reload it first (`Ctrl+Shift+R`) and compare its "N résultats disponibles" count
with what `scrape.py` reads. A cached tab once looked like missing models; nothing
distinguishes a stale render from a live one but that count.

## Running it yourself

```sh
python3 scripts/providers/ovh/scrape.py --out-dir .ci-out          # live
python3 scripts/providers/ovh/scrape.py --out-dir .ci-out --html tests/fixtures/ovh/catalog_ok.html
python3 scripts/providers/ovh/check_coverage.py                    # the coverage check, live
python3 scripts/providers/ovh/check_coverage.py --html tests/fixtures/ovh/catalog_ok.html --models-json tests/fixtures/ovh/models_api_ok.json
```
