"""Tests for scripts/providers/xai/scrape.py.

The fixture is xAI's public pricing page as served on 2026-11-09, trimmed to the two
things the scraper reads: the Next.js chunk that carries globalThis.__XAI_PUBLIC_MODELS__,
and the server-side tools table.
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
sys.path.insert(0, str(REPO_ROOT))

# Via the package, not a flat `import scrape`: every provider's scraper is named
# scrape.py, and `unittest discover` runs them all in one process.
from providers.xai import scrape  # noqa: E402
import pricing_validate as validate  # noqa: E402
from pricing_validate import JSONDict  # noqa: E402
from tests.published import published_file  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "xai"
PRICING_OK = FIXTURES / "pricing_ok.html"
BASELINE_JSON = FIXTURES / "baseline.json"
MAPPING_JSON = REPO_ROOT / "scripts" / "providers" / "xai" / "mapping.json"
PRICING_JSON = published_file()

FIXED_NOW = "2026-11-09T10:00:00Z"


class ScrapeTestCase(unittest.TestCase):
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
        self.source = self.tmp / "pricing.html"
        self.page = PRICING_OK.read_text(encoding="utf-8")

    def run_scrape(self, body: str, mapping: Path = MAPPING_JSON) -> tuple[int, str, str]:
        self.source.write_text(body, encoding="utf-8")
        argv = ["--out-dir", str(self.out_dir), "--current", str(self.current), "--mapping", str(mapping),
                "--html", str(self.source), "--now", FIXED_NOW]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scrape.main(argv)
        return code, out.getvalue(), err.getvalue()

    def published(self) -> dict[str, JSONDict]:
        name = "updated.json" if (self.out_dir / "updated.json").exists() else "stamped.json"
        return json.loads((self.out_dir / name).read_text(encoding="utf-8"))["providers"]["xai"]["models"]

    def notes(self) -> str:
        return (self.out_dir / "notes.txt").read_text(encoding="utf-8")

    def mapping_with(self, **changes: object) -> Path:
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
        mapping.update(changes)
        path = self.tmp / "mapping.json"
        path.write_text(json.dumps(mapping), encoding="utf-8")
        return path

    def assert_failed(self, code: int, stderr: str, fragment: str) -> None:
        self.assertEqual(code, 1, f"expected a failure, got {code}")
        self.assertFalse((self.out_dir / "stamped.json").exists())
        self.assertFalse((self.out_dir / "updated.json").exists())
        self.assertIn(fragment, stderr)
        self.assertEqual(self.current.read_bytes(), self.current_bytes)


class TestUnchanged(ScrapeTestCase):
    def test_the_fixture_reproduces_the_baseline_exactly(self) -> None:
        code, stdout, stderr = self.run_scrape(self.page)
        self.assertEqual(code, 0, stderr)
        self.assertIn("unchanged", stdout)
        self.assertFalse((self.out_dir / "updated.json").exists())
        self.assertEqual(self.notes(), "")

    def test_the_fixture_baseline_is_valid(self) -> None:
        validate.validate_document(json.loads(BASELINE_JSON.read_text(encoding="utf-8")))


class TestFigures(ScrapeTestCase):
    """Read off the page's own human-readable table on 2026-11-09."""

    def setUp(self) -> None:
        super().setUp()
        code, _, stderr = self.run_scrape(self.page)
        self.assertEqual(code, 0, stderr)
        self.models = self.published()

    def test_token_prices_are_converted_from_cents_per_100m_tokens(self) -> None:
        grok = self.models["grok-4.7"]
        self.assertEqual((grok["in_per_mtok"], grok["out_per_mtok"], grok["cache_read_per_mtok"]), (2.0, 6.0, 0.5))

    def test_long_context_prices_carry_their_threshold(self) -> None:
        grok = self.models["grok-4.7"]
        self.assertEqual(grok["in_per_mtok_long_context"], 4.0)
        self.assertEqual(grok["out_per_mtok_long_context"], 12.0)
        self.assertEqual(grok["cache_read_per_mtok_long_context"], 1.0)
        self.assertEqual(grok["long_context_threshold"], 200000)

    def test_aliases_follow_the_dated_name(self) -> None:
        api_ids = self.models["grok-build-0.1"]["api_ids"]
        self.assertEqual(api_ids[0], "grok-build-0.1")
        self.assertIn("grok-code-fast-1", api_ids)

    def test_speech_prices(self) -> None:
        self.assertEqual(self.models["grok-tts"]["per_mchars"], 15.0)
        self.assertAlmostEqual(self.models["grok-voice-transcribe-2.0"]["per_audio_second"] * 3600, 0.1, places=5)
        streaming = self.models["grok-voice-transcribe-2.0 (streaming)"]
        self.assertAlmostEqual(streaming["per_audio_second"] * 3600, 0.2, places=5)
        self.assertEqual(streaming["api_ids"], ["grok-voice-transcribe-2.0"])

    def test_tools_are_products_priced_per_thousand(self) -> None:
        self.assertEqual(self.models["web_search"], {"per_1k_calls": 5.0, "display_name": "Web Search", "kind": "product"})
        x_search = self.models["x_search"]
        self.assertEqual((x_search["per_1k_posts"], x_search["per_1k_profiles"]), (5.0, 10.0))
        self.assertEqual(self.models["collections_search"]["per_1k_calls"], 2.5)

    def test_tools_not_billed_per_thousand_are_left_out(self) -> None:
        for tool in ("image_generation", "view_image", "view_x_video"):
            self.assertNotIn(tool, self.models)

    def test_the_real_time_voice_models_are_not_published(self) -> None:
        self.assertNotIn("grok-voice-3.0", self.models)


class TestFailures(ScrapeTestCase):
    def test_a_page_without_the_catalogue(self) -> None:
        code, _, stderr = self.run_scrape(self.page.replace("__XAI_PUBLIC_MODELS__", "__SOMETHING_ELSE__"))
        self.assert_failed(code, stderr, "__XAI_PUBLIC_MODELS__")

    def test_a_cluster_the_catalogue_does_not_have(self) -> None:
        code, _, stderr = self.run_scrape(self.page, mapping=self.mapping_with(cluster="nowhere-1"))
        self.assert_failed(code, stderr, "no cluster 'nowhere-1'")

    def test_a_price_that_is_not_a_whole_number(self) -> None:
        body = self.page.replace('\\"promptTextTokenPrice\\":\\"20000\\"', '\\"promptTextTokenPrice\\":\\"2.0\\"', 1)
        self.assertNotEqual(body, self.page, "fixture drift: no grok price to break")
        code, _, stderr = self.run_scrape(body)
        self.assert_failed(code, stderr, "not a whole number")

    def test_a_page_without_the_tools_table(self) -> None:
        code, _, stderr = self.run_scrape(self.page.replace("server-side tool invocations", "something else"))
        self.assert_failed(code, stderr, "tools table")


class TestNotes(ScrapeTestCase):
    def test_an_audio_model_the_mapping_does_not_name_is_noted(self) -> None:
        mapping = self.mapping_with(audio_skipped=["grok-voice-2.0"])
        code, _, stderr = self.run_scrape(self.page, mapping=mapping)
        self.assertEqual(code, 0, stderr)
        self.assertIn("grok-voice-3.0: an audio model the mapping does not name", self.notes())

    def test_a_tool_unit_the_mapping_does_not_name_is_noted(self) -> None:
        mapping = self.mapping_with(tool_units={"calls": "per_1k_calls", "posts": "per_1k_posts"})
        code, _, stderr = self.run_scrape(self.page, mapping=mapping)
        self.assertEqual(code, 0, stderr)
        self.assertIn("x_search: priced per 1k 'profiles'", self.notes())
        # Not dropped: the runner keeps what was published, marked absent, as for any
        # entry the source stops offering.
        self.assertEqual(self.published()["x_search"]["absent_since"], FIXED_NOW[:10])


class TestPublishedFile(unittest.TestCase):
    """Run against the file about to be published, as every refresh does."""

    def test_every_long_context_price_has_its_threshold(self) -> None:
        models = json.loads(PRICING_JSON.read_text(encoding="utf-8"))["providers"]["xai"]["models"]
        for model_id, entry in models.items():
            if any(k.endswith("_long_context") for k in entry):
                with self.subTest(model=model_id):
                    self.assertGreater(entry["long_context_threshold"], 0)


if __name__ == "__main__":
    unittest.main()
