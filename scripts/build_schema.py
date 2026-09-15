#!/usr/bin/env python3
"""Generate pricing.schema.json from pricing_validate.py, which stays the authority.

`pricing.json` is published at a stable public URL and carries a `schema_version`,
and the README tells a consumer that does not recognise that number to ignore the
file. That instruction presumes the consumer can find out what the number means --
and until this script existed the only way was to read Python. A public contract
whose specification is only readable by running the producer's implementation is
half-published.

So the schema is a delivered artifact rather than prose. It is GENERATED, never
hand-written: every rule it states is read out of the constants in
pricing_validate.py, so there is one definition of the contract and this file only
re-expresses it. A test regenerates and compares, and fails if the committed
artifact has drifted from the validator.

What it deliberately cannot say is listed in the schema's own description. JSON
Schema describes one document; three of this repository's rules are not properties
of one document at all:

    the change factor    compares a figure against the PREVIOUS file
    duplicate api_ids    across entries, not within one
    currency pinning     compares the block against a provider's mapping.json

A consumer that validates against this schema has checked the shape, the units and
the plausible range of every figure. It has not checked that the file is sane, and
saying so here is the difference between a useful artifact and a misleading one.

Usage:
    scripts/build_schema.py            # write pricing.schema.json
    scripts/build_schema.py --check    # exit 1 if the committed file is stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

import pricing_validate as validate  # noqa: E402

REPO_ROOT = _SCRIPTS_DIR.parent
SCHEMA_PATH = REPO_ROOT / "pricing.schema.json"
SCHEMA_URL = "https://raw.githubusercontent.com/Simon-LM/ai-pricing/main/pricing.schema.json"

CANNOT_EXPRESS = (
    "Generated from scripts/pricing_validate.py, which remains the authority -- do "
    "not edit this file by hand. It describes one document, so three rules this "
    "repository enforces are necessarily absent: a figure may not move by more than "
    "a factor of "
    f"{validate.MAX_CHANGE_FACTOR:g} from the previous published file; api_ids must "
    "not repeat across entries; and a block's currency must match the mapping its "
    "scraper reads. Validating against this schema proves the shape, the units and "
    "the plausible range of every figure. It does not prove the file is sane."
)


def _price_property(field: str) -> dict[str, Any]:
    low, high = validate.PRICE_BOUNDS.get(field, (validate.MIN_PLAUSIBLE, validate.MAX_PLAUSIBLE))
    return {
        "type": "number",
        "minimum": low,
        "maximum": high,
        "description": (
            f"Price in the block's currency, per the unit named by the key. A figure "
            f"outside [{low:g}, {high:g}] normally means a page changed shape and the "
            f"wrong number was read, not that the price moved."
        ),
    }


def build() -> dict[str, Any]:
    """Return the whole schema, every rule read out of the validator's constants."""
    day = validate._ISO_DAY.pattern
    stamp = validate._ISO_UTC.pattern

    # Either a stated price, or the marker that explains its absence.
    priced_or_unpriced = [{"required": [f]} for f in validate.KNOWN_PRICE_FIELDS]
    priced_or_unpriced.append({"required": ["unpriced_since"]})

    entry: dict[str, Any] = {
        "type": "object",
        "required": ["display_name"],
        "additionalProperties": False,
        "properties": {
            "display_name": {
                "type": "string",
                "minLength": 1,
                "description": "A human-readable label, kept so a diff is readable. Never match on it.",
            },
            "free": {
                "const": True,
                "description": (
                    "Present, and only ever true, when the provider gives this away. The "
                    "entry then carries no price field at all -- never a price of 0."
                ),
            },
            "kind": {
                "enum": list(validate.KNOWN_KINDS),
                "description": (
                    "Absent means \"model\". \"product\" is a billable thing that is not a "
                    "model and has no API model id."
                ),
            },
            "absent_since": {
                "type": "string",
                "pattern": day,
                "description": (
                    "The day the source was first seen no longer offering this. Every price "
                    "beside it is the last one observed, not a current one. Dropped after "
                    f"{validate.ABSENT_RETENTION_DAYS} days."
                ),
            },
            "unpriced_since": {
                "type": "string",
                "pattern": day,
                "description": (
                    "The day the source was first seen offering this, still calling it paid, "
                    "and stating no usable price. It may carry the last prices observed, or "
                    "none at all. A consumer must not quote a price for such an entry."
                ),
            },
            "api_ids": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
                "description": (
                    "The strings to pass AS THE MODEL when calling that provider, most "
                    "specific first. A model identifier, never a URL: this file publishes "
                    "no endpoints."
                ),
            },
            **{field: _price_property(field) for field in validate.KNOWN_PRICE_FIELDS},
        },
        # Two cross-field rules, and the same list answers both: a free entry may carry
        # none of these, and every other entry must carry at least one. They describe a
        # single document, so unlike the three named in the description they belong here
        # rather than only in Python.
        "allOf": [
            {
                "if": {"required": ["free"]},
                "then": {
                    "not": {"anyOf": priced_or_unpriced},
                    "description": (
                        "Free and priced at once is a contradiction, and so is free and "
                        "unpriced_since: a source cannot both give this away and decline "
                        "to say what it costs."
                    ),
                },
                "else": {
                    "anyOf": priced_or_unpriced,
                    "description": (
                        "An entry that is not free states a price, or says with "
                        "unpriced_since why it cannot."
                    ),
                },
            },
        ],
    }

    provider_block: dict[str, Any] = {
        "type": "object",
        "required": ["checked_utc", "updated", "source", "currency", "models"],
        "additionalProperties": False,
        "properties": {
            "checked_utc": {
                "type": "string",
                "pattern": stamp,
                "description": "When this provider's figures were last VERIFIED, changed or not.",
            },
            "updated": {
                "type": "string",
                "pattern": day,
                "description": "When this provider's figures last actually CHANGED.",
            },
            "source": {
                "type": "string",
                "minLength": 1,
                "description": "The page these numbers came from. Always present.",
            },
            "sources": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
                "description": (
                    "Present only where a block is built from more than one page: the full "
                    "list, in the order read, always containing `source`."
                ),
            },
            "currency": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Never assume it, and never assume it matches another provider's. "
                    "Nothing in this repository performs conversion."
                ),
            },
            "models": {
                "type": "object",
                "minProperties": 1,
                "additionalProperties": {"$ref": "#/$defs/entry"},
                "description": "Keyed by whatever identifies the entry at that source.",
            },
        },
    }

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_URL,
        "title": f"ai-pricing published prices, schema_version {validate.SCHEMA_VERSION}",
        "description": CANNOT_EXPRESS,
        "type": "object",
        "required": ["schema_version", "providers"],
        "additionalProperties": False,
        "properties": {
            "schema_version": {
                "const": validate.SCHEMA_VERSION,
                "description": (
                    "A consumer that does not recognise this number must ignore the file "
                    "and fall back, rather than misread it."
                ),
            },
            "providers": {
                "type": "object",
                "minProperties": 1,
                "additionalProperties": {"$ref": "#/$defs/providerBlock"},
                "description": (
                    "Keyed by provider id. Never flatten this: the same model name at two "
                    "providers is two different prices, not a collision to resolve."
                ),
            },
        },
        "$defs": {"providerBlock": provider_block, "entry": entry},
    }


def render() -> str:
    return json.dumps(build(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if the committed schema is stale"
    )
    parser.add_argument("--out", default=str(SCHEMA_PATH))
    args = parser.parse_args(argv)

    wanted = render()
    out = Path(args.out)
    if args.check:
        current = out.read_text(encoding="utf-8") if out.is_file() else ""
        if current != wanted:
            print(
                f"{out} is not what pricing_validate.py describes. "
                f"Run scripts/build_schema.py and commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"{out} matches the validator.")
        return 0

    out.write_text(wanted, encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
