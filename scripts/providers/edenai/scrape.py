#!/usr/bin/env python3
"""Read Eden AI's public model list and work out what pricing.json's providers.edenai
block should say.

Standard library only. Reads a public endpoint and nothing else: it takes no API
key, must never be given one, and never touches anyone's Eden AI account.

This script DOES NOT WRITE pricing.json. It writes candidate files into an output
directory and reports which one, if any, the caller should promote. That is
deliberate: "leave pricing.json untouched on every failure path" is then a property
of the design rather than a branch of code somebody has to remember to get right.

Eden AI is an aggregator: you call one API and it forwards to a provider underneath.
The price published here is therefore what EDEN charges, which is not the price of
calling that provider directly -- codestral-latest is $1.00/$3.00 through Eden and
$0.30/$0.90 straight from Mistral. providers.edenai is a provider in its own right,
not a second opinion on providers.mistral.

Unlike the other providers here, the source is a real JSON API rather than a
marketing page: <https://api.edenai.run/v3/models>, public and keyless, with
`pricing.input_cost_per_token` and `pricing.output_cost_per_token` stated per model
under explicit field names. There is no label to string-match and no layout to
latch onto, so this scraper is short. What it still refuses to do is guess: the
mapping pins which upstream providers are covered and which model ids are expected,
so a model appearing or vanishing is reported rather than silently absorbed.

Outcomes, matching the three the workflow must implement:

  unchanged  figures identical -> out/stamped.json  (same figures, fresh checked_utc)
  changed    a figure moved    -> out/stamped.json and out/updated.json (the new figures)
  failure    fetch, parse or validation failed -> out/error.txt, exit code 1, no candidates

Usage:
    scripts/providers/edenai/scrape.py --out-dir .ci-out                     # live
    scripts/providers/edenai/scrape.py --out-dir .ci-out --html models.json  # offline
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
from provider_runner import ScrapeError  # noqa: E402
from pricing_validate import JSONDict, check_price  # noqa: E402

PROVIDER_ID = "edenai"

REPO_ROOT = _SCRIPTS_DIR.parent
DEFAULT_MAPPING = Path(__file__).resolve().parent / "mapping.json"
DEFAULT_CURRENT = REPO_ROOT / "pricing.json"

# Kept as its own constant rather than read off __doc__: the module docstring is
# stripped to None under `python -OO`, which would otherwise take this script down
# for a reason with nothing to do with argparse.
CLI_SUMMARY = "Read Eden AI's public model list and work out what providers.edenai should say."

# Prices arrive per single token and are published per million. Written out rather
# than left as a literal so the two places that care -- the conversion and the error
# messages -- cannot drift apart.
TOKENS_PER_MTOK = 1_000_000


def per_million(raw: float) -> float:
    """Scale a per-token price up to a per-million-token one, in decimal.

    `raw * 1_000_000` is wrong often enough to matter: it turns Eden's 4e-07 into
    0.39999999999999997 and 2e-07 into 0.19999999999999998, because neither the
    input nor the result is exactly representable in binary floating point. Left
    alone that publishes 48 of this provider's ~245 figures as long strings of
    noise, and makes them churn in a diff a human is supposed to be reading for
    real price movements.

    Going through Decimal, seeded from `repr(raw)` -- the shortest string that
    round-trips back to the same float, which is the decimal number the endpoint
    meant -- makes the scaling exact, and the float it converts back to prints as
    the plain 0.4 a reviewer expects.
    """
    return float(Decimal(repr(raw)) * TOKENS_PER_MTOK)


def parse_model_list(body: str) -> dict[str, JSONDict]:
    """Parse the endpoint's response into {model id: model object}.

    Raises ScrapeError, never returns something partial: every shape assumption is
    checked here so that the mapping step below can read plain fields.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ScrapeError(f"the model list is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ScrapeError(f"the model list is not an object (got {type(payload).__name__})")
    payload = cast(JSONDict, payload)

    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise ScrapeError(
            'the model list has no non-empty "data" array. Eden AI\'s endpoint has '
            "changed shape, or answered with an error document."
        )
    data = cast("list[Any]", data)

    models: dict[str, JSONDict] = {}
    for entry in data:
        if not isinstance(entry, dict):
            raise ScrapeError("a model list entry is not an object -- the data shape has changed.")
        entry = cast(JSONDict, entry)

        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            raise ScrapeError('a model list entry has no string "id" -- the data shape has changed.')
        if model_id in models:
            raise ScrapeError(
                f"the model list contains {model_id!r} more than once. Refusing to guess "
                f"which entry carries the real price."
            )
        models[model_id] = entry

    return models


def extract_models(catalog: dict[str, JSONDict], mapping: JSONDict) -> dict[str, JSONDict]:
    """Turn the model list into a `models` block, through the explicit mapping only."""
    upstreams = mapping["upstreams"]
    expected: list[str] = mapping["models"]
    field_specs: JSONDict = mapping["fields"]

    # The mapping pins the exact set. Eden adding or retiring a model is a real
    # change a human should see, not something to absorb quietly in either
    # direction: a vanished model would otherwise disappear from pricing.json
    # without a word, and a new one would appear without anyone reading its price.
    missing = [m for m in expected if m not in catalog]
    if missing:
        raise ScrapeError(
            f"{len(missing)} mapped model(s) are no longer in Eden AI's list: {missing[:10]}. "
            f"Either Eden retired them or renamed them. Update {DEFAULT_MAPPING} by hand "
            f"after checking {mapping['source']} -- do not let this be guessed."
        )

    offered = sorted(m for m, e in catalog.items() if e.get("owned_by") in upstreams)
    unexpected = [m for m in offered if m not in set(expected)]
    if unexpected:
        raise ScrapeError(
            f"Eden AI now offers {len(unexpected)} model(s) from the mapped upstreams that "
            f"{DEFAULT_MAPPING} does not list: {unexpected[:10]}. Add them by hand after "
            f"reading their prices, or the file would publish a figure nobody reviewed."
        )

    models: dict[str, JSONDict] = {}

    for model_id in expected:
        entry = catalog[model_id]

        # `list_pricing` is the public list price; `pricing` is what the caller of the
        # endpoint would actually pay, which can be lower when a discount applies to
        # the account asking. This scraper is unauthenticated, so the two agree today
        # -- but publishing the list price is the only figure that means the same
        # thing to every consumer, so that is the one read, and a divergence is
        # reported rather than silently resolved.
        listed = entry.get("list_pricing")
        effective = entry.get("pricing")
        if not isinstance(listed, dict):
            raise ScrapeError(f"{model_id}: no list_pricing object. The data shape has changed.")
        listed = cast(JSONDict, listed)
        if effective != listed:
            raise ScrapeError(
                f"{model_id}: list_pricing and pricing disagree, which means this "
                f"unauthenticated read is being quoted a discounted rate. Publishing it "
                f"would give consumers a price only this caller can get. Resolve by hand."
            )

        result_entry: JSONDict = {}

        for field, field_spec in field_specs.items():
            api_field = field_spec["api_field"]
            raw = listed.get(api_field)

            if raw is None:
                if field_spec.get("optional"):
                    continue
                raise ScrapeError(
                    f"{model_id}.{field}: no {api_field!r} in list_pricing. Fields present: "
                    f"{sorted(listed)}. A model this file publishes must state this price."
                )

            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ScrapeError(
                    f"{model_id}.{field}: {api_field!r} is {type(raw).__name__}, not a number. "
                    f"The data shape has changed."
                )

            result_entry[field] = check_price(
                f"{PROVIDER_ID}/{model_id}", field, per_million(float(raw))
            )

        if not result_entry:
            raise ScrapeError(f"{model_id}: no price field could be read at all.")

        display_name = entry.get("model_name")
        result_entry["display_name"] = display_name if isinstance(display_name, str) and display_name else model_id
        models[model_id] = result_entry

    return models


def extract_new_models(body: str, mapping: JSONDict) -> dict[str, JSONDict]:
    """The one callback provider_runner needs: source text + mapping -> a models dict."""
    return extract_models(parse_model_list(body), mapping)


def main(argv: list[str] | None = None) -> int:
    return provider_runner.main(PROVIDER_ID, extract_new_models, DEFAULT_MAPPING, DEFAULT_CURRENT, CLI_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
