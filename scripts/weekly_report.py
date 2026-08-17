#!/usr/bin/env python3
"""Say, once a week, whether every provider block was actually refreshed.

Standard library only. Reads pricing.json and nothing else -- no network, no
credentials, no writes to the published file.

This exists because "no news" is not the same statement as "everything is fine", and
only one of the two is worth trusting. The failure paths in this repository are loud:
a scrape that breaks opens an issue. But a scheduled workflow that never ran at all
-- disabled by GitHub after 60 idle days, broken by a bad edit to its own YAML, or
silently skipped -- produces no failure to report, and pricing.json simply stops
moving while looking exactly as it always did.

So this reads the four `checked_utc` stamps and reports what it finds either way:
every provider verified today, or which ones were not. It is a receipt, sent whether
or not anything is wrong, and the absence of the receipt is itself the alarm.

Usage:
    scripts/weekly_report.py --out-dir .ci-out
    scripts/weekly_report.py --out-dir .ci-out --now 2026-08-17T08:00:00Z
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from pricing_validate import JSONDict, ValidationError, validate_document  # noqa: E402

REPO_ROOT = _SCRIPTS_DIR.parent
DEFAULT_CURRENT = REPO_ROOT / "pricing.json"

CLI_SUMMARY = "Report whether every provider block in pricing.json was refreshed on schedule."

# Each provider is checked weekly, a few hours before this report runs. A stamp older
# than this means its workflow did not run today, which is the thing worth saying out
# loud. Deliberately not a week: a stamp that is six days old would still be "within
# the schedule" while meaning the job has been dead since the last report.
MAX_AGE_HOURS = 24


def describe(provider_id: str, block: JSONDict, now: _dt.datetime) -> tuple[bool, str]:
    """Return (verified_on_schedule, one markdown table row) for one provider block."""
    checked = _dt.datetime.strptime(block["checked_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=_dt.timezone.utc
    )
    age_hours = (now - checked).total_seconds() / 3600
    fresh = age_hours <= MAX_AGE_HOURS

    models = block["models"]
    absent = [m for m, e in models.items() if "absent_since" in e]

    row = (
        f"| {provider_id} | {'yes' if fresh else f'**NO — {age_hours / 24:.0f} days old**'} "
        f"| {block['checked_utc']} | {block['updated']} | {len(models)} | {len(absent)} |"
    )
    return fresh, row


def build_report(doc: JSONDict, now: _dt.datetime) -> tuple[bool, str, str]:
    """Return (all_fresh, issue title, issue body)."""
    rows: list[str] = []
    stale: list[str] = []

    for provider_id, block in doc["providers"].items():
        fresh, row = describe(provider_id, block, now)
        rows.append(row)
        if not fresh:
            stale.append(provider_id)

    total = sum(len(b["models"]) for b in doc["providers"].values())
    absent = [
        (p, m, e["absent_since"])
        for p, b in doc["providers"].items()
        for m, e in b["models"].items()
        if "absent_since" in e
    ]

    today = now.strftime("%Y-%m-%d")
    if stale:
        title = f"Weekly pricing report {today} — {len(stale)} provider(s) NOT refreshed"
        opening = (
            f"**{', '.join(stale)} did not refresh on schedule.** Its workflow did not run, "
            f"or ran and failed without reporting. The prices it publishes are still the "
            f"last ones verified, and they are now older than they look."
        )
    else:
        title = f"Weekly pricing report {today} — all providers refreshed"
        opening = (
            "Every provider was verified in the last 24 hours. Nothing needs doing; this "
            "is the receipt that says so."
        )

    lines = [
        opening,
        "",
        "| provider | refreshed today | last checked (UTC) | figures last moved | entries | absent |",
        "| --- | --- | --- | --- | --- | --- |",
        *rows,
        "",
        f"{total} entries published in total.",
    ]

    if absent:
        lines += [
            "",
            "### No longer offered by their source",
            "",
            "These keep the last prices that were actually observed, frozen. They are "
            "dropped a year after the date shown. A consumer must treat these as a last "
            "known price, not a current one.",
            "",
        ]
        lines += [f"- `{p}` / `{m}` — absent since {since}" for p, m, since in sorted(absent)]

    return not stale, title, "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=CLI_SUMMARY)
    parser.add_argument("--out-dir", default=".ci-out", help="where the report is written")
    parser.add_argument("--current", default=str(DEFAULT_CURRENT), help="the published pricing.json")
    parser.add_argument("--now", help="override the UTC stamp, format YYYY-MM-DDTHH:MM:SSZ (tests)")
    args = parser.parse_args(argv)

    now = (
        _dt.datetime.strptime(args.now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
        if args.now
        else _dt.datetime.now(_dt.timezone.utc)
    )

    doc = json.loads(Path(args.current).read_text(encoding="utf-8"))
    try:
        validate_document(doc)
    except ValidationError as exc:
        # Worth its own loud report: the published file is what consumers read, and it
        # being invalid is more urgent than any staleness this script came to measure.
        print(f"pricing.json is invalid: {exc}", file=sys.stderr)
        return 1

    all_fresh, title, body = build_report(doc, now)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report-title.txt").write_text(title + "\n", encoding="utf-8")
    (out_dir / "report-body.md").write_text(body, encoding="utf-8")

    print(title)
    print()
    print(body)

    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"all_fresh={'true' if all_fresh else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
