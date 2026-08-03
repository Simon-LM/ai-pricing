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
sys.path.insert(0, str(REPO_ROOT / "scripts" / "providers" / "mistral"))

import scrape  # noqa: E402
import pricing_validate as validate  # noqa: E402
from pricing_validate import JSONDict  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "mistral"
PAGE_OK = FIXTURES / "page_ok.html"

# The scrape scenarios compare the fixture page against a baseline pinned to that
# same page, never against the live pricing.json. Otherwise every genuine price
# change would fail the test suite until somebody re-captured the fixture, which
# would put friction on exactly the path that must stay quick: a reviewed price
# correction reaching consumers. The two only move together when the fixture is
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
        self.assertEqual(models["mistral-ocr-latest"]["per_1k_pages"], 4.0)
        self.assertNotEqual(models["mistral-ocr-latest"]["per_1k_pages"], 3.0)

    def test_voxtral_mini_price_is_not_read_from_the_realtime_card(self) -> None:
        """The second decoy: 'voxtral mini transcribe realtime' has a row labelled
        'Audio Input/min' too, at $0.006 -- double. Same label, different card."""
        self.run_scrape(self.page_ok)
        models = self.candidate_block("stamped.json")["models"]
        self.assertEqual(models["voxtral-mini-latest"]["per_audio_minute"], 0.003)
        self.assertNotEqual(models["voxtral-mini-latest"]["per_audio_minute"], 0.006)

    def test_a_model_billed_in_two_units_keeps_both(self) -> None:
        """voxtral-small is billed per minute of audio AND per million tokens. Code
        that assumes one unit per model truncates it, and the truncation is silent."""
        self.run_scrape(self.page_ok)
        entry = self.candidate_block("stamped.json")["models"]["voxtral-small-latest"]

        self.assertEqual(entry["per_audio_minute"], 0.004)
        self.assertEqual(entry["in_per_mtok"], 0.1)
        self.assertEqual(entry["out_per_mtok"], 0.4)

    def test_every_mapped_model_reaches_the_output(self) -> None:
        """Adding a model to the mapping without it appearing here would be a silent
        omission, which is the one failure mode this repository must not have."""
        self.run_scrape(self.page_ok)
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
        self.assertEqual(
            set(self.candidate_block("stamped.json")["models"]), set(mapping["models"])
        )

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
        html = mutate(self.page_ok, "&quot;priceUsd&quot;:1.5", "&quot;priceUsd&quot;:2.25")

        code, stdout, _ = self.run_scrape(html)

        self.assertEqual(code, 0)
        self.assertIn("changed", stdout)
        self.assertIn("mistral-medium-latest.in_per_mtok", stdout)
        self.assertIn("1.5", stdout)
        self.assertIn("2.25", stdout)

        updated = self.candidate_block("updated.json")
        self.assertEqual(updated["models"]["mistral-medium-latest"]["in_per_mtok"], 2.25)
        self.assertEqual(updated["checked_utc"], FIXED_NOW)
        self.assertEqual(updated["updated"], FIXED_NOW[:10], "updated must move with the figure")
        self.assertEqual(
            updated["models"]["mistral-medium-latest"]["out_per_mtok"],
            7.5,
            "an unrelated figure was disturbed",
        )

        # The stamp is still produced: the keepalive commit happens on every run.
        stamped = self.candidate_block("stamped.json")
        self.assertEqual(stamped["models"], json.loads(self.current_bytes)["providers"]["mistral"]["models"])

        self.assert_current_untouched()

    def test_a_price_going_down_is_also_a_change(self) -> None:
        html = mutate(self.page_ok, "&quot;priceUsd&quot;:4", "&quot;priceUsd&quot;:2")
        code, stdout, _ = self.run_scrape(html)
        self.assertEqual(code, 0)
        self.assertIn("mistral-ocr-latest.per_1k_pages", stdout)
        self.assertEqual(self.candidate_block("updated.json")["models"]["mistral-ocr-latest"]["per_1k_pages"], 2.0)
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

    def test_a_published_model_may_not_be_dropped_by_the_job(self) -> None:
        published = json.loads(self.current_bytes)
        published["providers"]["mistral"]["models"]["mistral-large-latest"] = {
            "in_per_mtok": 0.5,
            "out_per_mtok": 1.5,
            "display_name": "Mistral Large 3",
        }
        self.current.write_text(json.dumps(published, indent=2) + "\n", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()

        code, _, stderr = self.run_scrape(self.page_ok)
        self.assert_failed(code, stderr, "mapping does not know", "mistral-large-latest")


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
        """Figures may differ -- the baseline is pinned to an older capture -- but the
        set of models and the units they are priced in may not drift apart unnoticed."""
        published = self.mistral_block()["models"]
        baseline = json.loads(BASELINE_JSON.read_text(encoding="utf-8"))["providers"]["mistral"]["models"]

        self.assertEqual(set(published), set(baseline))
        for model_id, entry in published.items():
            self.assertEqual(
                {k for k in entry if k in validate.KNOWN_PRICE_FIELDS},
                {k for k in baseline[model_id] if k in validate.KNOWN_PRICE_FIELDS},
                model_id,
            )

    def test_the_committed_file_matches_the_mapping(self) -> None:
        published = self.mistral_block()
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))

        self.assertEqual(set(published["models"]), set(mapping["models"]))
        self.assertEqual(published["source"], mapping["source"])
        self.assertEqual(published["currency"], mapping["currency"])
        for model_id, entry in published["models"].items():
            self.assertEqual(entry["display_name"], mapping["models"][model_id]["display_name"])
            expected_fields = set(mapping["models"][model_id]["fields"])
            actual_fields = {k for k in entry if k in validate.KNOWN_PRICE_FIELDS}
            self.assertEqual(actual_fields, expected_fields, model_id)

    def test_the_published_audio_prices_have_room_to_fall(self) -> None:
        """Guards the floor against the figures actually published: if a price ever sits
        too close to it, a real price cut starts failing the job instead of opening a
        pull request. Caught here rather than on the Monday it happens."""
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
