"""Shared plumbing for a provider's scrape.py.

Reading pricing.json, merging one provider's block into it, writing candidates,
and the run()/main() control flow are identical for every provider -- three
outcomes, never write the published file directly, never touch another
provider's block. Only how to fetch and parse ONE provider's own page is
genuinely provider-specific, and that is the one thing this module does not do.

A provider's own scrape.py supplies:
  - PROVIDER_ID, a page parser, and an `extract_new_models(html_text, mapping)`
    callback that turns that page into a `models` dict;
  - its own CLI_SUMMARY and default paths;
and calls `provider_runner.main(...)` from its own `main()`.

Standard library only. Imported by every provider's scrape.py and exercised
directly by the tests.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from fetch import FetchError, fetch_page  # noqa: E402
from pricing_validate import (  # noqa: E402
    KNOWN_PRICE_FIELDS,
    SCHEMA_VERSION,
    JSONDict,
    ValidationError,
    check_change,
    diff_models,
    validate_document,
)

# (html_text, mapping) -> the models dict a provider's page currently says.
ExtractNewModels = Callable[[str, JSONDict], "dict[str, JSONDict]"]


class ScrapeError(Exception):
    """Anything that means: do not publish, report it, leave the file alone.

    One class shared by every provider, not one per provider: run() below needs
    to catch it regardless of which provider raised it, and a provider's own
    page-parsing code raises it for the same reason across all of them --
    fetch failed, layout changed, a mapped model went missing, a figure looks
    like a misparse rather than a price.
    """


def build_provider_block(models: dict[str, JSONDict], checked_utc: str, updated: str, mapping: JSONDict) -> JSONDict:
    """Assemble a `providers.<id>`-shaped block with a stable, reviewable key order."""
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


def merge_provider_block(full_doc: JSONDict, provider_id: str, provider_block: JSONDict) -> JSONDict:
    """Return a copy of `full_doc` with only `providers.<provider_id>` replaced.

    Every other provider's block is carried over exactly as it stood, in its
    original position. A provider's scraper has no business reading, let alone
    rewriting, a block it did not scrape.

    Key order matters here for a reason beyond taste: a merge that rebuilt the
    `providers` dict as "everyone else, then this one" would move this provider
    to the end whenever it did not already sit there, and every other provider's
    block would show up as removed-and-re-added in the git diff a human is about
    to review -- noise indistinguishable, at a glance, from an actual change to
    that provider.
    """
    providers = full_doc["providers"]
    merged = {pid: (provider_block if pid == provider_id else block) for pid, block in providers.items()}
    merged.setdefault(provider_id, provider_block)
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


def run(provider_id: str, extract_new_models: ExtractNewModels, args: argparse.Namespace) -> tuple[str, str]:
    """Do the whole job for one provider. Returns (outcome, human-readable summary)."""
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

    current_block = current["providers"].get(provider_id)
    if current_block is None:
        raise ScrapeError(
            f"pricing.json has no providers.{provider_id} block yet. Seed one by hand "
            f"before the first automated run -- this job updates a provider's figures, "
            f"it does not decide to start publishing a new one."
        )

    if current_block.get("currency") != mapping["currency"]:
        raise ScrapeError(
            f"currency mismatch: providers.{provider_id} says {current_block.get('currency')!r}, "
            f"mapping says {mapping['currency']!r}. This file performs no conversion, so this "
            f"must be fixed by hand."
        )

    published_but_unmapped = sorted(set(current_block["models"]) - set(mapping["models"]))
    if published_but_unmapped:
        raise ScrapeError(
            f"providers.{provider_id} publishes model(s) the mapping does not know: "
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

    new_models = extract_new_models(html_text, mapping)

    # Compare against what is published before believing any of it.
    for model_id, entry in new_models.items():
        old_entry = current_block["models"].get(model_id)
        if not old_entry:
            continue
        for field in KNOWN_PRICE_FIELDS:
            if field in entry and field in old_entry:
                check_change(f"{provider_id}/{model_id}", field, float(old_entry[field]), float(entry[field]))

    now = args.now or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now[:10]

    changes = diff_models(current_block["models"], new_models)

    # Always produced: the same figures with a fresh verification stamp. This is a real
    # statement to consumers ("confirmed unchanged today") and it is also the commit that
    # keeps GitHub from disabling the schedule after 60 days of no commits.
    stamped_block = build_provider_block(current_block["models"], now, current_block["updated"], mapping)
    stamped = merge_provider_block(current, provider_id, stamped_block)
    validate_document(stamped)
    write_json(out_dir / "stamped.json", stamped)

    if not changes:
        return "unchanged", f"Figures unchanged. Verified against {mapping['source']} at {now}."

    updated_block = build_provider_block(new_models, now, today, mapping)
    updated_doc = merge_provider_block(current, provider_id, updated_block)
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


def main(
    provider_id: str,
    extract_new_models: ExtractNewModels,
    default_mapping: Path,
    default_current: Path,
    cli_summary: str,
    argv: list[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=cli_summary)
    parser.add_argument("--out-dir", default=".ci-out", help="where candidate files are written")
    parser.add_argument("--current", default=str(default_current), help="the published pricing.json")
    parser.add_argument("--mapping", default=str(default_mapping), help="the explicit mapping")
    parser.add_argument("--html", help="read this local HTML file instead of fetching (tests)")
    parser.add_argument("--now", help="override the UTC stamp, format YYYY-MM-DDTHH:MM:SSZ (tests)")
    args = parser.parse_args(argv)

    try:
        outcome, summary = run(provider_id, extract_new_models, args)
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
