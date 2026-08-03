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


def extract_models(catalog_models: list[JSONDict], mapping: JSONDict) -> dict[str, JSONDict]:
    """Turn the catalog into a `models` block, through the explicit mapping only.

    Every lookup here is an exact string match against a hand-committed value. A
    catalog id, or a pricing unit, the mapping does not know is a failure to
    report, never a row to guess at.
    """
    by_catalog_id: dict[str, JSONDict] = {}
    for entry in catalog_models:
        catalog_id = cast(str, entry["id"])
        if catalog_id in by_catalog_id:
            raise ScrapeError(
                f"the catalog lists {catalog_id!r} more than once. Refusing to guess "
                f"which entry carries the real price."
            )
        by_catalog_id[catalog_id] = entry

    models: dict[str, JSONDict] = {}

    for model_id, spec in mapping["models"].items():
        catalog_id = spec["catalog_id"]
        entry = by_catalog_id.get(catalog_id)

        if entry is None:
            raise ScrapeError(
                f"{model_id}: the catalog has no model with id {catalog_id!r}. Either OVH "
                f"renamed or withdrew it. Update {DEFAULT_MAPPING} by hand after checking "
                f"{mapping['source']} -- do not let this be guessed."
            )

        raw_pricing = entry.get("metadata", {}).get("usage_information", {}).get("pricing")
        if not isinstance(raw_pricing, list):
            raise ScrapeError(
                f"{model_id}: {catalog_id!r} has no metadata.usage_information.pricing "
                f"list. The catalog's data shape has changed."
            )
        raw_pricing = cast("list[Any]", raw_pricing)
        if not all(isinstance(p, dict) for p in raw_pricing):
            raise ScrapeError(
                f"{model_id}: {catalog_id!r}'s metadata.usage_information.pricing "
                f"list contains something other than objects. The catalog's data "
                f"shape has changed."
            )
        pricing = cast("list[JSONDict]", raw_pricing)

        result_entry: JSONDict = {}

        for field, field_spec in spec["fields"].items():
            price_unit = field_spec["price_unit"]
            matches = [p for p in pricing if p.get("price_unit") == price_unit]

            if not matches:
                # A pricing entry missing its own price_unit is itself a shape change,
                # not something to silently drop from this message -- but it must not
                # crash the message either: sorting a set that mixes None with strings
                # raises, and this branch runs precisely when the catalog looks wrong.
                found_units = sorted(str(p.get("price_unit")) for p in pricing)
                raise ScrapeError(
                    f"{model_id}.{field}: {catalog_id!r} has no pricing entry with "
                    f"price_unit {price_unit!r}. Units found: {found_units}. The unit may "
                    f"have changed; {field} would no longer mean what it says."
                )
            if len(matches) > 1:
                raise ScrapeError(
                    f"{model_id}.{field}: {catalog_id!r} has {len(matches)} pricing entries "
                    f"with price_unit {price_unit!r}. Refusing to guess which is the price."
                )

            result_entry[field] = check_price(f"{PROVIDER_ID}/{model_id}", field, matches[0].get("price"))

        result_entry["display_name"] = spec["display_name"]
        models[model_id] = result_entry

    return models


def extract_new_models(html_text: str, mapping: JSONDict) -> dict[str, JSONDict]:
    """The one callback provider_runner needs: page text + mapping -> a models dict."""
    return extract_models(extract_catalog_models(html_text), mapping)


def main(argv: list[str] | None = None) -> int:
    return provider_runner.main(PROVIDER_ID, extract_new_models, DEFAULT_MAPPING, DEFAULT_CURRENT, CLI_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
