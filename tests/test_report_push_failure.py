"""Tests for scripts/report_push_failure.py, the issue a refused push opens.

What this has to get right is the reading, not the plumbing: the figures were read and
are correct, the published file is untouched, and the reason the run is red is a publish
that was refused. An issue that reads like "Google is broken" would send its reader
looking at a scraper that worked.

Nothing here talks to GitHub. The text is built by a pure function, and the one path
that would call `gh` is exercised with --dry-run, which prints and touches nothing.
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import report_push_failure  # noqa: E402

REFUSAL = (
    "remote: Permission to Simon-LM/ai-pricing.git denied to github-actions[bot].\n"
    "fatal: unable to access 'https://github.com/Simon-LM/ai-pricing/': "
    "The requested URL returned error: 403\n"
)


class TestTheIssueSaysTheRightThing(unittest.TestCase):
    def body(self, detail: str = REFUSAL) -> str:
        return report_push_failure.issue_text("Google", "2026-11-16", "https://run/1", detail)[1]

    def test_the_title_names_the_provider_and_the_day(self) -> None:
        title, _ = report_push_failure.issue_text("Google", "2026-11-16", "https://run/1", "")
        self.assertEqual(title, "Google prices could not be published on 2026-11-16")

    def test_it_carries_what_git_said(self) -> None:
        """The refusal is the whole diagnosis; without it the reader cannot tell a
        permission problem from a network one."""
        self.assertIn("denied to github-actions[bot]", self.body())

    def test_it_carries_the_run(self) -> None:
        self.assertIn("https://run/1", self.body())

    def test_it_says_the_published_file_is_untouched(self) -> None:
        """The reason this is an issue and not a shrug: someone reading it must not go
        looking for a wrong price that was never published."""
        body = self.body()
        self.assertIn("pricing.json` on main is untouched", body)
        self.assertIn("Nothing wrong was published", body)

    def test_it_does_not_blame_the_scraper(self) -> None:
        body = self.body()
        self.assertIn("the figures are correct", body)
        self.assertNotIn("could not complete", body)

    def test_it_says_what_to_do(self) -> None:
        self.assertIn("re-run this workflow from the Actions tab", self.body())


class TestTheLabelIsPerProvider(unittest.TestCase):
    def test_every_provider_gets_its_own_label(self) -> None:
        """As scraper-failure:<slug> is, so a stuck xAI push cannot suppress a fresh
        Google one, and an open issue is commented on rather than duplicated."""
        self.assertEqual(report_push_failure.label_for("google"), "publish-failure:google")
        self.assertNotEqual(
            report_push_failure.label_for("google"), report_push_failure.label_for("xai")
        )


class TestTheDetailIsReadable(unittest.TestCase):
    def test_a_missing_file_says_git_said_nothing(self) -> None:
        self.assertEqual(report_push_failure.detail_from(None), "(git said nothing this run)")

    def test_an_empty_file_says_the_same(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".txt") as handle:
            handle.write("\n")
            handle.flush()
            self.assertEqual(report_push_failure.detail_from(handle.name),
                             "(git said nothing this run)")

    def test_a_file_that_cannot_be_read_says_so(self) -> None:
        self.assertIn("could not read", report_push_failure.detail_from("/nowhere/at/all.txt"))

    def test_a_wall_of_output_is_trimmed(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".txt") as handle:
            handle.write("x" * (report_push_failure.DETAIL_LIMIT * 2))
            handle.flush()
            detail = report_push_failure.detail_from(handle.name)
        self.assertLess(len(detail), report_push_failure.DETAIL_LIMIT + 100)
        self.assertTrue(detail.endswith("[trimmed]"))


class TestTheDryRunTouchesNothing(unittest.TestCase):
    def test_it_prints_the_issue_and_returns_zero(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = report_push_failure.main([
                "--provider", "Google", "--slug", "google", "--run-url", "https://run/1",
                "--date", "2026-11-16", "--dry-run",
            ])
        self.assertEqual(code, 0)
        printed = out.getvalue()
        self.assertIn("title: Google prices could not be published on 2026-11-16", printed)
        self.assertIn("label: publish-failure:google", printed)


if __name__ == "__main__":
    unittest.main()
