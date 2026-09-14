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

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import fetch  # noqa: E402
import provider_runner  # noqa: E402
from pricing_validate import ABSENT_RETENTION_DAYS, KNOWN_PRICE_FIELDS, JSONDict  # noqa: E402

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


class TestOfferedButUnpriced(unittest.TestCase):
    """A source that sells something and will not say what it costs.

    Hugging Face's router does this: `status: live`, `is_free: false`, and either a
    price of 0 or no pricing object at all. So does OVH, for a unit this repository has
    no field for. Publishing the entry with `unpriced_since` says the true thing --
    "this exists, it is not free, and there is no price here" -- where dropping it made
    that indistinguishable from a model that no longer exists.

    A scraper signals it by handing back the internal `unpriced` key carrying the reason
    in words. The date is stamped here, and the marker itself never reaches the file.
    """

    def unpriced(self, reason: str = "input is 0") -> JSONDict:
        return {"display_name": "m", "unpriced": reason}

    def test_the_date_is_stamped_and_the_marker_never_leaks(self) -> None:
        merged, notes = provider_runner.reconcile_inventory({}, {"m": self.unpriced()}, TODAY)
        self.assertEqual(merged["m"]["unpriced_since"], TODAY)
        self.assertNotIn("unpriced", merged["m"], "internal marker reached pricing.json")
        self.assertEqual(len(notes), 1)
        self.assertIn("input is 0", notes[0], "the note does not say what the source did")

    def test_it_is_reported_once_and_then_never_again(self) -> None:
        """The whole point of keeping the state in the file. Before this, the scraper
        recomputed a note every run, so the same unpriced route opened a fresh issue --
        and a fresh email -- every Monday until the source got round to pricing it. A
        weekly reminder of a fact nobody can act on is how an alert channel gets muted.
        """
        first, notes = provider_runner.reconcile_inventory({}, {"m": self.unpriced()}, TODAY)
        self.assertEqual(len(notes), 1)

        for week in ("2026-08-24", "2026-08-31", "2026-09-07"):
            first, notes = provider_runner.reconcile_inventory(first, {"m": self.unpriced()}, week)
            self.assertEqual(notes, [], f"said it again on {week}")
            self.assertEqual(
                first["m"]["unpriced_since"], TODAY, "re-stamped an old gap as if it were new"
            )

    def test_the_last_prices_observed_are_kept_beside_the_marker(self) -> None:
        """A consumer then has a figure to reason with AND the warning that the source
        no longer stands behind it -- strictly more than either alone."""
        merged, notes = provider_runner.reconcile_inventory(
            {"m": entry(2.5)}, {"m": self.unpriced()}, TODAY
        )
        self.assertEqual(merged["m"]["in_per_mtok"], 2.5)
        self.assertEqual(merged["m"]["unpriced_since"], TODAY)
        self.assertIn("last prices observed are kept", notes[0])

    def test_one_that_was_never_priced_carries_no_price(self) -> None:
        merged, notes = provider_runner.reconcile_inventory({}, {"m": self.unpriced()}, TODAY)
        self.assertEqual([k for k in merged["m"] if k in KNOWN_PRICE_FIELDS], [])
        self.assertIn("never been quoted one", notes[0])

    def test_a_price_returning_clears_the_marker_and_says_so(self) -> None:
        """The other half of the requirement: it has to be able to come back."""
        merged, notes = provider_runner.reconcile_inventory(
            {"m": {"display_name": "m", "unpriced_since": "2026-08-10"}}, {"m": entry(4.0)}, TODAY
        )
        self.assertNotIn("unpriced_since", merged["m"])
        self.assertEqual(merged["m"]["in_per_mtok"], 4.0)
        self.assertEqual(len(notes), 1)
        self.assertIn("priced again", notes[0])

    def test_unpriced_is_not_absent(self) -> None:
        """Two different statements, and a consumer must not merge them: one says the
        source stopped offering it, the other says the source still offers it and will
        not quote it."""
        merged, _ = provider_runner.reconcile_inventory({}, {"m": self.unpriced()}, TODAY)
        self.assertNotIn("absent_since", merged["m"])

    def test_an_entry_that_goes_away_while_unpriced_goes_absent(self) -> None:
        """Disappearing outranks being unpriced: it is no longer offered at all."""
        merged, notes = provider_runner.reconcile_inventory(
            {"m": {"display_name": "m", "in_per_mtok": 1.0, "unpriced_since": "2026-08-10"}},
            {},
            TODAY,
        )
        self.assertEqual(merged["m"]["absent_since"], TODAY)
        self.assertIn("no longer offered", notes[0])


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


class TestASourceListIsPublishedOnlyWhenThereIsOne(unittest.TestCase):
    """`source` is one url and stays required. A block built from two pages says so
    with `sources` as well, which is additive: a consumer reading `source` alone sees
    no change."""

    MAPPING = {"source": "https://example.test/a", "currency": "USD"}
    MODELS = {"m": {"in_per_mtok": 1.0, "display_name": "M"}}

    def build(self, **extra: object) -> JSONDict:
        return provider_runner.build_provider_block(
            dict(self.MODELS), "2026-08-17T04:00:00Z", "2026-08-17", {**self.MAPPING, **extra}
        )

    def test_one_page_publishes_no_list(self) -> None:
        """Three providers out of four read a single page; a one-element list there
        would be noise saying nothing `source` does not already say."""
        self.assertNotIn("sources", self.build())

    def test_two_pages_publish_the_list(self) -> None:
        pages = ["https://example.test/a", "https://example.test/b"]
        self.assertEqual(self.build(sources=pages)["sources"], pages)

    def test_the_list_is_copied_not_shared_with_the_mapping(self) -> None:
        """The block is written to disk and the mapping is reused across a run; an
        alias would let one mutate the other."""
        pages = ["https://example.test/a", "https://example.test/b"]
        block = self.build(sources=pages)
        block["sources"].append("https://example.test/c")
        self.assertEqual(len(pages), 2)

    def test_source_still_comes_first(self) -> None:
        """Key order is reviewed by humans in diffs, so it is fixed on purpose."""
        keys = list(self.build(sources=["https://example.test/a"]))
        self.assertEqual(keys, ["checked_utc", "updated", "source", "sources", "currency", "models"])


class TestHowASourceIsAskedFor(unittest.TestCase):
    """A marketing page and a JSON endpoint need different requests and different
    ideas of "too short to be real", and the mapping says which is which.

    Synthetic, like the rest of this file: it never names a real provider, so a
    provider changing its mapping cannot fail another provider's refresh. Each
    provider's own tests assert what its own mapping declares.
    """

    def fetcher(self, mapping: JSONDict) -> object:
        args = argparse.Namespace(offline=None, html=None)
        return provider_runner.build_fetcher(args, mapping)

    def calls(self, mapping: JSONDict) -> list[tuple[str, dict[str, object]]]:
        """Run the fetcher with the network replaced, and report how it asked."""
        seen: list[tuple[str, dict[str, object]]] = []

        def spy(url: str, **kwargs: object) -> str:
            seen.append((url, kwargs))
            return "{}"

        original = provider_runner.fetch_page
        provider_runner.fetch_page = spy  # type: ignore[assignment]
        try:
            self.fetcher(mapping)("https://example.test/prices")
        finally:
            provider_runner.fetch_page = original  # type: ignore[assignment]
        return seen

    def test_a_page_is_asked_for_as_html_and_must_be_big(self) -> None:
        (_, kwargs), = self.calls({"source": "https://example.test/prices", "format": "html"})
        self.assertEqual(kwargs["accept"], "text/html")
        self.assertEqual(kwargs["min_bytes"], fetch.MIN_PAGE_BYTES)

    def test_an_endpoint_is_asked_for_as_json_and_may_be_small(self) -> None:
        """The point of the whole field: a JSON body is data and is legitimately far
        smaller than a page, so the page floor would reject a perfectly good answer."""
        (_, kwargs), = self.calls({"source": "https://example.test/prices", "format": "json"})
        self.assertEqual(kwargs["accept"], "application/json")
        self.assertEqual(kwargs["min_bytes"], fetch.MIN_JSON_BYTES)
        self.assertLess(kwargs["min_bytes"], fetch.MIN_PAGE_BYTES)

    def test_an_undeclared_source_is_treated_as_a_page(self) -> None:
        """The strict end of both, on purpose: a source someone forgot to declare
        fails on the size floor rather than passing quietly."""
        (_, kwargs), = self.calls({"source": "https://example.test/prices"})
        self.assertEqual(kwargs["accept"], "text/html")
        self.assertEqual(kwargs["min_bytes"], fetch.MIN_PAGE_BYTES)

    def test_an_unknown_format_is_refused_before_anything_is_fetched(self) -> None:
        with self.assertRaises(provider_runner.ScrapeError) as caught:
            self.fetcher({"source": "https://example.test/prices", "format": "xml"})
        self.assertIn("xml", str(caught.exception))

    def test_a_fixture_is_served_whatever_the_format_says(self) -> None:
        """Offline runs read committed bytes; the header and the floor are the
        network's business and must not change what a fixture serves."""
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "payload.json"
            fixture.write_text('{"tiny": true}', encoding="utf-8")
            manifest = Path(tmp) / "offline.json"
            manifest.write_text(
                json.dumps({"https://example.test/prices": "payload.json"}), encoding="utf-8"
            )
            args = argparse.Namespace(offline=str(manifest), html=None)
            fetch_it = provider_runner.build_fetcher(
                args, {"source": "https://example.test/prices", "format": "json"}
            )
            self.assertEqual(fetch_it("https://example.test/prices"), '{"tiny": true}')


if __name__ == "__main__":
    unittest.main()
