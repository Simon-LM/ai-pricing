#!/usr/bin/env python3
"""Read Google's public Gemini API pricing page and work out what pricing.json's
providers.google block should say.

Standard library only. Reads a public page and nothing else: it takes no API key, must
never be given one, and never touches anyone's Google account.

This script DOES NOT WRITE pricing.json. It writes candidate files into an output
directory and reports which one, if any, the caller should promote, exactly like the
other providers' scrapers (see provider_runner).

THE SOURCE IS THE PAGE'S MARKDOWN, <https://aistudio.google.com/docs/pricing.md>, which
Google serves beside the rendered page: the rendered page builds its tables in the
browser, the Markdown holds them all. It is Markdoc: one `##` section per model or family,
a line naming the model ids, then one table per tab ("Standard", "Batch", "Flex",
"Priority", "Contributor"), each row a feature ("Input price", "Output price"...) with a
free tier cell and a paid tier cell.

THE PAID TIER CELL IS PROSE, read clause by clause. A clause is a dollar figure and what
follows it, up to the next one:

  $0.30 (text / image / video)            the kinds of token it prices
  $1.25, prompts <= 200k tokens           a price by prompt length, 200001 the threshold
  $2.00 through November 30, 2026.        a price with dates: the one valid on the day of
  $4.00 starting December 1, 2026.        the run is published
  $1.00 / 1,000,000 tokens per hour       the price of keeping a context cache
    (storage price)
  $0.039 per image, $0.08 per song        a price per item

Estimates are taken out first and never published: "or $0.005/min", a parenthesis
"($0.00012 per image)", and everything after "equivalent to". A dollar figure left that
fits no clause fails the run: a price this cannot place is never guessed at.

Usage:
    scripts/providers/google/scrape.py --out-dir .ci-out                    # live
    scripts/providers/google/scrape.py --out-dir .ci-out --html pricing.md  # offline
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import provider_runner  # noqa: E402
from provider_runner import Fetcher, ScrapeError  # noqa: E402
from pricing_validate import JSONDict, check_price  # noqa: E402

PROVIDER_ID = "google"
DEFAULT_MAPPING = Path(__file__).resolve().parent / "mapping.json"
DEFAULT_CURRENT = REPO_ROOT / "pricing.json"

# Kept as its own constant rather than read off __doc__: the module docstring is
# stripped to None under `python -OO`.
CLI_SUMMARY = "Read Google's public Gemini API pricing page and work out what providers.google should say."

_MODEL_ID = re.compile(r"\[`(?P<id>[a-z0-9][a-z0-9.\-]*)`\]\(/docs/models/")
_SECTION_ID = re.compile(r'\{%\s*id="(?P<id>[a-z0-9.\-]+)"\s*%\}')
_TAB = re.compile(r'\{%\s*tab title="(?P<title>[^"]+)"\s*%\}(?P<body>.*?)\{%\s*/tab\s*%\}', re.S)
_TABLE = re.compile(r"\{%\s*table\s*%\}(?P<body>.*?)\{%\s*/table\s*%\}", re.S)
_ROW_BREAK = re.compile(r"\n\s*---\s*\n")
_ITEM = re.compile(r"(?m)^\s*\* ")
_DATE = r"[A-Z][a-z]+ [0-9]{1,2}, [0-9]{4}"


# --------------------------------------------------------------------------------------
# The page: sections, tabs, tables, rows
# --------------------------------------------------------------------------------------


def clean(text: str) -> str:
    """A cell's words: Markdoc tags, links and line-break marks taken out."""
    text = re.sub(r"\{%\s*sup\s*/%\}", "", text)
    text = re.sub(r"\{%\s*sup\s*%\}.*?\{%\s*/sup\s*%\}", "", text, flags=re.S)
    text = re.sub(r"\{%[^%]*%\}", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.replace("\\\\", " ").replace("\\", " ")
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Section:
    title: str
    anchor: str
    ids: list[str]
    tabs: dict[str, list[list[str]]]


def sections(page: str) -> list[Section]:
    out: list[Section] = []
    for chunk in re.split(r"(?m)^## ", page)[1:]:
        title_line, _, body = chunk.partition("\n")
        anchor = _SECTION_ID.search(title_line)
        title = clean(_SECTION_ID.sub("", title_line))
        # The ids are the italic line under the title, `*[`id`](...), [`id`](...)*`: only
        # that line, since a warning further down may name another model.
        start = body.find("*[`")
        id_line = body[start:body.find("*", start + 1)] if start >= 0 else ""
        ids = list(dict.fromkeys(m["id"] for m in _MODEL_ID.finditer(id_line)))
        tabs: dict[str, list[list[str]]] = {}
        found_tabs = list(_TAB.finditer(body))
        for title_tab, tab_body in ([(t["title"], t["body"]) for t in found_tabs] or [("Standard", body)]):
            table = _TABLE.search(tab_body)
            if table is None:
                continue
            rows = []
            for row in _ROW_BREAK.split(table["body"]):
                cells = [clean(c) for c in _ITEM.split("\n" + row)[1:]]
                if cells:
                    rows.append(cells)
            tabs[title_tab] = rows
        out.append(Section(title, anchor["id"] if anchor else "", ids, tabs))
    return out


# --------------------------------------------------------------------------------------
# A paid tier cell, clause by clause
# --------------------------------------------------------------------------------------


@dataclass
class Clause:
    amount: Decimal
    unit: str            # "mtok", "storage", "image", "song"
    kinds: list[str]     # token kinds: "text", "image", "audio", "video"; [] if none stated
    tier: str            # "", "low", "high"
    size: int | None     # the prompt size a tier names, in thousands
    through: dt.date | None
    starting: dt.date | None


def _date(text: str) -> dt.date:
    return dt.datetime.strptime(text, "%B %d, %Y").date()


def clauses(cell: str, mapping: JSONDict, where: str) -> list[Clause]:
    """Every price a paid tier cell states, estimates left out."""
    kinds_known = cast("dict[str, list[str]]", mapping["kinds"])
    prose = set(cast("list[str]", mapping["prose_without_price"]))
    text = re.sub(r"\s+or\s+\$[0-9.]+\s*/\s*min", "", cell)
    text = re.sub(r"\(\$[0-9.]+ per [a-z]+\)", "", text)
    text = re.split(r"(?i)equivalent to", text, maxsplit=1)[0]
    found: list[Clause] = []
    for match in re.finditer(r"\$(?P<amount>[0-9]+(?:\.[0-9]+)?)(?P<rest>[^$]*)", text):
        rest = match["rest"]
        unit = "mtok"
        storage = re.match(r"\s*/ 1,000,000 tokens per hour \(storage price\)", rest)
        per_item = re.match(r"\s*per (?P<item>image|song)\b", rest)
        if storage:
            unit, rest = "storage", rest[storage.end():]
        elif per_item:
            unit, rest = per_item["item"], rest[per_item.end():]
        kinds: list[str] = []
        stated = re.match(r"\s*\((?P<kinds>[^)]*)\)", rest)
        if stated and unit == "mtok":
            words = re.split(r"\s*/\s*|\s+and\s+", stated["kinds"].strip())
            for word in words:
                if word not in kinds_known:
                    raise ScrapeError(f"{where}: ${match['amount']} is for {stated['kinds']!r}, "
                                      f"a kind of token the mapping does not name.")
                kinds += kinds_known[word]
            rest = rest[stated.end():]
        tier, size = "", None
        prompts = re.match(r"\s*,\s*prompts\s*(?P<op><=|>)\s*(?P<size>[0-9]+)k(?: tokens)?", rest)
        if prompts:
            tier, size = ("low" if prompts["op"] == "<=" else "high"), int(prompts["size"])
        through = re.search(r"through (" + _DATE + r")\.?", rest)
        starting = re.search(r"starting (" + _DATE + r")\.?", rest)
        leftover = rest
        for part in (prompts, through, starting):
            if part:
                leftover = leftover.replace(part.group(0), " ")
        # Whatever is left must be words the mapping knows to carry no price: a figure
        # followed by anything else ("per sticker", "per minute") is not placed by guess.
        if re.sub(r"\s+", " ", leftover).strip() not in prose:
            raise ScrapeError(f"{where}: after ${match['amount']}, {rest.strip()!r} is not a clause this can read.")
        found.append(Clause(Decimal(match["amount"]), unit, sorted(set(kinds)), tier, size,
                            _date(through.group(1)) if through else None,
                            _date(starting.group(1)) if starting else None))
    return found


def valid_on(found: list[Clause], day: dt.date) -> list[Clause]:
    """The clauses that hold on `day`: undated ones, and dated ones whose dates include it."""
    return [c for c in found
            if (c.through is None or day <= c.through) and (c.starting is None or day >= c.starting)]


# --------------------------------------------------------------------------------------
# Rows into fields
# --------------------------------------------------------------------------------------


FIELD_BY_SIDE = {"in": "in_per_mtok", "out": "out_per_mtok", "cache": "cache_read_per_mtok"}
UNIT_FIELDS = {"storage": "cache_storage_per_mtok_hour", "image": "per_image", "song": "per_song"}


def row_fields(side: str, forced: list[str] | None, cell: str, mapping: JSONDict, day: dt.date,
               where: str) -> tuple[dict[str, Decimal], int | None]:
    """A row's prices as fields, and the threshold its prompt-length tiers state."""
    prefixes = cast("dict[str, str | None]", mapping["kind_prefixes"])
    fields: dict[str, Decimal] = {}
    sizes: set[int] = set()
    found = clauses(cell, mapping, where)
    valid = valid_on(found, day)
    if found and not valid:
        raise ScrapeError(f"{where}: no price in {cell!r} holds on {day}. Its dates leave a gap.")
    for clause in valid:
        if clause.unit != "mtok":
            names = [UNIT_FIELDS[clause.unit]]
        else:
            kinds = forced if forced is not None else (clause.kinds or ["text"])
            names = []
            for kind in kinds:
                prefix = prefixes[kind]
                if prefix is None:
                    continue  # video: not published
                names.append(prefix + FIELD_BY_SIDE[side])
        for name in names:
            if clause.tier == "high":
                name += "_long_context"
            if clause.size is not None:
                sizes.add(clause.size)
            if name in fields and fields[name] != clause.amount:
                raise ScrapeError(f"{where}: two prices for {name} on {day}: {fields[name]} and {clause.amount}.")
            fields[name] = clause.amount
    if len(sizes) > 1:
        raise ScrapeError(f"{where}: prompt-length tiers at {sorted(sizes)}k; one threshold per model.")
    return fields, (sizes.pop() * 1000 + 1 if sizes else None)


def tab_entry(section: Section, rows: list[list[str]], mapping: JSONDict, day: dt.date,
              where: str) -> tuple[list[tuple[str, JSONDict]], list[str]]:
    """One tab's table: the entry for every model of the section, or, for a section whose rows
    each name a model (Lyria 3), one entry per row."""
    features = cast("dict[str, JSONDict]", mapping["features"])
    ignored = set(cast("list[str]", mapping["features_ignored"]))
    header = rows[0] if rows else []
    if header[:2] != ["Feature", "Free Tier"] or len(header) != 3 or not header[2].startswith("Paid Tier"):
        raise ScrapeError(f"{where}: a table headed {header}, not Feature | Free Tier | Paid Tier.")
    entry: JSONDict = {}
    per_row: list[JSONDict] = []
    notes: list[str] = []
    threshold: int | None = None
    free_inputs = False
    for cells in rows[1:]:
        if len(cells) != 3:
            raise ScrapeError(f"{where}: a row of {len(cells)} cells: {cells!r}.")
        feature, free, paid = cells
        if feature in ignored:
            continue
        spec = features.get(feature)
        if spec is None:
            if re.search(r"\$[0-9]", paid) and re.search(r"per (song|image)", paid):
                fields, _ = row_fields("out", None, paid, mapping, day, where)
                per_row.append(dict(fields))
                continue
            notes.append(f"{where}: a row {feature!r} the mapping does not name; not read")
            continue
        if paid in ("Not available", "Not Supported", ""):
            if spec["side"] == "in" and free == "Free of charge":
                free_inputs = True
            continue
        forced = cast("list[str] | None", spec.get("kinds"))
        fields, size = row_fields(cast(str, spec["side"]), forced, paid, mapping, day, f"{where}, {feature}")
        for name, value in fields.items():
            if name in entry and entry[name] != value:
                raise ScrapeError(f"{where}: {name} is priced twice.")
            entry[name] = value
        if size is not None:
            if threshold is not None and threshold != size:
                raise ScrapeError(f"{where}: prompt-length tiers at two sizes.")
            threshold = size
    if threshold is not None:
        entry["long_context_threshold"] = threshold
    if not entry and not per_row and free_inputs:
        entry = {"free": True}
    if per_row:
        if entry:
            raise ScrapeError(f"{where}: rows priced per item and rows priced per token in one table.")
        if len(per_row) != len(section.ids):
            raise ScrapeError(f"{where}: {len(per_row)} priced rows for {len(section.ids)} model ids.")
        return list(zip(section.ids, per_row)), notes
    ids = section.ids or ([section.anchor] if section.anchor else [])
    return [(model_id, dict(entry)) for model_id in ids], notes


# --------------------------------------------------------------------------------------
# The tools table
# --------------------------------------------------------------------------------------


def tool_entries(section: Section, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    specs = cast("list[JSONDict]", mapping["tools"])
    ignored = set(cast("list[str]", mapping["tools_ignored"]))
    rows = section.tabs.get("Standard") or []
    out: dict[str, JSONDict] = {}
    notes: list[str] = []
    named = {s["row"] for s in specs}
    for cells in rows[1:]:
        feature = cells[0]
        if feature in ignored:
            continue
        if feature not in named:
            notes.append(f"tools: a row {feature!r} the mapping does not name; not read")
    for spec in specs:
        cells = next((c for c in rows[1:] if c[0] == spec["row"]), None)
        if cells is None:
            raise ScrapeError(f"tools: no row {spec['row']!r}. The tools table has changed.")
        match = re.search(cast(str, spec["pattern"]), cells[2])
        if match is None:
            raise ScrapeError(f"tools: {spec['row']} is priced {cells[2]!r}, not as the mapping expects.")
        key = cast(str, spec["key"])
        out[key] = {"kind": "product", "display_name": cast(str, spec["display_name"]),
                    "per_1k_calls": check_price(f"{PROVIDER_ID}/{key}", "per_1k_calls", float(match["amount"]))}
    return out, notes


# --------------------------------------------------------------------------------------
# Putting it together
# --------------------------------------------------------------------------------------


def _batch_name(field_name: str) -> str:
    if field_name.endswith("_long_context"):
        return field_name[: -len("_long_context")] + "_batch_long_context"
    return field_name + "_batch"


def extract_models(page: str, mapping: JSONDict, day: dt.date) -> tuple[dict[str, JSONDict], list[str]]:
    speeds = cast("dict[str, str | None]", mapping["speeds"])
    skipped = set(cast("list[str]", mapping["sections_not_read"]))
    by_speed: dict[str, dict[str, JSONDict]] = {}
    products: dict[str, JSONDict] = {}
    notes: list[str] = []
    for section in sections(page):
        if section.anchor in skipped:
            continue
        if section.anchor == mapping["tools_section"]:
            products, tool_notes = tool_entries(section, mapping)
            notes += tool_notes
            continue
        if not section.tabs:
            notes.append(f"a section {section.title!r} with no price table; not read")
            continue
        for tab, rows in section.tabs.items():
            if tab not in speeds:
                notes.append(f"{section.title}: a tab {tab!r} the mapping does not name; not read")
                continue
            speed = speeds[tab]
            if speed is None:
                continue  # Contributor: invitation only
            entries, tab_notes = tab_entry(section, rows, mapping, day, f"{section.title} / {tab}")
            notes += tab_notes
            target = by_speed.setdefault(speed, {})
            for model_id, entry in entries:
                if model_id in target:
                    raise ScrapeError(f"{model_id}: priced in two sections.")
                if entry:
                    target[model_id] = entry
    models: dict[str, JSONDict] = {k: dict(v) for k, v in by_speed.get("standard", {}).items()}
    for model_id, entry in by_speed.get("batch", {}).items():
        if model_id not in models:
            notes.append(f"{model_id}: batch prices, and no standard price; its batch prices are not published")
            continue
        for name, value in entry.items():
            if name in ("long_context_threshold", "free"):
                continue
            models[model_id][_batch_name(name)] = value
    for speed in ("flex", "priority"):
        for model_id, entry in by_speed.get(speed, {}).items():
            key = f"{model_id} ({speed})"
            models[key] = dict(entry)
            models[key]["api_ids"] = [model_id]
    out: dict[str, JSONDict] = {}
    for key, entry in models.items():
        out[key] = {k: (check_price(f"{PROVIDER_ID}/{key}", k, float(v)) if isinstance(v, Decimal) else v)
                    for k, v in entry.items()}
        out[key].setdefault("display_name", key)
    clash = sorted(set(out) & set(products))
    if clash:
        raise ScrapeError(f"{clash} named both as a model and as a tool.")
    out.update(products)
    if len(out) < int(cast(int, mapping["min_entries"])):
        raise ScrapeError(f"only {len(out)} entries read. The page changed shape; refusing to publish so few.")
    return out, notes


def extract_new_models(fetch: Fetcher, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    """The one callback provider_runner needs. One source, read once, for the run's date."""
    return extract_models(fetch(cast(str, mapping["source"])), mapping, provider_runner.run_date())


def main(argv: list[str] | None = None) -> int:
    return provider_runner.main(PROVIDER_ID, extract_new_models, DEFAULT_MAPPING, DEFAULT_CURRENT, CLI_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
