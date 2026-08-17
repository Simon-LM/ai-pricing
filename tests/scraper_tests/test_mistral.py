"""Tests for the Mistral scraper, and for what happens when things break.

Standard library only: `python3 -m unittest discover tests`.

Every scenario asserts the same thing at the end, through `assert_current_untouched`:
pricing.json is byte-for-byte what it was. That is the property the whole design
exists to protect -- a stale price whose date the user can see is far better than a
wrong one they cannot.

The failure scenarios are derived from tests/fixtures/mistral/page_ok.html, which is
real captured markup, by explicit mutation. `mutate` refuses to run when there was
nothing to replace, so a fixture that drifts away from the page fails the tests
loudly instead of quietly testing nothing.

Cross-file consistency checks -- does providers.mistral in the committed pricing.json
actually match scripts/providers/mistral/mapping.json -- live here too: they are
statements about Mistral specifically, not about the shared schema.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# Imported via the providers.mistral package, not a flat `import scrape` off a
# directly-inserted directory: OVH's scraper is also a module literally named
# scrape.py, and a flat import would collide in sys.modules the moment both test
# files run in the same process (as `unittest discover` does) -- whichever loads
# first wins the name, and the other file silently gets the wrong module.
from providers.mistral import scrape  # noqa: E402
import pricing_validate as validate  # noqa: E402
from pricing_validate import JSONDict  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "mistral"
PAGE_OK = FIXTURES / "page_ok.html"

# The scrape scenarios compare the fixture page against a baseline pinned to that
# same page, never against the live pricing.json. Otherwise every genuine price
# change would fail the test suite until somebody re-captured the fixture, which
# would put friction on exactly the path that must stay quick: a price correction
# reaching consumers. The two only move together when the fixture is
# deliberately re-captured.
BASELINE_JSON = FIXTURES / "baseline.json"

PRICING_JSON = REPO_ROOT / "pricing.json"
MAPPING_JSON = REPO_ROOT / "scripts" / "providers" / "mistral" / "mapping.json"

FIXED_NOW = "2026-08-03T04:00:00Z"


def mutate(source: str, old: str, new: str, expected_count: int | None = 1) -> str:
    """Replace `old` with `new`, checking first that there was something to replace.

    `expected_count` is an exact number where uniqueness is the point of the test --
    a price mutation that silently hit two rows would prove nothing. Pass None for
    structural mutations that sweep the whole page, where the count is incidental and
    changes every time a card is added to the fixture; those still refuse to run
    against zero matches, which is the failure that would leave a test testing nothing.
    """
    found = source.count(old)
    if found == 0 or (expected_count is not None and found != expected_count):
        raise AssertionError(
            f"fixture drift: expected {expected_count or 'at least one'} occurrence(s) of "
            f"{old!r}, found {found}. Re-capture tests/fixtures/mistral/page_ok.html from the live page."
        )
    return source.replace(old, new)


def card_span(source: str, data_name: str) -> tuple[int, int]:
    """The exact character range one model card occupies in the page."""
    anchor = source.index(f'data-name="{data_name}"')
    start = source.rindex('<div class="model-item', 0, anchor)
    end = source.index("</mistral-block-card-model>", start) + len("</mistral-block-card-model>")
    end = source.index("</div>", end) + len("</div>")
    return start, end


def drop_card(source: str, data_name: str) -> str:
    """Remove one whole model card from the page, as if the model had been withdrawn."""
    start, end = card_span(source, data_name)
    return source[:start] + source[end:]


def mutate_in_card(source: str, data_name: str, old: str, new: str, expected_count: int | None = 1) -> str:
    """Replace `old` with `new` inside ONE named card, not across the whole page.

    The fixture holds all 25 cards of the real page, so an ordinary figure like 1.5
    is on several of them at once. A test that means "move Mistral Medium's input
    price" has to say which card, or it quietly rewrites three unrelated models and
    stops testing the thing its name claims.
    """
    start, end = card_span(source, data_name)
    card = source[start:end]
    found = card.count(old)
    if found == 0 or (expected_count is not None and found != expected_count):
        raise AssertionError(
            f"fixture drift: expected {expected_count or 'at least one'} occurrence(s) of "
            f"{old!r} inside the {data_name!r} card, found {found}. Re-capture "
            f"tests/fixtures/mistral/page_ok.html from the live page."
        )
    return source[:start] + card.replace(old, new) + source[end:]


class ScrapeTestCase(unittest.TestCase):
    """Base class: gives each test a private copy of pricing.json and an output dir."""

    maxDiff = None

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        self.out_dir = self.tmp / "out"
        self.current = self.tmp / "pricing.json"
        shutil.copy2(BASELINE_JSON, self.current)
        self.current_bytes = self.current.read_bytes()

        self.page = self.tmp / "page.html"

    # -- helpers ------------------------------------------------------------------

    def run_scrape(self, html: str, now: str = FIXED_NOW) -> tuple[int, str, str]:
        """Run the script end to end on `html`. Returns (exit_code, stdout, stderr)."""
        self.page.write_text(html, encoding="utf-8")
        argv = [
            "--out-dir", str(self.out_dir),
            "--current", str(self.current),
            "--mapping", str(MAPPING_JSON),
            "--html", str(self.page),
            "--now", now,
        ]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scrape.main(argv)
        return code, out.getvalue(), err.getvalue()

    def assert_current_untouched(self) -> None:
        self.assertEqual(
            self.current.read_bytes(),
            self.current_bytes,
            "pricing.json was modified -- the scraper must never write it",
        )

    def assert_failed(self, code: int, stderr: str, *expected_fragments: str) -> None:
        self.assertEqual(code, 1, f"expected a failing exit code, got {code}")
        self.assertFalse((self.out_dir / "stamped.json").exists(), "published a stamp on failure")
        self.assertFalse((self.out_dir / "updated.json").exists(), "published figures on failure")
        self.assertTrue((self.out_dir / "error.txt").exists(), "failed without reporting why")
        for fragment in expected_fragments:
            self.assertIn(fragment, stderr)
        self.assert_current_untouched()

    def notes(self) -> str:
        return (self.out_dir / "notes.txt").read_text(encoding="utf-8")

    def candidate(self, name: str) -> JSONDict:
        return json.loads((self.out_dir / name).read_text(encoding="utf-8"))

    def candidate_block(self, name: str) -> JSONDict:
        return self.candidate(name)["providers"]["mistral"]

    @property
    def page_ok(self) -> str:
        return PAGE_OK.read_text(encoding="utf-8")


# ======================================================================================
# Outcome 1: figures unchanged
# ======================================================================================


class TestUnchanged(ScrapeTestCase):
    def test_unchanged_page_stamps_and_publishes_nothing_new(self) -> None:
        code, stdout, _ = self.run_scrape(self.page_ok)

        self.assertEqual(code, 0)
        self.assertIn("unchanged", stdout)
        self.assertFalse((self.out_dir / "updated.json").exists())

        stamped = self.candidate_block("stamped.json")
        published = json.loads(self.current_bytes)["providers"]["mistral"]

        self.assertEqual(stamped["checked_utc"], FIXED_NOW, "checked_utc was not refreshed")
        self.assertEqual(
            stamped["updated"],
            published["updated"],
            "updated must not move when no figure moved",
        )
        self.assertEqual(stamped["models"], published["models"])
        self.assert_current_untouched()

    def test_baseline_matches_the_fixture_page(self) -> None:
        """The fixture and its baseline are two views of the same capture; keep them so."""
        code, stdout, _ = self.run_scrape(self.page_ok)
        self.assertEqual(code, 0)
        self.assertIn("unchanged", stdout)

    def test_ocr_price_is_not_read_from_the_libraries_card(self) -> None:
        """The decoy: 'libraries' also has an OCR row, at $3 per 1K pages."""
        self.run_scrape(self.page_ok)
        models = self.candidate_block("stamped.json")["models"]
        self.assertEqual(models["ocr 4.1 / ocr"]["per_1k_pages"], 4.0)
        self.assertEqual(models["libraries"]["per_1k_pages"], 3.0)

    def test_two_rows_of_one_card_in_the_same_unit_become_two_entries(self) -> None:
        """The 'ocr 4.1' card prices OCR at $4 and Document AI at $5, both per 1000
        pages. One entry has exactly one per_1k_pages field, so they cannot share one --
        and both are keyed by their row label rather than the first one keeping the
        plain card name, so the two cannot swap the day Mistral reorders the rows."""
        self.run_scrape(self.page_ok)
        models = self.candidate_block("stamped.json")["models"]

        self.assertEqual(models["ocr 4.1 / ocr"]["per_1k_pages"], 4.0)
        self.assertEqual(models["ocr 4.1 / document ai"]["per_1k_pages"], 5.0)
        self.assertNotIn("ocr 4.1", models, "the plain card key would be ambiguous here")
        self.assertEqual(models["ocr 4.1 / document ai"]["kind"], "product")
        self.assertNotIn("kind", models["ocr 4.1 / ocr"])

    def test_a_model_billed_in_two_units_keeps_both(self) -> None:
        """'voxtral small' is billed per minute of audio AND per million tokens. Code
        that assumes one unit per model truncates it, and the truncation is silent."""
        self.run_scrape(self.page_ok)
        entry = self.candidate_block("stamped.json")["models"]["voxtral small"]

        self.assertEqual(entry["per_audio_minute"], 0.004)
        self.assertEqual(entry["in_per_mtok"], 0.1)
        self.assertEqual(entry["out_per_mtok"], 0.4)

    def test_a_cached_input_price_is_published_under_its_own_field(self) -> None:
        """'glm 5.2' states three prices, not two. Folding a cached-read rate into
        in_per_mtok would understate every ordinary input token by a factor of ten."""
        self.run_scrape(self.page_ok)
        entry = self.candidate_block("stamped.json")["models"]["glm 5.2"]
        self.assertEqual(entry["in_per_mtok"], 1.4)
        self.assertEqual(entry["cache_read_per_mtok"], 0.14)
        self.assertEqual(entry["out_per_mtok"], 4.4)

    def test_every_priced_card_reaches_the_output(self) -> None:
        """There is no list to reconcile: every card the page states a price for is
        published, keyed by the card name the page states. A card quietly dropping out
        between the page and the file is the one failure mode this repository must not
        have."""
        self.run_scrape(self.page_ok)
        cards = scrape.parse_page(self.page_ok)
        priced = {name for name, rows in cards.items() if rows}
        published = set(self.candidate_block("stamped.json")["models"])

        # 'ocr 4.1' is the one card that becomes two entries; every other priced card
        # is one entry under its own name.
        self.assertEqual(published - {"ocr 4.1 / ocr", "ocr 4.1 / document ai"}, priced - {"ocr 4.1"})

    def test_other_providers_blocks_are_never_touched(self) -> None:
        """The entire reason pricing.json nests under providers.<name>: a run that
        scrapes Mistral must carry any other provider's block through byte-for-byte,
        never read it, never rewrite it -- not even to reformat it."""
        doc = json.loads(self.current_bytes)
        doc["providers"]["acme"] = {
            "checked_utc": "2020-01-01T00:00:00Z",
            "updated": "2020-01-01",
            "source": "https://example.invalid",
            "currency": "JPY",
            "models": {"weird-model": {"in_per_mtok": 42.0, "display_name": "Weird"}},
        }
        self.current.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()

        self.run_scrape(self.page_ok)

        candidate_providers = self.candidate("stamped.json")["providers"]
        self.assertEqual(candidate_providers["acme"], doc["providers"]["acme"])

        # Order matters too, not just value equality: a merge that rebuilt the
        # providers dict as "everyone else, then mistral" would leave every value
        # equal while still turning ["mistral", "acme"] into ["acme", "mistral"],
        # which shows up as the whole acme block being removed-and-re-added in the
        # diff a human reviews. Dict == ignores order; this assertion does not.
        self.assertEqual(
            list(candidate_providers),
            list(doc["providers"]),
            "providers were reordered even though only mistral changed",
        )


# ======================================================================================
# Outcome 2: a figure changed
# ======================================================================================


class TestPriceChange(ScrapeTestCase):
    def test_normal_price_change_produces_a_candidate_not_a_publication(self) -> None:
        html = mutate_in_card(self.page_ok, "mistral medium 3.5", "&quot;priceUsd&quot;:1.5", "&quot;priceUsd&quot;:2.25")

        code, stdout, _ = self.run_scrape(html)

        self.assertEqual(code, 0)
        self.assertIn("changed", stdout)
        self.assertIn("mistral medium 3.5.in_per_mtok", stdout)
        self.assertIn("1.5", stdout)
        self.assertIn("2.25", stdout)

        updated = self.candidate_block("updated.json")
        self.assertEqual(updated["models"]["mistral medium 3.5"]["in_per_mtok"], 2.25)
        self.assertEqual(updated["checked_utc"], FIXED_NOW)
        self.assertEqual(updated["updated"], FIXED_NOW[:10], "updated must move with the figure")
        self.assertEqual(
            updated["models"]["mistral medium 3.5"]["out_per_mtok"],
            7.5,
            "an unrelated figure was disturbed",
        )

        # The stamp is still produced: the keepalive commit happens on every run.
        stamped = self.candidate_block("stamped.json")
        self.assertEqual(stamped["models"], json.loads(self.current_bytes)["providers"]["mistral"]["models"])

        self.assert_current_untouched()

    def test_a_price_going_down_is_also_a_change(self) -> None:
        html = mutate_in_card(self.page_ok, "ocr 4.1", "&quot;priceUsd&quot;:4,", "&quot;priceUsd&quot;:2,")
        code, stdout, _ = self.run_scrape(html)
        self.assertEqual(code, 0)
        self.assertIn("ocr 4.1 / ocr.per_1k_pages", stdout)
        self.assertEqual(self.candidate_block("updated.json")["models"]["ocr 4.1 / ocr"]["per_1k_pages"], 2.0)
        self.assert_current_untouched()

    def test_a_renamed_card_publishes_the_new_name_and_keeps_the_old(self) -> None:
        """The key IS the card name, so a rename is two facts at once: an entry exists
        under a new name, and the old name is not on the page any more. Both are
        published -- the new key with today's price, the old one frozen and dated.

        This is the case that failed the real job: Mistral renamed six cards in one
        week, and refusing to publish until a human reconciled a list held back
        twenty-two prices that had nothing to do with the renames."""
        html = mutate(self.page_ok, 'data-name="mistral medium 3.5"', 'data-name="mistral medium 4"')
        code, _, _ = self.run_scrape(html)
        self.assertEqual(code, 0)

        models = self.candidate_block("updated.json")["models"]
        self.assertEqual(models["mistral medium 4"]["in_per_mtok"], 1.5)
        self.assertNotIn("absent_since", models["mistral medium 4"])
        self.assertEqual(models["mistral medium 3.5"]["absent_since"], FIXED_NOW[:10])
        self.assertEqual(models["mistral medium 3.5"]["in_per_mtok"], 1.5)
        self.assertIn("no longer offered", self.notes())
        self.assert_current_untouched()


# ======================================================================================
# Outcome 3: fetch or parse failed
# ======================================================================================


class TestLayoutFailures(ScrapeTestCase):
    def test_page_whose_layout_no_longer_parses(self) -> None:
        html = mutate(self.page_ok, 'class="model-item', 'class="product-tile', expected_count=None)
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "no model card found")

    def test_price_markup_replaced_by_plain_text(self) -> None:
        html = mutate(self.page_ok, " data-prices=", " data-figures=", expected_count=None)
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "without data-prices")

    def test_an_unrecognised_row_label_is_reported_and_never_guessed_at(self) -> None:
        """There is no honest way to name the unit of a figure whose label this file
        does not recognise, so that row is not published. The rows that WERE recognised
        still are: a renamed input label says nothing about whether the output price
        beside it was read correctly."""
        html = mutate(self.page_ok, "Input (/M tokens)", "Prompt (/M tokens)", expected_count=None)
        code, _, _ = self.run_scrape(html)
        self.assertEqual(code, 0)

        entry = self.candidate_block("updated.json")["models"]["mistral medium 3.5"]
        self.assertNotIn("in_per_mtok", entry)
        self.assertEqual(entry["out_per_mtok"], 7.5)
        self.assertIn("Prompt (/M tokens)", self.notes())
        self.assertIn("has no field for that label", self.notes())

    def test_empty_page(self) -> None:
        code, _, stderr = self.run_scrape("<html><body></body></html>")
        self.assert_failed(code, stderr, "no model card found")

    def test_unparseable_price_json(self) -> None:
        html = mutate(
            self.page_ok,
            "&quot;priceEur&quot;:1.25,&quot;priceUsd&quot;:1.5",
            "&quot;priceEur&quot;:1.25 &quot;priceUsd&quot;:1.5",
        )
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "not JSON")

    def test_missing_usd_figure_is_reported_and_not_converted(self) -> None:
        """This file publishes USD and performs no conversion anywhere. A row that stops
        stating a USD figure has no publishable price, whatever else it states."""
        html = mutate_in_card(self.page_ok, "mistral medium 3.5", "&quot;priceUsd&quot;:1.5", "&quot;priceGbp&quot;:1.5")
        code, _, _ = self.run_scrape(html)
        self.assertEqual(code, 0)

        entry = self.candidate_block("updated.json")["models"]["mistral medium 3.5"]
        self.assertNotIn("in_per_mtok", entry)
        self.assertEqual(entry["out_per_mtok"], 7.5)
        self.assertIn("could not be read as stated", self.notes())


class TestWithdrawnCard(ScrapeTestCase):
    """A card the page stops showing. This is the case that used to fail the whole job,
    blocking correct prices for everything else until a human edited a list."""

    def without_codestral(self) -> str:
        html = drop_card(self.page_ok, "codestral")
        self.assertNotIn('data-name="codestral"', html)
        return html

    def test_it_is_kept_with_its_last_prices_and_a_date(self) -> None:
        code, _, _ = self.run_scrape(self.without_codestral())
        self.assertEqual(code, 0)

        entry = self.candidate_block("updated.json")["models"]["codestral"]
        published = json.loads(self.current_bytes)["providers"]["mistral"]["models"]["codestral"]
        self.assertEqual(entry["absent_since"], FIXED_NOW[:10])
        self.assertEqual(entry["in_per_mtok"], published["in_per_mtok"])
        self.assertEqual(entry["out_per_mtok"], published["out_per_mtok"])

    def test_every_other_card_is_still_published(self) -> None:
        """The point of the whole change: one model going away is not a reason to
        withhold every other price on the page."""
        self.run_scrape(self.without_codestral())
        models = self.candidate_block("updated.json")["models"]
        still_offered = [m for m in models if "absent_since" not in models[m]]
        self.assertEqual(len(still_offered), 22)
        self.assertEqual(models["mistral medium 3.5"]["in_per_mtok"], 1.5)

    def test_it_is_reported_once_and_not_again(self) -> None:
        self.run_scrape(self.without_codestral())
        self.assertIn("codestral", self.notes())
        self.assertIn("365 days", self.notes())

        self.current.write_text(
            (self.out_dir / "updated.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.current_bytes = self.current.read_bytes()
        code, stdout, _ = self.run_scrape(self.without_codestral(), now="2026-08-10T04:00:00Z")

        self.assertEqual(code, 0)
        self.assertIn("unchanged", stdout)
        self.assertEqual(self.notes(), "", "the same disappearance was reported twice")

    def test_it_is_dropped_after_a_year_and_not_before(self) -> None:
        self.run_scrape(self.without_codestral())
        aged = json.loads((self.out_dir / "updated.json").read_text(encoding="utf-8"))
        aged["providers"]["mistral"]["models"]["codestral"]["absent_since"] = "2025-08-03"
        self.current.write_text(json.dumps(aged, indent=2) + "\n", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()

        self.run_scrape(self.without_codestral(), now="2026-08-02T04:00:00Z")
        self.assertIn("codestral", self.candidate_block("stamped.json")["models"])

        code, _, _ = self.run_scrape(self.without_codestral(), now="2026-08-04T04:00:00Z")
        self.assertEqual(code, 0)
        self.assertNotIn("codestral", self.candidate_block("updated.json")["models"])
        self.assertIn("Dropped from the file", self.notes())

    def test_a_card_that_comes_back_loses_the_marker(self) -> None:
        self.run_scrape(self.without_codestral())
        self.current.write_text(
            (self.out_dir / "updated.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.current_bytes = self.current.read_bytes()

        code, _, _ = self.run_scrape(self.page_ok, now="2026-08-10T04:00:00Z")
        self.assertEqual(code, 0)

        entry = self.candidate_block("updated.json")["models"]["codestral"]
        self.assertNotIn("absent_since", entry)
        self.assertEqual(entry["in_per_mtok"], 0.3)
        self.assertIn("offered again", self.notes())


class TestSanityBounds(ScrapeTestCase):
    def test_out_of_bounds_figure_is_refused(self) -> None:
        html = mutate_in_card(self.page_ok, "mistral medium 3.5", "&quot;priceUsd&quot;:1.5", "&quot;priceUsd&quot;:4000")
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "outside the plausible range")

    def test_figure_below_the_floor_is_refused(self) -> None:
        html = mutate_in_card(self.page_ok, "mistral medium 3.5", "&quot;priceUsd&quot;:1.5", "&quot;priceUsd&quot;:0")
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "outside the plausible range")

    def test_plausible_figure_that_moved_implausibly_is_refused(self) -> None:
        """20 is a perfectly ordinary price. Going 1.5 -> 20 in one week is not."""
        html = mutate_in_card(self.page_ok, "mistral medium 3.5", "&quot;priceUsd&quot;:1.5", "&quot;priceUsd&quot;:20")
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "factor of", "Refusing to publish")

    def test_a_change_just_under_the_factor_limit_is_allowed_through(self) -> None:
        html = mutate_in_card(self.page_ok, "mistral medium 3.5", "&quot;priceUsd&quot;:1.5", "&quot;priceUsd&quot;:6")
        code, stdout, _ = self.run_scrape(html)
        self.assertEqual(code, 0)
        self.assertIn("changed", stdout)
        self.assert_current_untouched()

    def test_unit_change_on_the_page_is_reported_and_never_published(self) -> None:
        """per_1k_pages must not keep its name if the page stops meaning 1000 pages. The
        figure is dropped rather than republished under a field name that would now be a
        lie, and the two entries that lose their only price go absent like any other."""
        html = mutate(
            self.page_ok,
            "&quot;suffix&quot;:&quot;/ 1000 pages&quot;",
            "&quot;suffix&quot;:&quot;/ 100 pages&quot;",
            expected_count=2,
        )
        code, _, _ = self.run_scrape(html)
        self.assertEqual(code, 0)

        models = self.candidate_block("updated.json")["models"]
        self.assertIn("/ 100 pages", self.notes())
        self.assertIn("no price on this card could be published", self.notes())
        for key in ("ocr 4.1 / ocr", "ocr 4.1 / document ai"):
            self.assertEqual(models[key]["absent_since"], FIXED_NOW[:10])
        self.assertEqual(models["mistral medium 3.5"]["in_per_mtok"], 1.5)


class TestCorruptInputs(ScrapeTestCase):
    def test_invalid_committed_pricing_json_stops_everything(self) -> None:
        self.current.write_text(
            json.dumps({"schema_version": validate.SCHEMA_VERSION, "providers": {}}), encoding="utf-8"
        )
        self.current_bytes = self.current.read_bytes()
        code, _, stderr = self.run_scrape(self.page_ok)
        self.assert_failed(code, stderr, "committed pricing.json is invalid")

    def test_unparseable_committed_pricing_json_stops_everything(self) -> None:
        self.current.write_text("{ not json", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()
        code, _, stderr = self.run_scrape(self.page_ok)
        self.assert_failed(code, stderr, "not valid JSON")

    def test_a_missing_mistral_block_stops_everything(self) -> None:
        """This job updates an existing provider's figures; it does not decide, on
        its own, to start publishing a provider that was never seeded by a human."""
        doc = json.loads(self.current_bytes)
        del doc["providers"]["mistral"]
        doc["providers"]["placeholder"] = {
            "checked_utc": "2026-08-03T04:00:00Z",
            "updated": "2026-08-03",
            "source": "https://example.invalid",
            "currency": "USD",
            "models": {"x": {"in_per_mtok": 1.0, "display_name": "X"}},
        }
        self.current.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()

        code, _, stderr = self.run_scrape(self.page_ok)
        self.assert_failed(code, stderr, "no providers.mistral block")

    def test_a_published_model_the_page_never_shows_is_kept_not_dropped(self) -> None:
        """Something in the file that the page has never shown is not deleted on sight.
        It is dated and kept for a year, like anything else that goes away."""
        published = json.loads(self.current_bytes)
        published["providers"]["mistral"]["models"]["mistral retired legacy"] = {
            "in_per_mtok": 0.5,
            "out_per_mtok": 1.5,
            "display_name": "mistral retired legacy",
        }
        self.current.write_text(json.dumps(published, indent=2) + "\n", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()

        code, _, _ = self.run_scrape(self.page_ok)
        self.assertEqual(code, 0)

        entry = self.candidate_block("updated.json")["models"]["mistral retired legacy"]
        self.assertEqual(entry["absent_since"], FIXED_NOW[:10])
        self.assertEqual(entry["in_per_mtok"], 0.5)
        self.assertIn("mistral retired legacy", self.notes())
        self.assert_current_untouched()


# ======================================================================================
# Cross-file consistency: does providers.mistral agree with Mistral's own files?
# ======================================================================================


class TestPublishedFileMatchesMistral(unittest.TestCase):
    def base(self) -> JSONDict:
        return json.loads(PRICING_JSON.read_text(encoding="utf-8"))

    def mistral_block(self) -> JSONDict:
        return self.base()["providers"]["mistral"]

    def test_the_committed_file_is_valid(self) -> None:
        validate.validate_document(self.base())

    def test_the_fixture_baseline_is_valid(self) -> None:
        validate.validate_document(json.loads(BASELINE_JSON.read_text(encoding="utf-8")))

    def test_the_baseline_has_the_same_shape_as_the_published_file(self) -> None:
        """Figures may differ -- the baseline is pinned to its own capture -- but the
        set of entries still on sale, and the units they are priced in, may not drift
        apart unnoticed. Entries marked `absent_since` are excluded on both sides: the
        published file keeps a withdrawn model for a year and the fixture, being a
        single capture, has no way to know about one."""
        published = {
            k: v for k, v in self.mistral_block()["models"].items() if "absent_since" not in v
        }
        baseline = json.loads(BASELINE_JSON.read_text(encoding="utf-8"))["providers"]["mistral"]["models"]
        baseline = {k: v for k, v in baseline.items() if "absent_since" not in v}

        self.assertEqual(set(published), set(baseline))
        for model_id, entry in published.items():
            self.assertEqual(
                {k for k in entry if k in validate.KNOWN_PRICE_FIELDS},
                {k for k in baseline[model_id] if k in validate.KNOWN_PRICE_FIELDS},
                model_id,
            )

    def test_the_committed_file_matches_the_mapping(self) -> None:
        """There is no model list to agree with any more, so what is checked is what the
        mapping still decides: where the figures come from, what currency they are in,
        and which price fields the rows table can produce."""
        published = self.mistral_block()
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))

        self.assertEqual(published["source"], mapping["source"])
        self.assertEqual(published["currency"], mapping["currency"])

        producible = {spec["field"] for spec in mapping["rows"].values()}
        for model_id, entry in published["models"].items():
            fields = {k for k in entry if k in validate.KNOWN_PRICE_FIELDS}
            self.assertTrue(fields, model_id)
            self.assertLessEqual(fields, producible, model_id)

    def test_every_key_is_a_card_name_the_page_states(self) -> None:
        """The keys are page card names, deliberately not API model ids. An API-shaped
        key creeping back in would mean somebody had started translating names by hand
        again, which is the thing that made this block need a human every week."""
        for model_id, entry in self.mistral_block()["models"].items():
            self.assertEqual(model_id, entry["display_name"], model_id)
            self.assertEqual(model_id, model_id.lower(), model_id)

    def test_the_published_audio_prices_have_room_to_fall(self) -> None:
        """Guards the floor against the figures actually published: if a price ever sits
        too close to it, a real price cut starts failing the job instead of being
        published. Caught here rather than on the Monday it happens."""
        published = self.mistral_block()["models"]
        for model_id, entry in published.items():
            for field, value in entry.items():
                if field not in validate.KNOWN_PRICE_FIELDS:
                    continue
                low, _ = validate.PRICE_BOUNDS.get(
                    field, (validate.MIN_PLAUSIBLE, validate.MAX_PLAUSIBLE)
                )
                self.assertGreaterEqual(
                    value / low,
                    validate.MAX_CHANGE_FACTOR,
                    f"{model_id}.{field} = {value} sits less than a factor of "
                    f"{validate.MAX_CHANGE_FACTOR} above its floor of {low}; a legitimate "
                    f"price cut would be refused as out of bounds. Lower the floor for "
                    f"this unit in pricing_validate.PRICE_BOUNDS.",
                )


# ======================================================================================
# The mapping is a document a human maintains, so check it stays legible
# ======================================================================================


class TestMapping(unittest.TestCase):
    def mapping(self) -> JSONDict:
        return json.loads(MAPPING_JSON.read_text(encoding="utf-8"))

    def test_there_is_no_model_list(self) -> None:
        """Load-bearing, not pedantry. A hand-written model list is exactly what made a
        week of renames block twenty-two correct prices, and re-adding one would bring
        that back without anything else in the tests noticing."""
        self.assertNotIn("models", self.mapping())

    def test_every_mapped_row_names_a_known_price_field(self) -> None:
        for label, spec in self.mapping()["rows"].items():
            self.assertIn(spec["field"], validate.KNOWN_PRICE_FIELDS, label)
            if "kind" in spec:
                self.assertIn(spec["kind"], validate.KNOWN_KINDS, label)

    def test_every_row_label_on_the_page_is_mapped(self) -> None:
        """Catches the gap on the day the fixture is re-captured rather than on the
        Monday a price silently stops being published."""
        rows_table = self.mapping()["rows"]
        cards = scrape.parse_page(PAGE_OK.read_text(encoding="utf-8"))
        for card_name, rows in cards.items():
            for label, _ in rows:
                self.assertIsNotNone(
                    scrape.resolve_field(label, rows_table), f"{card_name}: {label!r}"
                )

    def test_a_prefix_never_shadows_a_longer_exact_label(self) -> None:
        """'OCR' is in the table and so is 'OCR (per 1K pages)'. Longest match wins, and
        an exact match wins outright -- otherwise the libraries card's per-1K-pages row
        would be read through the OCR model's entry, which expects a different suffix."""
        rows_table = self.mapping()["rows"]
        matched, _ = scrape.resolve_field("OCR (per 1K pages)", rows_table)
        self.assertEqual(matched, "OCR (per 1K pages)")

    def test_products_are_card_names_that_exist_on_the_page(self) -> None:
        """The list is an annotation and gates nothing, so a stale name cannot break a
        run -- but it can quietly stop marking anything, which is worth catching."""
        cards = set(scrape.parse_page(PAGE_OK.read_text(encoding="utf-8")))
        self.assertLessEqual(set(self.mapping()["products"]), cards)


if __name__ == "__main__":
    unittest.main()
