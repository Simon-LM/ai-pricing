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
from provider_runner import ScrapeError  # noqa: E402
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


def extract_models(routes: dict[str, JSONDict], mapping: JSONDict) -> dict[str, JSONDict]:
    """Turn the routes into a `models` block, through the explicit mapping only."""
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

    for route in offered:
        offer = routes[route]

        result_entry: JSONDict = {}

        # A partner Hugging Face marks as free is published with the shared `free`
        # marker and no price, exactly as OVH's free models are -- never as a price
        # of 0, which is also what a broken parser reads.
        if offer.get("is_free") is True:
            result_entry["free"] = True
        else:
            pricing = offer.get("pricing")
            if not isinstance(pricing, dict):
                raise ScrapeError(
                    f"{route}: no pricing object and not marked free. The data shape has "
                    f"changed, or this route stopped publishing a price."
                )
            pricing = cast(JSONDict, pricing)

            for field, field_spec in field_specs.items():
                api_field = field_spec["api_field"]
                raw = pricing.get(api_field)

                if raw is None:
                    raise ScrapeError(
                        f"{route}.{field}: no {api_field!r} in pricing. Fields present: "
                        f"{sorted(pricing)}. A route this file publishes must state this price."
                    )
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    raise ScrapeError(
                        f"{route}.{field}: {api_field!r} is {type(raw).__name__}, not a number. "
                        f"The data shape has changed."
                    )

                # No scaling: unlike Eden AI's per-token figures, the router already
                # quotes per million tokens. Cross-checked against OVHcloud's own
                # catalog, whose EUR prices these track at the USD conversion rate.
                result_entry[field] = check_price(f"{PROVIDER_ID}/{route}", field, float(raw))

        result_entry["display_name"] = route
        models[route] = result_entry

    return models


def extract_new_models(body: str, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    """The one callback provider_runner needs: source text + mapping -> a models dict.

    No notes: the listing states every route's price under a fixed field name, so there
    is nothing left over for a human to look at.
    """
    return extract_models(parse_routes(body), mapping), []


def main(argv: list[str] | None = None) -> int:
    return provider_runner.main(PROVIDER_ID, extract_new_models, DEFAULT_MAPPING, DEFAULT_CURRENT, CLI_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
