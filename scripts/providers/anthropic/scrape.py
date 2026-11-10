#!/usr/bin/env python3
"""Read Anthropic's public pricing page, and its model deprecations page for the ids,
and work out what pricing.json's providers.anthropic block should say.

Standard library only. Reads public pages and nothing else: it takes no API key, must
never be given one, and never touches anyone's Anthropic account.

This script DOES NOT WRITE pricing.json. It writes candidate files into an output
directory and reports which one, if any, the caller should promote, exactly like the
other providers' scrapers (see provider_runner).

Two pages are read:

  `source`         <https://platform.claude.com/docs/en/about-claude/pricing>. Its
                   tables a human reads are rendered on the server, and are what this
                   reads: the model table (input, output, and the three prompt caching
                   rates), the batch table, the fast mode table, and the Claude Managed
                   Agents table. Three tools are priced in sentences, not tables: web
                   search, code execution and web fetch.
  `status_source`  <https://platform.claude.com/docs/en/about-claude/model-deprecations>.
                   The pricing page names models ("Claude Opus 4.5") and states no id.
                   This page's status table states each model's API id and its state:
                   Active, Deprecated or Retired.

A priced name is matched to its id by the rule Anthropic's "Model IDs and versions" page
states: "Claude Opus 4.5" is `claude-opus-4-5`, followed by `-YYYYMMDD` for a model
before the 4.6 generation. A Retired model is not published, though the pricing page
may still price it: on 2026-11-10 it priced Claude Opus 4.1, Opus 4 and Sonnet 4, all
three retired. A priced model the status table does not list is reported and not
published. An id with a snapshot date also gets the shorter alias the same page says
the Claude API accepts for it, `claude-opus-4-5`, which points to the most recent
snapshot of that minor version.

Usage:
    scripts/providers/anthropic/scrape.py --out-dir .ci-out                       # live
    scripts/providers/anthropic/scrape.py --out-dir .ci-out --offline manifest.json  # fixtures
"""

from __future__ import annotations

import html
import re
import sys
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import provider_runner  # noqa: E402
from provider_runner import Fetcher, ScrapeError  # noqa: E402
from pricing_validate import JSONDict, check_price  # noqa: E402

PROVIDER_ID = "anthropic"
DEFAULT_MAPPING = Path(__file__).resolve().parent / "mapping.json"
DEFAULT_CURRENT = REPO_ROOT / "pricing.json"

# Kept as its own constant rather than read off __doc__: the module docstring is
# stripped to None under `python -OO`.
CLI_SUMMARY = "Read Anthropic's public pricing page and work out what providers.anthropic should say."


# --------------------------------------------------------------------------------------
# The page's tables, as text
# --------------------------------------------------------------------------------------


class Cell:
    def __init__(self, header: bool, rowspan: int) -> None:
        self.header = header
        self.rowspan = rowspan
        self.parts: list[str] = []

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self.parts)).strip()


class _Tables(HTMLParser):
    """Every <table> on a page: its rows, each a list of cells with their text.

    Comments are dropped, which is what joins "$8<!-- --> / MTok" back into "$8 / MTok".
    A nested table is not expected on these pages and would be read into its parent.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[Cell]]] = []
        self._depth = 0
        self._row: list[Cell] | None = None
        self._cell: Cell | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self.tables.append([])
        elif self._depth and tag == "tr":
            self._row = []
            self.tables[-1].append(self._row)
        elif self._depth and tag in ("td", "th") and self._row is not None:
            span = dict(attrs).get("rowspan") or "1"
            self._cell = Cell(tag == "th", int(span) if span.isdigit() else 1)
            self._row.append(self._cell)
        elif self._cell is not None and tag in ("br", "div", "p", "span", "a", "button"):
            self._cell.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._depth:
            self._depth -= 1
        elif tag in ("td", "th"):
            self._cell = None
        elif tag == "tr":
            self._row = None
        elif self._cell is not None and tag in ("div", "p", "span", "a", "button"):
            self._cell.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.parts.append(data)


def tables(page: str) -> list[list[list[Cell]]]:
    parser = _Tables()
    parser.feed(page)
    parser.close()
    return parser.tables


def _header_rows(table: list[list[Cell]]) -> list[list[str]]:
    """The rows of header cells a table starts with. A band of header cells further
    down, such as "Additional models", is not one of them."""
    rows: list[list[str]] = []
    for row in table:
        if not row or not all(c.header for c in row):
            break
        rows.append([c.text for c in row])
    return rows


def find_table(page_tables: list[list[list[Cell]]], headers: list[list[str]], what: str, url: str) -> list[list[Cell]]:
    """The one table whose header rows are exactly `headers`."""
    found = [t for t in page_tables if _header_rows(t) == headers]
    if len(found) != 1:
        raise ScrapeError(
            f"{url}: {len(found)} tables headed {headers}, where the {what} is expected in "
            f"exactly one. The page changed shape, or a column was renamed: check the "
            f"page against the mapping's `tables`."
        )
    return found[0]


def body_rows(table: list[list[Cell]]) -> list[list[Cell]]:
    """The rows that hold data cells. A row of header cells only -- the column headers,
    or a band such as "Additional models" -- is not one."""
    return [row for row in table if any(not c.header for c in row)]


def page_text(page: str) -> str:
    """The page's text, scripts and styles left out, whitespace collapsed."""
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S | re.I)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body)))


# --------------------------------------------------------------------------------------
# Names and ids
# --------------------------------------------------------------------------------------


_NAME = re.compile(r"Claude (?P<family>[A-Z][a-z]+) (?P<version>[0-9]+(?:\.[0-9]+)?)(?![0-9.])")
_DATED = re.compile(r"(?P<alias>.+)-(?P<date>[0-9]{8})")


def model_names(cell_text: str) -> list[str]:
    """The model names a name cell states: one, or several joined by " / ", as the fast
    mode table does for "Claude Opus 5 / Claude Opus 4.8". A description after the name
    ("For demanding reasoning...") is not part of it."""
    names = [m.group(0) for m in _NAME.finditer(cell_text)]
    if not names or not cell_text.startswith(names[0]):
        return []
    return names


def base_id(name: str) -> str:
    """"Claude Opus 4.5" -> "claude-opus-4-5", the format Anthropic's ids page states."""
    match = _NAME.fullmatch(name)
    if match is None:
        raise ScrapeError(f"{name!r} is not a model name this can read.")
    return f"claude-{match['family'].lower()}-{match['version'].replace('.', '-')}"


def status_table(page: str, mapping: JSONDict) -> dict[str, str]:
    """API model id -> current state, from the deprecations page's status table."""
    url = cast(str, mapping["status_source"])
    spec = cast(JSONDict, mapping["tables"]["status"])
    table = find_table(tables(page), cast("list[list[str]]", spec["headers"]), "model status table", url)
    states: dict[str, str] = {}
    for row in body_rows(table):
        if len(row) < 2:
            continue
        model_id, state = row[0].text, row[1].text
        if not re.fullmatch(r"claude-[a-z0-9.-]+", model_id):
            raise ScrapeError(f"{url}: the status table lists {model_id!r}, not an API model id.")
        if model_id in states:
            raise ScrapeError(f"{url}: the status table lists {model_id!r} twice.")
        states[model_id] = state
    if not states:
        raise ScrapeError(f"{url}: the status table lists no model. Its layout has changed.")
    return states


def _alias(model_id: str) -> str | None:
    """The id without its snapshot date, or None for an id that has none."""
    dated = _DATED.fullmatch(model_id)
    return dated["alias"] if dated else None


def resolve(name: str, states: dict[str, str], mapping: JSONDict) -> tuple[str | None, str | None]:
    """(the model's id, what its state means), or (None, None) if the status table does
    not list it."""
    base = base_id(name)
    found = [i for i in states if base in (i, _alias(i))]
    if len(found) > 1:
        raise ScrapeError(f"{name}: the status table lists {found}, all of them this model. Refusing to guess.")
    if not found:
        return None, None
    state = states[found[0]]
    known = cast("dict[str, str]", mapping["states"])
    if state not in known:
        raise ScrapeError(
            f"{found[0]}: the status table says {state!r}, a state the mapping does not know. "
            f"Add it to the mapping's `states`, saying whether it is still sold."
        )
    return found[0], known[state]


def ids_entry(model_id: str) -> JSONDict:
    """`api_ids` for a dated id: the id, then the alias Anthropic says points to it."""
    alias = _alias(model_id)
    return {"api_ids": [model_id, alias]} if alias else {}


# --------------------------------------------------------------------------------------
# Prices
# --------------------------------------------------------------------------------------


_PRICE = re.compile(
    r"\$(?P<amount>[0-9]+(?:\.[0-9]+)?)(?: USD)? / MTok"
    r"(?: for prompts (?P<side>up to|over) (?P<size>[0-9][0-9,]*) tokens)?"
)


def _price(cell: str, where: str) -> tuple[Decimal, str | None, int | None]:
    """(dollars per million tokens, "up to"/"over" or None, the size it names or None)."""
    match = _PRICE.fullmatch(cell)
    if match is None:
        raise ScrapeError(f"{where}: {cell!r} is not a price per MTok this can read.")
    size = int(match["size"].replace(",", "")) if match["size"] else None
    return Decimal(match["amount"]), match["side"], size


def priced_rows(
    table: list[list[Cell]], columns: list[str], where: str
) -> tuple[dict[str, dict[str, list[tuple[Decimal, str | None, int | None]]]], list[str]]:
    """name -> column -> the prices stated, one per row the name spans.

    A name cell spanning two rows (Claude Haiku 5.5, priced up to and over 100,000
    tokens) gives each column two prices; the second row has no name cell of its own.
    """
    found: dict[str, dict[str, list[tuple[Decimal, str | None, int | None]]]] = {}
    notes: list[str] = []
    current: str | None = None
    carried = 0
    for row in body_rows(table):
        cells = [c.text for c in row]
        if len(cells) == len(columns) + 1:
            names = model_names(cells[0])
            if len(names) != 1:
                notes.append(f"{where}: a row named {cells[0]!r}, not one model name this can read; not published")
                current, carried = None, 0
                continue
            current, carried = names[0], row[0].rowspan - 1
            if current in found:
                raise ScrapeError(f"{where}: {current} is priced twice.")
            found[current] = {c: [] for c in columns}
            prices = cells[1:]
        elif len(cells) == len(columns) and carried > 0 and current is not None:
            carried -= 1
            prices = cells
        else:
            raise ScrapeError(f"{where}: a row of {len(cells)} cells, where {len(columns) + 1} are expected: {cells!r}.")
        if current is None:
            continue
        for column, cell in zip(columns, prices):
            found[current][column].append(_price(cell, f"{where}, {current}, {column}"))
    return found, notes


def tiered(
    name: str, column: str, prices: list[tuple[Decimal, str | None, int | None]], where: str
) -> tuple[Decimal, Decimal | None, int | None]:
    """(the price, the price past the threshold or None, the threshold or None).

    One price is flat. Two must be "up to N" then "over N": the prompt that reaches
    N + 1 tokens is billed at the second, so the threshold published is N + 1, the same
    reading Perplexity's "(≤272k) / (>272k)" gets.
    """
    if len(prices) == 1 and prices[0][1] is None:
        return prices[0][0], None, None
    if (
        len(prices) == 2
        and (prices[0][1], prices[1][1]) == ("up to", "over")
        and prices[0][2] is not None
        and prices[0][2] == prices[1][2]
    ):
        return prices[0][0], prices[1][0], cast(int, prices[0][2]) + 1
    # A tier stated in the first column only is still a tier for the row: the other
    # columns of the same two rows carry no words of their own.
    if len(prices) == 2 and prices[0][1] is None and prices[1][1] is None:
        return prices[0][0], prices[1][0], None
    raise ScrapeError(f"{where}, {name}, {column}: prices {prices!r} are not one price, nor 'up to N' then 'over N'.")


def model_prices(
    table: list[list[Cell]], spec: JSONDict, where: str
) -> tuple[dict[str, JSONDict], list[str]]:
    """name -> its price fields, from a table whose columns the mapping names."""
    columns = cast("dict[str, str]", spec["columns"])
    rows, notes = priced_rows(table, list(columns), where)
    out: dict[str, JSONDict] = {}
    for name, by_column in rows.items():
        entry: JSONDict = {}
        threshold: int | None = None
        for column, field in columns.items():
            low, high, size = tiered(name, column, by_column[column], where)
            entry[field] = low
            if high is not None:
                entry[f"{field}_long_context"] = high
            if size is not None:
                threshold = size
        if any(k.endswith("_long_context") for k in entry):
            if threshold is None:
                raise ScrapeError(f"{where}, {name}: two rows of prices, and no size stated where they change.")
            entry["long_context_threshold"] = threshold
        out[name] = entry
    return out, notes


def _published(model_id: str, entry: JSONDict) -> JSONDict:
    return {
        field: (check_price(f"{PROVIDER_ID}/{model_id}", field, float(value)) if isinstance(value, Decimal) else value)
        for field, value in entry.items()
    }


# --------------------------------------------------------------------------------------
# Tools and products, stated in sentences
# --------------------------------------------------------------------------------------


def sentence_price(text: str, pattern: str, what: str, url: str) -> Decimal | None:
    """The one figure a sentence of the mapping's states, or None for a sentence that
    states no figure ("at no additional cost"). Fails if the sentence is gone, or says
    two different things."""
    found = {m.groupdict().get("amount") for m in re.finditer(pattern, text)}
    if not found:
        raise ScrapeError(
            f"{url}: the page no longer says how {what} is priced (expected a sentence "
            f"matching {pattern!r}). Read the page, then update the mapping's `products`."
        )
    if len(found) > 1:
        raise ScrapeError(f"{url}: the page prices {what} {len(found)} ways: {sorted(map(str, found))}.")
    amount = found.pop()
    return Decimal(amount) if amount else None


def product_entries(page: str, page_tables: list[list[list[Cell]]], mapping: JSONDict) -> dict[str, JSONDict]:
    url = cast(str, mapping["source"])
    text = page_text(page)
    out: dict[str, JSONDict] = {}
    for key, spec in cast("dict[str, JSONDict]", mapping["products"]).items():
        entry: JSONDict = {"kind": "product", "display_name": spec["display_name"]}
        if "sentence" in spec:
            amount = sentence_price(text, cast(str, spec["sentence"]), key, url)
            if spec.get("field") is None:
                # The sentence says it is free, and states no figure: a price for it
                # would come in another sentence, and this one would be gone.
                entry["free"] = True
            else:
                if amount is None:
                    raise ScrapeError(f"{url}: the sentence for {key} states no figure.")
                field = cast(str, spec["field"])
                entry[field] = check_price(f"{PROVIDER_ID}/{key}", field, float(amount * Decimal(str(spec.get("scale", 1)))))
        else:
            table = find_table(page_tables, cast("list[list[str]]", spec["headers"]), key, url)
            rows = [[c.text for c in row] for row in body_rows(table) if row[0].text == spec["row"]]
            if len(rows) != 1:
                raise ScrapeError(f"{url}: the {key} table has {len(rows)} rows named {spec['row']!r}, not one.")
            amount = sentence_price(rows[0][1], cast(str, spec["rate"]), key, url)
            if amount is None:
                raise ScrapeError(f"{url}: the {key} rate states no figure.")
            field = cast(str, spec["field"])
            entry[field] = check_price(f"{PROVIDER_ID}/{key}", field, float(amount))
        out[key] = entry
    return out


# --------------------------------------------------------------------------------------
# Putting it together
# --------------------------------------------------------------------------------------


def extract_models(pricing_page: str, status_page: str, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    url = cast(str, mapping["source"])
    specs = cast("dict[str, JSONDict]", mapping["tables"])
    page_tables = tables(pricing_page)
    states = status_table(status_page, mapping)

    main, notes = model_prices(
        find_table(page_tables, cast("list[list[str]]", specs["models"]["headers"]), "model table", url),
        specs["models"], "model table")
    batch, batch_notes = model_prices(
        find_table(page_tables, cast("list[list[str]]", specs["batch"]["headers"]), "batch table", url),
        specs["batch"], "batch table")
    fast, fast_notes = fast_prices(
        find_table(page_tables, cast("list[list[str]]", specs["fast"]["headers"]), "fast mode table", url),
        specs["fast"])
    notes += batch_notes + fast_notes

    models: dict[str, JSONDict] = {}
    for name, prices in main.items():
        extra = batch.pop(name, None)
        model_id, sold = resolve(name, states, mapping)
        if model_id is None:
            notes.append(f"{name}: priced, but the deprecations page's status table does not list it; not published")
            continue
        if sold != "sold":
            continue
        entry = dict(prices)
        if extra is not None:
            threshold = extra.pop("long_context_threshold", None)
            if threshold is not None and threshold != entry.get("long_context_threshold"):
                raise ScrapeError(f"{name}: the batch table changes price at {threshold} tokens, the model table at "
                                  f"{entry.get('long_context_threshold')}. One threshold per model.")
            entry.update(extra)
        entry["display_name"] = name
        entry.update(ids_entry(model_id))
        models[model_id] = _published(model_id, entry)
    for name in batch:
        notes.append(f"{name}: in the batch table, not in the model table; its batch prices are not published")

    for name, prices in fast.items():
        model_id, sold = resolve(name, states, mapping)
        if model_id is None or sold != "sold":
            if model_id is None:
                notes.append(f"{name}: has a fast mode price, but the status table does not list it; not published")
            continue
        key = f"{model_id} (fast)"
        entry = dict(prices)
        entry["display_name"] = f"{name} (fast mode)"
        entry["api_ids"] = ids_entry(model_id).get("api_ids", [model_id])
        models[key] = _published(key, entry)

    products = product_entries(pricing_page, page_tables, mapping)
    clash = sorted(set(products) & set(models))
    if clash:
        raise ScrapeError(f"{clash} named both as a model and as a product.")
    models.update(products)
    if not any("kind" not in e for e in models.values()):
        raise ScrapeError("not a single model could be published. Refusing to publish an empty block.")
    return models, notes


def fast_prices(table: list[list[Cell]], spec: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    """name -> input and output in fast mode. A row may name several models."""
    columns = cast("dict[str, str]", spec["columns"])
    out: dict[str, JSONDict] = {}
    notes: list[str] = []
    for row in body_rows(table):
        cells = [c.text for c in row]
        if len(cells) != len(columns) + 1:
            raise ScrapeError(f"fast mode table: a row of {len(cells)} cells: {cells!r}.")
        names = model_names(cells[0])
        if not names or " / ".join(names) != cells[0]:
            notes.append(f"fast mode table: a row named {cells[0]!r}, not model names this can read; not published")
            continue
        entry: JSONDict = {}
        for (column, field), cell in zip(columns.items(), cells[1:]):
            amount, side, _ = _price(cell, f"fast mode table, {cells[0]}, {column}")
            if side is not None:
                raise ScrapeError(f"fast mode table, {cells[0]}: a price by prompt length, which this does not read.")
            entry[field] = amount
        for name in names:
            if name in out:
                raise ScrapeError(f"fast mode table: {name} is priced twice.")
            out[name] = dict(entry)
    return out, notes


def extract_new_models(fetch: Fetcher, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    """The one callback provider_runner needs: the pricing page, then the status table."""
    return extract_models(fetch(cast(str, mapping["source"])), fetch(cast(str, mapping["status_source"])), mapping)


def main(argv: list[str] | None = None) -> int:
    return provider_runner.main(PROVIDER_ID, extract_new_models, DEFAULT_MAPPING, DEFAULT_CURRENT, CLI_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
