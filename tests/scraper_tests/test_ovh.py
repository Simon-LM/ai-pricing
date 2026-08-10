"""Tests for the OVH scraper, and for what happens when things break.

Standard library only: `python3 -m unittest discover tests`.

Every scenario asserts the same thing at the end, through `assert_current_untouched`:
pricing.json is byte-for-byte what it was. That is the property the whole design
exists to protect -- a stale price whose date the user can see is far better than a
wrong one they cannot.

The failure scenarios are derived from tests/fixtures/ovh/catalog_ok.html, which is
one real captured "push" data chunk from OVH's catalog page (a Next.js React Server
Components payload, not scrapeable card markup the way Mistral's page is), by
explicit mutation. `mutate` refuses to run when there was nothing to replace, so a
fixture that drifts away from the page fails the tests loudly instead of quietly
testing nothing.

Cross-file consistency checks -- does providers.ovh in the committed pricing.json
actually match scripts/providers/ovh/mapping.json -- live here too: they are
statements about OVH specifically, not about the shared schema.
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

# Imported via the providers.ovh package, not a flat `import scrape` off a
# directly-inserted directory: Mistral's scraper is also a module literally named
# scrape.py, and a flat import would collide in sys.modules the moment both test
# files run in the same process (as `unittest discover` does) -- whichever loads
# first wins the name, and the other file silently gets the wrong module.
from providers.ovh import scrape  # noqa: E402
import pricing_validate as validate  # noqa: E402
from pricing_validate import JSONDict  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ovh"
CATALOG_OK = FIXTURES / "catalog_ok.html"

# The scrape scenarios compare the fixture page against a baseline pinned to that
# same page, never against the live pricing.json. Otherwise every genuine price
# change would fail the test suite until somebody re-captured the fixture, which
# would put friction on exactly the path that must stay quick: a price correction
# reaching consumers. The two only move together when the fixture is
# deliberately re-captured.
BASELINE_JSON = FIXTURES / "baseline.json"

PRICING_JSON = REPO_ROOT / "pricing.json"
MAPPING_JSON = REPO_ROOT / "scripts" / "providers" / "ovh" / "mapping.json"

FIXED_NOW = "2026-08-10T04:00:00Z"

# Catalog entries OVH currently gives away. They are published, but as `"free": true`
# with no price field -- never as a price of 0, which is also exactly what a broken
# parser reads. Listed here so the tests can prove both halves of that.
FREE_MODELS = (
    "nvr-tts-it-it",
    "nvr-tts-en-us",
    "nvr-tts-de-de",
    "nvr-tts-es-es",
    "Qwen3Guard-Gen-8B",
    "Qwen3Guard-Gen-0.6B",
    "stable-diffusion-xl-base-v10",
)


def mutate(source: str, old: str, new: str, expected_count: int | None = 1) -> str:
    """Replace `old` with `new`, checking first that there was something to replace.

    `expected_count` is an exact number where uniqueness is the point of the test --
    a mutation that silently hit two entries would prove nothing. Pass None for
    structural mutations that sweep the whole page, where the count is incidental;
    those still refuse to run against zero matches, which is the failure that would
    leave a test testing nothing.
    """
    found = source.count(old)
    if found == 0 or (expected_count is not None and found != expected_count):
        raise AssertionError(
            f"fixture drift: expected {expected_count or 'at least one'} occurrence(s) of "
            f"{old!r}, found {found}. Re-capture tests/fixtures/ovh/catalog_ok.html from the live page."
        )
    return source.replace(old, new)


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

        self.page = self.tmp / "catalog.html"

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
        return self.candidate(name)["providers"]["ovh"]

    @property
    def catalog_ok(self) -> str:
        return CATALOG_OK.read_text(encoding="utf-8")


# ======================================================================================
# Outcome 1: figures unchanged
# ======================================================================================


class TestUnchanged(ScrapeTestCase):
    def test_unchanged_page_stamps_and_publishes_nothing_new(self) -> None:
        code, stdout, _ = self.run_scrape(self.catalog_ok)

        self.assertEqual(code, 0)
        self.assertIn("unchanged", stdout)
        self.assertFalse((self.out_dir / "updated.json").exists())

        stamped = self.candidate_block("stamped.json")
        published = json.loads(self.current_bytes)["providers"]["ovh"]

        self.assertEqual(stamped["checked_utc"], FIXED_NOW, "checked_utc was not refreshed")
        self.assertEqual(stamped["updated"], published["updated"], "updated must not move when no figure moved")
        self.assertEqual(stamped["models"], published["models"])
        self.assert_current_untouched()

    def test_baseline_matches_the_fixture_page(self) -> None:
        """The fixture and its baseline are two views of the same capture; keep them so."""
        code, stdout, _ = self.run_scrape(self.catalog_ok)
        self.assertEqual(code, 0)
        self.assertIn("unchanged", stdout)

    def test_every_mapped_model_reaches_the_output(self) -> None:
        """Adding a model to the mapping without it appearing here would be a silent
        omission, which is the one failure mode this repository must not have."""
        self.run_scrape(self.catalog_ok)
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
        self.assertEqual(set(self.candidate_block("stamped.json")["models"]), set(mapping["models"]))

    def test_free_models_are_published_as_free_and_never_as_zero(self) -> None:
        """The catalog states 0 for these and the page renders the word "Gratuit".
        Both facts are real, and `"free": true` is the only one of the two this file
        can publish honestly: a literal 0 is indistinguishable from the number a
        parser reads off a page whose layout moved."""
        self.run_scrape(self.catalog_ok)
        published = self.candidate_block("stamped.json")["models"]
        for model_id in FREE_MODELS:
            with self.subTest(model=model_id):
                entry = published[model_id]
                self.assertIs(entry["free"], True)
                prices = [k for k in entry if k in validate.KNOWN_PRICE_FIELDS]
                self.assertEqual(prices, [], "a free model must carry no price field at all")
                self.assertNotIn(0, entry.values())

    def test_a_renamed_api_model_id_is_refused(self) -> None:
        """The pricing.json key IS the callable API model id, and the catalog states it
        in `name`. If OVH renames it, every consumer's calls start failing -- so the
        run must stop and get a human, not republish an id that resolves against
        nothing while the price it carries still looks perfectly plausible."""
        html = mutate(
            self.catalog_ok,
            'name\\":\\"Qwen3.6-27B\\"',
            'name\\":\\"Qwen3.6-27B-v2\\"',
        )
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "Qwen3.6-27B", "is now named")

    def test_a_free_model_that_starts_costing_money_is_refused(self) -> None:
        """The free marker is a claim about the catalog, and the catalog is re-read
        every week. The day OVH prices one of these, the run must stop rather than
        keep publishing "free" for something that now bills."""
        html = mutate(
            self.catalog_ok,
            '\\"price\\":0,\\"price_unit\\":\\"million_input_chars\\"',
            '\\"price\\":0.5,\\"price_unit\\":\\"million_input_chars\\"',
            expected_count=None,
        )
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "mapped as free", "0.5")

    def test_other_providers_blocks_are_never_touched(self) -> None:
        """The entire reason pricing.json nests under providers.<name>: a run that
        scrapes OVH must carry any other provider's block through byte-for-byte,
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

        self.run_scrape(self.catalog_ok)

        candidate_providers = self.candidate("stamped.json")["providers"]
        self.assertEqual(candidate_providers["acme"], doc["providers"]["acme"])
        self.assertEqual(
            list(candidate_providers),
            list(doc["providers"]),
            "providers were reordered even though only ovh changed",
        )


# ======================================================================================
# Outcome 2: a figure changed
# ======================================================================================


class TestPriceChange(ScrapeTestCase):
    def test_normal_price_change_produces_a_candidate_not_a_publication(self) -> None:
        html = mutate(self.catalog_ok, '\\"price\\":0.08,\\"price_unit\\":\\"million_input_tokens\\"',
                       '\\"price\\":0.12,\\"price_unit\\":\\"million_input_tokens\\"')

        code, stdout, _ = self.run_scrape(html)

        self.assertEqual(code, 0)
        self.assertIn("changed", stdout)
        self.assertIn("gpt-oss-120b.in_per_mtok", stdout)
        self.assertIn("0.08", stdout)
        self.assertIn("0.12", stdout)

        updated = self.candidate_block("updated.json")
        self.assertEqual(updated["models"]["gpt-oss-120b"]["in_per_mtok"], 0.12)
        self.assertEqual(updated["checked_utc"], FIXED_NOW)
        self.assertEqual(updated["updated"], FIXED_NOW[:10], "updated must move with the figure")
        self.assertEqual(
            updated["models"]["gpt-oss-120b"]["out_per_mtok"], 0.4, "an unrelated figure was disturbed"
        )

        # The stamp is still produced: the keepalive commit happens on every run.
        stamped = self.candidate_block("stamped.json")
        self.assertEqual(stamped["models"], json.loads(self.current_bytes)["providers"]["ovh"]["models"])

        self.assert_current_untouched()

    def test_a_price_going_down_is_also_a_change(self) -> None:
        html = mutate(self.catalog_ok, '\\"price\\":0.4,\\"price_unit\\":\\"million_output_tokens\\"',
                       '\\"price\\":0.2,\\"price_unit\\":\\"million_output_tokens\\"')
        code, stdout, _ = self.run_scrape(html)
        self.assertEqual(code, 0)
        self.assertIn("gpt-oss-120b.out_per_mtok", stdout)
        self.assertEqual(self.candidate_block("updated.json")["models"]["gpt-oss-120b"]["out_per_mtok"], 0.2)
        self.assert_current_untouched()

    def test_a_renamed_catalog_id_is_never_absorbed_silently(self) -> None:
        """The pricing.json key comes from the mapping, so the catalog renaming its
        id for this model is a failure, never something to follow along with."""
        html = mutate(self.catalog_ok, '\\"id\\":\\"gpt-oss-120b\\"', '\\"id\\":\\"gpt-oss-120b-v2\\"')
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "gpt-oss-120b", "mapping.json")


# ======================================================================================
# Outcome 3: fetch or parse failed
# ======================================================================================


class TestLayoutFailures(ScrapeTestCase):
    def test_page_without_any_push_chunks(self) -> None:
        code, _, stderr = self.run_scrape("<html><body>nothing here</body></html>")
        self.assert_failed(code, stderr, "no self.__next_f.push")

    def test_page_with_push_chunks_but_no_models_key(self) -> None:
        html = mutate(self.catalog_ok, '\\"models\\":[', '\\"notmodels\\":[')
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, 'contain a "models" array')

    def test_unbalanced_models_array_does_not_parse(self) -> None:
        # Drop the closing bracket of one model's pricing array. "models":[ is left
        # intact -- this is not "no models key", it is "the array's JSON is broken" --
        # so the bracket-matching extractor keeps scanning past where the real array
        # ends and hands json.loads a string that cannot possibly parse.
        html = mutate(
            self.catalog_ok,
            '\\"pricing\\":[{\\"price\\":0.08,\\"price_unit\\":\\"million_input_tokens\\"},'
            '{\\"price\\":0.4,\\"price_unit\\":\\"million_output_tokens\\"}]',
            '\\"pricing\\":[{\\"price\\":0.08,\\"price_unit\\":\\"million_input_tokens\\"},'
            '{\\"price\\":0.4,\\"price_unit\\":\\"million_output_tokens\\"}',
        )
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, '"models" array could not be parsed')

    def test_a_catalog_entry_without_an_id(self) -> None:
        html = mutate(self.catalog_ok, '\\"id\\":\\"gpt-oss-120b\\"', '\\"noId\\":\\"gpt-oss-120b\\"')
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, 'missing a string "id"')

    def test_a_duplicate_catalog_id_is_refused(self) -> None:
        html = mutate(self.catalog_ok, '\\"id\\":\\"gpt-oss-20b\\"', '\\"id\\":\\"gpt-oss-120b\\"')
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "lists 'gpt-oss-120b' more than once")


class TestMissingModel(ScrapeTestCase):
    def test_model_in_the_mapping_but_absent_from_the_catalog(self) -> None:
        html = mutate(self.catalog_ok, '\\"id\\":\\"gpt-oss-120b\\"', '\\"id\\":\\"gpt-oss-120b-renamed\\"')
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "gpt-oss-120b", "no model with id 'gpt-oss-120b'")

    def test_the_other_models_are_not_published_when_one_is_missing(self) -> None:
        """A partial file is not an acceptable outcome: it is all eight or nothing."""
        html = mutate(self.catalog_ok, '\\"id\\":\\"gpt-oss-120b\\"', '\\"id\\":\\"gpt-oss-120b-renamed\\"')
        self.run_scrape(html)
        self.assertFalse(any(self.out_dir.glob("*.json")))


class TestSanityBounds(ScrapeTestCase):
    def test_out_of_bounds_figure_is_refused(self) -> None:
        html = mutate(self.catalog_ok, '\\"price\\":0.08,\\"price_unit\\":\\"million_input_tokens\\"',
                       '\\"price\\":4000,\\"price_unit\\":\\"million_input_tokens\\"')
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "outside the plausible range")

    def test_figure_below_the_floor_is_refused(self) -> None:
        html = mutate(self.catalog_ok, '\\"price\\":0.08,\\"price_unit\\":\\"million_input_tokens\\"',
                       '\\"price\\":0,\\"price_unit\\":\\"million_input_tokens\\"')
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "outside the plausible range")

    def test_plausible_figure_that_moved_implausibly_is_refused(self) -> None:
        """1.0 is a perfectly ordinary price for this unit. Going 0.08 -> 1.0 in one
        week is not."""
        html = mutate(self.catalog_ok, '\\"price\\":0.08,\\"price_unit\\":\\"million_input_tokens\\"',
                       '\\"price\\":1.0,\\"price_unit\\":\\"million_input_tokens\\"')
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "factor of", "Refusing to publish")

    def test_a_change_just_under_the_factor_limit_is_allowed_through(self) -> None:
        html = mutate(self.catalog_ok, '\\"price\\":0.08,\\"price_unit\\":\\"million_input_tokens\\"',
                       '\\"price\\":0.3,\\"price_unit\\":\\"million_input_tokens\\"')
        code, stdout, _ = self.run_scrape(html)
        self.assertEqual(code, 0)
        self.assertIn("changed", stdout)
        self.assert_current_untouched()

    def test_price_unit_change_is_refused(self) -> None:
        """per_audio_second must not keep its name if the catalog stops meaning
        seconds -- OVH's own machine-readable unit tag changing is unambiguous,
        unlike Mistral's page where only a formatted label hints at the unit."""
        html = mutate(self.catalog_ok, '\\"price_unit\\":\\"audio_duration_seconds\\"',
                       '\\"price_unit\\":\\"audio_duration_minutes\\"', expected_count=2)
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "no pricing entry with", "price_unit")

    def test_a_duplicate_price_unit_is_refused(self) -> None:
        """Two pricing entries for the same model claiming the same unit is exactly
        the kind of ambiguity this job must refuse to guess through, not just a
        theoretical case -- OVH's data is a list, nothing stops it from happening."""
        html = mutate(
            self.catalog_ok,
            '\\"pricing\\":[{\\"price\\":0.08,\\"price_unit\\":\\"million_input_tokens\\"},'
            '{\\"price\\":0.4,\\"price_unit\\":\\"million_output_tokens\\"}]',
            '\\"pricing\\":[{\\"price\\":0.08,\\"price_unit\\":\\"million_input_tokens\\"},'
            '{\\"price\\":0.09,\\"price_unit\\":\\"million_input_tokens\\"},'
            '{\\"price\\":0.4,\\"price_unit\\":\\"million_output_tokens\\"}]',
        )
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "2 pricing entries", "million_input_tokens")


class TestCorruptInputs(ScrapeTestCase):
    def test_invalid_committed_pricing_json_stops_everything(self) -> None:
        self.current.write_text(
            json.dumps({"schema_version": validate.SCHEMA_VERSION, "providers": {}}), encoding="utf-8"
        )
        self.current_bytes = self.current.read_bytes()
        code, _, stderr = self.run_scrape(self.catalog_ok)
        self.assert_failed(code, stderr, "committed pricing.json is invalid")

    def test_unparseable_committed_pricing_json_stops_everything(self) -> None:
        self.current.write_text("{ not json", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()
        code, _, stderr = self.run_scrape(self.catalog_ok)
        self.assert_failed(code, stderr, "not valid JSON")

    def test_a_missing_ovh_block_stops_everything(self) -> None:
        """This job updates an existing provider's figures; it does not decide, on
        its own, to start publishing a provider that was never seeded by a human."""
        doc = json.loads(self.current_bytes)
        del doc["providers"]["ovh"]
        doc["providers"]["placeholder"] = {
            "checked_utc": "2026-08-03T04:00:00Z",
            "updated": "2026-08-03",
            "source": "https://example.invalid",
            "currency": "EUR",
            "models": {"x": {"in_per_mtok": 1.0, "display_name": "X"}},
        }
        self.current.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()

        code, _, stderr = self.run_scrape(self.catalog_ok)
        self.assert_failed(code, stderr, "no providers.ovh block")

    def test_a_published_model_may_not_be_dropped_by_the_job(self) -> None:
        published = json.loads(self.current_bytes)
        published["providers"]["ovh"]["models"]["some-withdrawn-model"] = {
            "in_per_mtok": 0.5,
            "display_name": "Some Withdrawn Model",
        }
        self.current.write_text(json.dumps(published, indent=2) + "\n", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()

        code, _, stderr = self.run_scrape(self.catalog_ok)
        self.assert_failed(code, stderr, "mapping does not know", "some-withdrawn-model")


# ======================================================================================
# Cross-file consistency: does providers.ovh agree with OVH's own files?
# ======================================================================================


class TestPublishedFileMatchesOVH(unittest.TestCase):
    def base(self) -> JSONDict:
        return json.loads(PRICING_JSON.read_text(encoding="utf-8"))

    def ovh_block(self) -> JSONDict:
        return self.base()["providers"]["ovh"]

    def test_the_committed_file_is_valid(self) -> None:
        validate.validate_document(self.base())

    def test_the_fixture_baseline_is_valid(self) -> None:
        validate.validate_document(json.loads(BASELINE_JSON.read_text(encoding="utf-8")))

    def test_the_baseline_has_the_same_shape_as_the_published_file(self) -> None:
        """Figures may differ -- the baseline is pinned to an older capture -- but the
        set of models and the units they are priced in may not drift apart unnoticed."""
        published = self.ovh_block()["models"]
        baseline = json.loads(BASELINE_JSON.read_text(encoding="utf-8"))["providers"]["ovh"]["models"]

        self.assertEqual(set(published), set(baseline))
        for model_id, entry in published.items():
            self.assertEqual(
                {k for k in entry if k in validate.KNOWN_PRICE_FIELDS},
                {k for k in baseline[model_id] if k in validate.KNOWN_PRICE_FIELDS},
                model_id,
            )

    def test_the_committed_file_matches_the_mapping(self) -> None:
        published = self.ovh_block()
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))

        self.assertEqual(set(published["models"]), set(mapping["models"]))
        self.assertEqual(published["source"], mapping["source"])
        self.assertEqual(published["currency"], mapping["currency"])
        for model_id, entry in published["models"].items():
            spec = mapping["models"][model_id]
            self.assertEqual(entry["display_name"], spec["display_name"])
            if spec.get("free"):
                self.assertIs(entry.get("free"), True, model_id)
                expected_fields: set[str] = set()
            else:
                self.assertNotIn("free", entry, model_id)
                expected_fields = set(spec["fields"])
            actual_fields = {k for k in entry if k in validate.KNOWN_PRICE_FIELDS}
            self.assertEqual(actual_fields, expected_fields, model_id)

    def test_the_published_figures_have_room_to_fall(self) -> None:
        """Guards the floor against the figures actually published: if a price ever sits
        too close to it, a real price cut starts failing the job instead of being
        published. Caught here rather than on the Monday it happens. Especially
        relevant for OVH: whisper's per_audio_second prices are close to the smallest
        this repository has ever published."""
        published = self.ovh_block()["models"]
        for model_id, entry in published.items():
            for field, value in entry.items():
                if field not in validate.KNOWN_PRICE_FIELDS:
                    continue
                low, _ = validate.PRICE_BOUNDS.get(field, (validate.MIN_PLAUSIBLE, validate.MAX_PLAUSIBLE))
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
    def test_every_mapped_field_is_a_known_unit_with_a_price_unit(self) -> None:
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
        for model_id, spec in mapping["models"].items():
            self.assertTrue(spec["catalog_id"], model_id)
            self.assertTrue(spec["catalog_name"], model_id)
            self.assertTrue(spec["display_name"], model_id)

            # A free entry states the units it expects to find at 0 instead of
            # mapping them to price fields. It is one shape or the other, never both
            # and never neither -- an entry with no fields and no free marker would
            # publish a model with nothing in it.
            if spec.get("free"):
                self.assertNotIn("fields", spec, model_id)
                self.assertTrue(spec["expect_zero_units"], model_id)
                continue

            self.assertTrue(spec["fields"], model_id)
            for field, field_spec in spec["fields"].items():
                self.assertIn(field, validate.KNOWN_PRICE_FIELDS, f"{model_id}.{field}")
                self.assertTrue(field_spec.get("price_unit"), f"{model_id}.{field}")

    def test_every_key_is_the_api_model_id_the_catalog_states(self) -> None:
        """The pricing.json key is what a consumer passes to OVH's API, and the
        catalog puts it in `name`. Its `id` is a CMS slug and is NOT callable:
        'qwen-3-6-27b' against the real 'Qwen3.6-27B'. Keying off the wrong one
        would publish model ids that resolve against nothing."""
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
        for model_id, spec in mapping["models"].items():
            self.assertEqual(model_id, spec["catalog_name"], model_id)

    def test_catalog_ids_are_distinct(self) -> None:
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
        ids = [spec["catalog_id"] for spec in mapping["models"].values()]
        self.assertEqual(len(ids), len(set(ids)), "two pricing.json keys point at the same catalog id")

    def test_no_model_is_mapped_to_more_than_one_field_with_the_same_price_unit(self) -> None:
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
        for model_id, spec in mapping["models"].items():
            if spec.get("free"):
                units = spec["expect_zero_units"]
            else:
                units = [f["price_unit"] for f in spec["fields"].values()]
            self.assertEqual(len(units), len(set(units)), model_id)


if __name__ == "__main__":
    unittest.main()
