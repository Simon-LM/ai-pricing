"""Tests for the Hugging Face router scraper, and for what happens when things break.

Standard library only: `python3 -m unittest discover tests`.

Every scenario asserts the same thing at the end, through `assert_current_untouched`:
pricing.json is byte-for-byte what it was. That is the property the whole design
exists to protect -- a stale price whose date the user can see is far better than a
wrong one they cannot.

What makes this provider different from every other one here is that its unit of
pricing is a (model, partner) PAIR rather than a model: the router serves
openai/gpt-oss-120b through eleven partners at seven prices. Most of what these
tests guard follows from that -- keys carry the `:partner` suffix, the partner
filter must hold, and one model can legitimately appear twice.

Cross-file consistency checks -- does providers.huggingface in the committed
pricing.json actually match scripts/providers/huggingface/mapping.json -- live here
too: they are statements about this provider specifically, not about the shared
schema.
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

# Imported via the providers.huggingface package, not a flat `import scrape`: every
# provider's scraper is a module literally named scrape.py, and a flat import would
# collide in sys.modules the moment two test files run in the same process.
from providers.huggingface import scrape  # noqa: E402
import pricing_validate as validate  # noqa: E402
from pricing_validate import JSONDict  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "huggingface"
MODELS_OK = FIXTURES / "models_ok.json"
BASELINE_JSON = FIXTURES / "baseline.json"

PRICING_JSON = REPO_ROOT / "pricing.json"
MAPPING_JSON = REPO_ROOT / "scripts" / "providers" / "huggingface" / "mapping.json"

FIXED_NOW = "2026-08-10T07:00:00Z"


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

    def candidate_block(self, name: str) -> JSONDict:
        return json.loads((self.out_dir / name).read_text(encoding="utf-8"))["providers"]["huggingface"]

    def notes(self) -> str:
        return (self.out_dir / "notes.txt").read_text(encoding="utf-8")

    @property
    def models_ok(self) -> str:
        return MODELS_OK.read_text(encoding="utf-8")

    def payload(self) -> JSONDict:
        return json.loads(self.models_ok)

    def offer(self, payload: JSONDict, model_id: str, partner: str) -> JSONDict:
        for model in payload["data"]:
            if model["id"] == model_id:
                for offer in model["providers"]:
                    if offer["provider"] == partner:
                        return offer
        raise AssertionError(f"fixture drift: no {model_id}:{partner}. Re-capture the fixture.")


class TestUnchanged(ScrapeTestCase):
    def test_the_fixture_reproduces_the_baseline_exactly(self) -> None:
        code, stdout, _ = self.run_scrape(self.models_ok)
        self.assertEqual(code, 0)
        self.assertIn("unchanged", stdout)
        self.assertFalse((self.out_dir / "updated.json").exists())
        self.assert_current_untouched()

    def test_only_the_mapped_partners_are_published(self) -> None:
        """openai/gpt-oss-120b is in the fixture with eleven routes at seven prices.
        Exactly one of them may be published. Without the partner filter this block
        would quietly carry Groq and Cerebras prices nobody asked for."""
        self.run_scrape(self.models_ok)
        published = self.candidate_block("stamped.json")["models"]
        partners = set(json.loads(MAPPING_JSON.read_text(encoding="utf-8"))["partners"])
        for route in published:
            self.assertIn(route.rsplit(":", 1)[1], partners, route)
        gpt_oss = [r for r in published if r.startswith("openai/gpt-oss-120b:")]
        self.assertEqual(sorted(gpt_oss), ["openai/gpt-oss-120b:ovhcloud", "openai/gpt-oss-120b:scaleway"])

    def test_one_model_may_appear_once_per_partner(self) -> None:
        """The whole reason keys carry a :partner suffix. Two partners serving the
        same model are two prices, not a duplicate to collapse."""
        self.run_scrape(self.models_ok)
        published = self.candidate_block("stamped.json")["models"]
        both = [r for r in published if r.startswith("meta-llama/Llama-3.3-70B-Instruct:")]
        self.assertEqual(len(both), 2, both)
        prices = {published[r]["in_per_mtok"] for r in both}
        self.assertEqual(len(prices), 2, "two partners, two prices -- they must not be merged")

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
        candidate = json.loads((self.out_dir / "stamped.json").read_text(encoding="utf-8"))
        self.assertEqual(candidate["providers"]["acme"], doc["providers"]["acme"])
        self.assertEqual(list(candidate["providers"]), list(doc["providers"]))
        self.assert_current_untouched()


class TestPayloadFailures(ScrapeTestCase):
    def test_not_json_at_all(self) -> None:
        code, _, stderr = self.run_scrape("<html><body>bad gateway</body></html>")
        self.assert_failed(code, stderr, "not valid JSON")

    def test_no_data_array(self) -> None:
        for body in ('{"object": "list"}', '{"object": "list", "data": []}'):
            with self.subTest(body=body):
                code, _, stderr = self.run_scrape(body)
                self.assert_failed(code, stderr, 'no non-empty "data" array')

    def test_a_model_without_a_string_id(self) -> None:
        code, _, stderr = self.run_scrape(json.dumps({"data": [{"id": 7, "providers": []}]}))
        self.assert_failed(code, stderr, 'no string "id"')

    def test_a_provider_entry_without_a_name(self) -> None:
        payload = self.payload()
        self.offer(payload, "openai/gpt-oss-120b", "ovhcloud").pop("provider")
        code, _, stderr = self.run_scrape(json.dumps(payload))
        self.assert_failed(code, stderr, "no string 'provider'")

    def test_a_duplicated_route_is_refused(self) -> None:
        payload = self.payload()
        for model in payload["data"]:
            if model["id"] == "openai/gpt-oss-120b":
                model["providers"].append(dict(self.offer(payload, model["id"], "ovhcloud")))
        code, _, stderr = self.run_scrape(json.dumps(payload))
        self.assert_failed(code, stderr, "more than once", "Refusing to guess")


class TestRouteSetDrift(ScrapeTestCase):
    """What the two partners serve through the router moves constantly. The mapping
    names partners, never routes, so this is the ordinary case, not the exceptional
    one."""

    def test_a_route_that_disappears_is_kept_with_its_last_prices_and_a_date(self) -> None:
        payload = self.payload()
        for model in payload["data"]:
            if model["id"] == "openai/gpt-oss-20b":
                model["providers"] = [o for o in model["providers"] if o["provider"] != "ovhcloud"]
        code, _, _ = self.run_scrape(json.dumps(payload))
        self.assertEqual(code, 0)

        route = "openai/gpt-oss-20b:ovhcloud"
        entry = self.candidate_block("updated.json")["models"][route]
        published = json.loads(self.current_bytes)["providers"]["huggingface"]["models"][route]
        self.assertEqual(entry["absent_since"], FIXED_NOW[:10])
        self.assertEqual(entry["in_per_mtok"], published["in_per_mtok"])
        self.assertIn("no longer offered", self.notes())
        self.assert_current_untouched()

    def test_a_new_route_on_a_mapped_partner_is_published(self) -> None:
        """The router states the model id, the partner and the price itself, so there is
        nothing for a human to translate and nothing to wait for."""
        payload = self.payload()
        for model in payload["data"]:
            if model["id"] == "Qwen/Qwen3.5-9B":
                extra = dict(self.offer(payload, model["id"], "ovhcloud"))
                extra["provider"] = "scaleway"
                model["providers"].append(extra)
        code, stdout, _ = self.run_scrape(json.dumps(payload))

        self.assertEqual(code, 0)
        self.assertIn("+ Qwen/Qwen3.5-9B:scaleway: added", stdout)
        self.assertEqual(self.notes(), "", "a route being added is not something to alert about")
        self.assert_current_untouched()

    def test_a_new_route_on_an_unmapped_partner_is_ignored(self) -> None:
        """A partner outside the two this block covers adding a model is none of its
        business and must not disturb the weekly run."""
        payload = self.payload()
        for model in payload["data"]:
            if model["id"] == "openai/gpt-oss-120b":
                extra = dict(self.offer(payload, model["id"], "ovhcloud"))
                extra["provider"] = "some-new-partner"
                model["providers"].append(extra)
        code, stdout, _ = self.run_scrape(json.dumps(payload))
        self.assertEqual(code, 0)
        self.assertIn("unchanged", stdout)


class TestPriceFailures(ScrapeTestCase):
    def test_a_route_that_is_not_live_is_treated_as_not_offered(self) -> None:
        """A price for a call that cannot be served is worse than no price, so a route
        the router will not run is not published as a current one. It is not deleted
        either: it keeps its last observed prices under an `absent_since` stamp, like
        any other route that stops being available."""
        payload = self.payload()
        self.offer(payload, "openai/gpt-oss-120b", "ovhcloud")["status"] = "staging"
        code, _, _ = self.run_scrape(json.dumps(payload))
        self.assertEqual(code, 0)

        route = "openai/gpt-oss-120b:ovhcloud"
        entry = self.candidate_block("updated.json")["models"][route]
        self.assertEqual(entry["absent_since"], FIXED_NOW[:10])
        self.assertIn("no longer offered", self.notes())

    def test_a_missing_pricing_object_is_refused(self) -> None:
        payload = self.payload()
        self.offer(payload, "openai/gpt-oss-120b", "ovhcloud").pop("pricing")
        code, _, stderr = self.run_scrape(json.dumps(payload))
        self.assert_failed(code, stderr, "no pricing object and not marked free")

    def test_a_missing_output_price_is_refused(self) -> None:
        payload = self.payload()
        self.offer(payload, "openai/gpt-oss-120b", "ovhcloud")["pricing"].pop("output")
        code, _, stderr = self.run_scrape(json.dumps(payload))
        self.assert_failed(code, stderr, "out_per_mtok", "'output'")

    def test_a_non_numeric_price_is_refused(self) -> None:
        payload = self.payload()
        self.offer(payload, "openai/gpt-oss-120b", "ovhcloud")["pricing"]["input"] = "0.09"
        code, _, stderr = self.run_scrape(json.dumps(payload))
        self.assert_failed(code, stderr, "not a number")

    def test_a_price_of_zero_without_the_free_flag_is_refused(self) -> None:
        """The router does carry routes priced 0 with is_free false. That is not a
        declared free tier, it is a figure nothing vouches for, and the sanity floor
        is exactly the check that catches it."""
        payload = self.payload()
        self.offer(payload, "openai/gpt-oss-120b", "ovhcloud")["pricing"]["input"] = 0
        code, _, stderr = self.run_scrape(json.dumps(payload))
        self.assert_failed(code, stderr, "outside the plausible range")

    def test_a_route_marked_free_is_published_as_free(self) -> None:
        payload = self.payload()
        offer = self.offer(payload, "openai/gpt-oss-120b", "ovhcloud")
        offer["is_free"] = True
        offer["pricing"] = {"input": 0, "output": 0}
        code, stdout, _ = self.run_scrape(json.dumps(payload))
        self.assertEqual(code, 0)
        self.assertIn("changed", stdout)
        entry = self.candidate_block("updated.json")["models"]["openai/gpt-oss-120b:ovhcloud"]
        self.assertIs(entry["free"], True)
        self.assertEqual([k for k in entry if k in validate.KNOWN_PRICE_FIELDS], [])

    def test_a_figure_that_moves_too_far_is_refused(self) -> None:
        payload = self.payload()
        self.offer(payload, "openai/gpt-oss-120b", "ovhcloud")["pricing"]["input"] = 0.9  # x10
        code, _, stderr = self.run_scrape(json.dumps(payload))
        self.assert_failed(code, stderr, "factor of", "Refusing to publish")


class TestPriceChange(ScrapeTestCase):
    def test_a_real_change_produces_a_candidate_not_a_publication(self) -> None:
        payload = self.payload()
        self.offer(payload, "openai/gpt-oss-120b", "ovhcloud")["pricing"]["input"] = 0.12
        code, stdout, _ = self.run_scrape(json.dumps(payload))
        self.assertEqual(code, 0)
        self.assertIn("changed", stdout)
        self.assertIn("openai/gpt-oss-120b:ovhcloud", stdout)
        updated = self.candidate_block("updated.json")["models"]
        self.assertEqual(updated["openai/gpt-oss-120b:ovhcloud"]["in_per_mtok"], 0.12)
        self.assertEqual(
            updated["openai/gpt-oss-120b:scaleway"]["in_per_mtok"],
            0.171,
            "the other partner's price for the same model must not move with it",
        )
        self.assert_current_untouched()


class TestPublishedFileMatchesHuggingFace(unittest.TestCase):
    maxDiff = None

    def block(self) -> JSONDict:
        return json.loads(PRICING_JSON.read_text(encoding="utf-8"))["providers"]["huggingface"]

    def test_the_committed_file_matches_the_mapping(self) -> None:
        """There is no route list to agree with any more, so what is checked is what the
        mapping still decides: where the figures come from and what currency they are
        in."""
        published = self.block()
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
        self.assertEqual(published["source"], mapping["source"])
        self.assertEqual(published["currency"], mapping["currency"])

        producible = set(mapping["fields"])
        for route, entry in published["models"].items():
            fields = {k for k in entry if k in validate.KNOWN_PRICE_FIELDS}
            if entry.get("free") is True:
                self.assertEqual(fields, set(), f"{route} is free and priced at once")
            else:
                self.assertTrue(fields, route)
                self.assertLessEqual(fields, producible, route)

    def test_every_key_carries_a_mapped_partner_suffix(self) -> None:
        """The key is the exact string the router takes. Without the suffix it would
        name several different prices at once."""
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
        partners = set(mapping["partners"])
        for route in self.block()["models"]:
            self.assertIn(":", route, route)
            model_id, partner = route.rsplit(":", 1)
            self.assertIn(partner, partners, route)
            self.assertIn("/", model_id, route)

    def test_the_ovhcloud_routes_track_the_prices_ovh_publishes_itself(self) -> None:
        """A genuine cross-source check, and what pins the unit and currency here:
        this repository reads OVH's own catalog independently, in EUR. Every route
        the router quotes for ovhcloud must land within a sane USD/EUR band of it.
        If that band ever breaks, either the unit changed or one source is wrong."""
        doc = json.loads(PRICING_JSON.read_text(encoding="utf-8"))
        ovh = doc["providers"]["ovh"]["models"]
        checked = 0
        for route, entry in doc["providers"]["huggingface"]["models"].items():
            model_id, partner = route.rsplit(":", 1)
            if partner != "ovhcloud":
                continue
            direct = ovh.get(model_id.split("/")[-1])
            if not direct or "in_per_mtok" not in direct:
                continue
            ratio = entry["in_per_mtok"] / direct["in_per_mtok"]
            with self.subTest(route=route):
                self.assertTrue(1.0 < ratio < 1.4, f"{route}: USD/EUR ratio {ratio:.3f}")
            checked += 1
        self.assertGreaterEqual(checked, 5, "the cross-check stopped covering anything")

    def test_the_published_figures_have_room_to_fall(self) -> None:
        for route, entry in self.block()["models"].items():
            for field, value in entry.items():
                if field not in validate.KNOWN_PRICE_FIELDS:
                    continue
                low, _ = validate.PRICE_BOUNDS.get(field, (validate.MIN_PLAUSIBLE, validate.MAX_PLAUSIBLE))
                with self.subTest(route=route, field=field):
                    self.assertGreaterEqual(float(value), low * validate.MAX_CHANGE_FACTOR)

    def test_the_fixture_baseline_is_valid(self) -> None:
        validate.validate_document(json.loads(BASELINE_JSON.read_text(encoding="utf-8")))


class TestMapping(unittest.TestCase):
    def mapping(self) -> JSONDict:
        return json.loads(MAPPING_JSON.read_text(encoding="utf-8"))

    def test_the_mapping_is_shaped_as_the_scraper_expects(self) -> None:
        mapping = self.mapping()
        self.assertEqual(mapping["partners"], ["ovhcloud", "scaleway"])
        for field, spec in mapping["fields"].items():
            self.assertIn(field, validate.KNOWN_PRICE_FIELDS, field)
            self.assertTrue(spec["api_field"], field)

    def test_there_is_no_route_list(self) -> None:
        """Load-bearing, not pedantry. A hand-written route list would mean the first
        model either partner retires blocks the other partner's prices too, and
        re-adding one would bring that back without anything else in the tests
        noticing."""
        self.assertNotIn("models", self.mapping())


if __name__ == "__main__":
    unittest.main()
