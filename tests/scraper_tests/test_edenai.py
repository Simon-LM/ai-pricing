"""Tests for the Eden AI scraper, and for what happens when things break.

Standard library only: `python3 -m unittest discover tests`.

Every scenario asserts the same thing at the end, through `assert_current_untouched`:
pricing.json is byte-for-byte what it was. That is the property the whole design
exists to protect -- a stale price whose date the user can see is far better than a
wrong one they cannot.

Eden AI's source is a JSON API rather than a marketing page, so the failure modes
worth testing are different from Mistral's and OVH's. There is no label to rename
and no card to lose; what can go wrong is the payload changing shape, an
unauthenticated read being quoted a discounted price, and per-token figures being
scaled to per-million carelessly enough to publish floating-point noise.

Cross-file consistency checks -- does providers.edenai in the committed pricing.json
actually match scripts/providers/edenai/mapping.json -- live here too: they are
statements about Eden AI specifically, not about the shared schema.
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

# Imported via the providers.edenai package, not a flat `import scrape` off a
# directly-inserted directory: every provider's scraper is a module literally named
# scrape.py, and a flat import would collide in sys.modules the moment two test
# files run in the same process (as `unittest discover` does).
from providers.edenai import scrape  # noqa: E402
import pricing_validate as validate  # noqa: E402
from pricing_validate import JSONDict  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "edenai"
MODELS_OK = FIXTURES / "models_ok.json"
BASELINE_JSON = FIXTURES / "baseline.json"

PRICING_JSON = REPO_ROOT / "pricing.json"
MAPPING_JSON = REPO_ROOT / "scripts" / "providers" / "edenai" / "mapping.json"

FIXED_NOW = "2026-08-10T06:00:00Z"


def mutate(source: str, old: str, new: str, expected_count: int | None = 1) -> str:
    """Replace `old` with `new`, checking first that there was something to replace."""
    found = source.count(old)
    if found == 0 or (expected_count is not None and found != expected_count):
        raise AssertionError(
            f"fixture drift: expected {expected_count or 'at least one'} occurrence(s) of "
            f"{old!r}, found {found}. Re-capture tests/fixtures/edenai/models_ok.json "
            f"from the endpoint."
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

        self.source = self.tmp / "models.json"

    def run_scrape(self, body: str, now: str = FIXED_NOW) -> tuple[int, str, str]:
        self.source.write_text(body, encoding="utf-8")
        argv = [
            "--out-dir", str(self.out_dir),
            "--current", str(self.current),
            "--mapping", str(MAPPING_JSON),
            "--html", str(self.source),
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
        return self.candidate(name)["providers"]["edenai"]

    def notes(self) -> str:
        return (self.out_dir / "notes.txt").read_text(encoding="utf-8")

    @property
    def models_ok(self) -> str:
        return MODELS_OK.read_text(encoding="utf-8")


class TestUnchanged(ScrapeTestCase):
    def test_the_fixture_reproduces_the_baseline_exactly(self) -> None:
        code, stdout, _ = self.run_scrape(self.models_ok)
        self.assertEqual(code, 0)
        self.assertIn("unchanged", stdout)
        self.assertFalse((self.out_dir / "updated.json").exists())
        self.assert_current_untouched()

    def test_only_the_mapped_upstreams_are_published(self) -> None:
        """The fixture carries six models from upstreams the mapping does not cover.
        Publishing whatever the endpoint returns would quietly add OpenAI and
        Anthropic resale prices nobody asked for or reviewed."""
        self.run_scrape(self.models_ok)
        published = self.candidate_block("stamped.json")["models"]
        upstreams = set(json.loads(MAPPING_JSON.read_text(encoding="utf-8"))["upstreams"])
        for model_id in published:
            self.assertIn(model_id.split("/", 1)[0], upstreams, model_id)
        for decoy in ("openai/", "anthropic/", "google/", "groq/", "together_ai/"):
            self.assertFalse([m for m in published if m.startswith(decoy)], decoy)

    def test_other_providers_blocks_are_never_touched(self) -> None:
        doc = json.loads(self.current_bytes)
        doc["providers"]["acme"] = {
            "checked_utc": "2020-01-01T00:00:00Z",
            "updated": "2020-01-01",
            "source": "https://example.invalid/pricing",
            "currency": "GBP",
            "models": {"a-model": {"in_per_mtok": 1.0, "display_name": "A Model"}},
        }
        self.current.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()

        self.run_scrape(self.models_ok)
        candidate = self.candidate("stamped.json")
        self.assertEqual(candidate["providers"]["acme"], doc["providers"]["acme"])
        self.assertEqual(
            list(candidate["providers"]), list(doc["providers"]), "provider key order was rewritten"
        )
        self.assert_current_untouched()


class TestPerMillionScaling(unittest.TestCase):
    """Eden states a price per single token; this file publishes per million. Done
    with a plain float multiply, that is not a formatting nit -- it publishes
    0.39999999999999997 for a price of $0.40, on 48 of this provider's figures, and
    makes them churn in a diff a human is supposed to read for real movements."""

    def test_round_prices_stay_round(self) -> None:
        for raw, expected in ((4e-07, 0.4), (2e-07, 0.2), (1e-07, 0.1), (5e-08, 0.05), (1.5e-06, 1.5)):
            with self.subTest(raw=raw):
                self.assertEqual(scrape.per_million(raw), expected)
                self.assertEqual(repr(scrape.per_million(raw)), repr(expected))

    def test_the_naive_multiply_really_would_be_wrong(self) -> None:
        """Guards the reason this helper exists. If a future Python makes the naive
        form exact, this test says so rather than leaving dead cleverness behind."""
        self.assertNotEqual(4e-07 * 1_000_000, 0.4)

    def test_genuine_long_decimals_are_preserved_not_rounded(self) -> None:
        """Scaleway's figures really do carry many decimals -- Eden converts them
        from EUR. Those must survive: rounding them would invent a price."""
        self.assertEqual(scrape.per_million(2.8712497171819026e-07), 0.28712497171819026)

    def test_no_published_figure_carries_floating_point_noise(self) -> None:
        published = json.loads(PRICING_JSON.read_text(encoding="utf-8"))["providers"]["edenai"]["models"]
        for model_id, entry in published.items():
            for field, value in entry.items():
                if field not in validate.KNOWN_PRICE_FIELDS:
                    continue
                with self.subTest(model=model_id, field=field):
                    # 0.30000000000000004 and friends: a 17-significant-digit tail
                    # that ends in a run of 0s or 9s is noise, not precision.
                    text = repr(float(value))
                    self.assertFalse(
                        text.endswith("0000000000004") or "99999999999" in text,
                        f"{model_id}.{field} = {text} looks like float noise",
                    )


class TestPayloadFailures(ScrapeTestCase):
    def test_not_json_at_all(self) -> None:
        code, _, stderr = self.run_scrape("<html><body>gateway timeout</body></html>")
        self.assert_failed(code, stderr, "not valid JSON")

    def test_no_data_array(self) -> None:
        for body in ('{"object": "list"}', '{"object": "list", "data": []}', '{"data": "nope"}'):
            with self.subTest(body=body):
                code, _, stderr = self.run_scrape(body)
                self.assert_failed(code, stderr, 'no non-empty "data" array')

    def test_a_top_level_list_is_refused(self) -> None:
        code, _, stderr = self.run_scrape('[{"id": "mistral/x"}]')
        self.assert_failed(code, stderr, "not an object")

    def test_an_entry_without_a_string_id(self) -> None:
        code, _, stderr = self.run_scrape(json.dumps({"data": [{"id": 42}]}))
        self.assert_failed(code, stderr, 'no string "id"')

    def test_a_duplicated_model_id_is_refused(self) -> None:
        """Two entries for one id means two prices and no way to tell which is real."""
        body = mutate(
            self.models_ok,
            '"id": "mistral/codestral-latest"',
            '"id": "mistral/mistral-tiny-latest"',
        )
        code, _, stderr = self.run_scrape(body)
        self.assert_failed(code, stderr, "more than once", "Refusing to guess")


class TestModelSetDrift(ScrapeTestCase):
    """Eden's catalogue moves every week. The mapping names upstreams, never models, so
    this is the ordinary case rather than the exceptional one."""

    def test_a_model_that_disappears_is_kept_with_its_last_prices_and_a_date(self) -> None:
        body = mutate(self.models_ok, '"id": "xai/grok-4.5"', '"id": "xai/grok-4.5-renamed"')
        code, _, _ = self.run_scrape(body)
        self.assertEqual(code, 0)

        models = self.candidate_block("updated.json")["models"]
        published = json.loads(self.current_bytes)["providers"]["edenai"]["models"]["xai/grok-4.5"]

        self.assertEqual(models["xai/grok-4.5"]["absent_since"], FIXED_NOW[:10])
        self.assertEqual(models["xai/grok-4.5"]["in_per_mtok"], published["in_per_mtok"])
        self.assertNotIn("absent_since", models["xai/grok-4.5-renamed"])
        self.assertIn("no longer offered", self.notes())
        self.assert_current_untouched()

    def test_a_new_model_from_a_mapped_upstream_is_published(self) -> None:
        """Eden states the id and the price itself, so there is nothing for a human to
        translate and nothing to wait for. The figure still has to pass the bounds and
        the change-factor limit, which is what a reviewer would actually have caught."""
        payload = json.loads(self.models_ok)
        newcomer = dict(payload["data"][0])
        newcomer["id"] = "mistral/mistral-brand-new"
        newcomer["model_name"] = "mistral-brand-new"
        newcomer["owned_by"] = "mistral"
        payload["data"].append(newcomer)
        code, stdout, _ = self.run_scrape(json.dumps(payload))

        self.assertEqual(code, 0)
        self.assertIn("+ mistral/mistral-brand-new: added", stdout)
        self.assertIn("mistral/mistral-brand-new", self.candidate_block("updated.json")["models"])
        self.assertEqual(self.notes(), "", "a model being added is not something to alert about")
        self.assert_current_untouched()

    def test_a_new_model_from_an_unmapped_upstream_is_ignored(self) -> None:
        """The counterpart: Eden adding a Cohere model is none of this block's
        business and must not fail the weekly run."""
        payload = json.loads(self.models_ok)
        newcomer = dict(payload["data"][0])
        newcomer["id"] = "cohere/command-r-plus"
        newcomer["owned_by"] = "cohere"
        payload["data"].append(newcomer)
        code, stdout, _ = self.run_scrape(json.dumps(payload))
        self.assertEqual(code, 0)
        self.assertIn("unchanged", stdout)


class TestPriceFailures(ScrapeTestCase):
    def test_a_missing_required_price_is_refused(self) -> None:
        payload = json.loads(self.models_ok)
        for entry in payload["data"]:
            if entry["id"] == "mistral/codestral-latest":
                # Dropped from BOTH objects: removing it from list_pricing alone
                # makes the two disagree, and the discount guard fires first --
                # which is correct behaviour, but not what this test is about.
                del entry["list_pricing"]["output_cost_per_token"]
                entry["pricing"] = entry["list_pricing"]
        code, _, stderr = self.run_scrape(json.dumps(payload))
        self.assert_failed(code, stderr, "mistral/codestral-latest", "output_cost_per_token")

    def test_a_missing_optional_price_is_simply_absent(self) -> None:
        """cache_read is optional: 39 of 103 models state it. A model losing it must
        drop the field, not fail the run."""
        payload = json.loads(self.models_ok)
        for entry in payload["data"]:
            entry["list_pricing"].pop("cache_read_input_token_cost", None)
            entry["pricing"] = entry["list_pricing"]
        code, stdout, _ = self.run_scrape(json.dumps(payload))
        self.assertEqual(code, 0)
        self.assertIn("changed", stdout)
        published = self.candidate_block("updated.json")["models"]
        self.assertFalse([m for m, e in published.items() if "cache_read_per_mtok" in e])

    def test_a_non_numeric_price_is_refused(self) -> None:
        payload = json.loads(self.models_ok)
        for entry in payload["data"]:
            if entry["id"] == "xai/grok-4.5":
                entry["list_pricing"]["input_cost_per_token"] = "2e-06"
                entry["pricing"] = entry["list_pricing"]
        code, _, stderr = self.run_scrape(json.dumps(payload))
        self.assert_failed(code, stderr, "xai/grok-4.5", "not a number")

    def test_a_discounted_read_is_refused(self) -> None:
        """An unauthenticated read must never be quoted a rate only it can get: that
        price would be wrong for every consumer of this file."""
        payload = json.loads(self.models_ok)
        for entry in payload["data"]:
            if entry["id"] == "mistral/codestral-latest":
                entry["pricing"] = dict(entry["list_pricing"])
                entry["pricing"]["input_cost_per_token"] = 1e-07
        code, _, stderr = self.run_scrape(json.dumps(payload))
        self.assert_failed(code, stderr, "list_pricing and pricing disagree")

    def test_an_absurd_figure_is_refused(self) -> None:
        payload = json.loads(self.models_ok)
        for entry in payload["data"]:
            if entry["id"] == "xai/grok-4.5":
                entry["list_pricing"]["input_cost_per_token"] = 0.5  # $500k per Mtok
                entry["pricing"] = entry["list_pricing"]
        code, _, stderr = self.run_scrape(json.dumps(payload))
        self.assert_failed(code, stderr, "outside the plausible range")

    def test_a_figure_that_moves_too_far_is_refused(self) -> None:
        payload = json.loads(self.models_ok)
        for entry in payload["data"]:
            if entry["id"] == "xai/grok-4.5":
                entry["list_pricing"]["input_cost_per_token"] = 2e-05  # x10
                entry["pricing"] = entry["list_pricing"]
        code, _, stderr = self.run_scrape(json.dumps(payload))
        self.assert_failed(code, stderr, "factor of", "Refusing to publish")


class TestPriceChange(ScrapeTestCase):
    def test_a_real_change_produces_a_candidate_not_a_publication(self) -> None:
        payload = json.loads(self.models_ok)
        for entry in payload["data"]:
            if entry["id"] == "mistral/mistral-medium-2604":
                entry["list_pricing"]["input_cost_per_token"] = 2e-06
                entry["pricing"] = entry["list_pricing"]
        code, stdout, _ = self.run_scrape(json.dumps(payload))
        self.assertEqual(code, 0)
        self.assertIn("changed", stdout)
        self.assertIn("mistral/mistral-medium-2604", stdout)
        updated = self.candidate_block("updated.json")
        self.assertEqual(updated["models"]["mistral/mistral-medium-2604"]["in_per_mtok"], 2.0)
        self.assert_current_untouched()


class TestPublishedFileMatchesEdenAI(unittest.TestCase):
    maxDiff = None

    def block(self) -> JSONDict:
        return json.loads(PRICING_JSON.read_text(encoding="utf-8"))["providers"]["edenai"]

    def test_the_committed_file_matches_the_mapping(self) -> None:
        """There is no model list to agree with any more, so what is checked is what the
        mapping still decides: where the figures come from and what currency they are
        in."""
        published = self.block()
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
        self.assertEqual(published["source"], mapping["source"])
        self.assertEqual(published["currency"], mapping["currency"])

        producible = set(mapping["fields"])
        for model_id, entry in published["models"].items():
            fields = {k for k in entry if k in validate.KNOWN_PRICE_FIELDS}
            self.assertTrue(fields, model_id)
            self.assertLessEqual(fields, producible, model_id)

    def test_every_key_is_prefixed_by_a_mapped_upstream(self) -> None:
        """Eden's model id namespaces the upstream, and the key is that whole id --
        it is the exact string a consumer passes to Eden's API."""
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
        upstreams = set(mapping["upstreams"])
        for model_id in self.block()["models"]:
            self.assertIn("/", model_id, model_id)
            self.assertIn(model_id.split("/", 1)[0], upstreams, model_id)

    def test_the_published_figures_have_room_to_fall(self) -> None:
        for model_id, entry in self.block()["models"].items():
            for field, value in entry.items():
                if field not in validate.KNOWN_PRICE_FIELDS:
                    continue
                low, _ = validate.PRICE_BOUNDS.get(field, (validate.MIN_PLAUSIBLE, validate.MAX_PLAUSIBLE))
                with self.subTest(model=model_id, field=field):
                    self.assertGreaterEqual(
                        float(value), low * validate.MAX_CHANGE_FACTOR,
                        "too close to the floor: a real price cut would fail the job",
                    )

    def test_the_fixture_baseline_is_valid(self) -> None:
        validate.validate_document(json.loads(BASELINE_JSON.read_text(encoding="utf-8")))


class TestMapping(unittest.TestCase):
    def mapping(self) -> JSONDict:
        return json.loads(MAPPING_JSON.read_text(encoding="utf-8"))

    def test_the_mapping_is_shaped_as_the_scraper_expects(self) -> None:
        mapping = self.mapping()
        self.assertTrue(mapping["upstreams"])
        for field, spec in mapping["fields"].items():
            self.assertIn(field, validate.KNOWN_PRICE_FIELDS, field)
            self.assertTrue(spec["api_field"], field)

    def test_there_is_no_model_list(self) -> None:
        """Load-bearing, not pedantry. A hand-written model list is exactly what made a
        single retired model block 102 correct prices, and re-adding one would bring
        that back without anything else in the tests noticing."""
        self.assertNotIn("models", self.mapping())

    def test_upstreams_are_unique_and_sorted(self) -> None:
        upstreams = self.mapping()["upstreams"]
        self.assertEqual(len(upstreams), len(set(upstreams)))
        self.assertEqual(upstreams, sorted(upstreams))


if __name__ == "__main__":
    unittest.main()
