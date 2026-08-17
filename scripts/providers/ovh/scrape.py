#!/usr/bin/env python3
"""Read OVH's AI Endpoints catalog and work out what pricing.json's providers.ovh
block should say.

Standard library only. Reads a public web page and nothing else: it takes no API
key, must never be given one, and never touches anyone's OVH account.

This script DOES NOT WRITE pricing.json. It writes candidate files into an output
directory and reports which one, if any, the caller should promote. That is
deliberate: "leave pricing.json untouched on every failure path" is then a property
of the design rather than a branch of code somebody has to remember to get right.

pricing.json holds one block per provider under `providers.<name>`, because the same
model name can mean two different prices at two different providers. This script
only ever reads and writes `providers.ovh`: every other provider's block is carried
through the candidate files byte-for-byte, untouched. The plumbing that makes that
true -- reading, merging, writing, the three outcomes -- is shared with every other
provider and lives in scripts/provider_runner.py. This file supplies only what is
genuinely OVH-specific: how to read OVH's catalog page.

OVH's catalog is a Next.js app. Unlike Mistral's page, the price is not something a
CSS selector reads off rendered card markup: the full model list -- id, display
name, and a machine-readable `metadata.usage_information.pricing` list -- is
embedded in the page as JSON, inside a React Server Components ("Flight") data
chunk (`self.__next_f.push([1, "..."])`). The rendered "0.08€/Mtoken" text a
browser shows is derived from that same JSON at render time; reading the JSON
directly is more robust than re-parsing a formatted, French-language price string,
and it is what this scraper does.

Outcomes, matching the three the workflow must implement:

  unchanged  figures identical -> out/stamped.json  (same figures, fresh checked_utc)
  changed    a figure moved    -> out/stamped.json and out/updated.json (the new figures)
  failure    fetch, parse or validation failed -> out/error.txt, exit code 1, no candidates

Usage:
    scripts/providers/ovh/scrape.py --out-dir .ci-out                  # fetch the live page
    scripts/providers/ovh/scrape.py --out-dir .ci-out --html page.html # offline, for tests
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, cast

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

import provider_runner  # noqa: E402
from provider_runner import ScrapeError  # noqa: E402
from pricing_validate import JSONDict, check_price  # noqa: E402

PROVIDER_ID = "ovh"

REPO_ROOT = _SCRIPTS_DIR.parent
DEFAULT_MAPPING = Path(__file__).resolve().parent / "mapping.json"
DEFAULT_CURRENT = REPO_ROOT / "pricing.json"

# Kept as its own constant rather than read off __doc__: the module docstring is
# stripped to None under `python -OO`, which would otherwise take this script down
# for a reason with nothing to do with argparse.
CLI_SUMMARY = "Read OVH's AI Endpoints catalog and work out what providers.ovh should say."

_PUSH_START = re.compile(r'self\.__next_f\.push\(\[1,"')


# --------------------------------------------------------------------------------------
# Page parsing
# --------------------------------------------------------------------------------------


def _extract_push_strings(html_text: str) -> list[str]:
    """Every raw (still JS-escaped) string literal argument to a
    `self.__next_f.push([1, "..."])` call, in document order.

    Regex only finds where each one starts; the string itself is walked
    character by character so an escaped quote (`\\"`) inside it is never
    mistaken for the string's real closing quote.
    """
    out: list[str] = []
    for m in _PUSH_START.finditer(html_text):
        i = m.end()
        start = i
        while i < len(html_text):
            ch = html_text[i]
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                break
            i += 1
        out.append(html_text[start:i])
    return out


def _extract_bracketed_array(text: str, key: str) -> str:
    """Return the raw `[...]` substring for `"<key>":[...]` in `text`.

    Matches brackets one character at a time, skipping over quoted strings,
    rather than a regex -- the array holds nested arrays and objects of its own,
    which a regex cannot reliably bound.
    """
    marker = f'"{key}":['
    idx = text.index(marker)
    start = idx + len(marker) - 1
    depth = 0
    in_str = False
    i = start
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        i += 1
    raise ValueError(f"unterminated array for key {key!r}")


def extract_catalog_models(html_text: str) -> list[JSONDict]:
    """Pull the full model catalog out of the page's embedded React Flight data."""
    try:
        chunks = _extract_push_strings(html_text)
    except Exception as exc:  # a scan crash is a layout failure like any other
        raise ScrapeError(f"the page could not be scanned for its embedded data: {exc}") from exc

    if not chunks:
        raise ScrapeError(
            'no self.__next_f.push([1,"...")] data chunks found on the page. The layout '
            "has changed: this scraper expects OVH's catalog to embed its model list as "
            "a Next.js React Server Components payload."
        )

    target = next((c for c in chunks if '\\"models\\":[' in c), None)
    if target is None:
        raise ScrapeError(
            'none of the page\'s embedded data chunks contain a "models" array. '
            "The catalog's data shape has changed."
        )

    try:
        decoded = json.loads('"' + target + '"')
    except json.JSONDecodeError as exc:
        raise ScrapeError(f'the chunk containing "models" could not be decoded: {exc}') from exc

    try:
        models_array = _extract_bracketed_array(decoded, "models")
        models = json.loads(models_array)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ScrapeError(f'the "models" array could not be parsed: {exc}') from exc

    if not isinstance(models, list) or not models:
        raise ScrapeError('the "models" array is empty or not a list -- the catalog looks empty.')
    models = cast("list[Any]", models)

    for entry in models:
        if not isinstance(entry, dict):
            raise ScrapeError('a catalog entry is missing a string "id" -- the data shape has changed.')
        entry = cast(JSONDict, entry)
        if not isinstance(entry.get("id"), str):
            raise ScrapeError('a catalog entry is missing a string "id" -- the data shape has changed.')

    return cast("list[JSONDict]", models)


# --------------------------------------------------------------------------------------
# Mapping the catalog onto API model ids
# --------------------------------------------------------------------------------------


def extract_models(
    catalog_models: list[JSONDict], mapping: JSONDict
) -> tuple[dict[str, JSONDict], list[str]]:
    """Turn the catalog into a `models` block: every model the catalog prices, no list.

    The mapping says which PRICING UNITS this repository knows how to publish, and
    nothing else. Which models the catalog offers is OVH's business and changes week to
    week; following that is the whole point of this job. A model OVH adds is published
    on the next run, one it withdraws keeps its last observed prices under an
    `absent_since` stamp, and nothing here needs a human to reconcile a list first.

    That is possible because the catalog states the callable model id itself, in
    `name`. Note `name`, NOT `id`: `id` is a CMS slug for the catalog's own URLs
    ("qwen-3-6-27b" against the callable "Qwen3.6-27B"), and publishing it would give
    consumers a string OVH's API does not answer to.

    A unit the mapping does not know is skipped and reported, not guessed at: the unit
    is part of the field name, so there is no honest way to publish a figure whose unit
    this repository cannot name. Skipping one row still publishes the others, which is
    why this returns notes rather than raising.
    """
    units: dict[str, str] = mapping["units"]

    models: dict[str, JSONDict] = {}
    notes: list[str] = []

    for entry in catalog_models:
        catalog_id = cast(str, entry["id"])
        model_id = entry.get("name")
        if not isinstance(model_id, str) or not model_id:
            raise ScrapeError(
                f"catalog entry {catalog_id!r} has no string 'name'. That field is the "
                f"callable model id and the key this block is published under, so the "
                f"catalog's data shape has changed in a way that cannot be worked around."
            )
        if model_id in models:
            raise ScrapeError(
                f"the catalog lists two entries named {model_id!r}. Refusing to guess "
                f"which one carries the real price."
            )

        raw_pricing = entry.get("metadata", {}).get("usage_information", {}).get("pricing")
        if not isinstance(raw_pricing, list) or not raw_pricing:
            notes.append(
                f"{model_id}: the catalog lists it with no "
                f"metadata.usage_information.pricing, so there is no price to publish. "
                f"Skipped."
            )
            continue
        raw_pricing = cast("list[Any]", raw_pricing)
        if not all(isinstance(p, dict) for p in raw_pricing):
            raise ScrapeError(
                f"{model_id}: {catalog_id!r}'s metadata.usage_information.pricing "
                f"list contains something other than objects. The catalog's data "
                f"shape has changed."
            )
        pricing = cast("list[JSONDict]", raw_pricing)

        result_entry: JSONDict = {}

        # A model OVH gives away is published with the shared `free` marker and no price
        # field, never as a price of 0 -- 0 is also exactly what a broken parser reads,
        # which is what check_price's floor exists to catch. "Free" is read off the
        # catalog every week rather than remembered, so the day OVH starts charging for
        # one of these, the price simply appears and the marker simply goes.
        prices = [p.get("price") for p in pricing]
        if all(not isinstance(p, bool) and isinstance(p, (int, float)) and p == 0 for p in prices):
            result_entry["free"] = True
            result_entry["display_name"] = model_id
            models[model_id] = result_entry
            continue

        for price_entry in pricing:
            price_unit = str(price_entry.get("price_unit"))
            field = units.get(price_unit)

            if field is None:
                notes.append(
                    f"{model_id}: priced per {price_unit!r}, a unit "
                    f"{DEFAULT_MAPPING.name} has no field for. That price is not "
                    f"published. Add the unit to the mapping's 'units' table, and a "
                    f"matching field to KNOWN_PRICE_FIELDS, to publish it."
                )
                continue

            if field in result_entry:
                raise ScrapeError(
                    f"{model_id}.{field}: {catalog_id!r} has two pricing entries mapping "
                    f"to it. Refusing to guess which is the price."
                )

            price = price_entry.get("price")
            # Zero beside a real price is not "free" -- free is all-zero, handled above.
            # It is either a genuine giveaway of one side of a token price, which this
            # schema has no way to say, or a misparse. Either way it is not publishable.
            if not isinstance(price, bool) and isinstance(price, (int, float)) and price == 0:
                notes.append(
                    f"{model_id}: priced at 0 per {price_unit!r} while charging for other "
                    f"units. Not published: this file distinguishes a free model, which is "
                    f"free in every unit, from a price of 0, which is also what a misparse "
                    f"reads."
                )
                continue

            result_entry[field] = check_price(f"{PROVIDER_ID}/{model_id}", field, price)

        if not result_entry:
            notes.append(f"{model_id}: no publishable price could be read at all. Skipped.")
            continue

        result_entry["display_name"] = model_id
        models[model_id] = result_entry

    if not models:
        raise ScrapeError(
            "the catalog priced not a single model this scraper could publish. Either "
            "OVH restructured it, or the page is not what it looks like. Refusing to "
            "publish an empty block."
        )

    return models, notes


def extract_new_models(html_text: str, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    """The one callback provider_runner needs: page text + mapping -> a models dict."""
    return extract_models(extract_catalog_models(html_text), mapping)


def main(argv: list[str] | None = None) -> int:
    return provider_runner.main(PROVIDER_ID, extract_new_models, DEFAULT_MAPPING, DEFAULT_CURRENT, CLI_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
