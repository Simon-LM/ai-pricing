"""Tests for the Scaleway scraper, and for what happens when things break.

Standard library only: `python3 -m unittest discover tests`.

Every scenario asserts the same thing at the end, through `assert_current_untouched`:
pricing.json is byte-for-byte what it was. That is the property the whole design
exists to protect -- a stale price whose date the user can see is far better than a
wrong one they cannot.

Scaleway's source is a JSON API rather than a marketing page, so the failure modes
worth testing are the ones that endpoint can actually produce: a payload changing
shape, one model arriving as a dozen separate entries (one per token type, per
consumption mode, per region) and having to be reassembled without publishing the
same field twice or confusing a batch tier with the realtime one, and a price stated
as whole units plus nanoseconds being scaled to per-million without publishing
floating-point noise.

Cross-file consistency checks -- does providers.scaleway in the committed pricing.json
actually match scripts/providers/scaleway/mapping.json -- live here too: they are
statements about Scaleway specifically, not about the shared schema.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import unittest
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
# The repository root too, so `tests.published` resolves both under
# `unittest discover` and when this file is run directly as a script.
sys.path.insert(0, str(REPO_ROOT))

# Imported via the providers.scaleway package, not a flat `import scrape` off a
# directly-inserted directory: every provider's scraper is a module literally named
# scrape.py, and a flat import would collide in sys.modules the moment two test
# files run in the same process (as `unittest discover` does).
from providers.scaleway import scrape  # noqa: E402
import pricing_validate as validate  # noqa: E402
from pricing_validate import JSONDict  # noqa: E402
from tests.published import (  # noqa: E402
    assert_price_fields_are_producible,
    published_file,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "scaleway"
CATALOG_OK = FIXTURES / "catalog_ok.json"
BASELINE_JSON = FIXTURES / "baseline.json"

# Not REPO_ROOT / "pricing.json": a refresh points this at the candidate it is
# about to commit, so these checks run on the bytes about to be published.
PRICING_JSON = published_file()
MAPPING_JSON = REPO_ROOT / "scripts" / "providers" / "scaleway" / "mapping.json"

FIXED_NOW = "2026-11-03T09:00:00Z"
TODAY = FIXED_NOW[:10]

# Every status the catalogue uses. Kept here rather than read from the mapping, so
# that a mapping which quietly starts publishing "retired" is caught rather than
# agreed with.
ALL_STATUSES = ("general_availability", "preview", "end_of_new_features", "end_of_life", "retired")
WITHDRAWN_STATUSES = ("end_of_life", "retired")

# The (token type, consumption mode) pairs the mapping names, and the field each is
# published under. The mode is part of the descriptor: the batch rate of a unit is a
# different field, not a second reading of the same one.
FIELDS_BY_DESCRIPTOR = {
    ("input_token", "realtime"): "in_per_mtok",
    ("output_token", "realtime"): "out_per_mtok",
    ("input_cached_token", "realtime"): "cache_read_per_mtok",
    ("input_audio", "realtime"): "per_audio_minute",
    ("input_token", "batch"): "in_per_mtok_batch",
    ("output_token", "batch"): "out_per_mtok_batch",
    ("input_cached_token", "batch"): "cache_read_per_mtok_batch",
    ("input_audio", "batch"): "per_audio_minute_batch",
}

# Some skus worth naming, so a test that edits one reads as what it is doing.
GLM_5_3_INPUT = "/ai/generative_apis/consumption/glm_5_3_input/fr-par"
GLM_5_3_INPUT_BATCH = "/ai/generative_apis/consumption/glm_5_3_input_batch/fr-par"
WHISPER_AUDIO = "/ai/generative_apis/consumption/whisper_large_v3_input_audio/fr-par"
WHISPER_AUDIO_BATCH = "/ai/generative_apis/consumption/whisper_large_v3_input_audio_batch/fr-par"
BGE_INPUT = "/ai/generative_apis/consumption/bge_multilingual_gemma2_input/fr-par"
GLM_5_2_OUTPUT = "/ai/generative_apis/consumption/glm_5_2_output/fr-par"
GLM_5_2_OUTPUT_BATCH = "/ai/generative_apis/consumption/glm_5_2_output_batch/fr-par"


def entry(payload: JSONDict, sku: str) -> JSONDict:
    """The catalogue entry with this sku, or a loud failure.

    Fixture drift has to fail rather than silently mutate nothing: a test that edits
    an entry which is no longer there passes while proving nothing.
    """
    for product in payload["products"]:
        if product["sku"] == sku:
            return product
    raise AssertionError(f"fixture drift: no catalogue entry with sku {sku!r}. Re-capture tests/fixtures/scaleway/catalog_ok.json.")


def entries_for(payload: JSONDict, model_id: str, *, mode: str | None = None, token_type: str | None = None) -> list[JSONDict]:
    """The entries one model is assembled from, optionally narrowed to one kind."""
    found = [
        product
        for product in payload["products"]
        if product["product"] == model_id
        and (mode is None or product["properties"]["generative_apis"]["consumption_mode"] == mode)
        and (token_type is None or product["properties"]["generative_apis"]["token_type"] == token_type)
    ]
    if not found:
        raise AssertionError(
            f"fixture drift: no catalogue entry for {model_id!r} (mode={mode!r}, "
            f"token_type={token_type!r}). Re-capture tests/fixtures/scaleway/catalog_ok.json."
        )
    return found


def set_nanos(payload: JSONDict, model_id: str, token_type: str, nanos: int, *, mode: str = "realtime") -> None:
    """Point the catalogue's price for one (model, token type, mode) at `nanos`."""
    for product in entries_for(payload, model_id, mode=mode, token_type=token_type):
        product["price"]["retail_price"]["nanos"] = nanos


def drop_model(payload: JSONDict, model_id: str) -> None:
    payload["products"] = [p for p in payload["products"] if p["product"] != model_id]


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

        self.source = self.tmp / "catalog.json"

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

    def run_payload(self, payload: JSONDict, now: str = FIXED_NOW) -> tuple[int, str, str]:
        return self.run_scrape(json.dumps(payload), now)

    def payload(self) -> JSONDict:
        return json.loads(self.models_ok)

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
        return self.candidate(name)["providers"]["scaleway"]

    def notes(self) -> str:
        return (self.out_dir / "notes.txt").read_text(encoding="utf-8")

    @property
    def models_ok(self) -> str:
        return CATALOG_OK.read_text(encoding="utf-8")


class TestUnchanged(ScrapeTestCase):
    def test_the_fixture_reproduces_the_baseline_exactly(self) -> None:
        code, stdout, _ = self.run_scrape(self.models_ok)
        self.assertEqual(code, 0)
        self.assertIn("unchanged", stdout)
        self.assertFalse((self.out_dir / "updated.json").exists())
        self.assert_current_untouched()

    def test_only_the_models_the_catalogue_sells_are_published(self) -> None:
        """The fixture carries 33 models, 16 of them end_of_life or retired. Publishing
        whatever the catalogue returns would republish a model Scaleway has withdrawn as
        though it were still on sale, at a price nobody can pay."""
        self.run_scrape(self.models_ok)
        published = self.candidate_block("stamped.json")["models"]
        catalogue = self.payload()

        for model_id, entry in published.items():
            with self.subTest(model=model_id):
                statuses = {p["status"] for p in entries_for(catalogue, model_id)}
                self.assertEqual(len(statuses), 1)
                self.assertNotIn(statuses.pop(), WITHDRAWN_STATUSES)

        for model_id in ("deepseek-r1", "mistral-ocr-2512", "voxtral-small-24b-2507"):
            self.assertNotIn(model_id, published, f"{model_id} is withdrawn upstream")

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


class TestWhatIsNotPublished(ScrapeTestCase):
    def test_a_withdrawn_model_keeps_its_last_prices_and_gains_a_date(self) -> None:
        payload = self.payload()
        for product in entries_for(payload, "glm-5.3"):
            product["status"] = "end_of_life"
        code, stdout, _ = self.run_payload(payload)
        self.assertEqual(code, 0)
        self.assertIn("changed", stdout)

        models = self.candidate_block("updated.json")["models"]
        before = json.loads(self.current_bytes)["providers"]["scaleway"]["models"]["glm-5.3"]
        self.assertEqual(models["glm-5.3"]["absent_since"], TODAY)
        self.assertEqual(models["glm-5.3"]["in_per_mtok"], before["in_per_mtok"])
        self.assertIn("no longer offered", self.notes())
        self.assert_current_untouched()

    def test_a_model_that_comes_back_is_priced_again(self) -> None:
        doc = json.loads(self.current_bytes)
        doc["providers"]["scaleway"]["models"]["glm-5.3"]["absent_since"] = "2026-10-01"
        self.current.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()

        code, stdout, _ = self.run_scrape(self.models_ok)
        self.assertEqual(code, 0)
        self.assertIn("changed", stdout)
        models = self.candidate_block("updated.json")["models"]
        self.assertNotIn("absent_since", models["glm-5.3"])
        self.assertIn("offered again", self.notes())
        self.assert_current_untouched()

    def test_a_model_absent_for_a_year_is_dropped(self) -> None:
        doc = json.loads(self.current_bytes)
        doc["providers"]["scaleway"]["models"]["legacy-model"] = {
            "in_per_mtok": 1.0,
            "display_name": "legacy-model",
            "absent_since": "2024-01-01",
        }
        self.current.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()

        self.run_scrape(self.models_ok)
        self.assertNotIn("legacy-model", self.candidate_block("updated.json")["models"])
        self.assertIn("Dropped from the file", self.notes())
        self.assert_current_untouched()

    def test_a_batch_tier_is_published_under_its_own_field(self) -> None:
        """Both tiers are read from the catalogue and neither is derived from the other.
        The discount is usually half -- whisper-large-v3 halves (0.003 -> 0.0015 per
        audio minute) -- but not always: glm-5.2's output token goes 5.50 -> 2.70 where
        glm-5.3's goes 5.50 -> 2.75. Halving would publish a figure Scaleway never
        wrote, so the realtime field must keep the realtime figure and the '_batch'
        field must carry the batch one, side by side."""
        self.run_scrape(self.models_ok)
        models = self.candidate_block("stamped.json")["models"]
        catalogue = self.payload()

        cases = (
            (GLM_5_3_INPUT, GLM_5_3_INPUT_BATCH, 1.8, 0.9),
            (WHISPER_AUDIO, WHISPER_AUDIO_BATCH, 0.003, 0.0015),
            (GLM_5_2_OUTPUT, GLM_5_2_OUTPUT_BATCH, 5.5, 2.7),
        )
        for realtime_sku, batch_sku, expected, batch_figure in cases:
            with self.subTest(sku=realtime_sku):
                realtime = entry(catalogue, realtime_sku)
                batch = entry(catalogue, batch_sku)
                self.assertNotEqual(
                    realtime["price"]["retail_price"]["nanos"],
                    batch["price"]["retail_price"]["nanos"],
                    "fixture drift: the two tiers no longer differ, so this proves nothing",
                )
                apis = realtime["properties"]["generative_apis"]
                field = FIELDS_BY_DESCRIPTOR[(apis["token_type"], "realtime")]
                batch_field = FIELDS_BY_DESCRIPTOR[(apis["token_type"], "batch")]
                self.assertEqual(models[realtime["product"]][field], expected)
                self.assertEqual(models[realtime["product"]][batch_field], batch_figure)
                # Not one field quietly holding the other's figure.
                self.assertNotEqual(models[realtime["product"]][field], models[realtime["product"]][batch_field])

    def test_another_region_is_reported_rather_than_published(self) -> None:
        payload = self.payload()
        for product in entries_for(payload, "glm-5.3"):
            product["locality"]["region"] = "nl-ams"
        code, _, _ = self.run_payload(payload)
        self.assertEqual(code, 0)

        models = self.candidate_block("updated.json")["models"]
        self.assertEqual(models["glm-5.3"]["absent_since"], TODAY)
        self.assertIn("nl-ams", self.notes())
        self.assertIn("one price per model", self.notes())
        self.assert_current_untouched()

    def test_a_model_still_sold_without_a_price_is_marked_unpriced(self) -> None:
        """Offered, not free, and no figure this file can stand behind: the difference
        between a model that does not exist and one that must not be called before the
        cost is checked."""
        payload = self.payload()
        for product in entries_for(payload, "glm-5.3"):
            del product["price"]
        code, stdout, _ = self.run_payload(payload)
        self.assertEqual(code, 0)
        self.assertIn("changed", stdout)

        models = self.candidate_block("updated.json")["models"]
        self.assertEqual(models["glm-5.3"]["unpriced_since"], TODAY)
        self.assertNotIn("free", models["glm-5.3"])
        # The last prices actually observed are kept beside the marker, so a consumer
        # has a figure to reason with and the warning that it is no longer stood behind.
        self.assertEqual(models["glm-5.3"]["in_per_mtok"], 1.8)
        self.assertIn("unpriced_since", self.notes())
        self.assert_current_untouched()

    def test_a_model_sold_only_in_batch_publishes_only_its_batch_fields(self) -> None:
        """Both tiers are the same unit at different rates, so a model Scaleway offers
        only in batch must not have its batch figure published as the realtime one --
        and must still be published rather than marked unpriced."""
        payload = self.payload()
        payload["products"] = [
            product
            for product in payload["products"]
            if not (
                product["product"] == "glm-5.3"
                and product["properties"]["generative_apis"]["consumption_mode"] == "batch"
            )
        ]
        for product in entries_for(payload, "glm-5.3"):
            product["properties"]["generative_apis"]["consumption_mode"] = "batch"
        code, _, _ = self.run_payload(payload)
        self.assertEqual(code, 0)

        model = self.candidate_block("updated.json")["models"]["glm-5.3"]
        self.assertEqual(model["in_per_mtok_batch"], 1.8)
        self.assertEqual(model["out_per_mtok_batch"], 5.5)
        self.assertNotIn("in_per_mtok", model)
        self.assertNotIn("out_per_mtok", model)
        self.assertNotIn("unpriced_since", model)
        self.assert_current_untouched()

    def test_a_unit_this_mapping_cannot_name_is_reported_not_guessed_at(self) -> None:
        """The unit is part of the field name: a price per image published under a
        per-token key would be worse than publishing nothing."""
        payload = self.payload()
        for product in entries_for(payload, "bge-multilingual-gemma2"):
            product["properties"]["generative_apis"]["token_type"] = "input_image"
        code, _, _ = self.run_payload(payload)
        self.assertEqual(code, 0)

        models = self.candidate_block("updated.json")["models"]
        self.assertEqual(models["bge-multilingual-gemma2"]["unpriced_since"], TODAY)
        self.assertIn("input_image", self.notes())
        self.assertIn("no field for", self.notes())
        # One model skipped is not the others withheld.
        self.assertEqual(models["glm-5.3"]["in_per_mtok"], 1.8)
        self.assert_current_untouched()

    def test_a_model_priced_at_zero_everywhere_is_free_not_priced_at_zero(self) -> None:
        """0 is also exactly what a broken parser reads, which is what check_price's
        floor exists to catch; "free" is a statement the catalogue makes, not a price."""
        payload = self.payload()
        for product in entries_for(payload, "glm-5.3"):
            product["price"]["retail_price"]["nanos"] = 0
        code, stdout, _ = self.run_payload(payload)
        self.assertEqual(code, 0)
        self.assertIn("changed", stdout)

        models = self.candidate_block("updated.json")["models"]
        self.assertEqual(models["glm-5.3"], {"free": True, "display_name": "glm-5.3"})
        self.assertEqual(self.notes(), "", "a model becoming free is not something to alert about")
        self.assert_current_untouched()

    def test_a_zero_beside_a_real_price_is_reported_rather_than_published(self) -> None:
        payload = self.payload()
        for product in entries_for(payload, "glm-5.3", mode="realtime", token_type="input_token"):
            product["price"]["retail_price"]["nanos"] = 0
        code, _, _ = self.run_payload(payload)
        self.assertEqual(code, 0)

        models = self.candidate_block("updated.json")["models"]
        self.assertNotIn("in_per_mtok", models["glm-5.3"])
        self.assertEqual(models["glm-5.3"]["out_per_mtok"], 5.5)
        self.assertNotIn("free", models["glm-5.3"])
        self.assertIn("priced at 0 in in_per_mtok", self.notes())
        self.assert_current_untouched()


class TestModelSetDrift(ScrapeTestCase):
    """Scaleway's catalogue moves every week. The mapping names units, never models, so
    this is the ordinary case rather than the exceptional one."""

    def test_a_model_that_disappears_is_kept_with_its_last_prices_and_a_date(self) -> None:
        payload = self.payload()
        drop_model(payload, "glm-5.3")
        code, _, _ = self.run_payload(payload)
        self.assertEqual(code, 0)

        models = self.candidate_block("updated.json")["models"]
        before = json.loads(self.current_bytes)["providers"]["scaleway"]["models"]["glm-5.3"]
        self.assertEqual(models["glm-5.3"]["absent_since"], TODAY)
        self.assertEqual(models["glm-5.3"]["in_per_mtok"], before["in_per_mtok"])
        self.assertIn("no longer offered", self.notes())
        self.assert_current_untouched()

    def test_a_new_model_is_published_without_anyone_agreeing_a_list(self) -> None:
        payload = self.payload()
        template = entry(payload, GLM_5_3_INPUT)
        for token_type, field in (("input_token", "in_per_mtok"), ("output_token", "out_per_mtok")):
            newcomer = json.loads(json.dumps(template))
            newcomer["sku"] = f"/ai/generative_apis/consumption/brand_new_input/fr-par-{token_type}"
            newcomer["product"] = "brand-new-model"
            newcomer["properties"]["generative_apis"]["token_type"] = token_type
            payload["products"].append(newcomer)

        code, stdout, _ = self.run_payload(payload)
        self.assertEqual(code, 0)
        self.assertIn("+ brand-new-model: added", stdout)

        models = self.candidate_block("updated.json")["models"]
        self.assertEqual(models["brand-new-model"]["in_per_mtok"], 1.8)
        self.assertEqual(models["brand-new-model"]["out_per_mtok"], 1.8)
        self.assertEqual(self.notes(), "", "a model being added is not something to alert about")
        self.assert_current_untouched()

    def test_a_model_with_a_new_token_type_is_published_from_what_is_left(self) -> None:
        """A token type the mapping does not name costs that one field, not the model:
        the other figures it states are still correct and still published."""
        payload = self.payload()
        for product in entries_for(payload, "deepseek-v4-flash-0731", mode="realtime", token_type="input_token"):
            product["properties"]["generative_apis"]["token_type"] = "input_image"
        code, _, _ = self.run_payload(payload)
        self.assertEqual(code, 0)

        models = self.candidate_block("updated.json")["models"]
        entry_after = models["deepseek-v4-flash-0731"]
        self.assertNotIn("in_per_mtok", entry_after)
        self.assertEqual(entry_after["out_per_mtok"], 0.8)
        self.assertEqual(entry_after["cache_read_per_mtok"], 0.08)
        self.assertIn("input_image", self.notes())
        self.assert_current_untouched()


class TestCatalogueFailures(ScrapeTestCase):
    def test_not_json_at_all(self) -> None:
        code, _, stderr = self.run_scrape("<html><body>gateway timeout</body></html>")
        self.assert_failed(code, stderr, "not valid JSON")

    def test_no_products_array(self) -> None:
        for body in ('{"total_count": 0}', '{"products": []}', '{"products": "nope"}'):
            with self.subTest(body=body):
                code, _, stderr = self.run_scrape(body)
                self.assert_failed(code, stderr, 'no non-empty "products" array')

    def test_a_top_level_list_is_refused(self) -> None:
        code, _, stderr = self.run_scrape('[{"product": "glm-5.3"}]')
        self.assert_failed(code, stderr, "not an object")

    def test_an_entry_without_a_callable_id_is_refused(self) -> None:
        for key in ("product", "sku"):
            with self.subTest(key=key):
                payload = self.payload()
                del entry(payload, GLM_5_3_INPUT)[key]
                code, _, stderr = self.run_payload(payload)
                self.assert_failed(code, stderr, f'no non-empty string "{key}"')

    def test_two_entries_for_one_field_are_refused(self) -> None:
        """Two prices for one field and no way to tell which is real."""
        payload = self.payload()
        duplicate = json.loads(json.dumps(entry(payload, GLM_5_3_INPUT)))
        duplicate["sku"] = GLM_5_3_INPUT + "-copy"
        duplicate["price"]["retail_price"]["nanos"] = 2400000
        payload["products"].append(duplicate)
        code, _, stderr = self.run_payload(payload)
        self.assert_failed(code, stderr, "prices it twice", "Refusing to guess")

    def test_entries_disagreeing_about_status_are_refused(self) -> None:
        payload = self.payload()
        entries_for(payload, "glm-5.3")[0]["status"] = "retired"
        code, _, stderr = self.run_payload(payload)
        self.assert_failed(code, stderr, "across its entries", "Refusing to guess")

    def test_a_currency_other_than_the_published_one_is_refused(self) -> None:
        payload = self.payload()
        entry(payload, GLM_5_3_INPUT)["price"]["retail_price"]["currency_code"] = "USD"
        code, _, stderr = self.run_payload(payload)
        self.assert_failed(code, stderr, "'USD'", "'EUR'", "no conversion")

    def test_a_non_integer_nanos_is_refused(self) -> None:
        payload = self.payload()
        entry(payload, GLM_5_3_INPUT)["price"]["retail_price"]["nanos"] = "1800000"
        code, _, stderr = self.run_payload(payload)
        self.assert_failed(code, stderr, "retail_price.nanos", "not a whole number")


class TestPriceFailures(ScrapeTestCase):
    def test_an_absurd_figure_is_refused(self) -> None:
        payload = self.payload()
        set_nanos(payload, "glm-5.3", "input_token", 500_000_000_000)  # 500000 per Mtok
        code, _, stderr = self.run_payload(payload)
        self.assert_failed(code, stderr, "outside the plausible range")

    def test_a_figure_that_moves_too_far_is_refused(self) -> None:
        payload = self.payload()
        set_nanos(payload, "glm-5.3", "input_token", 18_000_000)  # x10
        code, _, stderr = self.run_payload(payload)
        self.assert_failed(code, stderr, "factor of", "Refusing to publish")

    def test_a_catalogue_that_prices_nothing_at_all_is_refused(self) -> None:
        """Every model unpriced would quietly turn the whole provider into last-known
        figures on the strength of one bad read."""
        payload = self.payload()
        for product in payload["products"]:
            if product["status"] not in WITHDRAWN_STATUSES:
                product["properties"]["generative_apis"]["token_type"] = "input_image"
        code, _, stderr = self.run_payload(payload)
        self.assert_failed(code, stderr, "not a single model")


class TestPriceChange(ScrapeTestCase):
    def test_a_real_change_produces_a_candidate_not_a_publication(self) -> None:
        payload = self.payload()
        set_nanos(payload, "glm-5.3", "input_token", 2_400_000)
        code, stdout, _ = self.run_payload(payload)
        self.assertEqual(code, 0)
        self.assertIn("changed", stdout)
        self.assertIn("glm-5.3.in_per_mtok", stdout)

        updated = self.candidate_block("updated.json")
        self.assertEqual(updated["models"]["glm-5.3"]["in_per_mtok"], 2.4)
        self.assertEqual(updated["updated"], TODAY)
        self.assertEqual(updated["checked_utc"], FIXED_NOW)
        self.assert_current_untouched()

    def test_the_candidate_keeps_every_other_model_exactly_as_it_was(self) -> None:
        payload = self.payload()
        set_nanos(payload, "whisper-large-v3", "input_audio", 4_000_000)
        self.run_payload(payload)

        before = json.loads(self.current_bytes)["providers"]["scaleway"]["models"]
        after = self.candidate_block("updated.json")["models"]
        self.assertEqual(after["whisper-large-v3"]["per_audio_minute"], 0.004)
        for model_id in before:
            if model_id != "whisper-large-v3":
                with self.subTest(model=model_id):
                    self.assertEqual(after[model_id], before[model_id])
        self.assert_current_untouched()


class TestNanosScaling(unittest.TestCase):
    """The catalogue states a price as whole units plus nanoseconds of one, and quotes
    it per 1000 tokens while this file publishes per million. Done with a plain float
    multiply that is not a formatting nit: it publishes 0.030000000000000002 for a
    price of 0.03, on 461 of the 2000 plausible nanosecond values below 0.02."""

    MTOK = {"unit": "token", "size": 1000, "per": 1_000_000}
    MINUTE = {"unit": "second", "size": 60, "per": 60}

    def test_round_prices_stay_round(self) -> None:
        cases = (
            (0, 1_800_000, self.MTOK, 1.8),
            (0, 5_500_000, self.MTOK, 5.5),
            (0, 30_000, self.MTOK, 0.03),
            (0, 100_000, self.MTOK, 0.1),
            (0, 3_000_000, self.MINUTE, 0.003),
            (0, 1_500_000, self.MINUTE, 0.0015),
            (2, 0, self.MTOK, 2000.0),
        )
        for units, nanos, spec, expected in cases:
            with self.subTest(units=units, nanos=nanos):
                value = scrape.per_unit({"units": units, "nanos": nanos}, spec)
                self.assertEqual(value, Decimal(repr(expected)))
                self.assertEqual(float(value), expected)
                self.assertEqual(repr(float(value)), repr(expected))

    def test_the_naive_multiply_really_would_be_wrong(self) -> None:
        """Guards the reason the conversion goes through Decimal. If a future Python
        made the naive form exact, this test says so rather than leaving dead
        cleverness behind."""
        self.assertNotEqual(30_000 / 1e9 * 1_000, 0.03)
        self.assertEqual(repr(30_000 / 1e9 * 1_000), "0.030000000000000002")

    def test_the_audio_price_is_published_per_minute(self) -> None:
        """The catalogue quotes per 60 seconds; the field is per_audio_minute."""
        self.assertEqual(float(scrape.per_unit({"units": 0, "nanos": 3_000_000}, self.MINUTE)), 0.003)
        self.assertEqual(float(scrape.per_unit({"units": 0, "nanos": 1_500_000}, self.MINUTE)), 0.0015)

    def test_no_published_figure_carries_floating_point_noise(self) -> None:
        published = json.loads(PRICING_JSON.read_text(encoding="utf-8"))["providers"]["scaleway"]["models"]
        for model_id, model_entry in published.items():
            for field, value in model_entry.items():
                if field not in validate.KNOWN_PRICE_FIELDS:
                    continue
                with self.subTest(model=model_id, field=field):
                    # 0.030000000000000002 and friends: a 17-significant-digit tail
                    # that ends in a run of 0s or 9s is noise, not precision.
                    text = repr(float(value))
                    self.assertFalse(
                        text.endswith("0000000000002") or text.endswith("99999999999999") or "99999999999" in text,
                        f"{model_id}.{field} = {text} looks like float noise",
                    )


class TestPublishedFileMatchesScaleway(unittest.TestCase):
    maxDiff = None

    def block(self) -> JSONDict:
        return json.loads(PRICING_JSON.read_text(encoding="utf-8"))["providers"]["scaleway"]

    def mapping(self) -> JSONDict:
        return json.loads(MAPPING_JSON.read_text(encoding="utf-8"))

    def test_the_committed_file_matches_the_mapping(self) -> None:
        """There is no model list to agree with any more, so what is checked is what the
        mapping still decides: where the figures come from, what currency they are in,
        and that every field published is one the mapping can produce."""
        published = self.block()
        mapping = self.mapping()
        self.assertEqual(published["source"], mapping["source"])
        self.assertEqual(published["currency"], mapping["currency"])

        producible = set(mapping["fields"])
        assert_price_fields_are_producible(self, published["models"], producible)

    def test_the_catalogue_is_declared_a_json_endpoint(self) -> None:
        """The source is an API, not a page. Declared `html` it would be asked for HTML
        and held to the 10 kB page floor, which is a rule about marketing copy rather
        than about data."""
        self.assertEqual(self.mapping()["format"], "json")

    def test_every_key_is_the_callable_model_id(self) -> None:
        """Scaleway states the callable id itself, so the key is that string as-is:
        bare, not namespaced like Eden's, and identical to the display name -- there is
        nothing for a hand-written translation to get wrong."""
        for model_id, entry_ in self.block()["models"].items():
            with self.subTest(model=model_id):
                self.assertNotIn("/", model_id, "a sku would not be a callable model id")
                self.assertEqual(entry_["display_name"], model_id)

    def test_the_published_figures_have_room_to_fall(self) -> None:
        for model_id, entry_ in self.block()["models"].items():
            for field, value in entry_.items():
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

    def test_the_fixture_carries_every_status_the_catalogue_uses(self) -> None:
        """The withdrawn ones are the load-bearing part: without them nothing here
        would notice the scraper starting to publish a model Scaleway has retired."""
        statuses = {p["status"] for p in json.loads(CATALOG_OK.read_text(encoding="utf-8"))["products"]}
        self.assertEqual(statuses, set(ALL_STATUSES))


class TestMapping(unittest.TestCase):
    def mapping(self) -> JSONDict:
        return json.loads(MAPPING_JSON.read_text(encoding="utf-8"))

    def test_the_mapping_is_shaped_as_the_scraper_expects(self) -> None:
        mapping = self.mapping()
        self.assertTrue(mapping["region"])
        self.assertTrue(mapping["published_statuses"])
        for field, spec in mapping["fields"].items():
            with self.subTest(field=field):
                self.assertIn(field, validate.KNOWN_PRICE_FIELDS, field)
                self.assertTrue(spec["token_type"], field)
                self.assertIn(spec["consumption_mode"], ("realtime", "batch"), field)
                self.assertGreater(spec["size"], 0, field)
                self.assertGreater(spec["per"], 0, field)

    def test_the_mapping_names_every_published_token_type(self) -> None:
        """A token type the catalogue uses and the mapping does not name is silently
        dropped from the block, one field at a time -- which is what the notes exist to
        report. This checks the two are not already out of step."""
        mapping = self.mapping()
        named = {spec["token_type"] for spec in mapping["fields"].values()}
        catalogue = json.loads(CATALOG_OK.read_text(encoding="utf-8"))["products"]
        used = {
            p["properties"]["generative_apis"]["token_type"]
            for p in catalogue
            if p["status"] in mapping["published_statuses"]
        }
        self.assertLessEqual(used, named, f"unmapped token types: {sorted(used - named)}")

    def test_withdrawn_statuses_are_not_published(self) -> None:
        published = set(self.mapping()["published_statuses"])
        self.assertFalse(published & set(WITHDRAWN_STATUSES), sorted(published & set(WITHDRAWN_STATUSES)))

    def test_batch_rates_are_read_under_their_own_field(self) -> None:
        """Load-bearing: the batch half of the mapping is what keeps a discounted rate
        from being published as the realtime one, and the realtime rate from being
        published as the batch one. One field per unit per mode, and the batch field
        named off the realtime one so the two cannot drift apart."""
        mapping = self.mapping()
        realtime = {f for f, spec in mapping["fields"].items() if spec["consumption_mode"] == "realtime"}
        batch = {f for f, spec in mapping["fields"].items() if spec["consumption_mode"] == "batch"}
        self.assertTrue(realtime)
        self.assertEqual(batch, {f"{field}_batch" for field in realtime})

    def test_every_consumption_mode_the_catalogue_uses_is_named(self) -> None:
        """A mode the catalogue uses and the mapping does not name is silently dropped
        from the block, one field at a time. Scaleway sells every model in two modes, so
        a mapping that lost the batch half would go unnoticed without this."""
        mapping = self.mapping()
        named = {spec["consumption_mode"] for spec in mapping["fields"].values()}
        catalogue = json.loads(CATALOG_OK.read_text(encoding="utf-8"))["products"]
        used = {
            p["properties"]["generative_apis"]["consumption_mode"]
            for p in catalogue
            if p["status"] in mapping["published_statuses"]
        }
        self.assertLessEqual(used, named, f"unmapped consumption modes: {sorted(used - named)}")

    def test_there_is_no_model_list(self) -> None:
        """A hand-written model list is exactly what made a single retired model block
        every correct price in another provider here, and re-adding one would bring that
        back without anything else in the tests noticing."""
        self.assertNotIn("models", self.mapping())


if __name__ == "__main__":
    unittest.main()
