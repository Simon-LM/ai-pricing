#!/usr/bin/env python3
"""Report a refused push as an issue, the way a failed scrape is reported.

A refresh reads its source and either publishes what it read or leaves pricing.json
alone. On 2026-11-16 a third ending showed up: the figures were read, found correct,
and committed, and the push itself was refused -- Google's run was told "Permission to
Simon-LM/ai-pricing.git denied to github-actions[bot]" on all three attempts, while the
runs minutes before and after it pushed without trouble. The run went red, and nothing
else said so: the room's watch line is the only place the red mark is read, and the
weekly report naming the provider is a week away. Meanwhile the provider reads as
refreshed while its figures age, which is the one thing a price file must not do.

So a refused push gets what a failed scrape gets: an issue, assigned, that names the
provider and carries what git said. Scoped per provider -- publish-failure:<slug>, as
scraper-failure:<slug> is -- so a stuck xAI push never suppresses a fresh Google one,
and an open issue for that provider gets a comment rather than a second issue.

Usage (from a workflow, with GH_TOKEN set):
    python3 scripts/report_push_failure.py --provider Google --slug google \
        --run-url "$RUN_URL" --detail-file /tmp/push-error.txt \
        --assignee "${{ github.repository_owner }}"
"""

from __future__ import annotations

import argparse
import datetime
import subprocess
import sys
from pathlib import Path

CLI_SUMMARY = "Open the issue that says a refreshed provider could not be published."

LABEL_COLOR = "fbca04"

# What git said, as it said it. Enough to recognise the refusal, not enough to paste a
# whole runner log into an issue a human is meant to read.
DETAIL_LIMIT = 1200


def label_for(slug: str) -> str:
    return f"publish-failure:{slug}"


def detail_from(path: str | None) -> str:
    """The refusal as git printed it, trimmed to something readable."""
    if not path:
        return "(git said nothing this run)"
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        return f"(could not read {path}: {exc})"
    if not text:
        return "(git said nothing this run)"
    if len(text) > DETAIL_LIMIT:
        text = text[:DETAIL_LIMIT] + "\n[trimmed]"
    return text


def issue_text(provider: str, when: str, run_url: str, detail: str) -> tuple[str, str]:
    """The issue's title and body. Pure, so a test can read them without a network."""
    title = f"{provider} prices could not be published on {when}"
    body = f"""The source was read, the figures are correct, and the publish was refused.

`{provider}` was checked on {when} and the scrape came back clean, so the runner's copy
of `pricing.json` was updated and committed to it. Then `git push` was refused on all
three attempts and the run stopped:

```
{detail}
```

`pricing.json` on main is untouched: it still holds {provider}'s last published figures
and its last `checked_utc` stamp. Nothing wrong was published -- nothing new was either,
and a provider that cannot publish reads as refreshed while its figures age.

Run: {run_url}

**What to do:** re-run this workflow from the Actions tab. A refused push is usually
GitHub's side rather than this repository's -- on 2026-11-16 the same refusal came while
the runs minutes earlier and later pushed fine, and a manual re-run published normally.
If it happens again for the same provider, check Settings -> Actions -> General ->
Workflow permissions, which has to allow read and write.
"""
    return title, body


def gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=False)


def open_issue_for(label: str) -> str:
    """The number of the open issue already carrying this label, or an empty string."""
    listed = gh(["issue", "list", "--label", label, "--state", "open", "--limit", "1",
                 "--json", "number", "--jq", ".[0].number // empty"])
    return listed.stdout.strip()


def report(provider: str, slug: str, title: str, body: str, assignee: str) -> int:
    label = label_for(slug)
    existing = open_issue_for(label)
    if existing:
        done = gh(["issue", "comment", existing, "--body", body])
        if done.returncode != 0:
            print(f"could not comment on issue #{existing}: {done.stderr.strip()}", file=sys.stderr)
            return 1
        print(f"commented on issue #{existing} ({label})")
        return 0
    # One label per provider, created the first time it is needed.
    gh(["label", "create", label, "--color", LABEL_COLOR,
        "--description", f"A refreshed {provider} could not be published"])
    # --assignee, not just the label, for the same reason the scraper-failure issue has
    # one: it notifies through a channel that is on by default and a busy inbox does not
    # bury, and it subscribes the owner to the comments a repeat failure adds.
    created = gh(["issue", "create", "--assignee", assignee, "--title", title,
                  "--label", label, "--body", body])
    if created.returncode != 0:
        print(f"could not open the issue: {created.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"opened {title}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=CLI_SUMMARY)
    parser.add_argument("--provider", required=True, help="the provider as its issues name it")
    parser.add_argument("--slug", required=True, help="the provider's directory name")
    parser.add_argument("--run-url", required=True, help="the run that could not publish")
    parser.add_argument("--assignee", default="", help="who the issue is assigned to")
    parser.add_argument("--detail-file", help="the file git's refusal was written to")
    parser.add_argument("--date", help="the day to name in the title (default: today, UTC)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the issue instead of opening it, and touch nothing")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    when = args.date or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    title, body = issue_text(args.provider, when, args.run_url, detail_from(args.detail_file))
    if args.dry_run:
        print(f"title: {title}\nlabel: {label_for(args.slug)}\n\n{body}")
        return 0
    return report(args.provider, args.slug, title, body, args.assignee)


if __name__ == "__main__":
    raise SystemExit(main())
