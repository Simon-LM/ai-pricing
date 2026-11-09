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
latch onto, so this scraper is short. The mapping picks which UPSTREAM providers are
covered; which models each of them offers is read from the endpoint every week, not
written down here.

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
from provider_runner import Fetcher, ScrapeError  # noqa: E402
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

    Some figures already carry the noise in the endpoint itself: from November 2026
    Eden states its `@eu` prices as 4.4000000000000004e-08 and 7.700000000000001e-08,
    and the candidate file's own noise check refused the whole block for two of them.
    Such a figure is read at 15 significant digits, which drops the binary tail and
    nothing else, but only when that shortens it by four characters or more: a
    converted price such as 6.715796448686838e-07 is precise, not noisy, and is kept
    to the last digit.
    """
    text = repr(raw)
    rounded = f"{raw:.15g}"
    if len(rounded) <= len(text) - 4:
        text = rounded
    return float(Decimal(text) * TOKENS_PER_MTOK)


def _not_scaled_by(listed: Any, effective: Any, factor: float, path: str = "") -> str | None:
    """Walk two price objects in step and describe the first leaf the scaling misses.

    Numbers must be `listed * factor`; objects must state the same fields and are
    walked key by key; anything else -- a list, a string, a null -- is not something
    a discount scales, so it must arrive unchanged (Eden's tiered arrays stop the run
    through that branch rather than being vouched for leaf by leaf: they also carry
    `range` bounds a discount does not touch, so "which numbers does it multiply"
    has no safe answer inside one). Returns the description, or None when `effective`
    is `listed` scaled by `factor` throughout.
    """
    where = path or "pricing"
    if isinstance(listed, dict):
        if not isinstance(effective, dict):
            return f"{where}: pricing holds {effective!r} where list_pricing holds an object"
        if set(listed) != set(effective):
            return f"{where}: pricing and list_pricing do not state the same fields"
        for key in sorted(listed):
            reason = _not_scaled_by(listed[key], effective[key], factor, f"{path}.{key}" if path else key)
            if reason is not None:
                return reason
        return None
    if isinstance(listed, bool) or not isinstance(listed, (int, float)):
        if effective == listed:
            return None
        return (
            f"{where}: pricing holds {effective!r} where list_pricing holds "
            f"{listed!r}, which no discount scales"
        )
    scaled = listed * factor
    if isinstance(effective, bool) or not isinstance(effective, (int, float)) or effective != scaled:
        return (
            f"{where}: pricing holds {effective!r}; list_pricing scaled by the "
            f"stated discount gives {scaled!r} (from {listed!r})"
        )
    return None


def unexplained_by_public_discount(listed: JSONDict, effective: Any, discount: Any) -> str | None:
    """Why `pricing` is not `list_pricing` under the entry's own public discount, or None.

    None means the divergence is fully explained: `pricing` is `list_pricing` with
    exactly the stated discount taken off, figure for figure -- an unauthenticated
    read being quoted a promotion the response itself declares, which every consumer
    of this file gets. Anything else comes back as a description of what does not
    add up.

    "Exactly" is judged in Eden's arithmetic, not an idealised decimal one: Eden
    computes the discounted figures in binary floating point (`1.5e-07` at 35% off
    arrives as `9.749999999999999e-08`, not `9.75e-08`), and a decimal re-derivation
    disagreed with its own bytes on 61 of the 79 discounted models it served on
    2026-10-12. So the comparison reproduces plain floating-point multiplication by
    `1 - discount`, and trusts that result -- and nothing else.
    """
    if effective == listed:
        return None
    if not isinstance(effective, dict):
        return "the response states no `pricing` object to compare it with"
    if discount is None:
        return "the response states no `discount` that could account for the lower rate"
    if isinstance(discount, bool) or not isinstance(discount, (int, float)) or not (0 < discount < 1):
        return f"`discount` is {discount!r}, which is not a fraction between 0 and 1"
    return _not_scaled_by(listed, effective, 1 - discount)


def parse_model_list(body: str) -> dict[str, list[JSONDict]]:
    """Parse the endpoint's response into {model id: [every entry listed under it]}.

    Raises ScrapeError, never returns something partial: every shape assumption is
    checked here so that the mapping step below can read plain fields. An id listed
    more than once keeps all its entries; `pick_entries` decides what that means.
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

    models: dict[str, list[JSONDict]] = {}
    for entry in data:
        if not isinstance(entry, dict):
            raise ScrapeError("a model list entry is not an object -- the data shape has changed.")
        entry = cast(JSONDict, entry)

        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            raise ScrapeError('a model list entry has no string "id" -- the data shape has changed.')
        models.setdefault(model_id, []).append(entry)

    return models


def _what_it_charges(entry: JSONDict) -> str:
    """Everything in an entry that bears on a price, as one comparable string."""
    return json.dumps(
        [entry.get("pricing"), entry.get("list_pricing"), entry.get("discount")], sort_keys=True
    )


def pick_entries(
    groups: dict[str, list[JSONDict]], mapping: JSONDict
) -> tuple[dict[str, JSONDict], set[str], list[str]]:
    """One entry per model id, and the ids no entry can be picked for without a human.

    Eden sometimes lists one id twice. Two entries that charge the same are one model
    listed twice: either will do. Two that charge differently cannot both be right, and
    nothing in the response says which is: `mistral/mistral-large-4` came twice from
    2026-10-06, at $0.68/$2.09 and at $0.70/$2.10, and the refusal that followed stopped
    every Eden price for four weeks. So such an id is HELD -- it keeps what is published,
    marked, and a note asks a human -- and every other model goes on. The human's pick
    is written in the mapping's `duplicate_choices`, by the entry's `created` stamp, and
    applies from the next run. A pick that no longer matches any entry holds the model
    again rather than guessing.

    Returns (catalog, held ids, notes).
    """
    choices: JSONDict = mapping.get("duplicate_choices") or {}
    catalog: dict[str, JSONDict] = {}
    held: set[str] = set()
    notes: list[str] = []
    for model_id, entries in groups.items():
        # A model from an upstream this file does not publish is not worth a human's look.
        mapped = any(e.get("owned_by") in mapping["upstreams"] for e in entries)
        if not mapped or len({_what_it_charges(e) for e in entries}) == 1:
            catalog[model_id] = entries[0]
            continue
        choice = choices.get(model_id)
        if isinstance(choice, dict):
            picked = [e for e in entries if e.get("created") == choice.get("created")]
            if len(picked) == 1:
                catalog[model_id] = picked[0]
                continue
        catalog[model_id] = entries[0]
        held.add(model_id)
        listed = "; ".join(
            f"created {e.get('created')!r}, pricing {json.dumps(e.get('pricing'), sort_keys=True)}"
            for e in entries
        )
        why = (
            "the pick in duplicate_choices matches none of them, or more than one"
            if isinstance(choice, dict)
            else "nothing says which is real"
        )
        notes.append(
            f"{model_id}: listed {len(entries)} times at different prices ({listed}), and "
            f"{why}. Its published prices are kept, marked disputed_since, until a human "
            f"writes the real entry's `created` in duplicate_choices in "
            f"scripts/providers/edenai/mapping.json."
        )
    return catalog, held, notes


def extract_models(
    catalog: dict[str, JSONDict], mapping: JSONDict, held: "set[str] | frozenset[str]" = frozenset()
) -> dict[str, JSONDict]:
    """Turn the model list into a `models` block, through the explicit mapping only.

    A held id gets the runner's placeholder instead of figures: see `pick_entries`.
    """
    upstreams = mapping["upstreams"]
    field_specs: JSONDict = mapping["fields"]

    # The mapping picks UPSTREAMS, not models. Which models each upstream offers is
    # Eden's business and changes constantly; tracking that is the whole job. A model
    # Eden adds is published, one it drops is dropped, and both appear as `+`/`-`
    # lines in the summary that reaches the commit and the run.
    #
    # Eden states the model id itself, in `id`, so nothing has to be translated by
    # hand here -- which is exactly why no hand-written model list is needed.
    offered = sorted(m for m, e in catalog.items() if e.get("owned_by") in upstreams)
    if not offered:
        raise ScrapeError(
            f"none of the mapped upstreams {sorted(upstreams)} offer a single model. "
            f"Either Eden restructured `owned_by`, or the response is not what it "
            f"looks like. Refusing to publish an empty block."
        )

    models: dict[str, JSONDict] = {}

    for model_id in offered:
        if model_id in held:
            models[model_id] = {provider_runner.HELD: True}
            continue
        entry = catalog[model_id]

        # `list_pricing` is the public list price; `pricing` is what this endpoint
        # actually charges, and the two differ when Eden runs a promotion. A
        # divergence used to stop the run, on the grounds that an unauthenticated
        # read might be quoted a rate private to one account. Eden then produced the
        # case that argument did not cover: a promotion it states itself, in a
        # `discount` field of the same unauthenticated response -- a price every
        # consumer of the endpoint gets, and the price they actually pay. So what is
        # published is `pricing`, on one narrow condition: the stated public discount
        # accounts for the whole gap, figure by figure, in Eden's own arithmetic
        # (`unexplained_by_public_discount` checks that and explains why). Anything
        # short of it stops the run, exactly as before.
        listed = entry.get("list_pricing")
        if not isinstance(listed, dict):
            raise ScrapeError(f"{model_id}: no list_pricing object. The data shape has changed.")
        listed = cast(JSONDict, listed)

        effective = entry.get("pricing")
        unexplained = unexplained_by_public_discount(listed, effective, entry.get("discount"))
        if unexplained is not None:
            raise ScrapeError(
                f"{model_id}: list_pricing and pricing disagree ({unexplained}). Publishing "
                f"the lower figures needs a public discount in the same response that "
                f"accounts for them. Resolve by hand."
            )
        prices = cast(JSONDict, effective)

        result_entry: JSONDict = {}

        for field, field_spec in field_specs.items():
            api_field = field_spec["api_field"]
            raw = prices.get(api_field)

            if raw is None:
                if field_spec.get("optional"):
                    continue
                raise ScrapeError(
                    f"{model_id}.{field}: no {api_field!r} in pricing. Fields present: "
                    f"{sorted(prices)}. A model this file publishes must state this price."
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


def extract_new_models(fetch: Fetcher, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    """The one callback provider_runner needs. One source, read once.

    Every field this source states is either read or deliberately skipped by the
    mapping. The only notes are for a model listed twice at different prices, which
    needs a human's pick (`pick_entries`).
    """
    catalog, held, notes = pick_entries(parse_model_list(fetch(mapping["source"])), mapping)
    return extract_models(catalog, mapping, held), notes


def main(argv: list[str] | None = None) -> int:
    return provider_runner.main(PROVIDER_ID, extract_new_models, DEFAULT_MAPPING, DEFAULT_CURRENT, CLI_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
