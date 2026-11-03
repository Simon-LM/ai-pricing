#!/usr/bin/env python3
"""Read Scaleway's public product catalog and work out what pricing.json's
providers.scaleway block should say.

Standard library only. Reads a public endpoint and nothing else: it takes no API
key, must never be given one, and never touches anyone's Scaleway account.

This script DOES NOT WRITE pricing.json. It writes candidate files into an output
directory and reports which one, if any, the caller should promote. That is
deliberate: "leave pricing.json untouched on every failure path" is then a property
of the design rather than a branch of code somebody has to remember to get right.

Scaleway serves generative models as a managed service ("Model as a Service"), under
the Scaleway Generative APIs brand. The source is its public product catalogue --
<https://api.scaleway.com/product-catalog/v2alpha1/public-catalog/products> filtered
to `product_types=generative_apis` -- which answers a keyless GET with JSON and
states, for every model it sells, the callable model id, the billing unit, and a
retail price as a whole number of euros plus nanoseconds of one.

That endpoint rather than the pricing page a human reads
(<https://www.scaleway.com/en/pricing/model-as-a-service/>), for one reason: the page
is behind a Bunny Shield challenge and answers a plain fetch with HTTP 403 unless the
request happens to carry browser headers, while the endpoint answers this scraper's
own user agent. Both carry the same figures -- checked by hand on 2026-11-03, all 17
models then on the page -- and the page is what a human is shown, so it stays worth a
look when a figure here is surprising. It is not what this job reads every week.

Unlike the other providers here, one model is several catalogue entries: one per
(token type, consumption mode, region), each with its own sku and its own price. This
scraper reassembles them into one entry per model.

Outcomes, matching the three the workflow must implement:

  unchanged  figures identical -> out/stamped.json  (same figures, fresh checked_utc)
  changed    a figure moved    -> out/stamped.json and out/updated.json (the new figures)
  failure    fetch, parse or validation failed -> out/error.txt, exit code 1, no candidates

Usage:
    scripts/providers/scaleway/scrape.py --out-dir .ci-out                     # live
    scripts/providers/scaleway/scrape.py --out-dir .ci-out --html catalog.json # offline
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

import provider_runner  # noqa: E402
from provider_runner import Fetcher, ScrapeError  # noqa: E402
from pricing_validate import JSONDict, check_price  # noqa: E402

PROVIDER_ID = "scaleway"

REPO_ROOT = _SCRIPTS_DIR.parent
DEFAULT_MAPPING = Path(__file__).resolve().parent / "mapping.json"
DEFAULT_CURRENT = REPO_ROOT / "pricing.json"

# Kept as its own constant rather than read off __doc__: the module docstring is
# stripped to None under `python -OO`, which would otherwise take this script down
# for a reason with nothing to do with argparse.
CLI_SUMMARY = "Read Scaleway's public product catalog and work out what providers.scaleway should say."

# The catalogue states a price as whole currency units plus nanoseconds of one.
# Written out rather than left as a literal so the conversion and the error messages
# cannot drift apart.
NANOS_PER_UNIT = Decimal(10**9)


def catalogue_products(body: str) -> list[JSONDict]:
    """Parse the catalogue's response into its list of product entries.

    Raises ScrapeError, never returns something partial: every shape assumption is
    checked here so that the mapping step below can read plain fields. The endpoint
    answers with an error document under the same content type on a bad query, so a
    missing `products` array is a real possibility and not just a layout change.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ScrapeError(f"the catalogue is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ScrapeError(f"the catalogue is not an object (got {type(payload).__name__})")
    payload = cast(JSONDict, payload)

    products = payload.get("products")
    if not isinstance(products, list) or not products:
        raise ScrapeError(
            'the catalogue has no non-empty "products" array. The endpoint has changed '
            "shape, or answered with an error document rather than a catalogue."
        )
    products = cast("list[Any]", products)

    for entry in products:
        if not isinstance(entry, dict):
            raise ScrapeError("a catalogue entry is not an object -- the data shape has changed.")
        entry = cast(JSONDict, entry)
        for key in ("sku", "product"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise ScrapeError(
                    f'a catalogue entry has no non-empty string "{key}". That field is '
                    f"what this block is published under, so the catalogue's data shape "
                    f"has changed in a way that cannot be worked around."
                )

    return cast("list[JSONDict]", products)


def per_unit(retail_price: JSONDict, spec: JSONDict) -> Decimal:
    """One catalogue price, restated in the unit the published field is quoted in.

    The catalogue quotes `units` whole euros plus `nanos` of one -- 0.0018 EUR arrives
    as units=0, nanos=1800000 -- and quotes it per `spec["size"]` source units, while
    the field publishes per `spec["per"]` of them. Token prices arrive per thousand and
    are published per million; audio arrives per 60 seconds and is published per
    minute.

    Decimal throughout, never a float multiply: 0.003 * 60 is 0.18000000000000002 as a
    float, and that string of noise would publish a wrong figure and churn in a diff a
    human reads for real price movements. `Decimal(units) + Decimal(nanos) / 1e9` is
    exact, and the float it converts back to prints as the plain 0.18 a reviewer
    expects.
    """
    units = retail_price.get("units", 0)
    nanos = retail_price.get("nanos", 0)
    for name, value in (("units", units), ("nanos", nanos)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ScrapeError(
                f"retail_price.{name} is {type(value).__name__} ({value!r}), not a whole "
                f"number. The catalogue's price shape has changed."
            )
    amount = Decimal(units) + Decimal(nanos) / NANOS_PER_UNIT
    return amount * Decimal(spec["per"]) / Decimal(spec["size"])


def extract_models(
    products: list[JSONDict], mapping: JSONDict
) -> tuple[dict[str, JSONDict], list[str]]:
    """Turn the catalogue into a `models` block: every model it prices, no list.

    The mapping says which PRICING UNITS this repository knows how to publish, and
    nothing else. Which models Scaleway offers is Scaleway's business and changes
    week to week; following that is the whole point of this job. A model it adds is
    published on the next run, one it withdraws keeps its last observed prices under
    an `absent_since` stamp, and nothing here needs a human to reconcile a list first.

    That is possible because each entry states the callable model id itself, in
    `product` -- so nothing is translated by hand, not even from the sku, which spells
    the same id with underscores.

    Batch rates are published like any other row, under the '_batch' suffix of the same
    unit. They are read, never derived: 31 of the 32 batch rates Scaleway stated on
    2026-11-03 are exactly half the realtime one, and glm-5.2's output token is not
    (5.50 -> 2.70), so halving would publish a figure the source never wrote.

    One thing is left out, declared in the mapping rather than decided here: a region
    other than the one it names. That is not a layout failure, so it does not stop the
    run; it is reported, because a model that vanishes from this file should be
    traceable to the line of the mapping that did it.
    """
    fields: JSONDict = mapping["fields"]
    published_statuses = set(mapping["published_statuses"])
    region = mapping["region"]
    currency = mapping["currency"]

    # (unit, size, token type, consumption mode) -> (field, spec). The unit is part of
    # the field name in pricing.json -- publishing a per-second price under a per-token
    # key would be worse than publishing nothing -- so a row whose descriptor is not in
    # this table is skipped and reported rather than guessed at. The mode belongs in the
    # descriptor for the same reason: the batch rate of a unit is a different field, not
    # a second reading of the same one.
    descriptors: dict[tuple[Any, Any, Any, Any], tuple[str, JSONDict]] = {}
    for field, spec in fields.items():
        descriptors[(spec["unit"], spec["size"], spec["token_type"], spec["consumption_mode"])] = (field, spec)

    by_model: dict[str, list[JSONDict]] = {}
    other_regions: set[str] = set()
    for product in products:
        locality = product.get("locality")
        found = locality.get("region") if isinstance(locality, dict) else None
        if found != region:
            other_regions.add(str(found))
            continue
        model_id = cast(str, product["product"])
        by_model.setdefault(model_id, []).append(product)

    models: dict[str, JSONDict] = {}
    notes: list[str] = []

    for model_id in sorted(by_model):
        entries = by_model[model_id]

        # A model is one status, not one per entry. If Scaleway ever splits it -- half
        # of a model withdrawn, half still sold -- publishing either answer would be a
        # guess, so this refuses instead.
        statuses = {entry.get("status") for entry in entries}
        if len(statuses) > 1:
            raise ScrapeError(
                f"{model_id}: the catalogue states {sorted(str(s) for s in statuses)} "
                f"across its entries. Refusing to guess whether it is still sold."
            )
        if statuses.pop() not in published_statuses:
            continue

        prices: dict[str, Decimal] = {}
        modes_seen: set[str] = set()
        rows_without_price = 0

        for entry in entries:
            properties = entry.get("properties")
            apis = properties.get("generative_apis") if isinstance(properties, dict) else None
            apis = apis if isinstance(apis, dict) else {}
            mode = apis.get("consumption_mode")
            modes_seen.add(str(mode))

            unit_of_measure = entry.get("unit_of_measure")
            unit_of_measure = unit_of_measure if isinstance(unit_of_measure, dict) else {}
            descriptor = (
                unit_of_measure.get("unit"),
                unit_of_measure.get("size"),
                apis.get("token_type"),
                mode,
            )
            match = descriptors.get(descriptor)
            if match is None:
                notes.append(
                    f"{model_id}: priced per {descriptor[1]} {descriptor[0]!r} as "
                    f"{descriptor[2]!r} in {descriptor[3]!r}, which {DEFAULT_MAPPING.name} "
                    f"has no field for. That price is not published. Add the unit to the "
                    f"mapping's 'fields' table, and a matching field to KNOWN_PRICE_FIELDS, "
                    f"to publish it."
                )
                continue

            field, spec = match
            if field in prices:
                raise ScrapeError(
                    f"{model_id}.{field}: the catalogue prices it twice "
                    f"({entry.get('sku')!r} and another entry). Refusing to guess which "
                    f"is the price."
                )

            price = entry.get("price")
            retail_price = price.get("retail_price") if isinstance(price, dict) else None
            if not isinstance(retail_price, dict):
                rows_without_price += 1
                continue

            found_currency = retail_price.get("currency_code")
            if found_currency != currency:
                raise ScrapeError(
                    f"{model_id}: the catalogue prices it in {found_currency!r}, and this "
                    f"block publishes {currency!r}. This repository performs no conversion, "
                    f"so this must be resolved by hand."
                )

            prices[field] = per_unit(retail_price, spec)

        if not prices:
            if rows_without_price:
                reason = "the catalogue lists it with no price at all"
            else:
                reason = (
                    f"no price row matched a unit this mapping can name (rows seen: "
                    f"{', '.join(sorted(modes_seen))})"
                )
            models[model_id] = {"unpriced": reason, "display_name": model_id}
            continue

        # A model Scaleway gives away is published with the shared `free` marker and no
        # price field, never as a price of 0 -- 0 is also exactly what a broken parser
        # reads, which is what check_price's floor exists to catch. "Free" is read off
        # the catalogue every week rather than remembered, so the day it starts charging
        # for one of these, the price simply appears and the marker simply goes.
        if all(value == 0 for value in prices.values()):
            models[model_id] = {"free": True, "display_name": model_id}
            continue

        entry: JSONDict = {}
        for field, value in prices.items():
            # Zero beside a real price is not "free" -- free is all-zero, handled above.
            # It is either a genuine giveaway of one side of a price, which this schema
            # has no way to say, or a misparse. Either way it is not publishable.
            if value == 0:
                notes.append(
                    f"{model_id}: priced at 0 in {field} while charging for other units. "
                    f"Not published: this file distinguishes a free model, which is free in "
                    f"every unit, from a price of 0, which is also what a misparse reads."
                )
                continue
            entry[field] = check_price(f"{PROVIDER_ID}/{model_id}", field, float(value))

        if not entry:
            models[model_id] = {
                "unpriced": "priced at 0 in every unit this mapping names, beside real prices elsewhere",
                "display_name": model_id,
            }
            continue

        entry["display_name"] = model_id
        models[model_id] = entry

    if other_regions:
        notes.append(
            f"{len(other_regions)} other region(s) are priced in the catalogue "
            f"({', '.join(sorted(other_regions))}) and are not published: this block holds "
            f"one price per model, for {region}."
        )

    # Counts PRICED models, not entries. Marking one model unpriced is an honest
    # report; marking every one of them would quietly turn the whole provider into
    # last-known figures, and no single bad read upstream may cause that.
    if not any("unpriced" not in e for e in models.values()):
        raise ScrapeError(
            "the catalogue priced not a single model this scraper could publish. Either "
            "Scaleway restructured it, or the response is not what it looks like. "
            "Refusing to republish the whole block as unpriced on the strength of one bad read."
        )

    return models, notes


def extract_new_models(fetch: Fetcher, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    """The one callback provider_runner needs. One source, read once."""
    return extract_models(catalogue_products(fetch(mapping["source"])), mapping)


def main(argv: list[str] | None = None) -> int:
    return provider_runner.main(PROVIDER_ID, extract_new_models, DEFAULT_MAPPING, DEFAULT_CURRENT, CLI_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
