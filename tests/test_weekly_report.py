"""Tests for the Monday receipt.

Standard library only: `python3 -m unittest discover tests`.

The thing being tested is a claim, not a computation: "every provider was refreshed".
It has to be false when it is false -- a report that says all is well whatever the
file says would be worse than no report, because it would replace silence, which is
at least honest, with a reassurance nobody should trust.

Deliberately synthetic documents, like tests/test_pricing_validate.py: this file must
keep working unchanged as providers are added, so it never names a real one.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import pricing_validate as validate  # noqa: E402
import weekly_report  # noqa: E402
from pricing_validate import JSONDict  # noqa: E402

NOW = "2026-08-17T08:00:00Z"
PRICING_JSON = REPO_ROOT / "pricing.json"


def block(checked_utc: str, models: JSONDict | None = None) -> JSONDict:
    return {
        "checked_utc": checked_utc,
        "updated": checked_utc[:10],
        "source": "https://example.invalid/pricing",
        "currency": "USD",
        "models": models or {"some-model": {"in_per_mtok": 1.0, "display_name": "Some Model"}},
    }


class ReportTestCase(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.out_dir = self.tmp / "out"
        self.current = self.tmp / "pricing.json"

    def run_report(self, **providers: JSONDict) -> tuple[int, str]:
        doc = {"schema_version": validate.SCHEMA_VERSION, "providers": providers}
        validate.validate_document(doc)
        self.current.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

        argv = ["--out-dir", str(self.out_dir), "--current", str(self.current), "--now", NOW]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = weekly_report.main(argv)
        return code, out.getvalue()

    def title(self) -> str:
        return (self.out_dir / "report-title.txt").read_text(encoding="utf-8").strip()

    def body(self) -> str:
        return (self.out_dir / "report-body.md").read_text(encoding="utf-8")


class TestEverythingFine(ReportTestCase):
    def test_all_fresh_says_so(self) -> None:
        code, _ = self.run_report(
            acme=block("2026-08-17T04:00:00Z"), beta=block("2026-08-17T07:00:00Z")
        )
        self.assertEqual(code, 0)
        self.assertIn("all providers refreshed", self.title())
        self.assertIn("Nothing needs doing", self.body())

    def test_the_report_names_every_provider(self) -> None:
        """A receipt that silently skipped a provider would be exactly the failure it
        exists to catch."""
        self.run_report(acme=block("2026-08-17T04:00:00Z"), beta=block("2026-08-17T07:00:00Z"))
        self.assertIn("| acme |", self.body())
        self.assertIn("| beta |", self.body())

    def test_a_stamp_just_inside_the_window_is_still_fresh(self) -> None:
        self.run_report(acme=block("2026-08-16T09:00:00Z"))
        self.assertIn("all providers refreshed", self.title())


class TestSomethingStale(ReportTestCase):
    def test_a_stale_provider_is_named_in_the_title(self) -> None:
        """The title is what lands in the email subject line, so the bad news has to be
        in it -- a subject that reads the same either way trains the reader to skip it."""
        code, _ = self.run_report(
            acme=block("2026-08-17T04:00:00Z"), beta=block("2026-07-01T05:00:00Z")
        )
        self.assertEqual(code, 0)
        self.assertIn("NOT refreshed", self.title())
        self.assertIn("beta did not refresh", self.body())

    def test_a_stamp_just_outside_the_window_is_stale(self) -> None:
        self.run_report(acme=block("2026-08-16T07:00:00Z"))
        self.assertIn("NOT refreshed", self.title())

    def test_a_working_provider_is_not_dragged_down_with_it(self) -> None:
        self.run_report(acme=block("2026-08-17T04:00:00Z"), beta=block("2026-07-01T05:00:00Z"))
        body = self.body()
        self.assertIn("| acme | yes |", body)
        self.assertIn("**NO", body)


class TestAbsentEntriesAreListed(ReportTestCase):
    def test_they_get_their_own_section_with_dates(self) -> None:
        """Someone reading the weekly mail is the person most likely to still be calling
        a model that went away, so the report says which ones and since when."""
        self.run_report(
            acme=block(
                "2026-08-17T04:00:00Z",
                {
                    "live-model": {"in_per_mtok": 1.0, "display_name": "Live"},
                    "gone-model": {
                        "in_per_mtok": 2.0,
                        "display_name": "Gone",
                        "absent_since": "2026-08-10",
                    },
                },
            )
        )
        body = self.body()
        self.assertIn("No longer offered", body)
        self.assertIn("`gone-model` — absent since 2026-08-10", body)
        self.assertNotIn("`live-model`", body)

    def test_no_section_when_nothing_is_absent(self) -> None:
        self.run_report(acme=block("2026-08-17T04:00:00Z"))
        self.assertNotIn("No longer offered", self.body())


class TestCorruptInput(ReportTestCase):
    def test_an_invalid_published_file_fails_loudly(self) -> None:
        """More urgent than any staleness this script came to measure: the invalid file
        is the one consumers are reading right now."""
        self.current.write_text(json.dumps({"schema_version": 999, "providers": {}}), encoding="utf-8")
        argv = ["--out-dir", str(self.out_dir), "--current", str(self.current), "--now", NOW]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = weekly_report.main(argv)
        self.assertEqual(code, 1)
        self.assertIn("pricing.json is invalid", err.getvalue())


class TestAgainstTheRealFile(unittest.TestCase):
    def test_the_committed_file_produces_a_report(self) -> None:
        """Cheap end-to-end guard: the script reads the real schema, not a shape only
        these tests build."""
        doc = json.loads(PRICING_JSON.read_text(encoding="utf-8"))
        checked = max(b["checked_utc"] for b in doc["providers"].values())
        import datetime as dt

        now = dt.datetime.strptime(checked, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        all_fresh, title, body = weekly_report.build_report(doc, now)

        self.assertTrue(title)
        for provider_id in doc["providers"]:
            self.assertIn(f"| {provider_id} |", body)


if __name__ == "__main__":
    unittest.main()
