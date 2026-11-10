#!/usr/bin/env python3
"""Read xAI's public pricing page and work out what pricing.json's providers.xai
block should say.

Standard library only. Reads a public page and nothing else: it takes no API key,
must never be given one, and never touches anyone's xAI account.

This script DOES NOT WRITE pricing.json. It writes candidate files into an output
directory and reports which one, if any, the caller should promote, exactly like the
other providers' scrapers (see provider_runner).

The page, <https://docs.x.ai/developers/pricing>, is a Next.js document. Besides the
tables a human reads, it carries xAI's whole public catalogue as data, in a script
that sets `globalThis.__XAI_PUBLIC_MODELS__`: per cluster, every language model with
its prices, its aliases and its long-context threshold, and every audio model with
its own prices. That data is what this reads, through nextjs_flight, rather than the
rendered tables -- with one exception: the server-side tools (web search, X search,
code execution...) are priced only in a table, so that table is read too.

Units, as the source states them:
  token prices  US cents per 100 million tokens (xAI's API reference says so for the
                same fields): 20000 is $2.00 per million
  audio prices  units of 1e-10 dollars: perAudioSecond 277778 is $0.10 per hour,
                perCharacter 150000 is $15.00 per million characters
Both were checked against the page's own human-readable table on 2026-11-09.

Usage:
    scripts/providers/xai/scrape.py --out-dir .ci-out                    # live
    scripts/providers/xai/scrape.py --out-dir .ci-out --html pricing.html  # offline
"""

from __future__ import annotations

import html as html_lib
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import nextjs_flight  # noqa: E402
import provider_runner  # noqa: E402
from provider_runner import Fetcher, ScrapeError  # noqa: E402
from pricing_validate import JSONDict, check_price  # noqa: E402

PROVIDER_ID = "xai"
DEFAULT_MAPPING = Path(__file__).resolve().parent / "mapping.json"
DEFAULT_CURRENT = REPO_ROOT / "pricing.json"

# Kept as its own constant rather than read off __doc__: the module docstring is
# stripped to None under `python -OO`.
CLI_SUMMARY = "Read xAI's public pricing page and work out what providers.xai should say."

CATALOGUE_MARKER = "__XAI_PUBLIC_MODELS__="

# cents per 100M tokens -> dollars per million tokens
TOKEN_DIVISOR = Decimal(10_000)
# units of 1e-10 dollars -> dollars
AUDIO_UNIT = Decimal(10) ** -10

TOKEN_FIELDS = {
    "promptTextTokenPrice": "in_per_mtok",
    "completionTextTokenPrice": "out_per_mtok",
    "cachedPromptTokenPrice": "cache_read_per_mtok",
    "promptTextTokenPriceLongContext": "in_per_mtok_long_context",
    "completionTokenPriceLongContext": "out_per_mtok_long_context",
    "cachedPromptTokenPriceLongContext": "cache_read_per_mtok_long_context",
}
REQUIRED_TOKEN_FIELDS = ("promptTextTokenPrice", "completionTextTokenPrice")


def _count(raw: Any, where: str) -> Decimal:
    """A price as the source writes it: a string of digits, or a whole number."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int)) or not str(raw).isdigit():
        raise ScrapeError(f"{where}: {raw!r} is not a whole number. The data shape has changed.")
    return Decimal(str(raw))


def _price(value: Decimal, model_id: str, field: str) -> float:
    return check_price(f"{PROVIDER_ID}/{model_id}", field, float(value))


def public_catalogue(page: str) -> JSONDict:
    """The object the page assigns to globalThis.__XAI_PUBLIC_MODELS__."""
    text = "".join(nextjs_flight.decoded_chunks(page))
    at = text.find(CATALOGUE_MARKER)
    if at < 0:
        raise ScrapeError(
            f"the page carries no {CATALOGUE_MARKER!r}. xAI moved its catalogue, or the "
            f"fetch returned something other than the pricing page."
        )
    try:
        catalogue, _ = json.JSONDecoder().raw_decode(text[at + len(CATALOGUE_MARKER):])
    except json.JSONDecodeError as exc:
        raise ScrapeError(f"the catalogue after {CATALOGUE_MARKER!r} is not valid JSON: {exc}") from exc
    if not isinstance(catalogue, dict) or not isinstance(catalogue.get("clusterConfigs"), list):
        raise ScrapeError("the catalogue has no clusterConfigs list. The data shape has changed.")
    return cast(JSONDict, catalogue)


def cluster_config(catalogue: JSONDict, name: str) -> JSONDict:
    for config in catalogue["clusterConfigs"]:
        if isinstance(config, dict) and config.get("clusterName") == name:
            return cast(JSONDict, config)
    seen = sorted(str(c.get("clusterName")) for c in catalogue["clusterConfigs"] if isinstance(c, dict))
    raise ScrapeError(f"the catalogue has no cluster {name!r} (it has {seen}). Update the mapping's `cluster`.")


def language_entries(config: JSONDict) -> dict[str, JSONDict]:
    models: dict[str, JSONDict] = {}
    for model in config.get("languageModels") or []:
        model_id = model.get("name") if isinstance(model, dict) else None
        if not isinstance(model_id, str) or not model_id:
            raise ScrapeError("a language model has no string `name`. The data shape has changed.")
        missing = [f for f in REQUIRED_TOKEN_FIELDS if f not in model]
        if missing:
            raise ScrapeError(f"{model_id}: no {missing} in the catalogue. A model published here states both.")
        entry: JSONDict = {}
        for source_field, target in TOKEN_FIELDS.items():
            if source_field in model:
                count = _count(model[source_field], f"{model_id}.{source_field}")
                entry[target] = _price(count / TOKEN_DIVISOR, model_id, target)
        if any(k.endswith("_long_context") for k in entry):
            threshold = model.get("longContextThreshold")
            entry["long_context_threshold"] = int(_count(threshold, f"{model_id}.longContextThreshold"))
        entry["display_name"] = model_id
        aliases = model.get("aliases") or []
        if aliases:
            # Most specific first, as api_ids promises: the dated name, then its aliases.
            entry["api_ids"] = [model_id] + [a for a in aliases if isinstance(a, str) and a and a != model_id]
        models[model_id] = entry
    if not models:
        raise ScrapeError("the cluster lists no language model. Refusing to publish an empty block.")
    return models


def audio_entries(config: JSONDict, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    wanted: JSONDict = mapping.get("audio") or {}
    skipped = set(mapping.get("audio_skipped") or [])
    models: dict[str, JSONDict] = {}
    notes: list[str] = []
    for model in config.get("audioModels") or []:
        model_id = model.get("name") if isinstance(model, dict) else None
        if not isinstance(model_id, str) or not model_id:
            raise ScrapeError("an audio model has no string `name`. The data shape has changed.")
        if model_id in skipped:
            continue
        fields = wanted.get(model_id)
        if fields is None:
            notes.append(f"{model_id}: an audio model the mapping does not name; not published")
            continue
        pricing: JSONDict = {}
        for endpoint in model.get("endpoints") or []:
            pricing.update(endpoint.get("pricing") or {})
        for source_field, target in fields.items():
            if source_field not in pricing:
                raise ScrapeError(f"{model_id}: no {source_field!r} in its pricing. The data shape has changed.")
            count = _count(pricing[source_field], f"{model_id}.{source_field}")
            field, _, variant = target.partition("@")
            key = f"{model_id} ({variant})" if variant else model_id
            dollars = count * AUDIO_UNIT * (Decimal(1_000_000) if field == "per_mchars" else 1)
            entry = models.setdefault(key, {"display_name": key})
            if variant:
                entry["api_ids"] = [model_id]
            entry[field] = _price(dollars, key, field)
    return models, notes


_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_CODE = re.compile(r"<code[^>]*>(.*?)</code>", re.S)
_TOOL_PRICE = re.compile(r"\$([0-9]+(?:\.[0-9]+)?)\s*/\s*1k\s+([a-z]+)")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def tool_entries(page: str, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    """The server-side tools table: a name, the tool ids, what it does, and its price."""
    at = page.find("server-side tool invocations")
    table_at = page.find("<table", at) if at >= 0 else -1
    if table_at < 0:
        raise ScrapeError("the page has no server-side tools table. xAI moved its tool prices.")
    table = page[table_at:page.find("</table>", table_at)]
    units: JSONDict = mapping["tool_units"]
    models: dict[str, JSONDict] = {}
    notes: list[str] = []
    for row in _ROW.findall(table):
        cells = _CELL.findall(row)
        if len(cells) < 4:
            continue
        codes = [_text(c) for c in _CODE.findall(cells[1])]
        if not codes:
            continue
        tool_id, name, price_text = codes[0], _text(cells[0]), _text(cells[3])
        prices = _TOOL_PRICE.findall(price_text)
        if not prices:
            continue  # "Token-based", "Imagine API rates": billed as tokens or elsewhere, not per call
        entry: JSONDict = {"kind": "product", "display_name": name}
        for amount, unit in prices:
            field = units.get(unit)
            if field is None:
                notes.append(f"{tool_id}: priced per 1k {unit!r}, a unit the mapping does not name; not published")
                entry = {}
                break
            entry[field] = _price(Decimal(amount), tool_id, field)
        if entry:
            models[tool_id] = entry
    if not models:
        raise ScrapeError("the tools table prices not a single tool per thousand. Its layout has changed.")
    return models, notes


_MODEL_LINK = re.compile(r'href="/developers/models/([^"/?#]+)"')
_PERCENT = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%")
BATCH_FIELDS = {"in_per_mtok": "in_per_mtok_batch", "out_per_mtok": "out_per_mtok_batch",
                "cache_read_per_mtok": "cache_read_per_mtok_batch"}


def batch_discounts(page: str) -> tuple[dict[str, Decimal], list[str]]:
    """The batch discount per model, from the table under "Batch discounts by model".

    The table, not the catalogue's own `batchDiscountPercent`: on 2026-11-09 the
    catalogue gave grok-build-0.1 20% while the table left it out, under the sentence
    "Models not listed above have no batch discount" -- and the table is what xAI
    states to a customer. A page without the table publishes no batch figure and says
    so in a note; it does not stop the other prices.
    """
    at = page.find("Batch discounts by model")
    table_at = page.find("<table", at) if at >= 0 else -1
    if table_at < 0:
        return {}, ["the page has no batch discount table; no batch figure is published this run"]
    table = page[table_at:page.find("</table>", table_at)]
    discounts: dict[str, Decimal] = {}
    notes: list[str] = []
    for row in _ROW.findall(table):
        cells = _CELL.findall(row)
        if len(cells) < 2:
            continue
        found = _PERCENT.findall(_text(cells[1]))
        names = _MODEL_LINK.findall(cells[0])
        if len(found) != 1 or not 0 < Decimal(found[0]) < 100:
            notes.append(f"batch discount row for {names} reads {_text(cells[1])!r}, not one percentage; skipped")
            continue
        for name in names:
            discounts[name] = Decimal(found[0])
    if not discounts and not notes:
        notes.append("the batch discount table lists no model; no batch figure is published this run")
    return discounts, notes


def add_batch(models: dict[str, JSONDict], discounts: dict[str, Decimal]) -> None:
    """Derive each listed model's batch rates: the discount applies to every token type."""
    for model_id, percent in discounts.items():
        entry = models.get(model_id)
        if entry is None or "in_per_mtok" not in entry:
            continue
        factor = 1 - percent / 100
        for field, batch_field in BATCH_FIELDS.items():
            if field in entry:
                entry[batch_field] = _price(Decimal(repr(entry[field])) * factor, model_id, batch_field)
        entry["batch_derived"] = f"{percent.normalize():f}% off the stated rate, as xAI's pricing page states for this model"


def extract_models(page: str, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    config = cluster_config(public_catalogue(page), mapping["cluster"])
    models = language_entries(config)
    discounts, batch_notes = batch_discounts(page)
    add_batch(models, discounts)
    audio, audio_notes = audio_entries(config, mapping)
    tools, tool_notes = tool_entries(page, mapping)
    for group in (audio, tools):
        clash = sorted(set(group) & set(models))
        if clash:
            raise ScrapeError(f"{clash} named twice on the page, as a model and as an audio model or tool.")
        models.update(group)
    return models, batch_notes + audio_notes + tool_notes


def extract_new_models(fetch: Fetcher, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    """The one callback provider_runner needs. One source, read once."""
    return extract_models(fetch(mapping["source"]), mapping)


def main(argv: list[str] | None = None) -> int:
    return provider_runner.main(PROVIDER_ID, extract_new_models, DEFAULT_MAPPING, DEFAULT_CURRENT, CLI_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
