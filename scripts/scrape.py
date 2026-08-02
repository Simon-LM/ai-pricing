#!/usr/bin/env python3
"""Read Mistral's public pricing page and work out what pricing.json should say.

Standard library only. Reads a public web page and nothing else: it takes no API
key, must never be given one, and never touches anyone's Mistral account.

This script DOES NOT WRITE pricing.json. It writes candidate files into an output
directory and reports which one, if any, the caller should promote. That is
deliberate: "leave pricing.json untouched on every failure path" is then a property
of the design rather than a branch of code somebody has to remember to get right.

Outcomes, matching the three the workflow must implement:

  unchanged  figures identical -> out/stamped.json  (same figures, fresh checked_utc)
  changed    a figure moved    -> out/stamped.json and out/updated.json (the new figures)
  failure    fetch, parse or validation failed -> out/error.txt, exit code 1, no candidates

Usage:
    scripts/scrape.py --out-dir .ci-out                    # fetch the live page
    scripts/scrape.py --out-dir .ci-out --html page.html   # offline, for tests
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from validate import (  # noqa: E402
    KNOWN_PRICE_FIELDS,
    SCHEMA_VERSION,
    ValidationError,
    check_change,
    check_price,
    diff_models,
    validate_document,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAPPING = REPO_ROOT / "scripts" / "mapping.json"
DEFAULT_CURRENT = REPO_ROOT / "pricing.json"

USER_AGENT = "ai-pricing-bot/1 (+https://github.com/Simon-LM/ai-pricing)"
FETCH_TIMEOUT_SECONDS = 30

# Below this, the response is not a pricing page -- it is an error page, a consent
# wall, or a redirect stub. Fail rather than parse it and find nothing.
MIN_PAGE_BYTES = 10_000


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
        self.cards: dict[str, list[tuple[str, dict]]] = {}
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

        label = self._last_label
        if label is None:
            self.malformed_prices.append(f"{self._card_name}: price with no preceding label")
            return

        assert self._card_name is not None
        self.cards[self._card_name].append((label, prices))
        self._last_label = None


def parse_page(html_text: str) -> dict[str, list[tuple[str, dict]]]:
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


def extract_models(cards: dict[str, list[tuple[str, dict]]], mapping: dict) -> dict[str, dict]:
    """Turn parsed cards into a `models` block, through the explicit mapping only.

    Every lookup here is an exact string match against a hand-committed value. A name
    the mapping does not know is a failure to report, never a row to guess at.
    """
    models: dict[str, dict] = {}

    for model_id, spec in mapping["models"].items():
        page_name = spec["page_name"]

        if page_name not in cards:
            raise ScrapeError(
                f"{model_id}: the page has no model card named {page_name!r}. Either the "
                f"page renamed it, or the model is gone. Update scripts/mapping.json by "
                f"hand after checking {mapping['source']} -- do not let this be guessed."
            )

        rows = cards[page_name]
        entry: dict[str, object] = {}

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

            entry[field] = check_price(model_id, field, prices["priceUsd"])

        entry["display_name"] = spec["display_name"]
        models[model_id] = entry

    return models


# --------------------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------------------


def fetch_page(url: str) -> str:
    """GET a public page. No credentials of any kind are sent, ever."""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"}
    )
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise ScrapeError(f"{url} returned HTTP {response.status}")
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        raise ScrapeError(f"{url} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ScrapeError(f"{url} could not be fetched: {exc}") from exc

    if len(body) < MIN_PAGE_BYTES:
        raise ScrapeError(
            f"{url} returned only {len(body)} characters, too short to be the pricing "
            f"page. Probably an error or consent page."
        )
    return body


# --------------------------------------------------------------------------------------
# Document assembly
# --------------------------------------------------------------------------------------


def build_document(base: dict, models: dict, checked_utc: str, updated: str, mapping: dict) -> dict:
    """Assemble a pricing.json document with a stable, reviewable key order."""
    ordered_models: dict[str, dict] = {}
    for model_id, entry in models.items():
        ordered: dict[str, object] = {}
        for field in KNOWN_PRICE_FIELDS:
            if field in entry:
                ordered[field] = entry[field]
        ordered["display_name"] = entry["display_name"]
        ordered_models[model_id] = ordered

    return {
        "schema_version": base.get("schema_version", SCHEMA_VERSION),
        "checked_utc": checked_utc,
        "updated": updated,
        "source": mapping["source"],
        "currency": mapping["currency"],
        "models": ordered_models,
    }


def write_json(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_json(path: Path, what: str) -> dict:
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

    # A committed file that is already invalid must not be used as a comparison base.
    try:
        validate_document(current)
    except ValidationError as exc:
        raise ScrapeError(f"the committed pricing.json is invalid: {exc}") from exc

    if current.get("currency") != mapping["currency"]:
        raise ScrapeError(
            f"currency mismatch: pricing.json says {current.get('currency')!r}, mapping says "
            f"{mapping['currency']!r}. This file performs no conversion, so this must be fixed by hand."
        )

    published_but_unmapped = sorted(set(current["models"]) - set(mapping["models"]))
    if published_but_unmapped:
        raise ScrapeError(
            f"pricing.json publishes model(s) the mapping does not know: {published_but_unmapped}. "
            f"Consumers may already depend on them; removing one is a deliberate human decision, "
            f"not something this job may do."
        )

    html_text = (
        Path(args.html).read_text(encoding="utf-8", errors="replace")
        if args.html
        else fetch_page(mapping["source"])
    )

    new_models = extract_models(parse_page(html_text), mapping)

    # Compare against what is published before believing any of it.
    for model_id, entry in new_models.items():
        old_entry = current["models"].get(model_id)
        if not old_entry:
            continue
        for field in KNOWN_PRICE_FIELDS:
            if field in entry and field in old_entry:
                check_change(model_id, field, float(old_entry[field]), float(entry[field]))

    now = args.now or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now[:10]

    changes = diff_models(current["models"], new_models)

    # Always produced: the same figures with a fresh verification stamp. This is a real
    # statement to consumers ("confirmed unchanged today") and it is also the commit that
    # keeps GitHub from disabling the schedule after 60 days of no commits.
    stamped = build_document(current, current["models"], now, current["updated"], mapping)
    validate_document(stamped)
    write_json(out_dir / "stamped.json", stamped)

    if not changes:
        return "unchanged", f"Figures unchanged. Verified against {mapping['source']} at {now}."

    updated_doc = build_document(current, new_models, now, today, mapping)
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
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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
