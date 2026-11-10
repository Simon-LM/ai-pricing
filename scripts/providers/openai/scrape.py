#!/usr/bin/env python3
"""Read OpenAI's public pricing page and work out what pricing.json's providers.openai
block should say.

Standard library only. Reads a public page and nothing else: it takes no API key, must
never be given one, and never touches anyone's OpenAI account.

This script DOES NOT WRITE pricing.json. It writes candidate files into an output
directory and reports which one, if any, the caller should promote, exactly like the
other providers' scrapers (see provider_runner).

THE SOURCE IS THE PAGE'S MARKDOWN. OpenAI serves every docs page as Markdown too, at the
same address plus `.md`: <https://developers.openai.com/api/docs/pricing.md>. It holds the
same tables as the rendered page, every tab of it at once, as plain Markdown tables, with
the model ids as the page states them. That is what this reads.

The page is a run of sections ("Flagship models", "Images", "Transcription"...), each
with one table per speed ("Standard", "Batch", "Flex", "Fast", "Ultrafast"). A table is
recognised by the section and speed it sits under and by its header row, word for word,
as `mapping.json` states them. What each kind of table becomes:

  Standard     the model's entry
  Batch        the same entry, in the `_batch` fields
  Flex, Fast,  an entry of its own, `<id> (flex)`, `<id> (fast)`, `<id> (ultrafast)`:
  Ultrafast    the same model, served at another speed for another price

A table this does not know is reported in the run's notes and not read. Prices in it
are not published: a unit is read from a header, and a header this has not seen is not
a unit it can trust.

Usage:
    scripts/providers/openai/scrape.py --out-dir .ci-out                    # live
    scripts/providers/openai/scrape.py --out-dir .ci-out --html pricing.md  # offline
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import provider_runner  # noqa: E402
from provider_runner import Fetcher, ScrapeError  # noqa: E402
from pricing_validate import JSONDict, check_price  # noqa: E402

PROVIDER_ID = "openai"
DEFAULT_MAPPING = Path(__file__).resolve().parent / "mapping.json"
DEFAULT_CURRENT = REPO_ROOT / "pricing.json"

# Kept as its own constant rather than read off __doc__: the module docstring is
# stripped to None under `python -OO`.
CLI_SUMMARY = "Read OpenAI's public pricing page and work out what providers.openai should say."

_ID = re.compile(r"[a-z0-9][a-z0-9.\-]*")
_DOLLARS = re.compile(r"\$(?P<amount>[0-9]+(?:\.[0-9]+)?)")


# --------------------------------------------------------------------------------------
# The page: sections, speeds, tables
# --------------------------------------------------------------------------------------


@dataclass
class Table:
    section: str
    speed: str
    header: list[str]
    rows: list[list[str]] = field(default_factory=list)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def page_tables(page: str, mapping: JSONDict) -> tuple[list[Table], list[str]]:
    """Every table on the page, with the section and the speed it sits under, and the
    page's lines that are not in a table."""
    sections = set(cast("list[str]", mapping["sections"]))
    speeds = cast("dict[str, str]", mapping["speeds"])
    section, speed = "", "Standard"
    found: list[Table] = []
    text: list[str] = []
    lines = page.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line in sections:
            section, speed = line, "Standard"
        elif line in speeds:
            speed = speeds[line]
        if line.startswith("|") and i + 1 < len(lines) and re.fullmatch(r"\|[\s\-:|]+\|", lines[i + 1].strip()):
            table = Table(section, speed, _cells(line))
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                table.rows.append(_cells(lines[i]))
                i += 1
            found.append(table)
            continue
        text.append(line)
        i += 1
    return found, text


def price(cell: str, where: str) -> Decimal | None:
    """A cell's dollars, or None for "-": not sold at that rate."""
    if cell in ("-", "—", ""):
        return None
    match = _DOLLARS.fullmatch(cell)
    if match is None:
        raise ScrapeError(f"{where}: {cell!r} is not a price this can read.")
    return Decimal(match["amount"])


def model_id(cell: str, where: str) -> str:
    if not _ID.fullmatch(cell):
        raise ScrapeError(f"{where}: {cell!r} is not a model id this can read.")
    return cell


def long_context_threshold(text: list[str], mapping: JSONDict) -> int | None:
    """N + 1 from "Short context: ≤NK input tokens. Long context: >NK input tokens.": a
    prompt of N thousand tokens is still billed at the short context rates."""
    pattern = re.compile(cast(str, mapping["long_context_note"]))
    sizes = {(m["short"], m["long"]) for line in text for m in pattern.finditer(line)}
    if not sizes:
        return None
    if len(sizes) > 1 or any(short != long for short, long in sizes):
        raise ScrapeError(f"the page states the long context threshold in two ways: {sorted(sizes)}.")
    return int(sizes.pop()[0]) * 1000 + 1


# --------------------------------------------------------------------------------------
# Reading each kind of table into fields
# --------------------------------------------------------------------------------------


Rows = dict[str, JSONDict]


def _add(rows: Rows, key: str, field_name: str, value: object, where: str) -> None:
    entry = rows.setdefault(key, {})
    if field_name in entry:
        raise ScrapeError(f"{where}: {key}.{field_name} is priced twice.")
    entry[field_name] = value


def read_context(table: Table, spec: JSONDict, threshold: int | None) -> Rows:
    """Model | five short context prices | five long context prices."""
    where = f"{table.section} / {table.speed}"
    fields = cast("list[str]", spec["fields"])
    annotation = re.compile(r"\s*\(<(?P<size>[0-9]+)K context length\)$")
    rows: Rows = {}
    for cells in table.rows:
        name = cells[0]
        stated = annotation.search(name)
        if stated:
            if threshold is None or int(stated["size"]) * 1000 + 1 != threshold:
                raise ScrapeError(f"{where}: {name!r} names a context length the page's note does not state.")
            name = name[: stated.start()]
        key = model_id(name, where)
        rows.setdefault(key, {})
        for column, cell in enumerate(cells[1:]):
            value = price(cell, f"{where}, {key}")
            if value is None:
                continue
            field_name = fields[column % len(fields)] + ("_long_context" if column >= len(fields) else "")
            _add(rows, key, field_name, value, where)
        if any(k.endswith("_long_context") for k in rows[key]):
            if threshold is None:
                raise ScrapeError(f"{where}: long context prices, and no note saying where long context starts.")
            rows[key]["long_context_threshold"] = threshold
    return rows


def read_flat(table: Table, spec: JSONDict) -> Rows:
    """Model | one price per field."""
    where = f"{table.section} / {table.speed}"
    fields = cast("list[str]", spec["fields"])
    rows: Rows = {}
    for cells in table.rows:
        key = model_id(cells[0], where)
        rows.setdefault(key, {})
        for field_name, cell in zip(fields, cells[1:]):
            value = price(cell, f"{where}, {key}")
            if value is not None:
                _add(rows, key, field_name, value, where)
    return rows


def read_modality(table: Table, spec: JSONDict) -> Rows:
    """Model | Modality | Input | Output | Cached input: one row per kind of token."""
    where = f"{table.section} / {table.speed}"
    prefixes = cast("dict[str, str]", spec["modalities"])
    fields = cast("list[str]", spec["fields"])
    per_chars = re.compile(r"\$(?P<amount>[0-9]+(?:\.[0-9]+)?) / 1M characters")
    rows: Rows = {}
    for cells in table.rows:
        key = model_id(cells[0], where)
        modality = cells[1]
        if modality not in prefixes:
            raise ScrapeError(f"{where}, {key}: a modality {modality!r} the mapping does not name.")
        rows.setdefault(key, {})
        for field_name, cell in zip(fields, cells[2:]):
            chars = per_chars.fullmatch(cell)
            if chars:
                if field_name != "in_per_mtok" or modality != "Text":
                    raise ScrapeError(f"{where}, {key}: a price per character where none is expected.")
                _add(rows, key, "per_mchars", Decimal(chars["amount"]), where)
                continue
            value = price(cell, f"{where}, {key}")
            if value is not None:
                _add(rows, key, prefixes[modality] + field_name, value, where)
    return rows


def read_per_minute(table: Table, spec: JSONDict) -> Rows:
    """Model | Price per minute."""
    where = f"{table.section} / {table.speed}"
    rows: Rows = {}
    for cells in table.rows:
        key = model_id(cells[0], where)
        value = price(cells[1], f"{where}, {key}")
        if value is not None:
            _add(rows, key, cast(str, spec["field"]), value, where)
    return rows


def read_transcription(table: Table, spec: JSONDict) -> Rows:
    """Model | Use case | Input | Output | Estimated cost.

    A model priced in tokens publishes its token prices: its cost per minute is, in the
    page's own words, an estimate. A model priced only per minute publishes that.
    """
    where = f"{table.section} / {table.speed}"
    per_minute = re.compile(r"\$(?P<amount>[0-9]+(?:\.[0-9]+)?) / minute")
    rows: Rows = {}
    for cells in table.rows:
        key = model_id(cells[0].lower(), where)
        entry = rows.setdefault(key, {"display_name": cells[0]})
        tokens = [price(cells[2], f"{where}, {key}"), price(cells[3], f"{where}, {key}")]
        for field_name, value in zip(("in_per_mtok", "out_per_mtok"), tokens):
            if value is not None:
                entry[field_name] = value
        if all(t is None for t in tokens):
            minute = per_minute.fullmatch(cells[4])
            if minute is None:
                raise ScrapeError(f"{where}, {key}: {cells[4]!r} is not a price per minute this can read.")
            entry[cast(str, spec["field"])] = Decimal(minute["amount"])
    return rows


def read_specialized(table: Table, spec: JSONDict, mapping: JSONDict) -> Rows:
    """Category | Model | prices. A row whose category is marked restricted is not read;
    "(short context)" and "(long context)" rows of one model become one entry."""
    where = f"{table.section} / {table.speed}"
    fields = cast("list[str]", spec["fields"])
    marker = cast(str, mapping["restricted_marker"])
    context = re.compile(r"\s*\((?P<which>short|long) context\)$")
    rows: Rows = {}
    for cells in table.rows:
        if marker in cells[0]:
            continue
        name = cells[1]
        which = context.search(name)
        suffix = ""
        if which:
            name = name[: which.start()]
            suffix = "_long_context" if which["which"] == "long" else ""
        key = model_id(name, where)
        rows.setdefault(key, {})
        stated = cells[2:]
        if stated[0] == "Free" and all(c in ("-", "") for c in stated[1:]):
            rows[key]["free"] = True
            continue
        for field_name, cell in zip(fields, stated):
            value = price(cell, f"{where}, {key}")
            if value is not None:
                _add(rows, key, field_name + suffix, value, where)
        if suffix:
            raise ScrapeError(f"{where}, {key}: a long context row outside the restricted models, "
                              f"with no threshold stated for it.")
    return rows


def read_tools(table: Table, mapping: JSONDict) -> tuple[Rows, list[str]]:
    """Tool | Details | Pricing: each row the mapping names, by its pattern."""
    where = f"{table.section} / {table.speed}"
    specs = cast("list[JSONDict]", mapping["tools"])
    rows: Rows = {}
    notes: list[str] = []
    seen: set[tuple[str, str]] = set()
    for cells in table.rows:
        tool, details, pricing = cells[0], cells[1], cells[2]
        seen.add((tool, details))
        spec = next((s for s in specs if (s["tool"], s["details"]) == (tool, details)), None)
        if spec is None:
            notes.append(f"{where}: a tool row {tool!r} / {details!r} the mapping does not name; not published")
            continue
        matches = list(re.finditer(cast(str, spec["pattern"]), pricing))
        if not matches or ("unit" in spec and cast(str, spec["unit"]) not in pricing):
            raise ScrapeError(f"{where}: {tool} / {details} is priced {pricing!r}, not as the mapping expects.")
        field_name = cast(str, spec["field"])
        for match in matches:
            key = cast(str, spec["key"])
            if "variant" in spec:
                key = f"{key} ({match[cast(str, spec['variant'])]} GB)"
            entry = rows.setdefault(key, {"kind": "product", "display_name": f"{tool}: {details}"})
            if field_name in entry:
                raise ScrapeError(f"{where}: {key} is priced twice.")
            entry[field_name] = Decimal(match["amount"])
            if "variant" in spec:
                entry["display_name"] = f"{tool}: {details}, {match[cast(str, spec['variant'])]} GB"
    for spec in specs:
        if (spec["tool"], spec["details"]) not in seen:
            raise ScrapeError(f"{where}: no row {spec['tool']!r} / {spec['details']!r}. The tools table has changed.")
    return rows, notes


# --------------------------------------------------------------------------------------
# Putting it together
# --------------------------------------------------------------------------------------


def _batch_name(field_name: str) -> str:
    """in_per_mtok -> in_per_mtok_batch; in_per_mtok_long_context ->
    in_per_mtok_batch_long_context."""
    if field_name.endswith("_long_context"):
        return field_name[: -len("_long_context")] + "_batch_long_context"
    return field_name + "_batch"


def extract_models(page: str, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    found, text = page_tables(page, mapping)
    threshold = long_context_threshold(text, mapping)
    kinds = cast("list[JSONDict]", mapping["tables"])
    skipped = set(cast("list[str]", mapping["sections_not_read"]))
    by_speed: dict[str, Rows] = {}
    products: Rows = {}
    notes: list[str] = []
    read_kinds: set[str] = set()

    for table in found:
        if table.section in skipped:
            continue
        spec = next((k for k in kinds if table.section in k["sections"] and table.header == k["header"]
                     and table.speed in k["speeds"]), None)
        if spec is None:
            notes.append(f"a table under {table.section!r} / {table.speed!r}, headed {table.header}, "
                         f"that the mapping does not name; not read")
            continue
        read_kinds.add(cast(str, spec["name"]))
        kind = spec["kind"]
        if kind == "context":
            rows = read_context(table, spec, threshold)
        elif kind == "flat":
            rows = read_flat(table, spec)
        elif kind == "modality":
            rows = read_modality(table, spec)
        elif kind == "per_minute":
            rows = read_per_minute(table, spec)
        elif kind == "transcription":
            rows = read_transcription(table, spec)
        elif kind == "specialized":
            rows = read_specialized(table, spec, mapping)
        elif kind == "tools":
            rows, tool_notes = read_tools(table, mapping)
            notes += tool_notes
            for key, entry in rows.items():
                if key in products:
                    raise ScrapeError(f"{key}: named twice in the tools table.")
                products[key] = entry
            continue
        else:
            raise ScrapeError(f"the mapping names a kind of table, {kind!r}, this does not read.")
        target = by_speed.setdefault(table.speed, {})
        for key, entry in rows.items():
            merged = target.setdefault(key, {})
            for field_name, value in entry.items():
                if field_name in merged and merged[field_name] != value:
                    raise ScrapeError(f"{table.section} / {table.speed}: {key}.{field_name} is priced twice.")
                merged[field_name] = value

    required = set(cast("list[str]", mapping["required_tables"]))
    missing = sorted(required - read_kinds)
    if missing:
        raise ScrapeError(f"the page has no {missing} table. Its layout has changed: check it against the mapping.")

    models: dict[str, JSONDict] = {}
    for key, entry in by_speed.get("Standard", {}).items():
        models[key] = dict(entry)
    for key, entry in by_speed.get("Batch", {}).items():
        if key not in models:
            notes.append(f"{key}: batch prices, and no standard price; its batch prices are not published")
            continue
        for field_name, value in entry.items():
            if field_name == "long_context_threshold":
                continue
            if field_name in ("free", "display_name"):
                continue
            models[key][_batch_name(field_name)] = value
    for speed in ("Flex", "Fast", "Ultrafast"):
        for key, entry in by_speed.get(speed, {}).items():
            variant = f"{key} ({speed.lower()})"
            models[variant] = dict(entry)
            models[variant]["display_name"] = f"{key} ({speed})"
            models[variant]["api_ids"] = [key]

    out: dict[str, JSONDict] = {}
    for key, entry in models.items():
        published: JSONDict = {}
        for field_name, value in entry.items():
            if isinstance(value, Decimal):
                published[field_name] = check_price(f"{PROVIDER_ID}/{key}", field_name, float(value))
            else:
                published[field_name] = value
        published.setdefault("display_name", key)
        out[key] = published
    clash = sorted(set(out) & set(products))
    if clash:
        raise ScrapeError(f"{clash} named both as a model and as a tool.")
    for key, entry in products.items():
        out[key] = {k: (check_price(f"{PROVIDER_ID}/{key}", k, float(v)) if isinstance(v, Decimal) else v)
                    for k, v in entry.items()}
    return out, notes


def extract_new_models(fetch: Fetcher, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    """The one callback provider_runner needs. One source, read once."""
    return extract_models(fetch(cast(str, mapping["source"])), mapping)


def main(argv: list[str] | None = None) -> int:
    return provider_runner.main(PROVIDER_ID, extract_new_models, DEFAULT_MAPPING, DEFAULT_CURRENT, CLI_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
