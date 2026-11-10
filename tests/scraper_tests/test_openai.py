"""Tests for scripts/providers/openai/scrape.py.

The fixture is OpenAI's pricing page as Markdown, `pricing.md`, as served on 2026-11-10,
whole. The figures asserted below were read off that page by a human, and every row of
the flagship models' Standard table is checked against what is published.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

# Via the package, not a flat `import scrape`: every provider's scraper is named
# scrape.py, and `unittest discover` runs them all in one process.
from providers.openai import scrape  # noqa: E402
import pricing_validate as validate  # noqa: E402
from pricing_validate import JSONDict  # noqa: E402
from tests.published import published_file  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "openai"
PRICING_OK = FIXTURES / "pricing_ok.md"
BASELINE_JSON = FIXTURES / "baseline.json"
MAPPING_JSON = REPO_ROOT / "scripts" / "providers" / "openai" / "mapping.json"
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

    def run_scrape(self, page: str | None = None, mapping: Path = MAPPING_JSON) -> tuple[int, str, str]:
        source = self.tmp / "pricing.md"
        source.write_text(self.page if page is None else page, encoding="utf-8")
        argv = ["--out-dir", str(self.out_dir), "--current", str(self.current), "--mapping", str(mapping),
                "--html", str(source), "--now", FIXED_NOW]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scrape.main(argv)
        return code, out.getvalue(), err.getvalue()

    def published(self) -> dict[str, JSONDict]:
        name = "updated.json" if (self.out_dir / "updated.json").exists() else "stamped.json"
        return json.loads((self.out_dir / name).read_text(encoding="utf-8"))["providers"]["openai"]["models"]

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

    def test_a_flagship_model_short_and_long_context_with_both_cache_writes(self) -> None:
        sol = self.models["gpt-6-sol"]
        self.assertEqual(
            [sol[f] for f in ("in_per_mtok", "out_per_mtok", "cache_read_per_mtok",
                              "cache_write_30m_per_mtok", "cache_write_12h_per_mtok")],
            [2.0, 10.0, 0.2, 2.5, 5.0])
        self.assertEqual(
            [sol[f + "_long_context"] for f in ("in_per_mtok", "out_per_mtok", "cache_read_per_mtok",
                                                "cache_write_30m_per_mtok", "cache_write_12h_per_mtok")],
            [4.0, 15.0, 0.4, 5.0, 10.0])
        self.assertEqual(sol["long_context_threshold"], 272001, "Short context: ≤272K input tokens")

    def test_batch_prices_go_in_the_batch_fields_long_context_included(self) -> None:
        sol = self.models["gpt-6-sol"]
        self.assertEqual((sol["in_per_mtok_batch"], sol["out_per_mtok_batch"], sol["cache_read_per_mtok_batch"]),
                         (1.0, 5.0, 0.1))
        self.assertEqual((sol["in_per_mtok_batch_long_context"], sol["cache_write_12h_per_mtok_batch_long_context"]),
                         (2.0, 5.0))

    def test_a_context_length_note_in_the_name_is_not_part_of_the_id(self) -> None:
        self.assertIn("gpt-5.5 (<272K context length)", self.page)
        self.assertEqual(self.models["gpt-5.5"]["in_per_mtok"], 5.0)
        self.assertFalse([k for k in self.models if "context length" in k])

    def test_a_dash_is_a_rate_not_sold(self) -> None:
        gpt4o = self.models["gpt-4o"]
        self.assertEqual((gpt4o["in_per_mtok"], gpt4o["cache_read_per_mtok"], gpt4o["in_per_mtok_batch"]),
                         (2.5, 1.25, 1.25))
        self.assertNotIn("cache_read_per_mtok_batch", gpt4o)
        self.assertNotIn("in_per_mtok_batch_long_context", self.models["gpt-5.5-pro"])

    def test_each_other_speed_is_an_entry_of_its_own(self) -> None:
        flex = self.models["gpt-6-luna (flex)"]
        self.assertEqual((flex["in_per_mtok"], flex["out_per_mtok"], flex["api_ids"]), (0.05, 0.25, ["gpt-6-luna"]))
        self.assertEqual(self.models["gpt-6-sol (fast)"]["in_per_mtok"], 4.0)
        self.assertEqual(self.models["gpt-6.1-sol (ultrafast)"]["out_per_mtok_long_context"], 90.0)
        self.assertEqual(self.models["gpt-5.3-codex (fast)"]["in_per_mtok"], 3.5)

    def test_image_models_price_text_and_image_tokens_apart(self) -> None:
        image = self.models["gpt-image-2"]
        self.assertEqual((image["image_in_per_mtok"], image["image_out_per_mtok"], image["image_cache_read_per_mtok"]),
                         (8.0, 30.0, 2.0))
        self.assertEqual((image["in_per_mtok"], image["cache_read_per_mtok"]), (5.0, 1.25))
        self.assertNotIn("out_per_mtok", image)
        self.assertEqual((image["image_in_per_mtok_batch"], image["in_per_mtok_batch"]), (4.0, 2.5))

    def test_realtime_models_price_audio_text_and_image_tokens_apart(self) -> None:
        rt = self.models["gpt-realtime-2"]
        self.assertEqual((rt["audio_in_per_mtok"], rt["audio_out_per_mtok"], rt["audio_cache_read_per_mtok"]),
                         (32.0, 64.0, 0.4))
        self.assertEqual((rt["in_per_mtok"], rt["out_per_mtok"], rt["image_in_per_mtok"]), (4.0, 24.0, 5.0))

    def test_speech(self) -> None:
        self.assertEqual(self.models["tts-1"]["per_mchars"], 15.0)
        self.assertEqual(self.models["tts-1-hd"]["per_mchars"], 30.0)
        tts = self.models["gpt-4o-mini-tts"]
        self.assertEqual((tts["in_per_mtok"], tts["audio_out_per_mtok"]), (0.6, 12.0))
        self.assertEqual(self.models["gpt-live-1"]["per_conversation_minute"], 0.05)

    def test_transcription_priced_per_minute_or_in_tokens(self) -> None:
        self.assertEqual(self.models["whisper"], {"per_audio_minute": 0.006, "display_name": "Whisper"})
        self.assertEqual(self.models["gpt-transcribe"]["per_audio_minute"], 0.0045)
        tokens = self.models["gpt-4o-transcribe"]
        self.assertEqual((tokens["in_per_mtok"], tokens["out_per_mtok"]), (2.5, 10.0))
        self.assertNotIn("per_audio_minute", tokens, "the page calls its cost per minute an estimate")

    def test_restricted_models_are_not_published(self) -> None:
        self.assertIn("Trusted Access", self.page)
        self.assertFalse([k for k in self.models if "cyber" in k or "rosalind" in k])
        self.assertEqual(self.models["gpt-6-astra-law"]["in_per_mtok"], 10.0)

    def test_the_specialized_models(self) -> None:
        self.assertIs(self.models["omni-moderation-latest"]["free"], True)
        self.assertEqual(self.models["text-embedding-3-large"], {"in_per_mtok": 0.13, "display_name": "text-embedding-3-large"})
        self.assertEqual(self.models["gpt-5-search-api"]["in_per_mtok"], 1.25)

    def test_fine_tuning_is_not_published(self) -> None:
        self.assertFalse([k for k in self.models if re.search(r"-20[0-9]{2}-[0-9]{2}-[0-9]{2}$", k)])

    def test_the_tools(self) -> None:
        self.assertEqual(self.models["web_search"]["per_1k_calls"], 10.0)
        self.assertEqual(self.models["web_search_preview (other models)"]["per_1k_calls"], 25.0)
        self.assertEqual(self.models["file_search"]["per_1k_calls"], 2.5)
        self.assertEqual(self.models["file_search (storage)"]["per_gb_day"], 0.1)
        self.assertEqual(self.models["chatkit (storage)"]["per_gb_day"], 0.1)
        sizes = {k: e["per_session"] for k, e in self.models.items() if k.startswith("containers (")}
        self.assertEqual(sizes, {"containers (1 GB)": 0.03, "containers (4 GB)": 0.12,
                                 "containers (16 GB)": 0.48, "containers (64 GB)": 1.92})
        for key in sizes:
            self.assertEqual(self.models[key]["kind"], "product")

    def test_every_row_of_the_standard_flat_table_is_published_as_stated(self) -> None:
        section = self.page.split("### Standard pricing data", 1)[1].split("### Batch pricing data", 1)[0]
        table = section.split("| Model | Input | Output | Cached input |", 1)[1]
        checked = 0
        for line in table.splitlines():
            if not line.startswith("| ") or line.startswith("| ---"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            entry = self.models[cells[0]]
            for field, cell in zip(("in_per_mtok", "out_per_mtok", "cache_read_per_mtok"), cells[1:]):
                if cell == "-":
                    self.assertNotIn(field, entry, cells[0])
                else:
                    self.assertEqual(entry[field], float(cell.lstrip("$")), cells[0])
            checked += 1
        self.assertEqual(checked, 15)


class TestFailures(ScrapeTestCase):
    def test_a_renamed_column(self) -> None:
        page = self.edited("| Short context 30m write |", "| Short context 30-minute write |", count=5)
        code, _, stderr = self.run_scrape(page)
        self.assert_failed(code, stderr, "has no ['flagship-context'] table")

    def test_a_price_this_cannot_read(self) -> None:
        page = self.edited("| gpt-4o | $2.50 | $10.00 | $1.25 |", "| gpt-4o | 2.50 USD | $10.00 | $1.25 |")
        code, _, stderr = self.run_scrape(page)
        self.assert_failed(code, stderr, "'2.50 USD' is not a price this can read")

    def test_long_context_prices_without_the_note(self) -> None:
        page = self.edited("Short context: ≤272K input tokens. Long context: >272K input tokens.", "")
        code, _, stderr = self.run_scrape(page)
        self.assert_failed(code, stderr, "no note saying where long context starts")

    def test_a_context_length_the_note_does_not_state(self) -> None:
        page = self.edited("| gpt-5.5 (<272K context length) | $5.00", "| gpt-5.5 (<200K context length) | $5.00")
        code, _, stderr = self.run_scrape(page)
        self.assert_failed(code, stderr, "names a context length the page's note does not state")

    def test_a_modality_the_mapping_does_not_name(self) -> None:
        page = self.edited("| gpt-image-1-mini | Image | $2.50 |", "| gpt-image-1-mini | Video | $2.50 |")
        code, _, stderr = self.run_scrape(page)
        self.assert_failed(code, stderr, "a modality 'Video' the mapping does not name")

    def test_a_tool_row_that_is_gone(self) -> None:
        page = self.edited("| File search | Tool call | $2.50 / 1k calls |\n", "")
        code, _, stderr = self.run_scrape(page)
        self.assert_failed(code, stderr, "no row 'File search' / 'Tool call'")

    def test_containers_priced_in_another_unit(self) -> None:
        page = self.edited("per 20-minute session per container", "per hour per container")
        code, _, stderr = self.run_scrape(page)
        self.assert_failed(code, stderr, "not as the mapping expects")


class TestNotes(ScrapeTestCase):
    def test_a_table_the_mapping_does_not_name(self) -> None:
        page = self.edited("\nGPT-Live\n", "\nGPT Live\n")
        code, _, stderr = self.run_scrape(page)
        self.assertEqual(code, 0, stderr)
        self.assertIn("headed ['Model', 'Price per minute'], that the mapping does not name; not read", self.notes())
        self.assertEqual(self.published()["gpt-live-1"]["absent_since"], FIXED_NOW[:10])

    def test_a_tool_row_the_mapping_does_not_name(self) -> None:
        page = self.edited("| File search | Tool call | $2.50 / 1k calls |\n",
                           "| File search | Tool call | $2.50 / 1k calls |\n| Video | Render | $1.00 / minute |\n")
        code, _, stderr = self.run_scrape(page)
        self.assertEqual(code, 0, stderr)
        self.assertIn("a tool row 'Video' / 'Render' the mapping does not name", self.notes())

    def test_a_model_with_batch_prices_only(self) -> None:
        page = self.edited("| gpt-5.4-nano | $0.20 | $1.25 | $0.02 |\n", "")
        code, _, stderr = self.run_scrape(page)
        self.assertEqual(code, 0, stderr)
        self.assertIn("gpt-5.4-nano: batch prices, and no standard price", self.notes())


class TestMapping(unittest.TestCase):
    def test_there_is_no_model_list(self) -> None:
        self.assertNotIn("models", MAPPING)

    def test_the_source_is_the_markdown_page(self) -> None:
        self.assertEqual(MAPPING["sources"], [MAPPING["source"]])
        self.assertTrue(MAPPING["source"].endswith(".md"))
        self.assertEqual(MAPPING["format"], "markdown")

    def test_every_field_named_is_a_known_price_field(self) -> None:
        for table in MAPPING["tables"]:
            fields = list(table.get("fields", [])) + ([table["field"]] if "field" in table else [])
            prefixes = list(table.get("modalities", {}).values()) or [""]
            for field in fields:
                for prefix in prefixes:
                    self.assertIn(prefix + field, validate.KNOWN_PRICE_FIELDS, table["name"])
        for tool in MAPPING["tools"]:
            self.assertIn(tool["field"], validate.KNOWN_PRICE_FIELDS)


class TestPublishedFile(unittest.TestCase):
    """Run against the file about to be published, as every refresh does."""

    def block(self) -> JSONDict:
        return json.loads(PRICING_JSON.read_text(encoding="utf-8"))["providers"]["openai"]

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
