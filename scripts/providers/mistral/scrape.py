#!/usr/bin/env python3
"""Read Mistral's public prices and work out what pricing.json's providers.mistral
block should say.

Standard library only. Reads two public pages and nothing else: it takes no API key,
must never be given one, and never touches anyone's Mistral account.

This script DOES NOT WRITE pricing.json. It writes candidate files into an output
directory and reports which one, if any, the caller should promote. That is
deliberate: "leave pricing.json untouched on every failure path" is then a property
of the design rather than a branch of code somebody has to remember to get right.

TWO SOURCES, because neither one is complete:

  MODELS come from <https://docs.mistral.ai/models>. Its index lists the models
  currently offered, one card each, and every card links to a page carrying a
  machine-readable price object -- `{"price": 4, "denominator": "/1000 Pages"}` --
  along with an `isRetired` flag. It is by far the better source: structured, with
  the unit stated per figure rather than implied by a label.

  PRODUCTS come from <https://mistral.ai/pricing/api>. Web search, code execution,
  image generation, libraries, data capture and the two classifier models are billed
  there and documented nowhere else. They are not models -- there is nothing to call
  -- and they carry `"kind": "product"`.

Reading only the pricing page, as an earlier version did, silently missed four
priced models the docs list and the pricing page does not: OCR 4.0, OCR 3, Leanstral
1.5, and Voxtral Mini Transcribe 2 -- the last of which the pricing page had dropped
while Mistral was still selling it, so this repository published it as withdrawn.
A marketing page is not an inventory.

Entries are keyed by the NAME each source states -- "ocr 4.0", "web search" -- not by
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
from html.parser import HTMLParser
from pathlib import Path
from typing import cast

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

import nextjs_flight  # noqa: E402
import provider_runner  # noqa: E402
from provider_runner import Fetcher, ScrapeError  # noqa: E402
from pricing_validate import JSONDict, check_price  # noqa: E402

# A price row on the page: (label, {"priceUsd": ..., "suffix": ..., ...}).
PriceRow = tuple[str, JSONDict]

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


# --------------------------------------------------------------------------------------
# Page parsing
# --------------------------------------------------------------------------------------


class PricingPageParser(HTMLParser):
    """Extract the price rows of every model card on the pricing page.

    The page marks each model as `<div class="model-item" data-name="...">` and each
    price row inside it as a label paragraph followed by a `<mistral-atom-text-price>`
    element carrying a JSON `data-prices` attribute. Rows are collected per card and
    never globally: the "Libraries" card carries a row labelled "OCR (per 1K pages)"
    that has nothing to do with the OCR model, and a global search for a label would
    happily publish it.

    Result: {card_name: [(label, prices_dict), ...]} in document order.
    """

    CARD_CLASS = "model-item"
    LABEL_CLASS = "text-body-base"
    PRICE_TAG = "mistral-atom-text-price"

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: dict[str, list[PriceRow]] = {}
        self.duplicate_cards: list[str] = []
        self.malformed_prices: list[str] = []

        self._card_name: str | None = None
        self._div_depth = 0
        self._card_div_depth = 0

        self._label_parts: list[str] | None = None
        self._label_depth = 0
        self._last_label: str | None = None

    # -- card and label boundaries ------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: (v or "") for k, v in attrs}
        classes = attr.get("class", "").split()

        if tag == "div":
            self._div_depth += 1

            if self.CARD_CLASS in classes:
                # A new card always ends the previous one, even if the markup nested
                # or unbalanced its divs. Cards must never bleed into each other.
                self._close_card()
                name = attr.get("data-name", "").strip()
                if name in self.cards:
                    self.duplicate_cards.append(name)
                else:
                    self.cards[name] = []
                self._card_name = name
                self._card_div_depth = self._div_depth
            return

        if self._card_name is None:
            return

        if tag == "p":
            if self._label_parts is not None:
                self._label_depth += 1
            elif self.LABEL_CLASS in classes:
                self._label_parts = []
                self._label_depth = 1
            return

        if tag == self.PRICE_TAG:
            self._record_price(attr)

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._label_parts is not None:
            self._label_depth -= 1
            if self._label_depth <= 0:
                self._last_label = " ".join("".join(self._label_parts).split())
                self._label_parts = None
            return

        if tag == "div":
            if self._card_name is not None and self._div_depth <= self._card_div_depth:
                self._close_card()
            self._div_depth = max(0, self._div_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._label_parts is not None:
            self._label_parts.append(data)

    def close(self) -> None:  # noqa: D102
        super().close()
        self._close_card()

    # -- helpers ------------------------------------------------------------------

    def _close_card(self) -> None:
        self._card_name = None
        self._label_parts = None
        self._label_depth = 0
        self._last_label = None

    def _record_price(self, attr: dict[str, str]) -> None:
        raw = attr.get("data-prices")
        if raw is None:
            self.malformed_prices.append(f"{self._card_name}: price element without data-prices")
            return
        try:
            prices = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.malformed_prices.append(f"{self._card_name}: data-prices is not JSON ({exc})")
            return
        if not isinstance(prices, dict):
            self.malformed_prices.append(f"{self._card_name}: data-prices is not an object")
            return
        prices = cast(JSONDict, prices)

        label = self._last_label
        if label is None:
            self.malformed_prices.append(f"{self._card_name}: price with no preceding label")
            return

        assert self._card_name is not None
        self.cards[self._card_name].append((label, prices))
        self._last_label = None


def parse_page(html_text: str) -> dict[str, list[PriceRow]]:
    """Parse the page into cards, or raise ScrapeError if it no longer looks like one."""
    parser = PricingPageParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception as exc:  # a parser crash is a layout failure like any other
        raise ScrapeError(f"the page could not be parsed at all: {exc}") from exc

    if not parser.cards:
        raise ScrapeError(
            "no model card found on the page. The layout has changed: this scraper "
            "looks for <div class='model-item' data-name='...'> elements."
        )

    if parser.duplicate_cards:
        raise ScrapeError(
            f"the same model card appears more than once: {sorted(set(parser.duplicate_cards))}. "
            "Refusing to guess which one carries the real price."
        )

    if parser.malformed_prices:
        raise ScrapeError("malformed price markup: " + "; ".join(parser.malformed_prices[:5]))

    return parser.cards


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

            field = labels.get(row.get("label")) or denominators.get(side, {}).get(denominator)
            if field is None:
                notes.append(
                    f"{model_id}: {side} priced at {price!r} per {denominator!r}"
                    + (f" (labelled {row.get('label')!r})" if row.get("label") else "")
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


def extract_docs_models(fetch: Fetcher, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    """Read every model the docs index currently lists, and price it."""
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
            notes.append(f"{model_id}: no price on this page could be published at all.")
            continue
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
# mistral.ai/pricing/api -- the products
# --------------------------------------------------------------------------------------


def resolve_field(label: str, rows_table: JSONDict) -> tuple[str, JSONDict] | None:
    """Find which price field a row label states, or None if this file cannot tell.

    An exact match wins. Failing that, the longest table entry the label STARTS with
    wins, because Mistral appends an explanatory sentence to some labels -- the storage
    row reads "Storage cost (per month per model) Price per month per model for storage
    (irrespective of model usage; models can be deleted any time)." Matching those in
    full would tie this repository to prose Mistral rewrites at will, including a figure
    ("minimum fee per fine-tuning job of $4") that is itself a price and will change.

    The unit is always in the leading segment, which is what makes a prefix enough here:
    "Training cost (/M tokens)" says per-million-tokens no matter what follows it.
    """
    exact = rows_table.get(label)
    if exact is not None:
        return label, exact

    candidates = [key for key in rows_table if label.startswith(key)]
    if not candidates:
        return None
    best = max(candidates, key=len)
    return best, rows_table[best]


def read_price(prices: JSONDict, spec: JSONDict) -> float | None:
    """Return the USD figure for a row, or None if it cannot be read as stated.

    Returning None rather than raising: one unreadable row is not a reason to withhold
    every other price on the page, and the caller turns it into a reported note.
    """
    # The unit lives in the field name, so a change of unit on the page must never be
    # absorbed silently: a suffix that used to read "/ 1000 pages" and now reads
    # something else invalidates per_1k_pages entirely.
    expected_suffix = spec.get("expect_suffix")
    if expected_suffix is not None:
        actual_suffix = (prices.get("suffix") or "").strip()
        if actual_suffix != expected_suffix.strip():
            return None

    raw = prices.get("priceUsd")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def extract_products(
    cards: dict[str, list[PriceRow]], mapping: JSONDict
) -> tuple[dict[str, JSONDict], list[str]]:
    """Read the billable non-models off the pricing page.

    Only the cards `mapping["products"]` names are read. Everything else on that page
    is a model, and models come from the docs site, which prices them better and lists
    more of them. A name in the list that matches no card is not an error -- the card
    is simply gone, and the runner will date and keep the entry like any other.
    """
    wanted = mapping["products"]
    rows_table: JSONDict = mapping["rows"]

    products: dict[str, JSONDict] = {}
    notes: list[str] = []

    for card_name in wanted:
        rows = cards.get(card_name)
        if not rows:
            continue

        entry: JSONDict = {"kind": "product", "display_name": card_name}

        for label, prices in rows:
            resolved = resolve_field(label, rows_table)
            if resolved is None:
                notes.append(
                    f"{card_name}: row {label!r} is priced at {prices.get('priceUsd')!r} USD, "
                    f"and {DEFAULT_MAPPING.name} has no field for that label. The price is "
                    f"not published. Add the label to the mapping's 'rows' table to publish it."
                )
                continue

            matched_label, spec = resolved
            value = read_price(prices, spec)
            if value is None:
                notes.append(
                    f"{card_name}: row {label!r} (read as {spec['field']} via "
                    f"{matched_label!r}) could not be read as stated -- the page now says "
                    f"suffix {prices.get('suffix')!r} and price {prices.get('priceUsd')!r}. "
                    f"The price is not published, because the field name would no longer "
                    f"say what the figure means."
                )
                continue

            field = spec["field"]
            if field in entry:
                raise ScrapeError(
                    f"{card_name}.{field}: two rows map to it. Refusing to guess which one "
                    f"is the real figure."
                )
            entry[field] = check_price(f"{PROVIDER_ID}/{card_name}", field, value)

        if len(entry) == 2:  # kind and display_name only
            notes.append(f"{card_name}: no price on this card could be published at all.")
            continue
        products[card_name] = entry

    return products, notes


def extract_new_models(fetch: Fetcher, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    """The one callback provider_runner needs. Two sources, merged."""
    models, notes = extract_docs_models(fetch, mapping)
    products, product_notes = extract_products(parse_page(fetch(mapping["products_source"])), mapping)

    clash = sorted(set(models) & set(products))
    if clash:
        raise ScrapeError(
            f"{clash} is published both as a model from the docs site and as a product "
            f"from the pricing page. One key cannot be two things; resolve it by hand."
        )

    return {**models, **products}, notes + product_notes


def main(argv: list[str] | None = None) -> int:
    return provider_runner.main(PROVIDER_ID, extract_new_models, DEFAULT_MAPPING, DEFAULT_CURRENT, CLI_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
