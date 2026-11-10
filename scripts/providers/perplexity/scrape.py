#!/usr/bin/env python3
"""Read Perplexity's public API pricing page and work out what pricing.json's
providers.perplexity block should say.

Standard library only. Reads a public page and nothing else: it takes no API key,
must never be given one, and never touches anyone's Perplexity account.

This script DOES NOT WRITE pricing.json. It writes candidate files into an output
directory and reports which one, if any, the caller should promote, exactly like the
other providers' scrapers (see provider_runner).

Two pages are read.

The Agent API's models come from <https://docs.perplexity.ai/docs/agent-api/models>,
one table per maker, each headed `Model | Input ($/1M) | Output ($/1M) | Cache read
($/1M)`: the unit is in the header, and a table headed otherwise is not read.

Everything else comes from <https://docs.perplexity.ai/docs/getting-started/pricing>,
which carries the rates of its own pricing calculator as data. The page is built with
Mintlify, which ships the compiled page as a JSON string, `compiledSource`; inside it,
the calculator starts from `const[defaultPricing]=useState(()=>({...}))`. That object
holds the tools, the sandbox, the Search API, embeddings and the Decisions API, as
numbers with a stated unit, and the tests check them against the tables a human reads
on the same page. It holds model rates too, but not every model: on 2026-11-10 it
lacked perplexity/glm-5.3-flash, which the models page lists and prices. So the models
page is read for models, and the object's model rates only serve the tests as a
cross-check.

The object states, in `_meta.units`, the unit of every section. Those are compared
with the units `mapping.json` expects before a single figure is read: a section that
changes unit fails the run rather than publishing a price a thousand times off.

The object is a JavaScript literal, not JSON: keys and some strings in double quotes,
other strings in backticks, and numbers written `.044` or `5e-4`. `js_object` turns
exactly that into data, and refuses anything else.

Usage:
    scripts/providers/perplexity/scrape.py --out-dir .ci-out    # live
    scripts/providers/perplexity/scrape.py --out-dir .ci-out \
        --offline tests/fixtures/perplexity/offline.json         # offline
"""

from __future__ import annotations

import html
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import provider_runner  # noqa: E402
from provider_runner import Fetcher, ScrapeError  # noqa: E402
from pricing_validate import JSONDict, check_price  # noqa: E402

PROVIDER_ID = "perplexity"
DEFAULT_MAPPING = Path(__file__).resolve().parent / "mapping.json"
DEFAULT_CURRENT = REPO_ROOT / "pricing.json"

# Kept as its own constant rather than read off __doc__: the module docstring is
# stripped to None under `python -OO`.
CLI_SUMMARY = "Read Perplexity's public API pricing page and work out what providers.perplexity should say."

MODEL_TABLE_HEAD = ["Model", "Input ($/1M)", "Output ($/1M)", "Cache read ($/1M)"]
COMPILED_SOURCE = '"compiledSource":'
RATES_MARKER = "const[defaultPricing]=useState(()=>("
THOUSAND = Decimal(1000)

# The sections of the rates this knows. Any other is reported, never guessed at: the
# calculator still has code for a `sonar` section -- the old Sonar API, with its
# per-request fees -- which the rates no longer carry, and it is the likeliest to
# come back. `agent.models` is known and not read (the models page is), and so is
# `agent.presets`: the effort levels' token counts are medians, not prices.
KNOWN_SECTIONS = {"_meta", "agent", "search", "embeddings", "decisions"}
KNOWN_AGENT_SECTIONS = {"models", "presets", "sandbox", "tools", "webSearchTypes"}

# The four forms a price cell takes on the models page. A threshold is written two
# ways, and they do not mean the same token: "2.00 (<200k) 4.00 (≥200k)" bills the
# 200000th token high, "0.10 (≤272k) 0.20 (>272k)" the 272001st.
FLAT = re.compile(r"(\d+(?:\.\d+)?)")
TIERED = re.compile(r"(\d+(?:\.\d+)?) \(([<≤])(\d+)k\) (\d+(?:\.\d+)?) \(([≥>])(\d+)k\)")
SHARE_OFF = re.compile(r"(\d+(?:\.\d+)?)% off input")
NO_PRICE = "—"


def page_source(page: str) -> str:
    """The compiled page that holds the rates, decoded from its JSON string."""
    decoder = json.JSONDecoder()
    for found in re.finditer(re.escape(COMPILED_SOURCE), page):
        try:
            source, _ = decoder.raw_decode(page, found.end())
        except ValueError:
            continue
        if isinstance(source, str) and RATES_MARKER in source:
            return source
    raise ScrapeError(
        "the page carries no pricing-calculator rates. Perplexity moved them, or the "
        "fetch returned something other than the pricing page."
    )


def js_object(source: str) -> JSONDict:
    """The object literal after RATES_MARKER, as data."""
    src = source[source.index(RATES_MARKER) + len(RATES_MARKER):]
    out: list[str] = []
    depth, i = 0, 0
    try:
        while True:
            ch = src[i]
            if ch == '"':
                j = i + 1
                while src[j] != '"':
                    j += 2 if src[j] == "\\" else 1
                out.append(src[i:j + 1])
                i = j + 1
                continue
            if ch == "`":
                j = src.index("`", i + 1)
                text = src[i + 1:j]
                if "\\" in text or "${" in text:
                    raise ValueError(f"a backtick string with an escape or a placeholder: `{text[:60]}`")
                out.append(json.dumps(text))
                i = j + 1
                continue
            if ch == "." and src[i + 1].isdigit() and out and out[-1][-1:] in ":[,-":
                out.append("0.")
                i += 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            out.append(ch)
            i += 1
            if depth == 0:
                break
        rates = json.loads("".join(out))
    except (IndexError, ValueError) as exc:
        raise ScrapeError(f"the pricing-calculator rates are not an object literal this can read: {exc}") from exc
    if not isinstance(rates, dict):
        raise ScrapeError("the pricing-calculator rates are not an object. The data shape has changed.")
    return cast(JSONDict, rates)


def _section(rates: JSONDict, *path: str) -> Any:
    node: Any = rates
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise ScrapeError(f"the rates have no {'.'.join(path)}. The data shape has changed.")
        node = node[key]
    return node


def check_units(rates: JSONDict, mapping: JSONDict) -> list[str]:
    """Fail on a section whose unit is not the one expected; note one never expected."""
    stated = _section(rates, "_meta", "units")
    expected = mapping["units"]
    if not isinstance(stated, dict):
        raise ScrapeError("the rates' `_meta.units` is not an object. The data shape has changed.")
    for section, unit in expected.items():
        if stated.get(section) != unit:
            raise ScrapeError(
                f"the rates state {section!r} in {stated.get(section)!r}, where the mapping expects "
                f"{unit!r}. A changed unit changes every figure in it: check the page, then the mapping."
            )
    known = set(expected) | set(mapping["units_not_read"])
    return [f"the rates state a unit for {section!r}, which nothing here reads" for section in stated
            if section not in known]


def unread_sections(rates: JSONDict) -> list[str]:
    notes = [f"the rates carry a section {key!r}, which nothing here reads" for key in rates if key not in KNOWN_SECTIONS]
    agent = _section(rates, "agent")
    notes += [f"the rates carry agent.{key}, which nothing here reads" for key in agent if key not in KNOWN_AGENT_SECTIONS]
    return notes


def _number(value: Any, where: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScrapeError(f"{where}: {value!r} is not a number. The data shape has changed.")
    return Decimal(repr(value))


def _price(value: Decimal, entry_id: str, field: str) -> float:
    return check_price(f"{PROVIDER_ID}/{entry_id}", field, float(value))


def _cells(row: str) -> list[str]:
    return [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split())
            for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]


def model_rows(page: str) -> list[list[str]]:
    """Every row of every table headed MODEL_TABLE_HEAD, as cell texts."""
    rows: list[list[str]] = []
    for table in re.findall(r"<table.*?</table>", page, re.S):
        trs = re.findall(r"<tr.*?</tr>", table, re.S)
        if trs and _cells(trs[0])[:len(MODEL_TABLE_HEAD)] == MODEL_TABLE_HEAD:
            rows += [_cells(tr) for tr in trs[1:]]
    if not rows:
        raise ScrapeError(
            f"the models page has no table headed {' | '.join(MODEL_TABLE_HEAD)}. Perplexity moved "
            f"its model prices, or changed their unit."
        )
    return rows


Tiers = tuple[Decimal, Decimal | None, int | None]  # (rate, long-context rate, threshold)


def _rate(cell: str, where: str) -> Tiers:
    if FLAT.fullmatch(cell):
        return Decimal(cell), None, None
    tiered = TIERED.fullmatch(cell)
    if tiered:
        low, below, k_low, high, above, k_high = tiered.groups()
        if k_low != k_high or (below, above) not in (("<", "≥"), ("≤", ">")):
            raise ScrapeError(f"{where}: {cell!r} states two tiers that do not meet.")
        threshold = int(k_low) * 1000 + (1 if below == "≤" else 0)
        return Decimal(low), Decimal(high), threshold
    raise ScrapeError(f"{where}: {cell!r} is not a price form this can read.")


def model_entries(page: str) -> dict[str, JSONDict]:
    """The Agent API's models: dollars per million tokens, some in two tiers.

    Keyed by the id a request names, `provider/model`, as Perplexity states it.
    """
    models: dict[str, JSONDict] = {}
    for row in model_rows(page):
        if len(row) < len(MODEL_TABLE_HEAD):
            raise ScrapeError(f"a row of the models page has {len(row)} cells: {row!r}.")
        model_id, cell_in, cell_out, cell_cache = row[:4]
        if model_id in models:
            raise ScrapeError(f"{model_id} is listed twice on the models page.")
        rates_in = _rate(cell_in, f"{model_id} input")
        rates: dict[str, Tiers] = {"in_per_mtok": rates_in, "out_per_mtok": _rate(cell_out, f"{model_id} output")}
        share = SHARE_OFF.fullmatch(cell_cache)
        if share:
            keep = 1 - Decimal(share.group(1)) / 100
            rates["cache_read_per_mtok"] = (rates_in[0] * keep, None if rates_in[1] is None else rates_in[1] * keep,
                                            rates_in[2])
        elif cell_cache != NO_PRICE:
            rates["cache_read_per_mtok"] = _rate(cell_cache, f"{model_id} cache read")

        thresholds = {threshold for _, _, threshold in rates.values() if threshold is not None}
        if len(thresholds) > 1:
            raise ScrapeError(f"{model_id}: its prices change tier at different sizes, {sorted(thresholds)}.")
        entry: JSONDict = {}
        for field, (rate, long_rate, _) in rates.items():
            entry[field] = _price(rate, model_id, field)
            if long_rate is not None:
                entry[f"{field}_long_context"] = _price(long_rate, model_id, f"{field}_long_context")
        if thresholds:
            entry["long_context_threshold"] = thresholds.pop()
        entry["display_name"] = model_id
        models[model_id] = entry
    return models


def _per_1k(per_call: Any, where: str) -> Decimal:
    """A per-call rate, as the per-thousand figure the page's own tables state."""
    return _number(per_call, where) * THOUSAND


def product_entries(rates: JSONDict) -> dict[str, JSONDict]:
    """The Agent API's tools and the Search API: billed per call, published per thousand.

    The keys are the tool's own `type`, as a request names it. Two rows of the page's
    tools table bill the same tool two ways, and those take a suffix, as xAI's
    streaming transcription does: `web_search (fast)` is web_search called with
    `search_type: "fast"`. The Search API is an endpoint, `POST /search`, not a tool,
    and is keyed `search_api` for it.
    """
    products: dict[str, JSONDict] = {}

    def add(key: str, display: str, **prices: Decimal) -> None:
        products[key] = {
            **{field: _price(value, key, field) for field, value in prices.items()},
            "kind": "product",
            "display_name": display,
        }

    for tool, per_call in _section(rates, "agent", "tools").items():
        add(tool, tool, per_1k_calls=_per_1k(per_call, f"tools.{tool}"))
    add("web_search (fast)", "web_search (Fast Search)",
        per_1k_calls=_per_1k(_section(rates, "agent", "webSearchTypes", "fast"), "webSearchTypes.fast"))
    # One container session, then every search the code inside it makes.
    sandbox = _section(rates, "agent", "sandbox")
    add("sandbox", "sandbox",
        per_session=_number(sandbox.get("session"), "sandbox.session"),
        per_1k_calls=_per_1k(sandbox.get("search"), "sandbox.search"))
    search = _section(rates, "search")
    add("search_api", "Search API", per_1k_calls=_number(search.get("per1k"), "search.per1k"))
    add("search_api (fast)", "Search API with Fast Search",
        per_1k_calls=_number(search.get("fastPer1k"), "search.fastPer1k"))
    return products


def other_model_entries(rates: JSONDict) -> dict[str, JSONDict]:
    """Embeddings and the Decisions API: dollars per million input tokens, nothing for
    output. Published as `in_per_mtok`, as Mistral's and Scaleway's embeddings are."""
    models: dict[str, JSONDict] = {}
    for section in (("embeddings",), ("decisions", "models")):
        for model in _section(rates, *section):
            model_id = model.get("id") if isinstance(model, dict) else None
            if not isinstance(model_id, str) or not model_id:
                raise ScrapeError(f"a model under {'.'.join(section)} has no string `id`. The data shape has changed.")
            rate = _number(model.get("rate"), f"{model_id}.rate")
            models[model_id] = {"in_per_mtok": _price(rate, model_id, "in_per_mtok"), "display_name": model_id}
    return models


def extract_models(pricing_page: str, models_page: str, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    rates = js_object(page_source(pricing_page))
    notes = check_units(rates, mapping) + unread_sections(rates)
    models = model_entries(models_page)
    for group in (product_entries(rates), other_model_entries(rates)):
        clash = sorted(set(group) & set(models))
        if clash:
            raise ScrapeError(f"{clash} named twice in the rates.")
        models.update(group)
    return models, notes


def extract_new_models(fetch: Fetcher, mapping: JSONDict) -> tuple[dict[str, JSONDict], list[str]]:
    """The one callback provider_runner needs. Two pages, each read once."""
    return extract_models(fetch(mapping["source"]), fetch(mapping["models_source"]), mapping)


def main(argv: list[str] | None = None) -> int:
    return provider_runner.main(PROVIDER_ID, extract_new_models, DEFAULT_MAPPING, DEFAULT_CURRENT, CLI_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
