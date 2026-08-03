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
model name can mean two different prices at two different providers. This script
only ever reads and writes `providers.mistral`: every other provider's block, once
one exists, is carried through the candidate files byte-for-byte, untouched.

Outcomes, matching the three the workflow must implement:

  unchanged  figures identical -> out/stamped.json  (same figures, fresh checked_utc)
  changed    a figure moved    -> out/stamped.json and out/updated.json (the new figures)
  failure    fetch, parse or validation failed -> out/error.txt, exit code 1, no candidates

Usage:
    scripts/providers/mistral/scrape.py --out-dir .ci-out                  # fetch the live page
    scripts/providers/mistral/scrape.py --out-dir .ci-out --html page.html # offline, for tests
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, cast

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from fetch import FetchError, fetch_page  # noqa: E402
from pricing_validate import (  # noqa: E402
    KNOWN_PRICE_FIELDS,
    SCHEMA_VERSION,
    JSONDict,
    ValidationError,
    check_change,
    check_price,
    diff_models,
    validate_document,
)

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


class ScrapeError(Exception):
    """Anything that means: do not publish, report it, leave the file alone."""


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
        models[model_id] = entry

    return models


# --------------------------------------------------------------------------------------
# Document assembly
# --------------------------------------------------------------------------------------


def build_provider_block(models: dict[str, JSONDict], checked_utc: str, updated: str, mapping: JSONDict) -> JSONDict:
    """Assemble a `providers.mistral`-shaped block with a stable, reviewable key order."""
    ordered_models: dict[str, JSONDict] = {}
    for model_id, entry in models.items():
        ordered: JSONDict = {}
        for field in KNOWN_PRICE_FIELDS:
            if field in entry:
                ordered[field] = entry[field]
        ordered["display_name"] = entry["display_name"]
        ordered_models[model_id] = ordered

    return {
        "checked_utc": checked_utc,
        "updated": updated,
        "source": mapping["source"],
        "currency": mapping["currency"],
        "models": ordered_models,
    }


def merge_provider_block(full_doc: JSONDict, provider_block: JSONDict) -> JSONDict:
    """Return a copy of `full_doc` with only `providers.mistral` replaced.

    Every other provider's block -- there are none today, OVH's will be the first --
    is carried over exactly as it stood, in its original position. This script has
    no business reading, let alone rewriting, a block it did not scrape.

    Key order matters here for a reason beyond taste: a merge that rebuilt the
    `providers` dict as "everyone else, then mistral" would move mistral to the end
    whenever it did not already sit there, and every other provider's block would
    show up as removed-and-re-added in the git diff a human is about to review --
    noise indistinguishable, at a glance, from an actual change to that provider.
    """
    providers = full_doc["providers"]
    merged = {pid: (provider_block if pid == PROVIDER_ID else block) for pid, block in providers.items()}
    merged.setdefault(PROVIDER_ID, provider_block)
    return {
        "schema_version": full_doc.get("schema_version", SCHEMA_VERSION),
        "providers": merged,
    }


def write_json(path: Path, doc: JSONDict) -> None:
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_json(path: Path, what: str) -> Any:
    """Parse a JSON file without asserting anything about its shape.

    Deliberately `Any`, not `JSONDict`: this only proves the bytes were valid JSON.
    A file could still parse to a list, a string, or anything else JSON allows.
    Callers that need an object -- `validate_document` for pricing.json, the shape
    checks in `run()` for the mapping -- assert that themselves.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScrapeError(f"{what} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ScrapeError(f"{what} is not valid JSON ({path}): {exc}") from exc


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------


def run(args: argparse.Namespace) -> tuple[str, str]:
    """Do the whole job. Returns (outcome, human-readable summary)."""
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    mapping = load_json(Path(args.mapping), "mapping")
    current = load_json(Path(args.current), "current pricing.json")

    # A committed file that is already invalid must not be used as a comparison base,
    # even if the corruption sits in some other provider's block, not ours.
    try:
        validate_document(current)
    except ValidationError as exc:
        raise ScrapeError(f"the committed pricing.json is invalid: {exc}") from exc

    current_block = current["providers"].get(PROVIDER_ID)
    if current_block is None:
        raise ScrapeError(
            f"pricing.json has no providers.{PROVIDER_ID} block yet. Seed one by hand "
            f"before the first automated run -- this job updates a provider's figures, "
            f"it does not decide to start publishing a new one."
        )

    if current_block.get("currency") != mapping["currency"]:
        raise ScrapeError(
            f"currency mismatch: providers.{PROVIDER_ID} says {current_block.get('currency')!r}, "
            f"mapping says {mapping['currency']!r}. This file performs no conversion, so this "
            f"must be fixed by hand."
        )

    published_but_unmapped = sorted(set(current_block["models"]) - set(mapping["models"]))
    if published_but_unmapped:
        raise ScrapeError(
            f"providers.{PROVIDER_ID} publishes model(s) the mapping does not know: "
            f"{published_but_unmapped}. Consumers may already depend on them; removing one "
            f"is a deliberate human decision, not something this job may do."
        )

    try:
        html_text = (
            Path(args.html).read_text(encoding="utf-8", errors="replace")
            if args.html
            else fetch_page(mapping["source"])
        )
    except FetchError as exc:
        raise ScrapeError(str(exc)) from exc

    new_models = extract_models(parse_page(html_text), mapping)

    # Compare against what is published before believing any of it.
    for model_id, entry in new_models.items():
        old_entry = current_block["models"].get(model_id)
        if not old_entry:
            continue
        for field in KNOWN_PRICE_FIELDS:
            if field in entry and field in old_entry:
                check_change(f"{PROVIDER_ID}/{model_id}", field, float(old_entry[field]), float(entry[field]))

    now = args.now or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now[:10]

    changes = diff_models(current_block["models"], new_models)

    # Always produced: the same figures with a fresh verification stamp. This is a real
    # statement to consumers ("confirmed unchanged today") and it is also the commit that
    # keeps GitHub from disabling the schedule after 60 days of no commits.
    stamped_block = build_provider_block(current_block["models"], now, current_block["updated"], mapping)
    stamped = merge_provider_block(current, stamped_block)
    validate_document(stamped)
    write_json(out_dir / "stamped.json", stamped)

    if not changes:
        return "unchanged", f"Figures unchanged. Verified against {mapping['source']} at {now}."

    updated_block = build_provider_block(new_models, now, today, mapping)
    updated_doc = merge_provider_block(current, updated_block)
    validate_document(updated_doc)
    write_json(out_dir / "updated.json", updated_doc)

    summary = "\n".join(
        [f"Figures changed, verified against {mapping['source']} at {now}:", ""]
        + [f"  {line}" for line in changes]
    )
    return "changed", summary


def emit_github_output(**values: str) -> None:
    """Write step outputs for the workflow, if we are running inside one."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}<<__AI_PRICING_EOF__\n{value}\n__AI_PRICING_EOF__\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=CLI_SUMMARY)
    parser.add_argument("--out-dir", default=".ci-out", help="where candidate files are written")
    parser.add_argument("--current", default=str(DEFAULT_CURRENT), help="the published pricing.json")
    parser.add_argument("--mapping", default=str(DEFAULT_MAPPING), help="the explicit mapping")
    parser.add_argument("--html", help="read this local HTML file instead of fetching (tests)")
    parser.add_argument("--now", help="override the UTC stamp, format YYYY-MM-DDTHH:MM:SSZ (tests)")
    args = parser.parse_args(argv)

    try:
        outcome, summary = run(args)
    except (ScrapeError, ValidationError) as exc:
        message = str(exc)
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "error.txt").write_text(message + "\n", encoding="utf-8")
        # Nothing may be published from here on. pricing.json was never opened for writing.
        for stale in ("stamped.json", "updated.json", "summary.txt"):
            (out_dir / stale).unlink(missing_ok=True)
        print(f"FAILED: {message}", file=sys.stderr)
        emit_github_output(outcome="failed", summary=message)
        return 1

    # The summary is written to a file as well as to the step output. The workflow
    # reads the file: it must never interpolate text derived from a remote page into
    # a shell command, and error messages quote labels found on that page.
    (Path(args.out_dir) / "summary.txt").write_text(summary + "\n", encoding="utf-8")

    print(summary)
    emit_github_output(outcome=outcome, summary=summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
