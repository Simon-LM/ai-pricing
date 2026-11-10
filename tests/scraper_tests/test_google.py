"""Tests for scripts/providers/google/scrape.py.

The fixture is Google's Gemini API pricing page as Markdown, `pricing.md`, as served on
2026-11-10, whole. The figures asserted below were read off that page by a human. Runs are
dated 2026-11-10 unless a test says otherwise: some prices on the page hold "through" one
date and others "starting" the next, and the one published is the one valid on the run's
date.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

# Via the package, not a flat `import scrape`: every provider's scraper is named
# scrape.py, and `unittest discover` runs them all in one process.
from providers.google import scrape  # noqa: E402
import pricing_validate as validate  # noqa: E402
from pricing_validate import JSONDict  # noqa: E402
from tests.published import published_file  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "google"
PRICING_OK = FIXTURES / "pricing_ok.md"
BASELINE_JSON = FIXTURES / "baseline.json"
MAPPING_JSON = REPO_ROOT / "scripts" / "providers" / "google" / "mapping.json"
MAPPING = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
PRICING_JSON = published_file()

FIXED_NOW = "2026-11-10T10:00:00Z"


class ScrapeTestCase(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.out_dir = self.tmp / "out"
        self.current = self.tmp / "pricing.json"
        self.current.write_bytes(BASELINE_JSON.read_bytes())
        self.current_bytes = self.current.read_bytes()
        self.page = PRICING_OK.read_text(encoding="utf-8")

    def run_scrape(self, page: str | None = None, now: str = FIXED_NOW) -> tuple[int, str, str]:
        source = self.tmp / "pricing.md"
        source.write_text(self.page if page is None else page, encoding="utf-8")
        argv = ["--out-dir", str(self.out_dir), "--current", str(self.current), "--mapping", str(MAPPING_JSON),
                "--html", str(source), "--now", now]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scrape.main(argv)
        return code, out.getvalue(), err.getvalue()

    def published(self) -> dict[str, JSONDict]:
        name = "updated.json" if (self.out_dir / "updated.json").exists() else "stamped.json"
        return json.loads((self.out_dir / name).read_text(encoding="utf-8"))["providers"]["google"]["models"]

    def notes(self) -> str:
        return (self.out_dir / "notes.txt").read_text(encoding="utf-8")

    def edited(self, old: str, new: str, count: int = 1) -> str:
        self.assertEqual(self.page.count(old), count, f"fixture drift: {old!r}")
        return self.page.replace(old, new)

    def assert_failed(self, code: int, stderr: str, fragment: str) -> None:
        self.assertEqual(code, 1, f"expected a failure, got {code}")
        self.assertFalse((self.out_dir / "stamped.json").exists())
        self.assertFalse((self.out_dir / "updated.json").exists())
        self.assertIn(fragment, stderr)
        self.assertEqual(self.current.read_bytes(), self.current_bytes)


class TestUnchanged(ScrapeTestCase):
    def test_the_fixture_reproduces_the_baseline_exactly(self) -> None:
        code, stdout, stderr = self.run_scrape()
        self.assertEqual(code, 0, stderr)
        self.assertIn("unchanged", stdout)
        self.assertFalse((self.out_dir / "updated.json").exists())
        self.assertEqual(self.notes(), "")

    def test_the_fixture_baseline_is_valid(self) -> None:
        validate.validate_document(json.loads(BASELINE_JSON.read_text(encoding="utf-8")))


class TestFigures(ScrapeTestCase):
    """Read off the page by a human on 2026-11-10."""

    def setUp(self) -> None:
        super().setUp()
        code, _, stderr = self.run_scrape()
        self.assertEqual(code, 0, stderr)
        self.models = self.published()

    def test_a_dated_price_publishes_the_one_valid_on_the_day(self) -> None:
        """Gemini 4 Argon: "$2.00 through November 30, 2026. $4.00 starting December 1, 2026." """
        argon = self.models["gemini-4-argon"]
        self.assertEqual((argon["in_per_mtok"], argon["out_per_mtok"], argon["cache_read_per_mtok"]), (2.0, 10.0, 0.1))
        self.assertEqual(argon["in_per_mtok_batch"], 1.0)

    def test_prices_by_kind_of_token(self) -> None:
        """Gemini 3.1 Flash-Lite: "$0.25 (text / image / video) $0.50 (audio)", caching
        "$0.025 (text / image / video) $0.05 (audio) $1.00 / 1,000,000 tokens per hour"."""
        lite = self.models["gemini-3.1-flash-lite"]
        self.assertEqual((lite["in_per_mtok"], lite["image_in_per_mtok"], lite["audio_in_per_mtok"]), (0.25, 0.25, 0.5))
        self.assertEqual((lite["cache_read_per_mtok"], lite["audio_cache_read_per_mtok"]), (0.025, 0.05))
        self.assertEqual(lite["cache_storage_per_mtok_hour"], 1.0)

    def test_video_tokens(self) -> None:
        """Gemini Omni 1.2 Flash: input "$1.50 (text / image / video)", output "$9.00 (text)
        $12.00 (video)"; Gemini Embedding 2: "Video input price $12.00"."""
        omni = self.models["gemini-omni-1.2-flash"]
        self.assertEqual((omni["in_per_mtok"], omni["video_in_per_mtok"]), (1.5, 1.5))
        self.assertEqual((omni["out_per_mtok"], omni["video_out_per_mtok"]), (9.0, 12.0))
        self.assertEqual(self.models["gemini-omni-1.1-flash"]["video_out_per_mtok"], 17.5)
        self.assertEqual(self.models["gemini-embedding-2"]["video_in_per_mtok"], 12.0)
        self.assertEqual(self.models["gemini-2.5-flash"]["video_cache_read_per_mtok"], 0.03)

    def test_prices_by_prompt_length(self) -> None:
        pro = self.models["gemini-2.5-pro"]
        self.assertEqual((pro["in_per_mtok"], pro["in_per_mtok_long_context"]), (1.25, 2.5))
        self.assertEqual((pro["out_per_mtok"], pro["out_per_mtok_long_context"]), (10.0, 15.0))
        self.assertEqual(pro["long_context_threshold"], 200001, "<= 200k tokens is the low price")
        self.assertEqual(pro["in_per_mtok_batch_long_context"], 1.25)

    def test_one_section_naming_several_models_prices_each(self) -> None:
        for model_id in ("gemini-3.8-live", "gemini-3.8-live-extended-thinking", "gemini-3.1-flash-live-preview"):
            with self.subTest(model=model_id):
                live = self.models[model_id]
                self.assertEqual((live["in_per_mtok"], live["audio_in_per_mtok"], live["audio_out_per_mtok"]),
                                 (0.75, 3.0, 12.0))

    def test_estimates_are_not_published(self) -> None:
        """"$2.00 or $0.003/min (audio)": the minute is an estimate, the token is billed."""
        transcribe = self.models["gemini-3.5-transcribe"]
        self.assertEqual(transcribe, {"audio_in_per_mtok": 2.0, "out_per_mtok": 12.0, "display_name": "gemini-3.5-transcribe"})
        self.assertNotIn("per_image", self.models["gemini-nano-banana-2.1"], "Equivalent to $0.0336 per 1K image")

    def test_prices_per_item(self) -> None:
        image = self.models["gemini-2.5-flash-image"]
        self.assertEqual((image["per_image"], image["per_image_batch"]), (0.039, 0.0195))
        self.assertEqual(self.models["lyria-3.5"]["per_song"], 0.08)
        self.assertEqual(self.models["lyria-3-clip-preview"]["per_song"], 0.04)
        self.assertEqual(self.models["lyria-3-pro-preview"]["per_song"], 0.08)

    def test_gemma_is_free(self) -> None:
        self.assertEqual(self.models["gemma-4"], {"free": True, "display_name": "gemma-4"})

    def test_the_speeds(self) -> None:
        flex = self.models["gemini-2.5-flash (flex)"]
        self.assertEqual((flex["in_per_mtok"], flex["api_ids"]), (0.15, ["gemini-2.5-flash"]))
        self.assertEqual(self.models["gemini-4-argon (priority)"]["in_per_mtok"], 3.6)
        self.assertFalse([k for k in self.models if "contributor" in k])

    def test_the_embeddings(self) -> None:
        emb = self.models["gemini-embedding-2"]
        self.assertEqual((emb["in_per_mtok"], emb["image_in_per_mtok"], emb["audio_in_per_mtok"]), (0.2, 0.45, 6.5))
        self.assertEqual(self.models["gemini-embedding-001"]["in_per_mtok"], 0.15)

    def test_grounding(self) -> None:
        self.assertEqual(self.models["google_search (gemini 2.5)"]["per_1k_calls"], 35.0)
        self.assertEqual(self.models["google_search (gemini 3 and newer)"]["per_1k_calls"], 14.0)
        self.assertEqual(self.models["google_maps (gemini 2.5)"]["per_1k_calls"], 25.0)
        self.assertEqual(self.models["google_maps (gemini 3 and newer)"]["kind"], "product")


class TestDates(ScrapeTestCase):
    def test_the_new_price_is_published_on_the_day_it_starts(self) -> None:
        code, stdout, stderr = self.run_scrape(now="2026-12-01T06:00:00Z")
        self.assertEqual(code, 0, stderr)
        argon = self.published()["gemini-4-argon"]
        self.assertEqual((argon["in_per_mtok"], argon["out_per_mtok"], argon["in_per_mtok_batch"]), (4.0, 20.0, 2.0))
        self.assertEqual(self.published()["gemini-3.8-flash"]["in_per_mtok"], 0.75, "its price changes on January 1")

    def test_dates_that_leave_a_gap(self) -> None:
        page = self.edited("$4.00 starting December 1, 2026.", "$4.00 starting December 5, 2026.")
        code, _, stderr = self.run_scrape(page, now="2026-12-02T06:00:00Z")
        self.assert_failed(code, stderr, "Its dates leave a gap")


class TestFailures(ScrapeTestCase):
    def test_a_kind_of_token_the_mapping_does_not_name(self) -> None:
        page = self.edited("$0.50 (audio)", "$0.50 (holograms)", count=self.page.count("$0.50 (audio)"))
        code, _, stderr = self.run_scrape(page)
        self.assert_failed(code, stderr, "a kind of token the mapping does not name")

    def test_a_price_followed_by_words_it_does_not_know(self) -> None:
        page = self.edited("$0.039 per image", "$0.039 per sticker")
        code, _, stderr = self.run_scrape(page)
        self.assert_failed(code, stderr, "is not a clause this can read")

    def test_a_table_headed_another_way(self) -> None:
        page = self.page.replace("* Paid Tier, per 1M tokens in USD", "* Pay-as-you-go, per 1M tokens in USD")
        code, _, stderr = self.run_scrape(page)
        self.assert_failed(code, stderr, "not Feature | Free Tier | Paid Tier")

    def test_grounding_priced_another_way(self) -> None:
        page = self.edited("Then $35 / 1,000 grounded prompts", "Then $35 per grounded hour")
        code, _, stderr = self.run_scrape(page)
        self.assert_failed(code, stderr, "not as the mapping expects")

    def test_too_few_entries(self) -> None:
        page = self.page.split("## Gemini 3.6 Flash", 1)[0]
        code, _, stderr = self.run_scrape(page)
        self.assert_failed(code, stderr, "refusing to publish so few")


class TestNotes(ScrapeTestCase):
    def test_a_row_the_mapping_does_not_name(self) -> None:
        page = self.edited("* Text input price\n", "* Hologram input price\n", count=self.page.count("* Text input price\n"))
        code, _, stderr = self.run_scrape(page)
        self.assertEqual(code, 0, stderr)
        self.assertIn("a row 'Hologram input price' the mapping does not name; not read", self.notes())

    def test_a_tab_the_mapping_does_not_name(self) -> None:
        page = self.page.replace('{% tab title="Priority" %}', '{% tab title="Turbo" %}')
        code, _, stderr = self.run_scrape(page)
        self.assertEqual(code, 0, stderr)
        self.assertIn("a tab 'Turbo' the mapping does not name; not read", self.notes())


class TestMapping(unittest.TestCase):
    def test_the_source_is_the_markdown_page(self) -> None:
        self.assertEqual(MAPPING["sources"], [MAPPING["source"]])
        self.assertTrue(MAPPING["source"].endswith(".md"))
        self.assertEqual(MAPPING["format"], "markdown")

    def test_every_field_a_row_can_make_is_known(self) -> None:
        sides = {"in_per_mtok", "out_per_mtok", "cache_read_per_mtok"}
        for prefix in MAPPING["kind_prefixes"].values():
            if prefix is None:
                continue
            for field in sides:
                self.assertIn(prefix + field, validate.KNOWN_PRICE_FIELDS)
        for field in scrape.UNIT_FIELDS.values():
            self.assertIn(field, validate.KNOWN_PRICE_FIELDS)

    def test_contributor_is_not_published(self) -> None:
        self.assertIsNone(MAPPING["speeds"]["Contributor"])


class TestPublishedFile(unittest.TestCase):
    """Run against the file about to be published, as every refresh does."""

    def block(self) -> JSONDict:
        return json.loads(PRICING_JSON.read_text(encoding="utf-8"))["providers"]["google"]

    def test_the_block_matches_the_mapping(self) -> None:
        block = self.block()
        self.assertEqual((block["source"], block["sources"], block["currency"]),
                         (MAPPING["source"], MAPPING["sources"], MAPPING["currency"]))

    def test_every_long_context_price_has_its_threshold(self) -> None:
        for model_id, entry in self.block()["models"].items():
            if any(k.endswith("_long_context") for k in entry):
                with self.subTest(model=model_id):
                    self.assertGreater(entry["long_context_threshold"], 0)


if __name__ == "__main__":
    unittest.main()
