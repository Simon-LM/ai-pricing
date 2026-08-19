"""Shared plumbing for a provider's scrape.py.

Reading pricing.json, merging one provider's block into it, writing candidates,
and the run()/main() control flow are identical for every provider -- three
outcomes, never write the published file directly, never touch another
provider's block. Only how to fetch and parse ONE provider's own page is
genuinely provider-specific, and that is the one thing this module does not do.

A provider's own scrape.py supplies:
  - PROVIDER_ID, a page parser, and an `extract_new_models(fetch, mapping)`
    callback that turns its source, or sources, into a `models` dict;
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
    ABSENT_RETENTION_DAYS,
    KNOWN_PRICE_FIELDS,
    SCHEMA_VERSION,
    JSONDict,
    ValidationError,
    check_change,
    diff_models,
    validate_document,
)

# url -> the text at that url. Handed to a provider's parser rather than a single
# already-fetched page, because not every source is one page: Mistral's models are
# documented one page each, behind an index that says which pages exist. A provider
# that does read exactly one page simply calls this once, with mapping["source"].
#
# Offline (tests, and `--html`/`--offline` on the command line) the same callable
# serves committed fixtures instead, so a provider's parser has no idea whether it is
# reading the network, and the tests exercise the real code path.
Fetcher = Callable[[str], str]

# (fetch, mapping) -> what a provider's source says today, plus any notes about
# things a human should look at that are nonetheless not reasons to publish nothing
# (a price row whose label this repository does not recognise, an entry the source
# lists without a price). Notes never block a run; a real problem raises ScrapeError.
ExtractNewModels = Callable[[Fetcher, JSONDict], "tuple[dict[str, JSONDict], list[str]]"]


class ScrapeError(Exception):
    """Anything that means: do not publish, report it, leave the file alone.

    One class shared by every provider, not one per provider: run() below needs
    to catch it regardless of which provider raised it, and a provider's own
    page-parsing code raises it for the same reason across all of them --
    fetch failed, layout changed, a mapped model went missing, a figure looks
    like a misparse rather than a price.
    """


def build_provider_block(models: dict[str, JSONDict], checked_utc: str, updated: str, mapping: JSONDict) -> JSONDict:
    """Assemble a `providers.<id>`-shaped block with a stable, reviewable key order.

    Entries are sorted by key, and each entry's own fields are emitted in a fixed
    order, rather than in whatever order a provider's parser happened to build them
    -- so that a diff a human reads shows only figures that moved. Sorting is not
    cosmetic here: two of these sources hand back their catalogue in an order they
    never promised to keep, and following it would turn one reshuffle upstream into
    a diff touching every line of the block, indistinguishable at a glance from a
    hundred price changes.

    The three non-price markers travel with the entry: `free` stands in place of the
    price fields for a model a provider gives away, `kind` marks an entry that is
    billable but is not a model at all, and `absent_since` marks one the source has
    stopped offering, whose prices are therefore the last ones observed rather than
    today's.
    """
    ordered_models: dict[str, JSONDict] = {}
    for model_id in sorted(models):
        entry = models[model_id]
        ordered: JSONDict = {}
        for field in KNOWN_PRICE_FIELDS:
            if field in entry:
                ordered[field] = entry[field]
        if entry.get("free") is True:
            ordered["free"] = True
        ordered["display_name"] = entry["display_name"]
        if "kind" in entry:
            ordered["kind"] = entry["kind"]
        if "absent_since" in entry:
            ordered["absent_since"] = entry["absent_since"]
        ordered_models[model_id] = ordered

    return {
        "checked_utc": checked_utc,
        "updated": updated,
        "source": mapping["source"],
        "currency": mapping["currency"],
        "models": ordered_models,
    }


def reconcile_inventory(
    old_models: dict[str, JSONDict], new_models: dict[str, JSONDict], today: str
) -> tuple[dict[str, JSONDict], list[str]]:
    """Combine what the source offers today with what this file already publishes.

    The inventory follows the source: an entry the source has added is published, and
    an entry it no longer offers stops being republished as though it were still on
    sale. What it does NOT do is delete on sight. The entry stays, with the prices last
    actually observed and an `absent_since` day stamp, until it has been gone for
    ABSENT_RETENTION_DAYS; only then is it dropped.

    Returns the merged models dict and a list of inventory notes -- disappearances,
    returns and expiries, in words. Notes are for a human to read, and each one is
    produced by the single run that makes the change, never repeated: an entry that is
    still absent next week already carries `absent_since`, so it says nothing further.
    A weekly reminder of a decision already taken is how an alert channel gets muted,
    and this one has to still work the day something real happens.

    Nothing here can fail. Neither a disappearance nor a return says anything about
    whether the OTHER models' prices were read correctly, so neither is allowed to
    stop them being published.
    """
    merged: dict[str, JSONDict] = {}
    notes: list[str] = []
    cutoff = _dt.date.fromisoformat(today) - _dt.timedelta(days=ABSENT_RETENTION_DAYS)

    for model_id, entry in new_models.items():
        entry = dict(entry)
        # Whatever the source says today is current by definition, so a stamp left over
        # from an earlier absence is not merely stale, it is now false.
        was_absent = old_models.get(model_id, {}).get("absent_since")
        entry.pop("absent_since", None)
        if was_absent:
            notes.append(f"{model_id}: offered again (absent since {was_absent}); prices are live again")
        merged[model_id] = entry

    for model_id, entry in old_models.items():
        if model_id in new_models:
            continue

        absent_since = entry.get("absent_since")
        if absent_since is None:
            kept = dict(entry)
            kept["absent_since"] = today
            merged[model_id] = kept
            notes.append(
                f"{model_id}: no longer offered by the source. Kept with the prices last "
                f"observed and absent_since={today}; it will be dropped after "
                f"{ABSENT_RETENTION_DAYS} days of absence."
            )
            continue

        if _dt.date.fromisoformat(absent_since) < cutoff:
            notes.append(
                f"{model_id}: absent since {absent_since}, more than {ABSENT_RETENTION_DAYS} "
                f"days. Dropped from the file."
            )
            continue

        merged[model_id] = dict(entry)

    return merged, notes


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


def build_fetcher(args: argparse.Namespace, mapping: JSONDict) -> Fetcher:
    """Return the url -> text callable a provider's parser will be handed.

    Live, it fetches. Offline, it serves committed fixtures and refuses any url the
    manifest does not name -- deliberately an error rather than a fall-through to the
    network, so that a test which forgets a fixture fails loudly instead of silently
    reaching out and passing for the wrong reason.
    """
    served: dict[str, Path] = {}

    if args.offline:
        manifest_path = Path(args.offline)
        manifest = load_json(manifest_path, "offline manifest")
        if not isinstance(manifest, dict):
            raise ScrapeError(f"the offline manifest must be an object of url -> path: {manifest_path}")
        served.update({url: (manifest_path.parent / rel) for url, rel in manifest.items()})

    # `--html` is the one-page shorthand: bind the mapping's own source to that file.
    # Every provider but Mistral reads exactly one page, so this is the common case.
    if args.html:
        served[mapping["source"]] = Path(args.html)

    def fetch(url: str) -> str:
        path = served.get(url)
        if path is not None:
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise ScrapeError(f"fixture for {url} could not be read: {exc}") from exc
        if served:
            raise ScrapeError(
                f"offline run asked for {url}, which no fixture serves. Add it to the "
                f"manifest rather than letting the run reach the network."
            )
        try:
            return fetch_page(url)
        except FetchError as exc:
            raise ScrapeError(str(exc)) from exc

    return fetch


def run(
    provider_id: str, extract_new_models: ExtractNewModels, args: argparse.Namespace
) -> tuple[str, str, list[str]]:
    """Do the whole job for one provider.

    Returns (outcome, human-readable summary, inventory notes). Notes are things a
    human should be told about but that are not reasons to withhold a price: an entry
    that disappeared, one that came back, one dropped after a year away, a price row
    whose label is not recognised. The workflow turns a non-empty list into one issue;
    the run itself still succeeds and still publishes.
    """
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

    # There is deliberately no check here that the published set still matches some
    # hand-written list. This job exists to track what each source currently offers:
    # a model the source has dropped is dropped, one it has added is added, and both
    # show up as `- id: removed` / `+ id: added` lines in the summary that goes into
    # the commit and the run's job summary. Refusing to publish anything until a
    # human reconciled a list is what turned an automated tracker into a weekly
    # chore, and it blocked correct prices for every other model in the block.

    offered_models, notes = extract_new_models(build_fetcher(args, mapping), mapping)

    # Compare against what is published before believing any of it.
    for model_id, entry in offered_models.items():
        old_entry = current_block["models"].get(model_id)
        if not old_entry:
            continue
        for field in KNOWN_PRICE_FIELDS:
            if field in entry and field in old_entry:
                check_change(f"{provider_id}/{model_id}", field, float(old_entry[field]), float(entry[field]))

    now = args.now or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now[:10]

    new_models, inventory_notes = reconcile_inventory(current_block["models"], offered_models, today)
    notes = notes + inventory_notes

    changes = diff_models(current_block["models"], new_models)

    # Always produced: the same figures with a fresh verification stamp. This is a real
    # statement to consumers ("confirmed unchanged today") and it is also the commit that
    # keeps GitHub from disabling the schedule after 60 days of no commits.
    stamped_block = build_provider_block(current_block["models"], now, current_block["updated"], mapping)
    stamped = merge_provider_block(current, provider_id, stamped_block)
    validate_document(stamped)
    write_json(out_dir / "stamped.json", stamped)

    if not changes:
        return (
            "unchanged",
            f"Figures unchanged. Verified against {mapping['source']} at {now}. "
            f"{len(new_models)} entries.",
            notes,
        )

    updated_block = build_provider_block(new_models, now, today, mapping)
    updated_doc = merge_provider_block(current, provider_id, updated_block)
    validate_document(updated_doc)
    write_json(out_dir / "updated.json", updated_doc)

    summary = "\n".join(
        [f"Figures changed, verified against {mapping['source']} at {now}:", ""]
        + [f"  {line}" for line in changes]
    )
    return "changed", summary, notes


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
    parser.add_argument("--html", help="read this local file instead of fetching the mapping's source (tests)")
    parser.add_argument(
        "--offline",
        help="a JSON file of {url: path} serving every source from disk (tests). Paths are "
        "relative to the manifest. A url the manifest does not list is an error rather "
        "than a live fetch, so an offline run cannot quietly reach the network.",
    )
    parser.add_argument("--now", help="override the UTC stamp, format YYYY-MM-DDTHH:MM:SSZ (tests)")
    args = parser.parse_args(argv)

    try:
        outcome, summary, notes = run(provider_id, extract_new_models, args)
    except (ScrapeError, ValidationError) as exc:
        message = str(exc)
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "error.txt").write_text(message + "\n", encoding="utf-8")
        # Nothing may be published from here on. pricing.json was never opened for writing.
        for stale in ("stamped.json", "updated.json", "summary.txt", "notes.txt"):
            (out_dir / stale).unlink(missing_ok=True)
        print(f"FAILED: {message}", file=sys.stderr)
        emit_github_output(outcome="failed", summary=message, notes="")
        return 1

    # The summary and the notes are written to files as well as to step outputs. The
    # workflow reads the files: it must never interpolate text derived from a remote
    # page into a shell command, and both quote labels found on that page.
    out_dir = Path(args.out_dir)
    (out_dir / "summary.txt").write_text(summary + "\n", encoding="utf-8")
    notes_text = "\n".join(notes)
    (out_dir / "notes.txt").write_text(notes_text + "\n" if notes else "", encoding="utf-8")

    print(summary)
    if notes:
        print("\nInventory notes:")
        for note in notes:
            print(f"  {note}")

    emit_github_output(outcome=outcome, summary=summary, notes=notes_text)
    return 0
