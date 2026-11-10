#!/usr/bin/env python3
"""Read Google Cloud's public Speech-to-Text pricing page and work out what pricing.json's
providers.googlecloud block should say.

Standard library only. Reads a public page and nothing else: it takes no API key, must
never be given one, and never touches anyone's Google Cloud account.

This script DOES NOT WRITE pricing.json. It writes candidate files into an output
directory and reports which one, if any, the caller should promote, exactly like the
other providers' scrapers (see provider_runner).

Google Cloud is a block of its own, apart from `providers.google`: the Gemini API and
Google Cloud are two products, billed apart. This block holds Speech-to-Text.

THE PAGE, <https://cloud.google.com/speech-to-text/pricing>, is rendered on the server.
Under a heading per API version ("Speech-to-Text V2 API", "Speech-to-Text V1 API"), each
table is headed "Category | Model | Price (USD)", then a column per commitment plan,
which is not read. A row's price cell is either one price, "$0.003 / 1 minute", or a list
of tiers by the minutes processed in the month:

    0 minute to 500,000 minute            $0.016 / 1 minute, per 1 month / account
    500,000 minute to 1,000,000 minute    $0.01 / 1 minute, per 1 month / account
    ...

A row with one price, or one paid tier after a free one, is one entry. A row with
several paid tiers is one entry per tier, its range in the key: each entry holds a single
price, `per_audio_minute`. A free tier ("$0.00 (Free)") is not an entry; the README says
how many minutes it covers.

Keys are made from what the row states, lowercased: "speech-to-text v2 recognition
standard (0 to 500,000 min/month)", "speech-to-text v1 medical dictation". The page
names no API id per price: "Standard" covers the models `default`, `latest_long`, `chirp`
and others, which are request parameters, not prices.

Usage:
    scripts/providers/googlecloud/scrape.py --out-dir .ci-out                      # live
    scripts/providers/googlecloud/scrape.py --out-dir .ci-out --html pricing.html  # offline
"""

from __future__ import annotations

import html
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import provider_runner  # noqa: E402
from provider_runner import Fetcher, ScrapeError  # noqa: E402
from pricing_validate import JSONDict, check_price  # noqa: E402

PROVIDER_ID = "googlecloud"
DEFAULT_MAPPING = Path(__file__).resolve().parent / "mapping.json"
DEFAULT_CURRENT = REPO_ROOT / "pricing.json"

# Kept as its own constant rather than read off __doc__: the module docstring is
# stripped to None under `python -OO`.
CLI_SUMMARY = "Read Google Cloud's public Speech-to-Text pricing page and work out what providers.googlecloud should say."

_TABLE = re.compile(r"<table\b.*?</table>", re.S)
_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S)
_TIER = re.compile(r"<div[^>]*>\s*<div[^>]*>(?P<range>[^<]*)</div>\s*<div[^>]*>(?P<price>[^<]*)</div>\s*</div>")
_HEADING = re.compile(r"<h[1-4]\b[^>]*>(?P<text>.*?)</h[1-4]>", re.S)


def text(fragment: str) -> str:
    """A fragment's words, tags out, a tooltip's hidden contents out."""
    fragment = re.sub(r"<(script|style|svg)\b.*?</\1>", " ", fragment, flags=re.S)
    fragment = re.sub(r"<sup>.*?</sup>", "", fragment, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def header_text(cell: str) -> str:
    """A header cell's own words: an info button's tooltip ("info Consumption model ID:
    ...") follows them, and is not part of the column's name."""
    return re.split(r"\s+info\b", text(cell), maxsplit=1)[0]


def tables(page: str, mapping: JSONDict) -> list[tuple[str, list[list[str]]]]:
    """Every table, with the API version of the last heading above it, and its rows as
    raw cell HTML."""
    version_heading = re.compile(cast(str, mapping["version_heading"]))
    headings = [(m.start(), version_heading.search(text(m["text"]))) for m in _HEADING.finditer(page)]
    out: list[tuple[str, list[list[str]]]] = []
    for table in _TABLE.finditer(page):
        above = [h for start, h in headings if start < table.start() and h]
        version = above[-1]["version"] if above else ""
        rows = [_CELL.findall(row) for row in _ROW.findall(table.group(0))]
        out.append((version, [r for r in rows if r]))
    return out


def price(cell: str, where: str) -> Decimal | None:
    """Dollars per minute, or None for a free tier."""
    if re.fullmatch(r"\$0\.00 \(Free\) / 1 minute(, per 1 month / account)?", cell):
        return None
    match = re.fullmatch(r"\$(?P<amount>[0-9]+(?:\.[0-9]+)?) / 1 minute(, per 1 month / account)?", cell)
    if match is None:
        raise ScrapeError(f"{where}: {cell!r} is not a price per minute this can read.")
    return Decimal(match["amount"])


def tier_label(cell: str, where: str) -> str:
    """"0 minute to 500,000 minute" -> "0 to 500,000 min/month";
    "2,000,000 minute and above" -> "2,000,000+ min/month"."""
    span = re.fullmatch(r"(?P<low>[0-9,]+) minute to (?P<high>[0-9,]+) minute", cell)
    if span:
        return f"{span['low']} to {span['high']} min/month"
    above = re.fullmatch(r"(?P<low>[0-9,]+) minute and above", cell)
    if above:
        return f"{above['low']}+ min/month"
    raise ScrapeError(f"{where}: {cell!r} is not a range of minutes this can read.")


def row_key(version: str, category: str, model: str) -> str:
    category = re.sub(r"\(sku:[^)]*\)|sku:\S+", "", category)
    category = re.sub(r"[()]", "", category).strip()
    model = model.rstrip("¹²³ ").strip()
    words = f"speech-to-text v{version} {category}"
    if model.lower() not in category.lower():
        words += f" {model}"
    return re.sub(r"\s+", " ", words).lower()


def extract_models(page: str, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    header = cast("list[str]", mapping["header"])
    field = cast(str, mapping["field"])
    models: dict[str, JSONDict] = {}
    notes: list[str] = []
    for version, rows in tables(page, mapping):
        if not rows or [header_text(c) for c in rows[0][: len(header)]] != header:
            notes.append(f"a table headed {[header_text(c) for c in rows[0]] if rows else []}, not {header}; not read")
            continue
        if not version:
            raise ScrapeError("a price table under no 'Speech-to-Text V… API' heading. The page changed shape.")
        for cells in rows[1:]:
            if len(cells) < 3:
                raise ScrapeError(f"V{version}: a row of {len(cells)} cells.")
            category, model = text(cells[0]), text(cells[1])
            key = row_key(version, category, model)
            where = f"V{version} / {category} / {model}"
            tiers = [(text(t["range"]), text(t["price"])) for t in _TIER.finditer(cells[2])]
            if not tiers:
                tiers = [("", text(cells[2]))]
            paid = [(label, price(cell, where)) for label, cell in tiers]
            paid = [(label, value) for label, value in paid if value is not None]
            if not paid:
                notes.append(f"{where}: every tier is free; not published")
                continue
            for label, value in paid:
                name = f"{key} ({tier_label(label, where)})" if len(paid) > 1 else key
                if name in models:
                    raise ScrapeError(f"{name}: priced twice.")
                models[name] = {field: check_price(f"{PROVIDER_ID}/{name}", field, float(cast(Decimal, value))),
                                "display_name": name}
    if len(models) < int(cast(int, mapping["min_entries"])):
        raise ScrapeError(f"only {len(models)} entries read. The page changed shape; refusing to publish so few.")
    return models, notes


def extract_new_models(fetch: Fetcher, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    """The one callback provider_runner needs. One source, read once."""
    return extract_models(fetch(cast(str, mapping["source"])), mapping)


def main(argv: list[str] | None = None) -> int:
    return provider_runner.main(PROVIDER_ID, extract_new_models, DEFAULT_MAPPING, DEFAULT_CURRENT, CLI_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
