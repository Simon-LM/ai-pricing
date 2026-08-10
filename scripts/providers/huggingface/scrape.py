#!/usr/bin/env python3
"""Read Hugging Face's Inference Providers router listing and work out what
pricing.json's providers.huggingface block should say.

Standard library only. Reads a public endpoint and nothing else: it takes no API
key, must never be given one, and never touches anyone's Hugging Face account.

This script DOES NOT WRITE pricing.json. It writes candidate files into an output
directory and reports which one, if any, the caller should promote. That is
deliberate: "leave pricing.json untouched on every failure path" is then a property
of the design rather than a branch of code somebody has to remember to get right.

Hugging Face routes a call to a partner that actually serves the model, and the same
model is served by several partners at several prices -- openai/gpt-oss-120b is
offered by eleven of them, from $0.05 to $0.35 per million input tokens. So the unit
of pricing here is not a model, it is a (model, partner) PAIR, and the key published
for it is exactly the string the router takes: `<model id>:<partner>`, the form
Hugging Face's own documentation uses ("openai/gpt-oss-120b:groq"). A key without
the suffix would name eleven different prices at once.

This block covers only the partners named in mapping.json. Note what that means: a
price here is what the partner charges when reached THROUGH the router, and is a
different figure from calling that partner directly -- OVHcloud's own catalog prices
Qwen3.6-27B at 0.40 EUR while the router quotes 0.47 USD for the same model. Both
are correct and both are in this file, under their own provider.

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
    expected: list[str] = mapping["models"]
    field_specs: JSONDict = mapping["fields"]

    # The mapping pins the exact set. A route appearing or disappearing is a real
    # change a human should see, not something to absorb quietly in either
    # direction: a vanished route would otherwise drop out of pricing.json without
    # a word, and a new one would appear without anyone reading its price.
    missing = [r for r in expected if r not in routes]
    if missing:
        raise ScrapeError(
            f"{len(missing)} mapped route(s) are no longer offered: {missing[:10]}. Either "
            f"the partner stopped serving the model or Hugging Face renamed it. Update "
            f"{DEFAULT_MAPPING} by hand after checking {mapping['source']} -- do not let "
            f"this be guessed."
        )

    offered = sorted(r for r, o in routes.items() if o.get("provider") in partners)
    unexpected = [r for r in offered if r not in set(expected)]
    if unexpected:
        raise ScrapeError(
            f"the mapped partners now serve {len(unexpected)} route(s) that "
            f"{DEFAULT_MAPPING} does not list: {unexpected[:10]}. Add them by hand after "
            f"reading their prices, or the file would publish a figure nobody reviewed."
        )

    models: dict[str, JSONDict] = {}

    for route in expected:
        offer = routes[route]

        status = offer.get("status")
        if status != LIVE:
            raise ScrapeError(
                f"{route}: the router reports status {status!r}, not {LIVE!r}. Publishing a "
                f"price for a route that cannot serve a call would be worse than publishing "
                f"nothing."
            )

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


def extract_new_models(body: str, mapping: JSONDict) -> dict[str, JSONDict]:
    """The one callback provider_runner needs: source text + mapping -> a models dict."""
    return extract_models(parse_routes(body), mapping)


def main(argv: list[str] | None = None) -> int:
    return provider_runner.main(PROVIDER_ID, extract_new_models, DEFAULT_MAPPING, DEFAULT_CURRENT, CLI_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
