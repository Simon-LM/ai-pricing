"""Tests for the scraper, the validator, and above all for what happens when things break.

Standard library only: `python3 -m unittest discover tests`.

Every scenario asserts the same thing at the end, through `assert_current_untouched`:
pricing.json is byte-for-byte what it was. That is the property the whole design
exists to protect -- a stale price whose date the user can see is far better than a
wrong one they cannot.

The failure scenarios are derived from tests/fixtures/page_ok.html, which is real
captured markup, by explicit mutation. `mutate` refuses to run when there was nothing
to replace, so a fixture that drifts away from the page fails the tests loudly instead
of quietly testing nothing.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import scrape  # noqa: E402
import validate  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PAGE_OK = FIXTURES / "page_ok.html"

# The scrape scenarios compare the fixture page against a baseline pinned to that
# same page, never against the live pricing.json. Otherwise every genuine price
# change would fail the test suite until somebody re-captured the fixture, which
# would put friction on exactly the path that must stay quick: a reviewed price
# correction reaching consumers. The two only move together when the fixture is
# deliberately re-captured.
BASELINE_JSON = FIXTURES / "baseline.json"

PRICING_JSON = REPO_ROOT / "pricing.json"
MAPPING_JSON = REPO_ROOT / "scripts" / "mapping.json"

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
            f"{old!r}, found {found}. Re-capture tests/fixtures/page_ok.html from the live page."
        )
    return source.replace(old, new)


def drop_card(source: str, data_name: str) -> str:
    """Remove one whole model card from the page, as if the model had been withdrawn."""
    anchor = source.index(f'data-name="{data_name}"')
    start = source.rindex('<div class="model-item', 0, anchor)
    end = source.index("</mistral-block-card-model>", start) + len("</mistral-block-card-model>")
    end = source.index("</div>", end) + len("</div>")
    return source[:start] + source[end:]


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

    def candidate(self, name: str) -> dict:
        return json.loads((self.out_dir / name).read_text(encoding="utf-8"))

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

        stamped = self.candidate("stamped.json")
        published = json.loads(self.current_bytes)

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
        models = self.candidate("stamped.json")["models"]
        self.assertEqual(models["mistral-ocr-latest"]["per_1k_pages"], 4.0)
        self.assertNotEqual(models["mistral-ocr-latest"]["per_1k_pages"], 3.0)

    def test_voxtral_mini_price_is_not_read_from_the_realtime_card(self) -> None:
        """The second decoy: 'voxtral mini transcribe realtime' has a row labelled
        'Audio Input/min' too, at $0.006 -- double. Same label, different card."""
        self.run_scrape(self.page_ok)
        models = self.candidate("stamped.json")["models"]
        self.assertEqual(models["voxtral-mini-latest"]["per_audio_minute"], 0.003)
        self.assertNotEqual(models["voxtral-mini-latest"]["per_audio_minute"], 0.006)

    def test_a_model_billed_in_two_units_keeps_both(self) -> None:
        """voxtral-small is billed per minute of audio AND per million tokens. Code
        that assumes one unit per model truncates it, and the truncation is silent."""
        self.run_scrape(self.page_ok)
        entry = self.candidate("stamped.json")["models"]["voxtral-small-latest"]

        self.assertEqual(entry["per_audio_minute"], 0.004)
        self.assertEqual(entry["in_per_mtok"], 0.1)
        self.assertEqual(entry["out_per_mtok"], 0.4)

    def test_every_mapped_model_reaches_the_output(self) -> None:
        """Adding a model to the mapping without it appearing here would be a silent
        omission, which is the one failure mode this repository must not have."""
        self.run_scrape(self.page_ok)
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
        self.assertEqual(
            set(self.candidate("stamped.json")["models"]), set(mapping["models"])
        )


# ======================================================================================
# Outcome 2: a figure changed
# ======================================================================================


class TestPriceChange(ScrapeTestCase):
    def test_normal_price_change_produces_a_candidate_not_a_publication(self) -> None:
        html = mutate(self.page_ok, "&quot;priceUsd&quot;:1.5", "&quot;priceUsd&quot;:2.25")

        code, stdout, _ = self.run_scrape(html)

        self.assertEqual(code, 0)
        self.assertIn("changed", stdout)
        self.assertIn("mistral-medium-latest.in_per_mtok", stdout)
        self.assertIn("1.5", stdout)
        self.assertIn("2.25", stdout)

        updated = self.candidate("updated.json")
        self.assertEqual(updated["models"]["mistral-medium-latest"]["in_per_mtok"], 2.25)
        self.assertEqual(updated["checked_utc"], FIXED_NOW)
        self.assertEqual(updated["updated"], FIXED_NOW[:10], "updated must move with the figure")
        self.assertEqual(
            updated["models"]["mistral-medium-latest"]["out_per_mtok"],
            7.5,
            "an unrelated figure was disturbed",
        )

        # The stamp is still produced: the keepalive commit happens on every run.
        stamped = self.candidate("stamped.json")
        self.assertEqual(stamped["models"], json.loads(self.current_bytes)["models"])

        self.assert_current_untouched()

    def test_a_price_going_down_is_also_a_change(self) -> None:
        html = mutate(self.page_ok, "&quot;priceUsd&quot;:4", "&quot;priceUsd&quot;:2")
        code, stdout, _ = self.run_scrape(html)
        self.assertEqual(code, 0)
        self.assertIn("mistral-ocr-latest.per_1k_pages", stdout)
        self.assertEqual(self.candidate("updated.json")["models"]["mistral-ocr-latest"]["per_1k_pages"], 2.0)
        self.assert_current_untouched()

    def test_a_renamed_marketing_name_is_never_absorbed_silently(self) -> None:
        """display_name comes from the mapping, so the page renaming a model is a failure."""
        html = mutate(self.page_ok, 'data-name="mistral medium 3.5"', 'data-name="mistral medium 4"')
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "mistral-medium-latest", "mapping.json")


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

    def test_row_label_renamed(self) -> None:
        html = mutate(self.page_ok, "Input (/M tokens)", "Prompt (/M tokens)", expected_count=2)
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "no row labelled", "'Input (/M tokens)'")

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

    def test_missing_usd_figure(self) -> None:
        html = mutate(self.page_ok, "&quot;priceUsd&quot;:1.5", "&quot;priceGbp&quot;:1.5")
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "no priceUsd", "no conversion")


class TestMissingModel(ScrapeTestCase):
    def test_model_in_the_mapping_but_absent_from_the_page(self) -> None:
        html = drop_card(self.page_ok, "ocr 4")
        self.assertNotIn('data-name="ocr 4"', html)

        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "mistral-ocr-latest", "no model card named 'ocr 4'")

    def test_the_other_models_are_not_published_when_one_is_missing(self) -> None:
        """A partial file is not an acceptable outcome: it is all three or nothing."""
        html = drop_card(self.page_ok, "ocr 4")
        self.run_scrape(html)
        self.assertFalse(any(self.out_dir.glob("*.json")))


class TestSanityBounds(ScrapeTestCase):
    def test_out_of_bounds_figure_is_refused(self) -> None:
        html = mutate(self.page_ok, "&quot;priceUsd&quot;:1.5", "&quot;priceUsd&quot;:4000")
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "outside the plausible range")

    def test_figure_below_the_floor_is_refused(self) -> None:
        html = mutate(self.page_ok, "&quot;priceUsd&quot;:1.5", "&quot;priceUsd&quot;:0")
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "outside the plausible range")

    def test_plausible_figure_that_moved_implausibly_is_refused(self) -> None:
        """20 is a perfectly ordinary price. Going 1.5 -> 20 in one week is not."""
        html = mutate(self.page_ok, "&quot;priceUsd&quot;:1.5", "&quot;priceUsd&quot;:20")
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "factor of", "Refusing to publish")

    def test_a_change_just_under_the_factor_limit_is_allowed_through(self) -> None:
        html = mutate(self.page_ok, "&quot;priceUsd&quot;:1.5", "&quot;priceUsd&quot;:6")
        code, stdout, _ = self.run_scrape(html)
        self.assertEqual(code, 0)
        self.assertIn("changed", stdout)
        self.assert_current_untouched()

    def test_unit_change_on_the_page_is_refused(self) -> None:
        """per_1k_pages must not keep its name if the page stops meaning 1000 pages."""
        html = mutate(
            self.page_ok,
            "&quot;suffix&quot;:&quot;/ 1000 pages&quot;",
            "&quot;suffix&quot;:&quot;/ 100 pages&quot;",
            expected_count=2,
        )
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "unit may have changed")


class TestCorruptInputs(ScrapeTestCase):
    def test_invalid_committed_pricing_json_stops_everything(self) -> None:
        self.current.write_text('{"schema_version": 1, "models": {}}', encoding="utf-8")
        self.current_bytes = self.current.read_bytes()
        code, _, stderr = self.run_scrape(self.page_ok)
        self.assert_failed(code, stderr, "committed pricing.json is invalid")

    def test_unparseable_committed_pricing_json_stops_everything(self) -> None:
        self.current.write_text("{ not json", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()
        code, _, stderr = self.run_scrape(self.page_ok)
        self.assert_failed(code, stderr, "not valid JSON")

    def test_a_published_model_may_not_be_dropped_by_the_job(self) -> None:
        published = json.loads(self.current_bytes)
        published["models"]["mistral-large-latest"] = {
            "in_per_mtok": 0.5,
            "out_per_mtok": 1.5,
            "display_name": "Mistral Large 3",
        }
        self.current.write_text(json.dumps(published, indent=2) + "\n", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()

        code, _, stderr = self.run_scrape(self.page_ok)
        self.assert_failed(code, stderr, "mapping does not know", "mistral-large-latest")


# ======================================================================================
# The validator, on its own
# ======================================================================================


class TestValidator(unittest.TestCase):
    def base(self) -> dict:
        return json.loads(PRICING_JSON.read_text(encoding="utf-8"))

    def test_the_committed_file_is_valid(self) -> None:
        validate.validate_document(self.base())

    def test_the_fixture_baseline_is_valid(self) -> None:
        validate.validate_document(json.loads(BASELINE_JSON.read_text(encoding="utf-8")))

    def test_the_baseline_has_the_same_shape_as_the_published_file(self) -> None:
        """Figures may differ -- the baseline is pinned to an older capture -- but the
        set of models and the units they are priced in may not drift apart unnoticed."""
        published = self.base()
        baseline = json.loads(BASELINE_JSON.read_text(encoding="utf-8"))

        self.assertEqual(set(published["models"]), set(baseline["models"]))
        for model_id, entry in published["models"].items():
            self.assertEqual(
                {k for k in entry if k in validate.KNOWN_PRICE_FIELDS},
                {k for k in baseline["models"][model_id] if k in validate.KNOWN_PRICE_FIELDS},
                model_id,
            )

    def test_the_committed_file_matches_the_mapping(self) -> None:
        published = self.base()
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))

        self.assertEqual(set(published["models"]), set(mapping["models"]))
        self.assertEqual(published["source"], mapping["source"])
        self.assertEqual(published["currency"], mapping["currency"])
        for model_id, entry in published["models"].items():
            self.assertEqual(entry["display_name"], mapping["models"][model_id]["display_name"])
            expected_fields = set(mapping["models"][model_id]["fields"])
            actual_fields = {k for k in entry if k in validate.KNOWN_PRICE_FIELDS}
            self.assertEqual(actual_fields, expected_fields, model_id)

    def test_bounds(self) -> None:
        for bad in (0, -1, 1000.01, 0.0001, float("nan"), float("inf")):
            with self.subTest(value=bad), self.assertRaises(validate.ValidationError):
                validate.check_price("m", "in_per_mtok", bad)
        for good in (0.001, 1.5, 1000.0):
            with self.subTest(value=good):
                validate.check_price("m", "in_per_mtok", good)

    def test_audio_prices_get_a_lower_floor_than_token_prices(self) -> None:
        """A minute of audio costs thousandths of a dollar. Under the global floor of
        0.001 an ordinary price cut would be refused as though the page had broken."""
        validate.check_price("m", "per_audio_minute", 0.0005)
        with self.assertRaises(validate.ValidationError):
            validate.check_price("m", "in_per_mtok", 0.0005)

        # The ceiling is not relaxed: letting a misparse through is the failure that matters.
        with self.assertRaises(validate.ValidationError):
            validate.check_price("m", "per_audio_minute", 1000.01)

    def test_the_published_audio_prices_have_room_to_fall(self) -> None:
        """Guards the floor against the figures actually published: if a price ever sits
        too close to it, a real price cut starts failing the job instead of opening a
        pull request. Caught here rather than on the Monday it happens."""
        published = self.base()
        for model_id, entry in published["models"].items():
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
                    f"this unit in validate.PRICE_BOUNDS.",
                )

    def test_a_boolean_is_not_a_price(self) -> None:
        with self.assertRaises(validate.ValidationError):
            validate.check_price("m", "in_per_mtok", True)

    def test_change_factor(self) -> None:
        validate.check_change("m", "in_per_mtok", 1.5, 7.5)  # exactly x5
        validate.check_change("m", "in_per_mtok", 7.5, 1.5)  # exactly /5
        with self.assertRaises(validate.ValidationError):
            validate.check_change("m", "in_per_mtok", 1.5, 7.51)
        with self.assertRaises(validate.ValidationError):
            validate.check_change("m", "in_per_mtok", 7.51, 1.5)

    def test_unknown_schema_version_is_refused(self) -> None:
        doc = self.base()
        doc["schema_version"] = 2
        with self.assertRaises(validate.ValidationError):
            validate.validate_document(doc)

    def test_generic_price_field_is_forbidden(self) -> None:
        doc = self.base()
        doc["models"]["mistral-medium-latest"]["price"] = 1.5
        with self.assertRaises(validate.ValidationError):
            validate.validate_document(doc)

    def test_a_model_with_no_price_field_is_refused(self) -> None:
        doc = self.base()
        doc["models"]["mistral-medium-latest"] = {"display_name": "Mistral Medium 3.5"}
        with self.assertRaises(validate.ValidationError):
            validate.validate_document(doc)

    def test_date_formats(self) -> None:
        doc = self.base()
        doc["checked_utc"] = "2026-07-30 04:00:00"
        with self.assertRaises(validate.ValidationError):
            validate.validate_document(doc)

        doc = self.base()
        doc["updated"] = "30/07/2026"
        with self.assertRaises(validate.ValidationError):
            validate.validate_document(doc)

    def test_diff_reports_additions_and_removals(self) -> None:
        old = {"a": {"in_per_mtok": 1.0, "display_name": "A"}}
        new = {"b": {"in_per_mtok": 2.0, "display_name": "B"}}
        lines = validate.diff_models(old, new)
        self.assertTrue(any(line.startswith("- a") for line in lines))
        self.assertTrue(any(line.startswith("+ b") for line in lines))

    def test_diff_is_empty_for_identical_models(self) -> None:
        models = self.base()["models"]
        self.assertEqual(validate.diff_models(models, json.loads(json.dumps(models))), [])


# ======================================================================================
# The mapping is a document a human maintains, so check it stays legible
# ======================================================================================


class TestMapping(unittest.TestCase):
    def test_every_mapped_field_is_a_known_unit(self) -> None:
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
        for model_id, spec in mapping["models"].items():
            self.assertTrue(spec["page_name"], model_id)
            self.assertTrue(spec["display_name"], model_id)
            self.assertTrue(spec["fields"], model_id)
            for field, field_spec in spec["fields"].items():
                self.assertIn(field, validate.KNOWN_PRICE_FIELDS, f"{model_id}.{field}")
                self.assertTrue(field_spec.get("label"), f"{model_id}.{field}")

    def test_page_names_are_distinct(self) -> None:
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
        names = [spec["page_name"] for spec in mapping["models"].values()]
        self.assertEqual(len(names), len(set(names)), "two API ids point at the same card")


if __name__ == "__main__":
    unittest.main()
