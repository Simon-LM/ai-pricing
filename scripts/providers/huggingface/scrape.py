#!/usr/bin/env python3
"""Read Hugging Face's Inference Providers router listing and work out what
pricing.json's providers.huggingface block should say.

Standard library only. Reads a public endpoint and nothing else: it takes no API
key, must never be given one, and never touches anyone's Hugging Face account.

This script DOES NOT WRITE pricing.json. It writes candidate files into an output
directory and reports which one, if any, the caller should promote. That is
deliberate: "leave pricing.json untouched on every failure path" is then a property
of the design rather than a branch of code somebody has to remember to get right.

This block covers OVHcloud and Scaleway, the two partners named in mapping.json, and
nothing else.

Hugging Face routes a call to the partner that actually serves the model, and the
same model can be served by both of those partners at different prices --
openai/gpt-oss-120b is $0.09 per million input tokens through OVHcloud and $0.171
through Scaleway. So the unit of pricing here is not a model, it is a (model,
partner) PAIR, and the key published for it is exactly the string the router takes:
`<model id>:<partner>`, the form Hugging Face's own documentation uses
("openai/gpt-oss-120b:ovhcloud"). A key without the suffix would name two different
prices at once.

Note what "through the router" means: a price here is what the partner charges when
reached THROUGH Hugging Face, and is a different figure from calling that partner
directly -- OVHcloud's own catalog prices Qwen3.6-27B at 0.40 EUR while the router
quotes 0.47 USD for the same model. Both are correct and both are in this file,
under their own provider.

Outcomes, matching the three the workflow must implement:

  unchanged  figures identical -> out/stamped.json  (same figures, fresh checked_utc)
  changed    a figure moved    -> out/stamped.json and out/updated.json (the new figures)
  failure    fetch, parse or validation failed -> out/error.txt, exit code 1, no candidates

Usage:
    scripts/providers/huggingface/scrape.py --out-dir .ci-out                     # live
    scripts/providers/huggingface/scrape.py --out-dir .ci-out --html models.json  # offline
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

import provider_runner  # noqa: E402
from provider_runner import Fetcher, ScrapeError  # noqa: E402
from pricing_validate import JSONDict, check_price  # noqa: E402

PROVIDER_ID = "huggingface"

REPO_ROOT = _SCRIPTS_DIR.parent
DEFAULT_MAPPING = Path(__file__).resolve().parent / "mapping.json"
DEFAULT_CURRENT = REPO_ROOT / "pricing.json"

# Kept as its own constant rather than read off __doc__: the module docstring is
# stripped to None under `python -OO`, which would otherwise take this script down
# for a reason with nothing to do with argparse.
CLI_SUMMARY = "Read Hugging Face's router listing and work out what providers.huggingface should say."

# Only a route the router says it can actually serve may be published. Anything else
# is a price for a call that would fail.
LIVE = "live"


def parse_routes(body: str) -> dict[str, JSONDict]:
    """Parse the listing into {"<model>:<partner>": partner entry}.

    Raises ScrapeError, never returns something partial: every shape assumption is
    checked here so that the mapping step below can read plain fields.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ScrapeError(f"the router listing is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ScrapeError(f"the router listing is not an object (got {type(payload).__name__})")
    payload = cast(JSONDict, payload)

    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise ScrapeError(
            'the router listing has no non-empty "data" array. The endpoint has changed '
            "shape, or answered with an error document."
        )
    data = cast("list[Any]", data)

    routes: dict[str, JSONDict] = {}
    for model in data:
        if not isinstance(model, dict):
            raise ScrapeError("a listing entry is not an object -- the data shape has changed.")
        model = cast(JSONDict, model)

        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id:
            raise ScrapeError('a listing entry has no string "id" -- the data shape has changed.')

        offers = model.get("providers")
        if offers is None:
            continue
        if not isinstance(offers, list):
            raise ScrapeError(f"{model_id}: 'providers' is not a list -- the data shape has changed.")
        offers = cast("list[Any]", offers)

        for offer in offers:
            if not isinstance(offer, dict):
                raise ScrapeError(f"{model_id}: a provider entry is not an object.")
            offer = cast(JSONDict, offer)

            partner = offer.get("provider")
            if not isinstance(partner, str) or not partner:
                raise ScrapeError(f"{model_id}: a provider entry has no string 'provider'.")

            route = f"{model_id}:{partner}"
            if route in routes:
                raise ScrapeError(
                    f"the listing offers {route!r} more than once. Refusing to guess which "
                    f"entry carries the real price."
                )
            routes[route] = offer

    if not routes:
        raise ScrapeError("the router listing contains no provider routes at all.")
    return routes


def extract_models(
    routes: dict[str, JSONDict], mapping: JSONDict
) -> tuple[dict[str, JSONDict], list[str]]:
    """Turn the routes into a `models` block, plus anything a human should see.

    Returns notes rather than raising on a route it cannot price, so that one bad route
    still publishes the others. See the zero-price branch below for why that matters.
    """
    partners = mapping["partners"]
    field_specs: JSONDict = mapping["fields"]

    # The mapping picks PARTNERS, not routes. Which models a partner serves through the
    # router is Hugging Face's business and changes constantly; following that is the
    # whole point of this job. The route id is stated by the source itself, so there is
    # nothing here for a hand-written list to translate -- and pinning one would only
    # mean that the first model a partner retires blocks every other partner's prices
    # too.
    #
    # A route the router does not mark `live` is treated as not offered, exactly like
    # one that has vanished: a price for a call that cannot be served is worse than no
    # price. The entry does not disappear from the file when that happens -- the runner
    # keeps it, with its last observed prices and an `absent_since` stamp.
    offered = sorted(
        r for r, o in routes.items() if o.get("provider") in partners and o.get("status") == LIVE
    )
    if not offered:
        raise ScrapeError(
            f"none of the mapped partners {sorted(partners)} serve a single live route. "
            f"Either Hugging Face restructured the listing, or the response is not what "
            f"it looks like. Refusing to publish an empty block."
        )

    models: dict[str, JSONDict] = {}
    notes: list[str] = []

    for route in offered:
        offer = routes[route]

        result_entry: JSONDict = {}

        # A partner Hugging Face marks as free is published with the shared `free`
        # marker and no price, exactly as OVH's free models are -- never as a price
        # of 0, which is also what a broken parser reads.
        if offer.get("is_free") is True:
            result_entry["free"] = True
        else:
            # ONE RULE for everything below: a route this scraper cannot price is noted
            # and skipped, never raised on. Raising freezes the whole provider, and the
            # other routes have done nothing wrong.
            #
            # That rule was learned twice. On 2026-09-07 Qwen/Qwen3.8-27B:ovhcloud
            # appeared priced 0/0 while marked not free, and the raise stopped the other
            # fifteen routes for a week. Only the zero branch was fixed, so on
            # 2026-09-14 deepseek-ai/DeepSeek-V4-Flash-0731:scaleway appeared with no
            # pricing object at all and froze the block again, from three lines away.
            # Hence one rule rather than four cases.
            #
            # The backstop is below, not here: if NO route survives, that is no longer
            # one bad route but a listing this scraper no longer understands, and the
            # run fails rather than publishing an empty block.
            #
            # check_price still raises, deliberately. A figure outside its bounds, or
            # one that moved further than MAX_CHANGE_FACTOR, is not an unpriceable route
            # -- it is a number this repository must not publish, and the whole point of
            # that guard is that nothing quietly routes around it.
            unusable: list[str] = []
            raw_pricing = offer.get("pricing")

            if not isinstance(raw_pricing, dict):
                unusable.append("no pricing object at all")
                field_specs_to_read: JSONDict = {}
                pricing: JSONDict = {}
            else:
                field_specs_to_read = field_specs
                pricing = cast(JSONDict, raw_pricing)

            for field, field_spec in field_specs_to_read.items():
                api_field = field_spec["api_field"]
                raw = pricing.get(api_field)

                if raw is None:
                    unusable.append(f"{api_field} missing (present: {sorted(pricing)})")
                    continue
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    unusable.append(f"{api_field} is {type(raw).__name__}, not a number")
                    continue
                if raw == 0:
                    unusable.append(f"{api_field} is 0")
                    continue

                # No scaling: unlike Eden AI's per-token figures, the router already
                # quotes per million tokens. Cross-checked against OVHcloud's own
                # catalog, whose EUR prices these track at the USD conversion rate.
                result_entry[field] = check_price(f"{PROVIDER_ID}/{route}", field, float(raw))

            # Skipped whole, not half. Unlike OVH's catalog -- where the units are
            # independent and a model can lose one and keep the others -- input and
            # output are two halves of one token price, and half of one prices nothing.
            # Published, not dropped. "The router sells this and will not say what it
            # costs" is a fact a consumer needs: it is the difference between a model
            # that does not exist and one that must not be called without checking the
            # bill first. Dropping the route threw that away and left a caller to
            # discover it at the invoice.
            #
            # The entry carries no price -- half a token price prices nothing, and a 0
            # would say the model is free -- so `unpriced` goes back with the reason in
            # words. provider_runner stamps the date, keeps any prices observed before
            # this happened, and reports the transition once rather than every Monday.
            if unusable:
                result_entry = {"unpriced": "; ".join(unusable)}

        result_entry["display_name"] = route
        models[route] = result_entry

    # The backstop, and it counts PRICED routes rather than routes. Marking one route
    # unpriced is an honest report; marking every one of them would silently turn the
    # whole provider into last-known figures, which is a change no consumer would see
    # coming and which no single upstream edit should be able to cause.
    if not any("unpriced" not in e for e in models.values()):
        raise ScrapeError(
            "not one live route from the mapped partners carries a price. Either Hugging "
            "Face restructured the listing, or it is quoting nothing at all. Refusing to "
            "republish the whole block as unpriced on the strength of one bad read."
        )

    return models, notes


def extract_new_models(fetch: Fetcher, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    """The one callback provider_runner needs. One source, read once."""
    return extract_models(parse_routes(fetch(mapping["source"])), mapping)


def main(argv: list[str] | None = None) -> int:
    return provider_runner.main(PROVIDER_ID, extract_new_models, DEFAULT_MAPPING, DEFAULT_CURRENT, CLI_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
