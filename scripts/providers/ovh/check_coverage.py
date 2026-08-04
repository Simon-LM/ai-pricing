#!/usr/bin/env python3
"""Ask whether OVH's catalog page still shows everything OVH actually sells.

Standard library only. Reads two public sources and nothing else: it takes no API
key, must never be given one, and never touches anyone's OVH account.

This is NOT part of the price scrape and shares none of its authority. It reads no
price, writes no candidate, and can never change a figure in pricing.json. It exists
because scrape.py has a blind spot it cannot close on its own:

  scrape.py fails loudly when a model the mapping KNOWS about vanishes from the
  catalog page. It says nothing at all about a model OVH sells that the page has
  never mentioned, or has quietly stopped mentioning -- an absent row is
  indistinguishable from a model that does not exist.

That gap is not theoretical. Between 2026-08-03 and 2026-08-04 the catalog page went
from 24 entries to 19, dropping five models OVH was still serving at live prices, and
the only reason it was caught was a human comparing a stale browser tab against a
fresh one. This script is that comparison, done every week by a machine.

It reports; it does not block. A model sold but not listed is a coverage question for
a human, not a reason to refuse publishing correct prices for everything else -- and
failing the weekly job over it would suppress the `checked_utc` stamp that keeps the
schedule alive, to fix a problem that is not a wrong number.

Exit codes, deliberately three rather than two:

    0  the two sources agree, allowing for the documented exceptions
    2  a gap worth a human's attention (this is a finding, not a malfunction)
    1  the check itself could not run: a source was unreachable or malformed

Usage:
    scripts/providers/ovh/check_coverage.py                       # read both live
    scripts/providers/ovh/check_coverage.py --html page.html --models-json api.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from fetch import FetchError, fetch_json, fetch_page  # noqa: E402
from pricing_validate import JSONDict  # noqa: E402
from providers.ovh import scrape  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_MAPPING = HERE / "mapping.json"
DEFAULT_COVERAGE = HERE / "coverage.json"

CLI_SUMMARY = "Check OVH's catalog page against OVH's own list of the models it serves."

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_GAP_FOUND = 2


class CoverageError(Exception):
    """The check could not be carried out. Distinct from "the check found something"."""


def api_model_ids(payload: Any) -> set[str]:
    """The set of model ids in an OpenAI-compatible /v1/models response."""
    if not isinstance(payload, dict):
        raise CoverageError(f"the model list is not an object (got {type(payload).__name__})")
    payload = cast(JSONDict, payload)

    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise CoverageError('the model list has no non-empty "data" array')
    data = cast("list[Any]", data)

    ids: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            raise CoverageError("a model list entry is not an object")
        entry = cast(JSONDict, entry)
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            raise CoverageError('a model list entry has no string "id"')
        ids.add(model_id)
    return ids


def catalog_model_names(html_text: str) -> set[str]:
    """The set of API model ids the catalog page states, read from each entry's `name`.

    `name`, not `id`: the catalog's `id` is a CMS slug for its own URLs, and comparing
    slugs against API ids would report every single model as missing.
    """
    return {str(entry["name"]) for entry in scrape.extract_catalog_models(html_text) if entry.get("name")}


def compare(catalog: set[str], api: set[str], mapped: set[str], ignore: dict[str, str]) -> list[str]:
    """Return the findings, most consequential first. Empty means the sources agree."""
    findings: list[str] = []

    sold_but_unlisted = sorted(api - catalog - set(ignore))
    if sold_but_unlisted:
        findings.append(
            "SOLD BUT NOT ON THE CATALOG PAGE -- OVH's API serves these, the page does "
            "not list them, so this repository has no price for them and cannot get "
            "one:\n  " + "\n  ".join(sold_but_unlisted)
        )

    listed_but_unpublished = sorted(catalog - mapped - set(ignore))
    if listed_but_unpublished:
        findings.append(
            "ON THE CATALOG PAGE BUT NOT PUBLISHED -- the page prices these and "
            "mapping.json does not mention them, so pricing.json is missing coverage "
            "it could have:\n  " + "\n  ".join(listed_but_unpublished)
        )

    listed_but_not_sold = sorted(catalog - api - set(ignore))
    if listed_but_not_sold:
        findings.append(
            "ON THE CATALOG PAGE BUT NOT IN THE API LIST -- either the page advertises "
            "something not yet servable, or the published key is not the callable "
            "model id after all:\n  " + "\n  ".join(listed_but_not_sold)
        )

    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=CLI_SUMMARY)
    parser.add_argument("--mapping", default=str(DEFAULT_MAPPING))
    parser.add_argument("--coverage", default=str(DEFAULT_COVERAGE))
    parser.add_argument("--html", help="read the catalog from a file instead of the network")
    parser.add_argument("--models-json", help="read the model list from a file instead of the network")
    args = parser.parse_args(argv)

    try:
        mapping = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
        coverage = json.loads(Path(args.coverage).read_text(encoding="utf-8"))

        html_text = (
            Path(args.html).read_text(encoding="utf-8", errors="replace")
            if args.html
            else fetch_page(mapping["source"])
        )
        payload = (
            json.loads(Path(args.models_json).read_text(encoding="utf-8"))
            if args.models_json
            else fetch_json(coverage["models_api"])
        )

        catalog = catalog_model_names(html_text)
        api = api_model_ids(payload)
    except (FetchError, CoverageError, OSError, json.JSONDecodeError, KeyError) as exc:
        # Not a finding: nothing was compared. Kept distinct from exit code 2 so a
        # workflow cannot report "OVH sells nothing new" when the truth is that the
        # endpoint timed out and no comparison happened at all.
        print(f"the coverage check could not run: {exc}", file=sys.stderr)
        return EXIT_CHECK_FAILED
    except scrape.ScrapeError as exc:
        print(f"the coverage check could not read the catalog: {exc}", file=sys.stderr)
        return EXIT_CHECK_FAILED

    findings = compare(catalog, api, set(mapping["models"]), coverage["ignore"])

    print(f"catalog page: {len(catalog)} models | API list: {len(api)} models | published: {len(mapping['models'])}")
    if not findings:
        print("No gap. Every model OVH serves is on the catalog page, and every model on")
        print("the catalog page is published, allowing for the exceptions in coverage.json.")
        return EXIT_OK

    for finding in findings:
        print()
        print(finding)
    print()
    print(
        "This does not mean a published price is wrong -- no price was read here. It "
        "means the catalog page and OVH's own service disagree about what exists. "
        "Decide what to do by hand, and record it in scripts/providers/ovh/mapping.json "
        "or, if it is expected forever, in coverage.json's ignore list with a reason."
    )
    return EXIT_GAP_FOUND


if __name__ == "__main__":
    raise SystemExit(main())
