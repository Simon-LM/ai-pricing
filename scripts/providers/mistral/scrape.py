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
# Mapping the page onto API model ids
# --------------------------------------------------------------------------------------


def extract_models(cards: dict[str, list[PriceRow]], mapping: JSONDict) -> dict[str, JSONDict]:
    """Turn parsed cards into a `models` block, through the explicit mapping only.

    Every lookup here is an exact string match against a hand-committed value. A name
    the mapping does not know is a failure to report, never a row to guess at.
    """
    models: dict[str, JSONDict] = {}

    for model_id, spec in mapping["models"].items():
        page_name = spec["page_name"]

        if page_name not in cards:
            raise ScrapeError(
                f"{model_id}: the page has no model card named {page_name!r}. Either the "
                f"page renamed it, or the model is gone. Update {DEFAULT_MAPPING} by "
                f"hand after checking {mapping['source']} -- do not let this be guessed."
            )

        rows = cards[page_name]
        entry: JSONDict = {}

        for field, field_spec in spec["fields"].items():
            label = field_spec["label"]
            matches = [prices for row_label, prices in rows if row_label == label]

            if not matches:
                raise ScrapeError(
                    f"{model_id}: card {page_name!r} has no row labelled {label!r}. "
                    f"Rows found: {sorted({row_label for row_label, _ in rows})}"
                )
            if len(matches) > 1:
                raise ScrapeError(
                    f"{model_id}: card {page_name!r} has {len(matches)} rows labelled "
                    f"{label!r}. Refusing to guess which one is the price."
                )

            prices = matches[0]

            # The unit lives in the field name, so a change of unit on the page must
            # never be absorbed silently: a suffix that used to read "/ 1000 pages"
            # and now reads something else invalidates per_1k_pages entirely.
            expected_suffix = field_spec.get("expect_suffix")
            if expected_suffix is not None:
                actual_suffix = (prices.get("suffix") or "").strip()
                if actual_suffix != expected_suffix.strip():
                    raise ScrapeError(
                        f"{model_id}.{field}: the page now labels this price "
                        f"{actual_suffix!r} instead of {expected_suffix!r}. The unit may "
                        f"have changed; {field} would no longer mean what it says."
                    )

            if "priceUsd" not in prices:
                raise ScrapeError(
                    f"{model_id}.{field}: no priceUsd in {sorted(prices)}. This file "
                    f"publishes USD and performs no conversion."
                )

            entry[field] = check_price(f"{PROVIDER_ID}/{model_id}", field, prices["priceUsd"])

        entry["display_name"] = spec["display_name"]
        # Only written when the mapping says so. The page prices web search, image
        # generation and code execution alongside the models, and those have no API
        # model id to be called by; "model" is the default and stays unwritten.
        if "kind" in spec:
            entry["kind"] = spec["kind"]
        models[model_id] = entry

    return models


def extract_new_models(html_text: str, mapping: JSONDict) -> dict[str, JSONDict]:
    """The one callback provider_runner needs: page text + mapping -> a models dict."""
    return extract_models(parse_page(html_text), mapping)


def main(argv: list[str] | None = None) -> int:
    return provider_runner.main(PROVIDER_ID, extract_new_models, DEFAULT_MAPPING, DEFAULT_CURRENT, CLI_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
