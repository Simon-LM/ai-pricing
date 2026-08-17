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

    def notes(self) -> str:
        return (self.out_dir / "notes.txt").read_text(encoding="utf-8")

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

    def test_every_catalog_entry_reaches_the_output(self) -> None:
        """There is no list to reconcile: every model the catalog states a price for is
        published, keyed by the callable id the catalog puts in `name`. A model quietly
        dropping out between the page and the file is the one failure mode this
        repository must not have."""
        self.run_scrape(self.catalog_ok)
        catalog = scrape.extract_catalog_models(self.catalog_ok)
        self.assertEqual(
            set(self.candidate_block("stamped.json")["models"]),
            {entry["name"] for entry in catalog},
        )

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

    def test_a_renamed_api_model_id_publishes_the_new_one_and_keeps_the_old(self) -> None:
        """The pricing.json key IS the callable API model id, and the catalog states it
        in `name`. A rename is therefore two facts at once: a model exists under a new
        id, and nothing answers to the old one any more. Both are published -- the new
        key with today's price, the old one frozen and dated -- because a consumer
        still calling the old id needs to find out, and a consumer calling the new one
        needs a price."""
        html = mutate(
            self.catalog_ok,
            'name\\":\\"Qwen3.6-27B\\"',
            'name\\":\\"Qwen3.6-27B-v2\\"',
        )
        code, stdout, _ = self.run_scrape(html)
        self.assertEqual(code, 0)

        models = self.candidate_block("updated.json")["models"]
        old = json.loads(self.current_bytes)["providers"]["ovh"]["models"]["Qwen3.6-27B"]

        self.assertNotIn("absent_since", models["Qwen3.6-27B-v2"])
        self.assertEqual(models["Qwen3.6-27B"]["absent_since"], FIXED_NOW[:10])
        self.assertEqual(
            {k: v for k, v in models["Qwen3.6-27B"].items() if k in validate.KNOWN_PRICE_FIELDS},
            {k: v for k, v in old.items() if k in validate.KNOWN_PRICE_FIELDS},
            "the frozen entry must keep the prices last actually observed",
        )
        self.assertIn("Qwen3.6-27B", self.notes())
        self.assertIn("no longer offered", self.notes())
        self.assert_current_untouched()

    def test_a_free_model_that_starts_costing_money_publishes_the_price(self) -> None:
        """The free marker is a claim about the catalog, and the catalog is re-read
        every week -- never remembered. The day OVH prices one of these, the price
        simply appears and the marker simply goes."""
        html = mutate(
            self.catalog_ok,
            '\\"price\\":0,\\"price_unit\\":\\"million_input_chars\\"',
            '\\"price\\":0.5,\\"price_unit\\":\\"million_input_chars\\"',
            expected_count=None,
        )
        code, stdout, _ = self.run_scrape(html)
        self.assertEqual(code, 0)
        self.assertIn("changed", stdout)

        entry = self.candidate_block("updated.json")["models"]["nvr-tts-en-us"]
        self.assertNotIn("free", entry, "a model with a price is not free")
        self.assertEqual(entry["per_mchars"], 0.5)
        self.assertEqual(self.notes(), "", "a price appearing is not something to alert about")
        self.assert_current_untouched()

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

    def test_a_renamed_cms_slug_changes_nothing(self) -> None:
        """The catalog's `id` is a slug for its own URLs and is NOT what a consumer
        calls. Nothing published here is keyed off it, so OVH reorganising its own URLs
        must not show up in pricing.json at all -- not as a change, and certainly not
        as a failure."""
        html = mutate(self.catalog_ok, '\\"id\\":\\"gpt-oss-120b\\"', '\\"id\\":\\"gpt-oss-120b-v2\\"')
        code, stdout, _ = self.run_scrape(html)
        self.assertEqual(code, 0)
        self.assertIn("unchanged", stdout)
        self.assertFalse((self.out_dir / "updated.json").exists())
        self.assert_current_untouched()


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

    def test_a_duplicate_model_name_is_refused(self) -> None:
        """`name` is the key everything is published under, so two entries claiming the
        same one is an ambiguity about which price is real -- and that is the one class
        of problem this job refuses to guess through."""
        html = mutate(self.catalog_ok, 'name\\":\\"gpt-oss-20b\\"', 'name\\":\\"gpt-oss-120b\\"')
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "two entries named 'gpt-oss-120b'")

    def test_an_entry_without_a_name_is_refused(self) -> None:
        html = mutate(self.catalog_ok, 'name\\":\\"gpt-oss-120b\\"', 'label\\":\\"gpt-oss-120b\\"')
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "has no string 'name'")


class TestWithdrawnModel(ScrapeTestCase):
    """A model the catalog stops listing. This is the case that used to fail the whole
    job, blocking correct prices for everything else until a human edited a list."""

    def withdraw_gpt_oss_120b(self) -> str:
        return mutate(
            self.catalog_ok, 'name\\":\\"gpt-oss-120b\\"', 'name\\":\\"gpt-oss-120b-renamed\\"'
        )

    def test_it_is_kept_with_its_last_prices_and_a_date(self) -> None:
        code, stdout, _ = self.run_scrape(self.withdraw_gpt_oss_120b())
        self.assertEqual(code, 0)

        models = self.candidate_block("updated.json")["models"]
        published = json.loads(self.current_bytes)["providers"]["ovh"]["models"]["gpt-oss-120b"]

        self.assertEqual(models["gpt-oss-120b"]["absent_since"], FIXED_NOW[:10])
        self.assertEqual(models["gpt-oss-120b"]["in_per_mtok"], published["in_per_mtok"])
        self.assertEqual(models["gpt-oss-120b"]["out_per_mtok"], published["out_per_mtok"])

    def test_every_other_model_is_still_published(self) -> None:
        """The point of the whole change: one model going away is not a reason to
        withhold eighteen correct prices."""
        self.run_scrape(self.withdraw_gpt_oss_120b())
        models = self.candidate_block("updated.json")["models"]
        still_offered = [m for m in models if "absent_since" not in models[m]]
        self.assertEqual(len(still_offered), 19)
        self.assertIn("gpt-oss-120b-renamed", still_offered)

    def test_it_is_reported_once_and_not_again(self) -> None:
        """The run that first sees it gone says so. A run a week later, with the stamp
        already in place, says nothing: a channel that repeats itself gets muted, and
        this one has to still work the day something real happens."""
        self.run_scrape(self.withdraw_gpt_oss_120b())
        self.assertIn("gpt-oss-120b", self.notes())
        self.assertIn("365 days", self.notes())

        # Second week: the file already carries the marker.
        self.current.write_text(
            (self.out_dir / "updated.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.current_bytes = self.current.read_bytes()
        code, stdout, _ = self.run_scrape(self.withdraw_gpt_oss_120b(), now="2026-08-17T04:00:00Z")

        self.assertEqual(code, 0)
        self.assertIn("unchanged", stdout)
        self.assertEqual(self.notes(), "", "the same disappearance was reported twice")

    def test_it_is_dropped_after_a_year_and_not_before(self) -> None:
        self.run_scrape(self.withdraw_gpt_oss_120b())
        aged = json.loads((self.out_dir / "updated.json").read_text(encoding="utf-8"))
        aged["providers"]["ovh"]["models"]["gpt-oss-120b"]["absent_since"] = "2025-08-10"
        self.current.write_text(json.dumps(aged, indent=2) + "\n", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()

        # 2026-08-09: 364 days. Still published, because a year has not passed.
        self.run_scrape(self.withdraw_gpt_oss_120b(), now="2026-08-09T04:00:00Z")
        self.assertIn("gpt-oss-120b", self.candidate_block("stamped.json")["models"])

        # 2026-08-11: 366 days. Gone, and said out loud.
        code, _, _ = self.run_scrape(self.withdraw_gpt_oss_120b(), now="2026-08-11T04:00:00Z")
        self.assertEqual(code, 0)
        self.assertNotIn("gpt-oss-120b", self.candidate_block("updated.json")["models"])
        self.assertIn("Dropped from the file", self.notes())

    def test_a_model_that_comes_back_loses_the_marker(self) -> None:
        self.run_scrape(self.withdraw_gpt_oss_120b())
        self.current.write_text(
            (self.out_dir / "updated.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.current_bytes = self.current.read_bytes()

        code, _, _ = self.run_scrape(self.catalog_ok, now="2026-08-17T04:00:00Z")
        self.assertEqual(code, 0)

        entry = self.candidate_block("updated.json")["models"]["gpt-oss-120b"]
        self.assertNotIn("absent_since", entry, "a model on sale again must not look withdrawn")
        self.assertEqual(entry["in_per_mtok"], 0.08)
        self.assertIn("offered again", self.notes())


class TestSanityBounds(ScrapeTestCase):
    def test_out_of_bounds_figure_is_refused(self) -> None:
        html = mutate(self.catalog_ok, '\\"price\\":0.08,\\"price_unit\\":\\"million_input_tokens\\"',
                       '\\"price\\":4000,\\"price_unit\\":\\"million_input_tokens\\"')
        code, _, stderr = self.run_scrape(html)
        self.assert_failed(code, stderr, "outside the plausible range")

    def test_a_lone_zero_beside_a_real_price_is_not_published(self) -> None:
        """Free means free in every unit, and it is checked that way. A single 0 next to
        a real price is either a giveaway this schema cannot express or a misparse; both
        get reported and neither gets published as a number."""
        html = mutate(self.catalog_ok, '\\"price\\":0.08,\\"price_unit\\":\\"million_input_tokens\\"',
                       '\\"price\\":0,\\"price_unit\\":\\"million_input_tokens\\"')
        code, _, _ = self.run_scrape(html)
        self.assertEqual(code, 0)

        entry = self.candidate_block("updated.json")["models"]["gpt-oss-120b"]
        self.assertNotIn("in_per_mtok", entry)
        self.assertNotIn("free", entry)
        self.assertEqual(entry["out_per_mtok"], 0.4, "the price that WAS readable must publish")
        self.assertIn("priced at 0", self.notes())

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

    def test_an_unknown_price_unit_is_reported_and_never_guessed_at(self) -> None:
        """per_audio_second must not keep its name if the catalog stops meaning seconds.
        There is no honest field to put a per-minute figure in, so that price is not
        published at all -- but the other eighteen models' prices are, because the unit
        of an audio model says nothing about whether a token price was read correctly."""
        html = mutate(self.catalog_ok, '\\"price_unit\\":\\"audio_duration_seconds\\"',
                       '\\"price_unit\\":\\"audio_duration_minutes\\"', expected_count=2)
        code, _, _ = self.run_scrape(html)
        self.assertEqual(code, 0)

        models = self.candidate_block("updated.json")["models"]
        self.assertIn("audio_duration_minutes", self.notes())
        self.assertIn("no publishable price", self.notes())
        for model_id in ("whisper-large-v3", "whisper-large-v3-turbo"):
            self.assertEqual(models[model_id]["absent_since"], FIXED_NOW[:10])
            self.assertEqual(models[model_id]["per_audio_second"], 4.083e-05 if model_id == "whisper-large-v3" else 1.278e-05)
        self.assertEqual(models["gpt-oss-120b"]["in_per_mtok"], 0.08)

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
        self.assert_failed(code, stderr, "two pricing entries mapping to it", "in_per_mtok")


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

    def test_a_published_model_the_catalog_never_mentions_is_kept_not_dropped(self) -> None:
        """Something in the file that the source has never heard of is not deleted on
        sight. It is dated and kept for a year, like anything else that goes away --
        deleting a price a consumer might still be reading, with no warning and no way
        to look up what it used to be, is not an improvement on a stale one."""
        published = json.loads(self.current_bytes)
        published["providers"]["ovh"]["models"]["some-withdrawn-model"] = {
            "in_per_mtok": 0.5,
            "display_name": "Some Withdrawn Model",
        }
        self.current.write_text(json.dumps(published, indent=2) + "\n", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()

        code, _, _ = self.run_scrape(self.catalog_ok)
        self.assertEqual(code, 0)

        entry = self.candidate_block("updated.json")["models"]["some-withdrawn-model"]
        self.assertEqual(entry["absent_since"], FIXED_NOW[:10])
        self.assertEqual(entry["in_per_mtok"], 0.5)
        self.assertIn("some-withdrawn-model", self.notes())
        self.assert_current_untouched()


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
        """There is no model list to agree with any more, so what is checked is what the
        mapping still decides: where the figures come from and what currency they are
        in. Every published price field must be one the units table can produce."""
        published = self.ovh_block()
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))

        self.assertEqual(published["source"], mapping["source"])
        self.assertEqual(published["currency"], mapping["currency"])

        producible = set(mapping["units"].values())
        for model_id, entry in published["models"].items():
            fields = {k for k in entry if k in validate.KNOWN_PRICE_FIELDS}
            if entry.get("free") is True:
                self.assertEqual(fields, set(), f"{model_id} is free and priced at once")
            else:
                self.assertTrue(fields, model_id)
                self.assertLessEqual(fields, producible, model_id)

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
    def mapping(self) -> JSONDict:
        return json.loads(MAPPING_JSON.read_text(encoding="utf-8"))

    def test_there_is_no_model_list(self) -> None:
        """Load-bearing, not pedantry. A hand-written model list is exactly what made a
        single withdrawn model block every other price in this block, and re-adding one
        would bring that back without anything else in the tests noticing."""
        self.assertNotIn("models", self.mapping())

    def test_every_mapped_unit_names_a_known_price_field(self) -> None:
        for price_unit, field in self.mapping()["units"].items():
            self.assertIn(field, validate.KNOWN_PRICE_FIELDS, price_unit)

    def test_no_two_units_map_to_the_same_field(self) -> None:
        """Two catalog units sharing a field would make the entry order-dependent, and
        would mean one of the two figures is silently the one that wins."""
        fields = list(self.mapping()["units"].values())
        self.assertEqual(len(fields), len(set(fields)))

    def test_every_unit_the_catalog_actually_uses_is_mapped(self) -> None:
        """Catches the gap on the day the fixture is re-captured rather than on the
        Monday a price silently stops being published."""
        used = {
            str(price["price_unit"])
            for entry in scrape.extract_catalog_models(CATALOG_OK.read_text(encoding="utf-8"))
            for price in entry["metadata"]["usage_information"]["pricing"]
        }
        self.assertLessEqual(used, set(self.mapping()["units"]))


if __name__ == "__main__":
    unittest.main()
