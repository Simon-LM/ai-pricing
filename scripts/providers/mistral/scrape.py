#!/usr/bin/env python3
"""Read Mistral's public prices and work out what pricing.json's providers.mistral
block should say.

Standard library only. Reads public pages and nothing else: it takes no API key,
must never be given one, and never touches anyone's Mistral account.

This script DOES NOT WRITE pricing.json. It writes candidate files into an output
directory and reports which one, if any, the caller should promote. That is
deliberate: "leave pricing.json untouched on every failure path" is then a property
of the design rather than a branch of code somebody has to remember to get right.

ONE SOURCE FOR THE PRICES: <https://docs.mistral.ai/models>. Its index lists the
models currently offered, one card each, and every card links to a page carrying a
machine-readable price object -- `{"price": 4, "denominator": "/1000 Pages"}` --
along with an `isRetired` flag. It is by far the better source: structured, with the
unit stated per figure rather than implied by a label.

A SECOND PAGE IS READ FOR ONE SENTENCE: the batch API documentation,
<https://docs.mistral.ai/studio/batch-processing>, which states that batch API is
available for all models and offers a 50% discount on it. Mistral states that
discount as words, not as a figure beside each price, so the batch figures this
block publishes are WORKED OUT rather than read -- and every entry carrying one says
so, in `batch_derived`, so a consumer can tell the two kinds of claim apart. Which
units the discount may be applied to is decided in mapping.json's `batch` block, not
here, and the percentage is parsed rather than written down: the day Mistral says
30%, this block follows, and the day the sentence goes, the run fails instead of
republishing last week's rate as though it were still stated.

A THIRD PAGE IS READ FOR ONE TABLE: the API pricing page,
<https://docs.mistral.ai/inference/pricing>, for its "Voice agents" section, the one
price on it that no model page states: the Voice Agents API, billed per minute of
conversation, $0.04 with Mistral Small 4 and $0.08 with "larger models" on 2026-11-10.
Its other sections list the models by family, which the model pages already price, and
are not read; a section that lists no model and is not Voice agents is reported in the
run's notes, so that a price Mistral adds there -- the products' prices returning, say
-- is seen rather than missed. The Voice agents rows are published as products, keyed
`voice agents (<row>)`, in `per_conversation_minute`.

The products -- web search, code execution, image generation, libraries, data
capture and the two classifier models -- used to come from
<https://mistral.ai/pricing/api>, once the only page that priced them. There is
nothing to call for any of them, and they carry `"kind": "product"`. Mistral
retired that page in October 2026: it states no product price any more, and no
replacement states one either. This scraper does not read it, nor any page, for
them -- the mapping's `products` list is their whole inventory now, kept by hand,
and every name in it is published sold-but-unpriced. The runner dates the
transition, keeps the last prices actually observed beside the marker, and reports
it once.

Reading only the pricing page, as an earlier version did, silently missed four
priced models the docs list and the pricing page did not: OCR 4.0, OCR 3, Leanstral
1.5, and Voxtral Mini Transcribe 2 -- the last of which the pricing page had dropped
while Mistral was still selling it, so this repository published it as withdrawn.
A marketing page is not an inventory.

Entries are keyed by the NAME the source states -- "ocr 4.0", "web search" -- not by
API model id. The docs pages do carry the ids, in a `names` array; publishing them is
a separate decision and this file does not make it.

Outcomes, matching the three the workflow must implement:

  unchanged  figures identical -> out/stamped.json  (same figures, fresh checked_utc)
  changed    a figure moved    -> out/stamped.json and out/updated.json (the new figures)
  failure    fetch, parse or validation failed -> out/error.txt, exit code 1, no candidates

Usage:
    scripts/providers/mistral/scrape.py --out-dir .ci-out                      # live
    scripts/providers/mistral/scrape.py --out-dir .ci-out --offline m.json     # fixtures
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import cast

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

import nextjs_flight  # noqa: E402
import provider_runner  # noqa: E402
from provider_runner import Fetcher, ScrapeError  # noqa: E402
from pricing_validate import JSONDict, check_price  # noqa: E402

PROVIDER_ID = "mistral"

REPO_ROOT = _SCRIPTS_DIR.parent
DEFAULT_MAPPING = Path(__file__).resolve().parent / "mapping.json"
DEFAULT_CURRENT = REPO_ROOT / "pricing.json"

# Kept as its own constant rather than read off __doc__: the module docstring is
# stripped to None under `python -OO`, which would otherwise take this script down
# for a reason with nothing to do with argparse.
CLI_SUMMARY = "Read Mistral's public prices and work out what providers.mistral should say."

# A model card on the docs index: an <a> tagged with the model's colour, wrapping an
# icon whose alt text is the model's name. Models Mistral has deprecated are listed
# further down the same page as plain table rows instead, which is what keeps them
# out -- matching bare hrefs would pull in forty-five retired models.
_INDEX_CARD = re.compile(
    r'--model-color:[^"]*"\s+href="(?P<href>/models/[a-z0-9.-]+)">'
    r'.{0,400}?<img alt="(?P<name>[^"]*) icon"',
    re.S,
)

# How the batch discount is stated on the batch API page: "at a 50% discount", "We
# offer a 50% discount on batch API". Matched loosely on the number so that a change
# from 50 to 30 is followed rather than ignored, and never matched on the sentence
# around it, which is marketing copy and gets rewritten.
_BATCH_DISCOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*discount", re.I)


# --------------------------------------------------------------------------------------
# docs.mistral.ai -- the models
# --------------------------------------------------------------------------------------


def parse_index(html_text: str) -> dict[str, str]:
    """Return {page path: model name} for every model card on the docs index.

    Cards only. The same page lists Mistral's deprecated models further down as plain
    table rows, and matching every /models/ link would pull in forty-five retired ones
    -- including several this repository correctly stopped publishing.
    """
    found = {m.group("href"): m.group("name").strip() for m in _INDEX_CARD.finditer(html_text)}
    if not found:
        raise ScrapeError(
            "no model card found on the docs index. The layout has changed: this scraper "
            "expects each current model to be linked from a card carrying a "
            "--model-color style and an icon whose alt text is the model name."
        )
    return found


def parse_model_page(html_text: str, path: str) -> tuple[str, bool, JSONDict | None, list[str]]:
    """Return (name, retired, pricing object, api ids) for one docs model page.

    Several values are optional because the pages are not uniform: a model with no
    published price carries no pricing object at all, and the pages of some models
    carry no `isRetired` flag. Deciding what to do with each combination is the
    caller's job; this function only reports what the page says.

    The api ids are the strings a caller passes as the model, which the page renders as
    copyable badges and streams as a `names` array -- most specific first, ending in a
    `-latest` alias where the model has one.
    """
    name = nextjs_flight.find_value(html_text, "currentModelName")
    retired = nextjs_flight.find_value(html_text, "isRetired")
    pricing = nextjs_flight.find_value(html_text, "pricing")
    names = nextjs_flight.find_value(html_text, "names")

    if not isinstance(name, str) or not name.strip():
        raise ScrapeError(
            f"{path}: the page states no currentModelName. Its data shape has changed, "
            f"and the key this model would be published under cannot be read."
        )
    if pricing is not None and not isinstance(pricing, dict):
        raise ScrapeError(f"{path}: the pricing value is not an object. The data shape has changed.")

    api_ids: list[str] = []
    if names is not None:
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ScrapeError(
                f"{path}: the names value is not a list of strings. The data shape has "
                f"changed, and publishing a malformed identifier is worse than publishing "
                f"none."
            )
        # Order is meaning here, so it is preserved rather than sorted: the versioned id
        # comes first and the moving alias last. Duplicates would fail validation, and
        # the page has been seen to repeat its payload.
        api_ids = list(dict.fromkeys(n.strip() for n in cast("list[str]", names) if n.strip()))

    return name.strip(), retired is True, cast("JSONDict | None", pricing), api_ids


def docs_entry(model_id: str, pricing: JSONDict, mapping: JSONDict) -> tuple[JSONDict, list[str]]:
    """Turn one docs pricing object into an entry, plus anything a human should see."""
    notes: list[str] = []

    # `free` is stated by the source itself rather than inferred from a figure of 0,
    # so it is taken as said -- unlike OVH's catalog, which has no such flag and where
    # "every unit is 0" is the only thing that can stand in for one.
    if pricing.get("free") is True:
        return {"free": True}, notes

    denominators: JSONDict = mapping["denominators"]
    labels: JSONDict = mapping["labels"]
    entry: JSONDict = {}

    # Two shapes in the wild. Most models state "input" and "output" lists; a model
    # billed at a single rate states that rate on the pricing object itself, with no
    # lists at all. The flat one is read as a single input row rather than ignored --
    # ignoring it would silently drop the only price such a model has.
    sides: dict[str, object] = {"input": pricing.get("input"), "output": pricing.get("output")}
    if sides["input"] is None and sides["output"] is None and "price" in pricing:
        sides["input"] = [pricing]

    for side in ("input", "output"):
        rows = sides[side]
        if rows is None:
            continue
        if not isinstance(rows, list):
            raise ScrapeError(f"{model_id}: pricing.{side} is not a list. The data shape has changed.")

        for row in cast("list[JSONDict]", rows):
            if not isinstance(row, dict):
                raise ScrapeError(f"{model_id}: a pricing.{side} row is not an object.")

            denominator = row.get("denominator")
            price = row.get("price")

            # A zero here is this source saying "this side is not billed" -- Voxtral
            # TTS charges for the audio it generates and nothing for the text it is
            # given. It is not the ambiguous zero OVH's catalog produces, because this
            # source states `free` separately when a model is actually free, so there
            # is nothing to report and nothing to publish.
            if not isinstance(price, bool) and isinstance(price, (int, float)) and price == 0:
                continue

            # Read once: the page decides what is in this cell, so it is `Any` until
            # it has been looked at, and only a string can name a row in `labels`.
            label = row.get("label")
            field = (labels.get(label) if isinstance(label, str) else None) or denominators.get(
                side, {}
            ).get(denominator)
            if field is None:
                notes.append(
                    f"{model_id}: {side} priced at {price!r} per {denominator!r}"
                    + (f" (labelled {label!r})" if label else "")
                    + f", a unit {DEFAULT_MAPPING.name} has no field for. That price is not "
                    f"published. Add it to the mapping's 'denominators' table to publish it."
                )
                continue

            if field in entry:
                raise ScrapeError(
                    f"{model_id}.{field}: two prices map to it. Refusing to guess which "
                    f"one is the real figure."
                )
            entry[field] = check_price(f"{PROVIDER_ID}/{model_id}", field, price)

    return entry, notes


def parse_batch_discount(html_text: str, url: str) -> Decimal:
    """The batch discount Mistral states on that page, as a fraction: 0.5 for 50%.

    The page states it in prose, twice, and the two must agree. Reading it rather than
    writing it down is what makes a change to it a change here instead of a silent
    disagreement, and refusing when it cannot be read is what stops last week's rate
    being republished as though Mistral still stated it.
    """
    found = {Decimal(m.group(1)) for m in _BATCH_DISCOUNT.finditer(html_text)}
    if not found:
        raise ScrapeError(
            f"{url} no longer states a batch discount in the form '<n>% discount'. The "
            f"batch figures this block publishes are worked out from that sentence and "
            f"from nothing else, so the run stops rather than republishing the last rate "
            f"observed as though it were still stated."
        )
    if len(found) > 1:
        raise ScrapeError(
            f"{url} states more than one batch discount "
            f"({', '.join(f'{d:g}%' for d in sorted(found))}). Which applies to which "
            f"unit is not stated anywhere, so this must be resolved by hand."
        )

    percent = found.pop()
    if not 0 < percent < 100:
        raise ScrapeError(
            f"{url} states a batch discount of {percent:g}%, which is not a discount. "
            f"The figure is read as a percentage off, so this is a misread rather than a "
            f"price."
        )
    return percent / 100


def derive_batch(
    model_id: str, entry: JSONDict, fields: list[str], discount: Decimal
) -> None:
    """Add the discounted figure of every unit the mapping says is batched, in place.

    In place, on an entry that already carries the rates the source states, because the
    two are read side by side: `in_per_mtok` is what Mistral writes and
    `in_per_mtok_batch` is the discount off it, and a consumer that cannot tell them
    apart would apply the wrong one to a batch run.

    Decimal throughout, for the reason the Scaleway scraper gives: a 30% discount on
    0.7 is 0.48999999999999994 as a float multiply, and that noise would publish a
    wrong figure and churn in a diff a human reads for real price movements.

    The marker is written once for the entry rather than per field. It says the same
    thing about all of them, and a consumer that finds one `_batch` figure in an entry
    carrying it knows to treat all of them the same way.

    Nothing is reported. This runs for every priced model every week, and a note
    repeating a fact nobody can act on is how a channel that has to still work the day
    something real happens gets muted. The marker is the record; a change to the rate
    shows up in the diff as the figures and the marker moving together.
    """
    derived = [field for field in fields if field in entry and f"{field}_batch" not in entry]
    if not derived:
        return

    # Written as a whole number of percent rather than as the fraction it is derived
    # from: "50%", not "50.0%", so that the marker reads the way the page states it.
    percent = (discount * 100).normalize()

    for field in derived:
        discounted = Decimal(repr(entry[field])) * (Decimal(1) - discount)
        entry[f"{field}_batch"] = check_price(
            f"{PROVIDER_ID}/{model_id}", f"{field}_batch", float(discounted)
        )

    entry["batch_derived"] = (
        f"{percent:f}% off the stated rate, as Mistral's batch API documentation states"
    )


def extract_docs_models(fetch: Fetcher, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    """Read every model the docs index currently lists, and price it."""
    batch = mapping.get("batch")
    if not isinstance(batch, dict) or not batch.get("source") or not batch.get("fields"):
        raise ScrapeError(
            f"{DEFAULT_MAPPING.name} declares no usable 'batch' block. The batch figures "
            f"published for this provider are derived from the sentence that block names "
            f"the source of; without it there is nothing to derive them from, and "
            f"publishing the model prices alone would drop figures the file already "
            f"carries."
        )
    batch_source = cast(str, batch["source"])
    batch_fields = cast("list[str]", batch["fields"])
    discount = parse_batch_discount(fetch(batch_source), batch_source)

    index = parse_index(fetch(mapping["source"]))
    base = mapping["source"].split("/models", 1)[0]

    models: dict[str, JSONDict] = {}
    notes: list[str] = []

    for path in sorted(index):
        name, retired, pricing, api_ids = parse_model_page(fetch(base + path), path)
        model_id = name.lower()

        # Listed but marked retired: treated as not offered, exactly like one that has
        # left the index. The runner keeps it, dated, rather than deleting it.
        if retired or pricing is None:
            continue

        if model_id in models:
            raise ScrapeError(
                f"two docs pages are both called {name!r}. Refusing to guess which one "
                f"carries the real price."
            )

        entry, entry_notes = docs_entry(model_id, pricing, mapping)
        notes += entry_notes
        if not entry:
            # Published carrying the shared `unpriced` marker rather than dropped.
            # Mistral still lists this model and still calls it paid; dropping it would
            # make "sold, at a price this file cannot read" indistinguishable from
            # "gone". provider_runner stamps the date, keeps any price observed before
            # this happened, and reports the transition once instead of every week.
            unpriced: JSONDict = {"unpriced": "no price on this page could be published at all"}
            entry = unpriced
        else:
            # Only where there is a rate to take a percentage off. A model the page
            # marks free arrives here too and derives nothing, which is the same
            # outcome by the same route: no rate, no figure.
            derive_batch(model_id, entry, batch_fields, discount)
        entry["display_name"] = model_id
        if api_ids:
            entry["api_ids"] = api_ids
        else:
            notes.append(
                f"{model_id}: the page states no callable identifier. Its price is "
                f"published, but a consumer has nothing to pass as the model."
            )
        models[model_id] = entry

    if not models:
        raise ScrapeError(
            "not a single model on the docs site could be published. Either Mistral "
            "restructured it, or the pages are not what they look like. Refusing to "
            "publish an empty block."
        )
    return models, notes


# --------------------------------------------------------------------------------------
# the products -- one hand-held list, published sold-but-unpriced
# --------------------------------------------------------------------------------------


# What the products' `unpriced` marker says, now that no page states their price any
# more. The runner folds it into the transition note it prints once, then drops it:
# what a consumer reads in pricing.json is `unpriced_since`, not why the figure stopped.
_NO_SOURCE_REASON = (
    "Mistral no longer states a price for this product: the page that used to carry "
    "product prices was retired in October 2026, and this provider no longer reads it"
)


def frozen_products(mapping: JSONDict) -> dict[str, JSONDict]:
    """Every product the mapping names, published as sold-but-unpriced, with the reason.

    Nothing is read for these any more. The page that used to price them was retired
    by Mistral in October 2026, no page states a product price, and the mapping's
    `products` list is the inventory now -- the one place a human adds a name. The
    reason is the whole of the marker: the runner copies the prices last actually
    observed from the published entry, stamps the date of the transition, and reports
    it once. What this function may not do is DROP a product instead -- an entry that
    is still sold must never be published as one the source has withdrawn.
    """
    return {
        name: {"kind": "product", "display_name": name, "unpriced": _NO_SOURCE_REASON}
        for name in mapping["products"]
    }


# --------------------------------------------------------------------------------------
# docs.mistral.ai/inference/pricing -- the Voice Agents API
# --------------------------------------------------------------------------------------


# The heading every section of the API pricing page carries, in the streamed tree.
_SECTION_HEADING = re.compile(r'"sectionId":"([a-z0-9-]+)"')


def _section_props(text: str, section_id: str) -> JSONDict | None:
    """The props of the section element `{"id": "<section_id>", ...}`, or None."""
    at = text.find(f'{{"id":"{section_id}",')
    if at < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text, at)
    except json.JSONDecodeError:
        return None
    return cast(JSONDict, value) if isinstance(value, dict) else None


def _children(node: object) -> Iterator[object]:
    """The elements and text below a node of the streamed tree, in order.

    An element is streamed as `["$", type, key, props]`; its content is
    `props["children"]`, a string, an element, or a list of either.
    """
    if isinstance(node, list):
        if len(node) == 4 and node[0] == "$" and isinstance(node[3], dict):
            yield node
        else:
            for item in node:
                yield from _children(item)
    elif isinstance(node, str):
        yield node


def _content(element: object) -> object:
    """What an element `["$", type, key, props]` holds: `props["children"]`."""
    return cast(JSONDict, cast("list[object]", element)[3]).get("children")


def _text(node: object) -> Iterator[str]:
    """Every string of text below a node, in order."""
    for child in _children(node):
        if isinstance(child, str):
            yield child
        else:
            yield from _text(_content(child))


def _rows(node: object) -> Iterator[tuple[str, str]]:
    """Every two-cell table row below a node: an element whose children are exactly
    two elements, each holding one string."""
    for child in _children(node):
        if isinstance(child, str):
            continue
        inner = _content(child)
        cells = [c for c in _children(inner) if not isinstance(c, str)]
        texts = [_content(c) for c in cells]
        if len(cells) == 2 and all(isinstance(t, str) for t in texts):
            yield cast(str, texts[0]), cast(str, texts[1])
        else:
            yield from _rows(inner)


def voice_agent_entries(html_text: str, spec: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    """The Voice Agents API's prices, from the API pricing page's "Voice agents" table.

    Read strictly: the section must still say what its unit is, in the words the
    mapping states, and every row must price one figure per minute. Anything else
    fails the run rather than publish a rate in a unit that may have changed.
    """
    section = cast(str, spec["section"])
    url = cast(str, spec["source"])
    text = next((c for c in nextjs_flight.decoded_chunks(html_text) if f'"sectionId":"{section}"' in c), None)
    if text is None:
        raise ScrapeError(
            f"{url} carries no {section!r} section. Mistral moved or dropped the Voice "
            f"Agents prices: check the page, then update the mapping's `voice_agents`."
        )

    notes = [
        f"{url}: a section {other!r} that lists no model and that nothing here reads; "
        f"any price in it is not published"
        for other in dict.fromkeys(_SECTION_HEADING.findall(text))
        if other != section and "slugs" not in (_section_props(text, other) or {})
    ]

    props = _section_props(text, section)
    if props is None:
        raise ScrapeError(f"{url}: the {section!r} section could not be read. The data shape has changed.")
    unit = cast(str, spec["unit"])
    if not any(unit in line for line in _text(props.get("children"))):
        raise ScrapeError(
            f"{url}: the {section!r} section no longer says it is {unit!r}. Its unit may "
            f"have changed; nothing is published from it until a human has looked."
        )

    suffix = cast(str, spec["price_suffix"])
    # The stream escapes a string's leading "$" as "$$".
    price_form = re.compile(r"\$?\$([0-9]+(?:\.[0-9]+)?)" + re.escape(suffix))
    header = tuple(cast("list[str]", spec["header"]))
    field = cast(str, spec["field"])
    entries: dict[str, JSONDict] = {}
    for label, cell in _rows(props.get("children")):
        if (label.strip(), cell.strip()) == header:
            continue
        match = price_form.fullmatch(cell.strip())
        if match is None:
            raise ScrapeError(
                f"{url}: the {section!r} section prices {label!r} at {cell!r}, not a "
                f"price per {suffix!r} this can read."
            )
        key = f"{spec['key_prefix']} ({label.strip().lower()})"
        if key in entries:
            raise ScrapeError(f"{url}: {key!r} is priced twice. Refusing to guess which one is real.")
        entries[key] = {
            "kind": "product",
            "display_name": key,
            field: check_price(f"{PROVIDER_ID}/{key}", field, float(match[1])),
        }
    if not entries:
        raise ScrapeError(f"{url}: the {section!r} section prices nothing. Its layout has changed.")
    return entries, notes


def extract_new_models(fetch: Fetcher, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    """The one callback provider_runner needs: the docs site, the Voice Agents prices,
    plus the frozen products.

    The models half is read strictly, exactly as before. The products half reads
    nothing: it republishes every name the mapping's `products` list holds, and the
    runner dates the state, keeps the last prices actually observed beside the marker
    and reports the transition once. The two halves are checked for a key claimed
    twice, because one key cannot be both a model and a product.
    """
    models, notes = extract_docs_models(fetch, mapping)

    spec = cast(JSONDict, mapping["voice_agents"])
    voice, voice_notes = voice_agent_entries(fetch(cast(str, spec["source"])), spec)
    notes += voice_notes

    products = frozen_products(mapping)

    clash = sorted(set(models) & set(products))
    if clash:
        raise ScrapeError(
            f"{clash} is published both as a model from the docs site and as a product "
            f"from mapping.json's products list. One key cannot be two things; resolve "
            f"it by hand."
        )
    clash = sorted(set(voice) & (set(models) | set(products)))
    if clash:
        raise ScrapeError(
            f"{clash} is published both from the Voice Agents table and as a model or as "
            f"a product from mapping.json's products list. One key cannot be two things; "
            f"resolve it by hand."
        )

    return {**models, **voice, **products}, notes


def main(argv: list[str] | None = None) -> int:
    return provider_runner.main(PROVIDER_ID, extract_new_models, DEFAULT_MAPPING, DEFAULT_CURRENT, CLI_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
