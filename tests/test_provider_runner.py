"""Tests for the reconciliation every provider shares, in isolation.

Standard library only: `python3 -m unittest discover tests`.

Each provider's own tests exercise this through a real page or payload, which is the
right way round for "does the OVH scraper behave". This file exists for the part that
is pure arithmetic on dates, where the interesting cases are the boundaries and no
fixture can put you exactly one day either side of them.

Deliberately synthetic, like tests/test_pricing_validate.py: it never names a real
provider or model.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import provider_runner  # noqa: E402
from pricing_validate import ABSENT_RETENTION_DAYS, JSONDict  # noqa: E402

TODAY = "2026-08-17"


def entry(price: float = 1.0, **overrides: object) -> JSONDict:
    result: JSONDict = {"in_per_mtok": price, "display_name": "Some Model"}
    result.update(overrides)
    return result


class TestStillOffered(unittest.TestCase):
    def test_the_source_wins_on_price(self) -> None:
        merged, notes = provider_runner.reconcile_inventory(
            {"m": entry(1.0)}, {"m": entry(2.0)}, TODAY
        )
        self.assertEqual(merged["m"]["in_per_mtok"], 2.0)
        self.assertEqual(notes, [])

    def test_a_new_model_is_published_without_ceremony(self) -> None:
        merged, notes = provider_runner.reconcile_inventory({}, {"m": entry()}, TODAY)
        self.assertEqual(set(merged), {"m"})
        self.assertEqual(notes, [], "an addition is visible in the diff; it is not an alert")


class TestGoingAbsent(unittest.TestCase):
    def test_it_is_kept_dated_and_reported(self) -> None:
        merged, notes = provider_runner.reconcile_inventory({"m": entry(1.0)}, {}, TODAY)
        self.assertEqual(merged["m"]["absent_since"], TODAY)
        self.assertEqual(merged["m"]["in_per_mtok"], 1.0, "the last observed price is kept")
        self.assertEqual(len(notes), 1)
        self.assertIn("no longer offered", notes[0])

    def test_the_stamp_is_set_once_and_never_moved(self) -> None:
        """Moving it every week would make a model that vanished a year ago look like it
        vanished on Monday, and it would never reach the retention cutoff."""
        merged, notes = provider_runner.reconcile_inventory(
            {"m": entry(1.0, absent_since="2026-08-10")}, {}, TODAY
        )
        self.assertEqual(merged["m"]["absent_since"], "2026-08-10")
        self.assertEqual(notes, [], "the same disappearance must not be reported twice")

    def test_the_original_entry_is_not_mutated(self) -> None:
        """The caller still needs the published block intact to diff against."""
        published = {"m": entry(1.0)}
        provider_runner.reconcile_inventory(published, {}, TODAY)
        self.assertNotIn("absent_since", published["m"])


class TestRetentionBoundary(unittest.TestCase):
    """A year, counted from the day the source was first seen without it."""

    def reconcile(self, absent_since: str, today: str) -> dict[str, JSONDict]:
        merged, _ = provider_runner.reconcile_inventory(
            {"m": entry(1.0, absent_since=absent_since)}, {}, today
        )
        return merged

    def test_a_day_before_the_year_is_up_it_is_still_published(self) -> None:
        self.assertIn("m", self.reconcile("2025-08-17", "2026-08-16"))

    def test_on_the_day_the_year_is_up_it_is_still_published(self) -> None:
        """365 days absent is not MORE than 365 days absent. Stated as its own case
        because this is exactly where an off-by-one would sit unnoticed."""
        self.assertIn("m", self.reconcile("2025-08-17", "2026-08-17"))

    def test_the_day_after_it_is_dropped(self) -> None:
        self.assertNotIn("m", self.reconcile("2025-08-17", "2026-08-18"))

    def test_dropping_it_is_reported(self) -> None:
        _, notes = provider_runner.reconcile_inventory(
            {"m": entry(1.0, absent_since="2025-01-01")}, {}, TODAY
        )
        self.assertEqual(len(notes), 1)
        self.assertIn("Dropped from the file", notes[0])

    def test_the_window_is_the_one_the_schema_promises(self) -> None:
        self.assertEqual(ABSENT_RETENTION_DAYS, 365)


class TestComingBack(unittest.TestCase):
    def test_the_marker_goes_and_the_price_is_refreshed(self) -> None:
        merged, notes = provider_runner.reconcile_inventory(
            {"m": entry(1.0, absent_since="2026-08-10")}, {"m": entry(3.0)}, TODAY
        )
        self.assertNotIn("absent_since", merged["m"])
        self.assertEqual(merged["m"]["in_per_mtok"], 3.0)
        self.assertEqual(len(notes), 1)
        self.assertIn("offered again", notes[0])

    def test_a_model_that_never_left_says_nothing(self) -> None:
        _, notes = provider_runner.reconcile_inventory({"m": entry(1.0)}, {"m": entry(1.0)}, TODAY)
        self.assertEqual(notes, [])


class TestOneModelDoesNotBlockTheOthers(unittest.TestCase):
    def test_a_disappearance_publishes_everything_else(self) -> None:
        """The property the whole change exists for. Everything above is detail; this is
        the behaviour that was actually wrong."""
        published = {"gone": entry(1.0), "kept": entry(2.0)}
        offered = {"kept": entry(2.5), "new": entry(3.0)}

        merged, notes = provider_runner.reconcile_inventory(published, offered, TODAY)

        self.assertEqual(merged["kept"]["in_per_mtok"], 2.5)
        self.assertEqual(merged["new"]["in_per_mtok"], 3.0)
        self.assertEqual(merged["gone"]["absent_since"], TODAY)
        self.assertEqual(len(notes), 1)


if __name__ == "__main__":
    unittest.main()
