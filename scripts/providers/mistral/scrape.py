#!/usr/bin/env python3
"""Read Mistral's public pricing page and work out what pricing.json's providers.mistral
block should say.

Standard library only. Reads a public web page and nothing else: it takes no API
key, must never be given one, and never touches anyone's Mistral account.

This script DOES NOT WRITE pricing.json. It writes candidate files into an output
directory and reports which one, if any, the caller should promote. That is
deliberate: "leave pricing.json untouched on every failure path" is then a property
of the design rather than a branch of code somebody has to remember to get right.

pricing.json holds one block per provider under `providers.<name>`, because the same
model name can mean two different prices at two different providers (OVH resells
some of the same open models under providers.ovh, at OVH's own prices). This script
only ever reads and writes `providers.mistral`: every other provider's block is
carried through the candidate files byte-for-byte, untouched. The plumbing that
makes that true -- reading, merging, writing, the three outcomes -- is shared with
every other provider and lives in scripts/provider_runner.py. This file supplies
only what is genuinely Mistral-specific: how to parse Mistral's page.

Entries are keyed by the CARD NAME the page states -- "devstral 2", not
"devstral-medium-latest". The page never says which API id a card refers to, so any
id here would be a human's translation of a name, re-checked by hand forever; that is
exactly the kind of hand-maintained list this job is not supposed to need. Consumers
that call the model resolve the id themselves.

Outcomes, matching the three the workflow must implement:

  unchanged  figures identical -> out/stamped.json  (same figures, fresh checked_utc)
  changed    a figure moved    -> out/stamped.json and out/updated.json (the new figures)
  failure    fetch, parse or validation failed -> out/error.txt, exit code 1, no candidates

Usage:
    scripts/providers/mistral/scrape.py --out-dir .ci-out                  # fetch the live page
    scripts/providers/mistral/scrape.py --out-dir .ci-out --html page.html # offline, for tests
"""

from __future__ import annotations

import json
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import cast

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

import provider_runner  # noqa: E402
from provider_runner import ScrapeError  # noqa: E402
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
CLI_SUMMARY = "Read Mistral's public pricing page and work out what providers.mistral should say."


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
# Turning cards into entries
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


def _entry_key(card_name: str, label: str) -> str:
    """The key for a row that has to live in its own entry. See extract_models."""
    return f"{card_name} / {' '.join(label.split()).lower()}"


def extract_models(
    cards: dict[str, list[PriceRow]], mapping: JSONDict
) -> tuple[dict[str, JSONDict], list[str]]:
    """Turn parsed cards into a `models` block: every card the page prices, no list.

    The mapping says which ROW LABELS this repository knows how to publish, and nothing
    else. Which models the page shows is Mistral's business and changes week to week;
    following that is the whole point of this job. A card Mistral adds is published on
    the next run, one it withdraws keeps its last observed prices under an
    `absent_since` stamp, and a card it renames is simply a removal and an addition.

    The key is the card name exactly as the page states it -- "devstral 2", not
    "devstral-medium-latest". The page does not say which API id a card refers to, and
    nothing else on it does either, so any such key would be a human's translation
    rather than something read from the source. Consumers that need to call the model
    resolve the id themselves, from Mistral's own model cards.

    Most cards become one entry. A card that prices two DIFFERENT things in the same
    unit cannot: "ocr 4.1" states a per-1000-pages price for OCR and another for
    Document AI, and one entry has exactly one per_1k_pages field. Those rows each get
    their own entry, keyed "<card> / <row label>", for every row involved rather than
    just the second one -- so that the keys do not swap the day Mistral reorders the
    rows. Rows on the same card that do not collide stay in the plain card entry.
    """
    rows_table: JSONDict = mapping["rows"]
    products = set(mapping.get("products", []))

    models: dict[str, JSONDict] = {}
    notes: list[str] = []

    for card_name, rows in cards.items():
        if not rows:
            # Three cards state no price at all -- an agent product, a moderation model
            # and a research preview. They are absent from the file rather than
            # published at 0, and they are not worth a weekly note: "still not priced"
            # is not news, and an alert channel that repeats itself gets ignored.
            continue

        # (field, value, label, spec) for every row this file knows how to read.
        read: list[tuple[str, float, str, JSONDict]] = []

        for label, prices in rows:
            resolved = resolve_field(label, rows_table)
            if resolved is None:
                notes.append(
                    f"{card_name}: row {label!r} is priced at "
                    f"{prices.get('priceUsd')!r} USD, and {DEFAULT_MAPPING.name} has no "
                    f"field for that label. The price is not published. Add the label to "
                    f"the mapping's 'rows' table to publish it."
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

            read.append((spec["field"], value, label, spec))

        if not read:
            notes.append(f"{card_name}: no price on this card could be published at all.")
            continue

        # A field claimed by more than one row on the same card cannot share an entry.
        collided = {f for f in {r[0] for r in read} if sum(1 for r in read if r[0] == f) > 1}

        for field, value, label, spec in read:
            if field in collided:
                key = _entry_key(card_name, label)
                kind = spec.get("kind")
            else:
                key = card_name
                kind = "product" if card_name in products else None

            entry = models.setdefault(key, {"display_name": key})
            if kind is not None:
                entry["kind"] = kind
            entry[field] = check_price(f"{PROVIDER_ID}/{key}", field, value)

    if not models:
        raise ScrapeError(
            "not a single card on the page could be published. Either Mistral "
            "restructured it, or the page is not what it looks like. Refusing to publish "
            "an empty block."
        )

    return models, notes


def extract_new_models(html_text: str, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    """The one callback provider_runner needs: page text + mapping -> a models dict."""
    return extract_models(parse_page(html_text), mapping)


def main(argv: list[str] | None = None) -> int:
    return provider_runner.main(PROVIDER_ID, extract_new_models, DEFAULT_MAPPING, DEFAULT_CURRENT, CLI_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
