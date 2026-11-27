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
models currently offered, one card each, and the models it has deprecated further down
the same page as plain table rows; every link, card or row, goes to a page streaming a
machine-readable model object -- `{"name": "Codestral", "modelIds": ["codestral-2508"],
"pricing": [{"direction": "input", "priceUsd": 0.3, "unit": "/M Tokens"}]}` -- with a
`status` flag and the dates a deprecated model retires. It is by far the better source:
structured, with the unit stated per figure rather than implied by a label.

THE PAGE STREAMS A WHOLE MODEL OBJECT, not the handful of named keys it used to. It
carries `name` (the label to publish under), `modelIds` (what a caller passes as the
model), `isFree`, a `status` -- "ga", "deprecated" or "retired" -- and a `pricing` LIST,
one row per figure, each naming its `direction` ("input" or "output"), its `priceUsd`,
its `unit` and its `label`. A model the index lists but the page marks `retired` is
treated as gone, exactly like one that has left the index. One it marks `deprecated` is
still sold, so it stays published with its prices, carrying `deprecated: true`, the day
it was deprecated (`deprecated_since`, from `deprecatedAt`) and the day it retires
(`retires_on`, from `retiredAt`) where the page states them.

A SECOND PAGE IS READ FOR ONE SENTENCE: the batch API documentation,
<https://docs.mistral.ai/studio/batch-processing>, which states that batch API is
available for all models and offers a 50% discount on it. Mistral states that
discount as words, not as a figure beside each price, so the batch figures this
block publishes are WORKED OUT rather than read -- and every entry carrying one says
so, in `batch_derived`, so a consumer can tell the two kinds of claim apart. Which
units the discount may be applied to is decided in mapping.json's `batch` block, not
here, and the percentage is parsed rather than written down: the day Mistral says
30%, this block follows.

A PAGE THAT STOPS STATING IT IS REPORTED, NOT FATAL. On 2026-11-25 that page began
returning 404, and the run stopped before reading a single model: one sentence about a
discount withheld every model price in the block. It no longer does. When the discount
cannot be read, the block derives its figures on the rate its own published entries
record in `batch_derived` -- the rate Mistral last stated -- marks every figure derived
that way as unconfirmed, and reports what it could not check. Those figures still
publish, and the alert is a note prefixed `unconfirmed: `, which the workflow turns into
an issue of its own that closes on the first run that confirms the rate again.

A THIRD PAGE USED TO BE READ FOR ONE TABLE: the API pricing page,
<https://docs.mistral.ai/inference/pricing>, whose "Voice agents" section was the one
price on it no model page stated -- the Voice Agents API, billed per minute of
conversation, $0.04 with Mistral Small 4 and $0.08 with "larger models" on 2026-11-10.
On 2026-11-26 that section went: the page now renders its own model-family tables
("Generalist models", "Audio models", ...) and states no per-conversation-minute price
anywhere, and the section that carried it -- identified by a `sectionId` and a sentence
naming its unit -- is gone. The price cannot be read from anything that is left, so
this scraper does not guess one at it: the Voice Agents rows are now published as
sold-but-unpriced, the same treatment the other products get, from the row names the
mapping states -- still sold, with the last figure observed kept beside the marker, and
reported once. Reading the table is still attempted first, so that if Mistral states the
price in the old shape again it is read rather than missed.

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
API model id. The docs pages do carry the ids, in a `modelIds` array; publishing them
is a separate decision, and this file makes it (see `api_ids` below) without using the
page's `aliases`, which repeat the slug -- a string nothing can be called as.

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
from provider_runner import UNCONFIRMED_PREFIX, Fetcher, ScrapeError  # noqa: E402
from pricing_validate import JSONDict, check_price  # noqa: E402

PROVIDER_ID = "mistral"

REPO_ROOT = _SCRIPTS_DIR.parent
DEFAULT_MAPPING = Path(__file__).resolve().parent / "mapping.json"
DEFAULT_CURRENT = REPO_ROOT / "pricing.json"

# Kept as its own constant rather than read off __doc__: the module docstring is
# stripped to None under `python -OO`, which would otherwise take this script down
# for a reason with nothing to do with argparse.
CLI_SUMMARY = "Read Mistral's public prices and work out what providers.mistral should say."

# How the docs index links a model, in the two shapes the page uses. A CURRENT model is
# a card: an <a> covering the whole card, pointing at the model's page, whose label --
# hidden from sight -- is the model's name. A model Mistral has deprecated or retired is
# a row of the "Deprecated & retired models" table further down the same page instead, a
# differently styled link with a visible title. Both are read: a deprecated model is
# still sold and must keep being visited, and the page that says "retired" is what tells
# the two apart. Matching bare hrefs would pull in every model either table names, so
# each shape is matched by its own markup.
_INDEX_CARD = re.compile(
    r'<a[^>]+href="(?P<href>/models/[a-z0-9.-]+)"[^>]*>'
    r'\s*<span class="opacity-0">(?P<name>[^<]*)</span>'
)
_INDEX_ROW = re.compile(
    r'<a class="w-full min-w-0 flex gap-1 hover:underline" href="(?P<href>/models/[a-z0-9.-]+)">'
    r'\s*<p class="text-button-small font-mistral truncate">(?P<name>[^<]*)</p></a>'
)

# How the batch discount is stated on the batch API page: "at a 50% discount", "We
# offer a 50% discount on batch API". Matched loosely on the number so that a change
# from 50 to 30 is followed rather than ignored, and never matched on the sentence
# around it, which is marketing copy and gets rewritten.
_BATCH_DISCOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*discount", re.I)

# How a published entry records the rate its batch figures were derived from: "50% off
# the stated rate, as Mistral's batch API documentation states". Read back out of
# `batch_derived` when the page that states the rate cannot be read, so that the
# fallback is the rate Mistral actually last said rather than a number written into this
# file. Anchored at the start and matched loosely after the rate: the wording on either
# side of it has changed once already (the unconfirmed form says something else after the
# comma) and neither half is the figure.
_PUBLISHED_BATCH_RATE = re.compile(r"^(?P<percent>\d+(?:\.\d+)?)% off the stated rate")


# --------------------------------------------------------------------------------------
# docs.mistral.ai -- the models
# --------------------------------------------------------------------------------------


def parse_index(html_text: str) -> dict[str, str]:
    """Return {page path: model name} for every model the docs index links.

    Both the current models, one card each, and the deprecated and retired ones the
    table further down lists as rows. A deprecated model is still sold, so it must keep
    being visited, and only its own page can say whether the source has really stopped
    offering it. Keyed by path, so a model listed in both shapes -- or twice, as the page
    has been seen to render its cards -- is visited once.
    """
    found = {m.group("href"): m.group("name").strip() for m in _INDEX_CARD.finditer(html_text)}
    found.update({m.group("href"): m.group("name").strip() for m in _INDEX_ROW.finditer(html_text)})
    if not found:
        raise ScrapeError(
            "no model card found on the docs index. The layout has changed: this scraper "
            "expects each model to be linked from a card whose hidden label -- a span "
            "carrying the opacity-0 class -- is the model name, or from a row of the "
            "deprecated-models table."
        )
    return found


def _first_model_record(node: object) -> JSONDict | None:
    """The first dict below `node` carrying both `slug` and `pricing`, or None.

    Both keys together, not either alone: the page's route params carry a `slug`, and
    objects nested inside the model -- its licence, its capabilities -- carry one too,
    so `slug` on its own is not the model; and a licence carries no `pricing`. The pair
    is unique to the model object.
    """
    if isinstance(node, dict):
        if "slug" in node and "pricing" in node:
            return cast(JSONDict, node)
        for value in node.values():
            record = _first_model_record(value)
            if record is not None:
                return record
    elif isinstance(node, list):
        for item in node:
            record = _first_model_record(item)
            if record is not None:
                return record
    return None


def find_model_record(html_text: str) -> JSONDict | None:
    """The model object a docs model page streams, or None if it streams none.

    The page renders its content from an object it streams in a Flight payload, split
    into records by a `\\n<id>:` boundary. Each record is tried on its own and walked
    for the model object, because the object is nested inside the rendered tree rather
    than being a record of its own. A record that does not parse is skipped: a page
    carries many, this is after one, and a malformed record elsewhere is not a reason to
    fail. Looking a value up by key instead -- `find_value(html, "name")` -- answers with
    a licence's name, which is why the object is located by its two unique keys.
    """
    for chunk in nextjs_flight.decoded_chunks(html_text):
        for part in re.split(r"\n[0-9a-z]{1,3}:", chunk):
            part = part.strip()
            if '"slug"' not in part or '"pricing"' not in part:
                continue
            try:
                payload = json.loads(part)
            except json.JSONDecodeError:
                continue
            record = _first_model_record(payload)
            if record is not None:
                return record
    return None


def parse_model_page(html_text: str, path: str) -> tuple[str, JSONDict, list[str]]:
    """Return (name, model object, api ids) for one docs model page.

    The whole object is returned rather than the handful of fields read out of it,
    because the caller needs several -- the price list, the status, the deprecation
    dates -- and re-walking the page for each would be the same parse done twice.

    The api ids are the strings a caller passes as the model, which the page states in
    `modelIds`, most specific first and usually ending in a `-latest` alias. Not the
    object's `aliases`, which repeat the page's own slug -- a string no API answers to.
    """
    record = find_model_record(html_text)
    if record is None:
        raise ScrapeError(
            f"{path}: the page streams no model object. The data shape has changed: no "
            f"record on it carries both a `slug` and a `pricing` list."
        )

    name = record.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ScrapeError(
            f"{path}: the model object states no name. Its data shape has changed, and "
            f"the key this model would be published under cannot be read."
        )

    api_ids: list[str] = []
    model_ids = record.get("modelIds")
    if model_ids:
        if not isinstance(model_ids, list) or not all(isinstance(i, str) for i in model_ids):
            raise ScrapeError(
                f"{path}: modelIds is not a list of strings. The data shape has changed, "
                f"and publishing a malformed identifier is worse than publishing none."
            )
        # Order is meaning here, so it is preserved rather than sorted: the versioned id
        # comes first and the moving alias last. Duplicates would fail validation, and
        # the page has been seen to repeat its payload.
        api_ids = list(dict.fromkeys(i.strip() for i in cast("list[str]", model_ids) if i.strip()))

    return name.strip(), record, api_ids


def _stated_day(record: JSONDict, key: str, path: str) -> str | None:
    """The day of an ISO timestamp the model object states, or None if it states none.

    `deprecatedAt` and `retiredAt` arrive as full timestamps -- "2026-10-31T00:00:00.000Z"
    -- and what is published is the day alone. A value that is present and is not one is
    a shape change, not a missing date, so it fails the run rather than quietly
    publishing a model with no retirement date.
    """
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}", value):
        raise ScrapeError(
            f"{path}: {key} is not an ISO timestamp ({value!r}). The data shape has changed."
        )
    return value[:10]


def docs_entry(model_id: str, record: JSONDict, mapping: JSONDict) -> tuple[JSONDict, list[str]]:
    """Turn one docs model object into an entry, plus anything a human should see."""
    notes: list[str] = []

    # `free` is stated by the source itself rather than inferred from a figure of 0,
    # so it is taken as said -- unlike OVH's catalog, which has no such flag and where
    # "every unit is 0" is the only thing that can stand in for one.
    if record.get("isFree") is True:
        return {"free": True}, notes

    denominators: JSONDict = mapping["denominators"]
    labels: JSONDict = mapping["labels"]
    entry: JSONDict = {}

    rows = record.get("pricing")
    if not isinstance(rows, list):
        raise ScrapeError(
            f"{model_id}: the model object's pricing is not a list. The data shape has changed."
        )

    for row in cast("list[JSONDict]", rows):
        if not isinstance(row, dict):
            raise ScrapeError(f"{model_id}: a pricing row is not an object.")

        # `direction` names the side; `unit` names the figure's unit, as the old page's
        # "denominator" did. Read once: the page decides what is in each cell, so each is
        # `Any` until it has been looked at, and only a string can name a row in `labels`.
        direction = row.get("direction")
        unit = row.get("unit")
        label = row.get("label")
        price = row.get("priceUsd")

        # A zero here is this source saying "this side is not billed" -- Voxtral TTS
        # charges for the audio it generates and nothing for the text it is given. It is
        # not the ambiguous zero OVH's catalog produces, because this source states
        # `isFree` separately when a model is actually free, so there is nothing to
        # report and nothing to publish.
        if not isinstance(price, bool) and isinstance(price, (int, float)) and price == 0:
            continue

        side = direction if isinstance(direction, str) else None
        by_label = labels.get(label) if isinstance(label, str) else None
        field = by_label or (
            denominators.get(side, {}).get(unit)
            if side is not None and isinstance(unit, str)
            else None
        )
        if field is None:
            where = side if side is not None else "a side the page does not name"
            notes.append(
                f"{model_id}: {where} priced at {price!r} per {unit!r}"
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
    disagreement.

    Raising when it cannot be read is not the same as stopping the run: the caller
    catches this exception, falls back on the rate the published block records, and
    publishes on that with an alert. The three messages below are therefore written to
    be read by a human on their own, as the body of that alert -- each says what the
    page did and what the figure cannot rest on.
    """
    found = {Decimal(m.group(1)) for m in _BATCH_DISCOUNT.finditer(html_text)}
    if not found:
        raise ScrapeError(
            f"{url} no longer states a batch discount in the form '<n>% discount'"
        )
    if len(found) > 1:
        raise ScrapeError(
            f"{url} states more than one batch discount "
            f"({', '.join(f'{d:g}%' for d in sorted(found))}); which applies to which "
            f"unit is stated nowhere"
        )

    percent = found.pop()
    if not 0 < percent < 100:
        raise ScrapeError(
            f"{url} states a batch discount of {percent:g}%, which is not a discount. "
            f"The figure is read as a percentage off, so this is a misread rather than a "
            f"price."
        )
    return percent / 100


def read_batch_discount(fetch: Fetcher, url: str) -> tuple[Decimal | None, str | None]:
    """The discount, and why it could not be read if it could not.

    Two things are kept apart on purpose: a page that cannot be fetched at all
    (ScrapeError from the fetcher) and a page that arrives and does not state the rate
    (ScrapeError from the parser). Both leave the run without a confirmed rate, and both
    are reported the same way, so this returns one reason rather than distinguishing them
    by the type of a re-raised exception.
    """
    try:
        return parse_batch_discount(fetch(url), url), None
    except ScrapeError as exc:
        return None, str(exc)


def published_batch_rate(current_block: JSONDict | None) -> Decimal | None:
    """The rate the published block says its batch figures were derived from.

    Read back out of `batch_derived`, so the fallback is whatever Mistral last said
    rather than a number kept in this repository. Every entry carrying a derived figure
    records the same rate; anything else -- no marked entry, a rate that is not a
    fraction strictly between 0 and 1, or two rates that disagree -- returns None rather
    than a guess. The caller then publishes no batch figure and says so: a missing figure
    is a gap, a wrong one is a wrong bill.
    """
    rates: set[Decimal] = set()
    for entry in (current_block or {}).get("models", {}).values():
        if not isinstance(entry, dict):
            continue
        marker = _PUBLISHED_BATCH_RATE.match(cast(str, entry.get("batch_derived") or ""))
        if marker is None:
            continue
        rate = Decimal(marker.group("percent")) / 100
        if not 0 < rate < 1:
            return None
        rates.add(rate)
    return rates.pop() if len(rates) == 1 else None


def derive_batch(
    model_id: str,
    entry: JSONDict,
    fields: list[str],
    discount: Decimal,
    confirmed: bool = True,
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

    `confirmed` says whether this run read the rate off the page or fell back on the
    rate the block last recorded. It changes the marker and nothing else -- the figures
    are derived the same way either way -- because the marker is the one place a
    consumer can see which of the two it is looking at.

    Nothing is reported. This runs for every priced model every week, and a note
    repeating a fact nobody can act on is how a channel that has to still work the day
    something real happens gets muted. The marker is the record; a change to the rate
    shows up in the diff as the figures and the marker moving together. An unconfirmed
    run is the exception, and that note is written once by the caller, not per model.
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

    if confirmed:
        entry["batch_derived"] = (
            f"{percent:f}% off the stated rate, as Mistral's batch API documentation states"
        )
    else:
        entry["batch_derived"] = (
            f"{percent:f}% off the stated rate, the rate Mistral last stated: this run "
            f"could not confirm it against the batch API page"
        )


def extract_docs_models(
    fetch: Fetcher, mapping: JSONDict, current_block: JSONDict | None = None
) -> tuple[dict[str, JSONDict], list[str]]:
    """Read every model the docs index currently lists, and price it.

    `current_block` is the published block, read only when the batch discount cannot be
    read off its page: it holds the rate Mistral last stated, in the `batch_derived`
    markers, and a figure derived on that rate is still worth publishing -- marked, and
    reported -- where publishing nothing is not.
    """
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

    models: dict[str, JSONDict] = {}
    notes: list[str] = []

    # Declared before the discount is read, because the notes below are what turns a
    # page this run could not read into an alert, and they describe the figures the loop
    # is about to derive.
    discount, unreadable = read_batch_discount(fetch, batch_source)
    if unreadable is not None:
        discount = published_batch_rate(current_block)
        if discount is None:
            notes.append(
                f"{UNCONFIRMED_PREFIX}{unreadable}. No published entry records a rate to "
                f"fall back on either, so no batch figure is published on this run."
            )
        else:
            notes.append(
                f"{UNCONFIRMED_PREFIX}{unreadable}. The batch figures are derived on the "
                f"{(discount * 100).normalize():f}% rate Mistral last stated, the "
                f"rate the published "
                f"block records, and every entry carrying one is marked as unconfirmed."
            )

    index = parse_index(fetch(mapping["source"]))
    base = mapping["source"].split("/models", 1)[0]

    for path in sorted(index):
        name, record, api_ids = parse_model_page(fetch(base + path), path)
        model_id = name.lower()

        # Listed but marked retired: treated as not offered, exactly like one that has
        # left the index. The runner keeps it, dated, rather than deleting it.
        if record.get("status") == "retired":
            continue

        # The page streams no price at all for this model -- Shieldstral, at public
        # preview, states an empty `pricing` list. There is nothing to publish and
        # nothing to mark: `unpriced` says "still sold, at a price this file cannot
        # read", which is not what an empty list says. An entry this file already
        # publishes and whose page loses every price is marked absent by the runner,
        # like anything else the source stops offering.
        if not record.get("pricing"):
            continue

        if model_id in models:
            raise ScrapeError(
                f"two docs pages are both called {name!r}. Refusing to guess which one "
                f"carries the real price."
            )

        entry, entry_notes = docs_entry(model_id, record, mapping)
        notes += entry_notes
        if not entry:
            # Published carrying the shared `unpriced` marker rather than dropped.
            # Mistral still lists this model and still calls it paid; dropping it would
            # make "sold, at a price this file cannot read" indistinguishable from
            # "gone". provider_runner stamps the date, keeps any price observed before
            # this happened, and reports the transition once instead of every week.
            unpriced: JSONDict = {"unpriced": "no price on this page could be published at all"}
            entry = unpriced
        elif discount is not None:
            # Only where there is a rate to take a percentage off. A model the page
            # marks free arrives here too and derives nothing, which is the same
            # outcome by the same route: no rate, no figure.
            #
            # `discount` is None only when this run could not read the rate and the
            # block records none to fall back on: no figure that week, rather than one
            # derived from a number nobody can point at.
            derive_batch(model_id, entry, batch_fields, discount, unreadable is None)

        # A model the page marks deprecated is still sold, so it stays published with its
        # prices -- these three fields are the only thing telling a consumer to plan for
        # the withdrawal. Written only where the page states them: a page that deprecates
        # a model without dating it leaves the dates off rather than having one guessed
        # at. Which models are deprecated is the page's own `status`, and only `retired`
        # means gone.
        if record.get("status") == "deprecated":
            entry["deprecated"] = True
            deprecated_since = _stated_day(record, "deprecatedAt", path)
            if deprecated_since is not None:
                entry["deprecated_since"] = deprecated_since
            retires_on = _stated_day(record, "retiredAt", path)
            if retires_on is not None:
                entry["retires_on"] = retires_on

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

# What the Voice Agents rows' `unpriced` marker says now that the section carrying their
# price is gone. The runner folds it into the transition note it prints once, then
# drops it: what a consumer reads is `unpriced_since`, not why the figure stopped.
#
# On 2026-11-26 that section went. The page now renders its own model-family tables
# ("Generalist models", "Audio models", ...) and states no per-conversation-minute
# figure anywhere; the words "billed per minute of conversation" are gone, and so is
# the `sectionId` the section used to be found by. "Voice agents" survives only as a
# navigation link to /studio/audio/voice-agents. Nothing left on the page says what the
# API costs, and a figure nobody wrote down cannot be published.
_VOICE_NO_SOURCE_REASON = (
    "the Voice Agents section of the API pricing page no longer states a price, and no "
    "other page states one either"
)


def _voice_agents_unpriced(spec: JSONDict, url: str) -> tuple[dict[str, JSONDict], list[str]]:
    """The Voice Agents rows the mapping names, published sold-but-unpriced.

    The price cannot be read any more: on 2026-11-26 the section that carried it went,
    and the page states no per-conversation-minute figure anywhere. Guessing one is not
    an option, and neither is dropping an entry Mistral still bills or failing the whole
    run over one unreadable table. So the rows the mapping lists are published the way
    the products are -- still sold, still not free, with no price this file can state --
    and the runner stamps the transition, keeps the last figure observed beside the
    marker, and reports it once. The row names travel in the mapping so that the same
    keys are produced whether the price is read or not.
    """
    prefix = cast(str, spec["key_prefix"])
    entries: dict[str, JSONDict] = {
        key: {"kind": "product", "display_name": key, "unpriced": _VOICE_NO_SOURCE_REASON}
        for key in (f"{prefix} ({row.strip().lower()})" for row in cast("list[str]", spec["rows"]))
    }
    notes = [
        f"{url}: the {spec['section']!r} section that priced the Voice Agents API is no "
        f"longer on the page, and nothing else states a price for it. Its rows are "
        f"published sold-but-unpriced; put the price back in the mapping's "
        f"`voice_agents` block when a page states it again."
    ]
    return entries, notes


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

    The section is read strictly where it is still there: it must say what its unit is,
    in the words the mapping states, and every row must price one figure per minute.
    Anything else fails the run rather than publish a rate in a unit that may have
    changed.

    Where the section is gone -- as it was by 2026-11-26 -- the rows the mapping names
    are published sold-but-unpriced instead of failing the run. A price nobody states
    cannot be published either way; failing would take every model price in the block
    down with it, which is the worse trade and the one that already happened once. The
    reading is attempted first, so a price Mistral states in the old shape again is read
    rather than missed.
    """
    section = cast(str, spec["section"])
    url = cast(str, spec["source"])
    text = next((c for c in nextjs_flight.decoded_chunks(html_text) if f'"sectionId":"{section}"' in c), None)
    if text is None:
        return _voice_agents_unpriced(spec, url)

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


def extract_new_models(
    fetch: Fetcher, mapping: JSONDict, current_block: JSONDict | None = None
) -> tuple[dict[str, JSONDict], list[str]]:
    """The one callback provider_runner needs: the docs site, the Voice Agents prices,
    plus the frozen products.

    `current_block` is handed on to the docs half, which is the only part of the run that
    needs it: it is the published block the batch rate is read back out of when the page
    that states that rate cannot be read.

    The models half is read strictly, exactly as before. The products half reads
    nothing: it republishes every name the mapping's `products` list holds, and the
    runner dates the state, keeps the last prices actually observed beside the marker
    and reports the transition once. The two halves are checked for a key claimed
    twice, because one key cannot be both a model and a product.
    """
    models, notes = extract_docs_models(fetch, mapping, current_block)

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
