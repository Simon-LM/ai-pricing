"""Tests for scripts/providers/anthropic/scrape.py.

Two fixtures, both as served on 2026-11-10, with scripts, styles, icons and attributes
stripped and the text and tables kept:

- `pricing_ok.html`, the pricing page: the model, batch, fast mode and Claude Managed
  Agents tables, and the sentences that price web search, code execution and web fetch;
- `deprecations_ok.html`, the model deprecations page: the status table that states
  each model's API id and whether it is Active, Deprecated or Retired.

The figures asserted below were read off the page by a human on 2026-11-10, and every
model row of the pricing page is checked against what is published.
"""

from __future__ import annotations

import contextlib
import html
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
from providers.anthropic import scrape  # noqa: E402
import pricing_validate as validate  # noqa: E402
from pricing_validate import JSONDict  # noqa: E402
from tests.published import published_file  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "anthropic"
PRICING_OK = FIXTURES / "pricing_ok.html"
STATUS_OK = FIXTURES / "deprecations_ok.html"
BASELINE_JSON = FIXTURES / "baseline.json"
MAPPING_JSON = REPO_ROOT / "scripts" / "providers" / "anthropic" / "mapping.json"
MAPPING = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
PRICING_JSON = published_file()

FIXED_NOW = "2026-11-10T10:00:00Z"


def status_row(model_id: str, state: str) -> str:
    return f"<tr><td><button>{model_id}</button></td><td>{state}</td>"


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
        self.pricing_page = PRICING_OK.read_text(encoding="utf-8")
        self.status_page = STATUS_OK.read_text(encoding="utf-8")

    def run_scrape(self, pricing: str | None = None, status: str | None = None,
                   mapping: Path = MAPPING_JSON) -> tuple[int, str, str]:
        (self.tmp / "pricing.html").write_text(self.pricing_page if pricing is None else pricing, encoding="utf-8")
        (self.tmp / "status.html").write_text(self.status_page if status is None else status, encoding="utf-8")
        manifest = self.tmp / "offline.json"
        manifest.write_text(json.dumps({MAPPING["source"]: "pricing.html", MAPPING["status_source"]: "status.html"}),
                            encoding="utf-8")
        argv = ["--out-dir", str(self.out_dir), "--current", str(self.current), "--mapping", str(mapping),
                "--offline", str(manifest), "--now", FIXED_NOW]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scrape.main(argv)
        return code, out.getvalue(), err.getvalue()

    def published(self) -> dict[str, JSONDict]:
        name = "updated.json" if (self.out_dir / "updated.json").exists() else "stamped.json"
        return json.loads((self.out_dir / name).read_text(encoding="utf-8"))["providers"]["anthropic"]["models"]

    def notes(self) -> str:
        return (self.out_dir / "notes.txt").read_text(encoding="utf-8")

    def edited(self, page: str, old: str, new: str, count: int = 1) -> str:
        self.assertEqual(page.count(old), count, f"fixture drift: {old!r}")
        return page.replace(old, new)

    def assert_failed(self, code: int, stderr: str, fragment: str) -> None:
        self.assertEqual(code, 1, f"expected a failure, got {code}")
        self.assertFalse((self.out_dir / "stamped.json").exists())
        self.assertFalse((self.out_dir / "updated.json").exists())
        self.assertIn(fragment, stderr)
        self.assertEqual(self.current.read_bytes(), self.current_bytes)


class TestUnchanged(ScrapeTestCase):
    def test_the_fixtures_reproduce_the_baseline_exactly(self) -> None:
        code, stdout, stderr = self.run_scrape()
        self.assertEqual(code, 0, stderr)
        self.assertIn("unchanged", stdout)
        self.assertFalse((self.out_dir / "updated.json").exists())
        self.assertEqual(self.notes(), "")

    def test_the_fixture_baseline_is_valid(self) -> None:
        validate.validate_document(json.loads(BASELINE_JSON.read_text(encoding="utf-8")))


class TestFigures(ScrapeTestCase):
    """Read off the pricing page by a human on 2026-11-10."""

    def setUp(self) -> None:
        super().setUp()
        code, _, stderr = self.run_scrape()
        self.assertEqual(code, 0, stderr)
        self.models = self.published()

    def test_a_model_carries_its_three_prompt_caching_rates_and_its_batch_rates(self) -> None:
        self.assertEqual(self.models["claude-opus-5-5"], {
            "in_per_mtok": 4.0, "out_per_mtok": 20.0, "cache_read_per_mtok": 0.2,
            "cache_write_5m_per_mtok": 5.0, "cache_write_1h_per_mtok": 8.0,
            "in_per_mtok_batch": 2.0, "out_per_mtok_batch": 10.0,
            "display_name": "Claude Opus 5.5",
        })

    def test_haiku_5_5_is_priced_up_to_and_over_100000_tokens(self) -> None:
        """"$0.10 / MTok for prompts up to 100,000 tokens", "$0.50 / MTok for prompts
        over 100,000 tokens": a prompt of 100,000 tokens is still billed low, so the
        threshold is 100,001."""
        haiku = self.models["claude-haiku-5-5"]
        self.assertEqual(haiku["long_context_threshold"], 100001)
        expected = {
            "in_per_mtok": (0.1, 0.5), "out_per_mtok": (0.5, 2.5), "cache_read_per_mtok": (0.01, 0.05),
            "cache_write_5m_per_mtok": (0.125, 0.625), "cache_write_1h_per_mtok": (0.2, 1.0),
            "in_per_mtok_batch": (0.05, 0.25), "out_per_mtok_batch": (0.25, 1.25),
        }
        for field, (low, high) in expected.items():
            with self.subTest(field=field):
                self.assertEqual((haiku[field], haiku[f"{field}_long_context"]), (low, high))

    def test_only_haiku_5_5_has_a_threshold(self) -> None:
        tiered = [k for k, e in self.models.items() if "long_context_threshold" in e]
        self.assertEqual(tiered, ["claude-haiku-5-5"])

    def test_a_dated_id_also_lists_its_alias(self) -> None:
        """Anthropic's ids page: models before the 4.6 generation have a dated id, and
        a shorter alias that points to the most recent snapshot of that minor version."""
        opus = self.models["claude-opus-4-5-20251101"]
        self.assertEqual(opus["api_ids"], ["claude-opus-4-5-20251101", "claude-opus-4-5"])
        self.assertEqual(opus["display_name"], "Claude Opus 4.5")
        self.assertNotIn("api_ids", self.models["claude-opus-4-6"], "a dateless id is the model itself")

    def test_a_retired_model_is_not_published_though_the_page_prices_it(self) -> None:
        """On 2026-11-10 the pricing page priced Claude Opus 4.1, Opus 4 and Sonnet 4;
        the deprecations page called all three Retired."""
        self.assertIn("Claude Opus 4.1", self.pricing_page)
        for model_id in ("claude-opus-4-1-20250805", "claude-opus-4-20250514", "claude-sonnet-4-20250514"):
            self.assertNotIn(model_id, self.models)

    def test_a_deprecated_model_is_still_published(self) -> None:
        self.assertEqual(self.models["claude-sonnet-4-5-20250929"]["in_per_mtok"], 3.0)

    def test_fast_mode_is_an_entry_of_its_own(self) -> None:
        self.assertEqual(self.models["claude-opus-5-5 (fast)"], {
            "in_per_mtok": 8.0, "out_per_mtok": 40.0,
            "display_name": "Claude Opus 5.5 (fast mode)", "api_ids": ["claude-opus-5-5"]})

    def test_a_fast_mode_row_naming_two_models_prices_both(self) -> None:
        for model_id in ("claude-opus-5", "claude-opus-4-8"):
            with self.subTest(model=model_id):
                fast = self.models[f"{model_id} (fast)"]
                self.assertEqual((fast["in_per_mtok"], fast["out_per_mtok"]), (10.0, 50.0))

    def test_the_tools_and_products(self) -> None:
        self.assertEqual(self.models["web_search"], {"per_1k_calls": 10.0, "display_name": "Web search", "kind": "product"})
        self.assertEqual(self.models["code_execution"]["per_container_hour"], 0.05)
        self.assertIs(self.models["web_fetch"]["free"], True)
        self.assertEqual(self.models["managed_agents"]["per_session_hour"], 0.08)

    def test_every_row_of_the_model_table_is_published_as_the_page_states_it(self) -> None:
        """Read independently of the scraper: each row's name and its five prices."""
        table = re.search(r"<table>.*?5m writes.*?</table>", self.pricing_page, re.S)
        assert table is not None
        checked = 0
        for row in re.findall(r"<tr>(.*?)</tr>", table.group(0), re.S):
            cells = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", c)).split())
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            if len(cells) != 6 or "for prompts" in row:
                continue
            name = re.match(r"Claude [A-Z][a-z]+ [0-9.]+", cells[0])
            assert name is not None, cells[0]
            entry = next((e for e in self.models.values() if e.get("display_name") == name.group(0)), None)
            if entry is None:
                continue  # retired: checked above
            figures = [float(re.match(r"\$([0-9.]+)", c).group(1)) for c in cells[1:]]  # type: ignore[union-attr]
            fields = ["in_per_mtok", "out_per_mtok", "cache_write_5m_per_mtok", "cache_write_1h_per_mtok",
                      "cache_read_per_mtok"]
            self.assertEqual([entry[f] for f in fields], figures, name.group(0))
            checked += 1
        self.assertEqual(checked, 17, "every flat row of a model still sold")


class TestFailures(ScrapeTestCase):
    def test_a_renamed_column(self) -> None:
        page = self.edited(self.pricing_page, "<button>5m writes</button>", "<button>5-minute writes</button>")
        code, _, stderr = self.run_scrape(pricing=page)
        self.assert_failed(code, stderr, "0 tables headed")

    def test_a_price_in_another_unit(self) -> None:
        page = self.edited(self.pricing_page, "$50 USD / MTok", "$50 USD / KTok")
        code, _, stderr = self.run_scrape(pricing=page)
        self.assert_failed(code, stderr, "not a price per MTok this can read")

    def test_two_tiers_that_do_not_meet(self) -> None:
        page = self.edited(self.pricing_page, "for prompts over 100,000 tokens</span></div></td><td><div>$2.50",
                           "for prompts over 200,000 tokens</span></div></td><td><div>$2.50")
        code, _, stderr = self.run_scrape(pricing=page)
        self.assert_failed(code, stderr, "not one price, nor 'up to N' then 'over N'")

    def test_batch_and_model_tables_changing_price_at_different_sizes(self) -> None:
        page = self.edited(self.pricing_page, "$0.05<!-- --> <span>/ MTok</span><span>for prompts up to 100,000 tokens",
                           "$0.05<!-- --> <span>/ MTok</span><span>for prompts up to 200,000 tokens")
        page = self.edited(page, "$0.25<!-- --> <span>/ MTok</span><span>for prompts over 100,000 tokens",
                           "$0.25<!-- --> <span>/ MTok</span><span>for prompts over 200,000 tokens")
        code, _, stderr = self.run_scrape(pricing=page)
        self.assert_failed(code, stderr, "One threshold per model")

    def test_a_page_without_the_status_table(self) -> None:
        status = self.edited(self.status_page, "API model name", "Model")
        code, _, stderr = self.run_scrape(status=status)
        self.assert_failed(code, stderr, "model status table")

    def test_a_state_the_mapping_does_not_know(self) -> None:
        status = self.edited(self.status_page, status_row("claude-sonnet-5", "Active"),
                             status_row("claude-sonnet-5", "Limited"))
        code, _, stderr = self.run_scrape(status=status)
        self.assert_failed(code, stderr, "'Limited', a state the mapping does not know")

    def test_two_ids_for_one_model(self) -> None:
        status = self.edited(self.status_page, status_row("claude-sonnet-5", "Active"),
                             status_row("claude-sonnet-5-20260101", "Active") + "<td></td><td></td></tr>"
                             + status_row("claude-sonnet-5", "Active"))
        code, _, stderr = self.run_scrape(status=status)
        self.assert_failed(code, stderr, "Refusing to guess")

    def test_a_tool_sentence_that_is_gone(self) -> None:
        page = self.edited(self.pricing_page, "Web search is available on the Claude API for",
                           "Web search costs, on the Claude API,")
        code, _, stderr = self.run_scrape(pricing=page)
        self.assert_failed(code, stderr, "no longer says how web_search is priced")

    def test_a_tool_priced_two_ways(self) -> None:
        sentence = "Web search is available on the Claude API for"
        page = self.edited(self.pricing_page, sentence,
                           "Web search is available on the Claude API for $12 USD per 1,000 searches. " + sentence)
        code, _, stderr = self.run_scrape(pricing=page)
        self.assert_failed(code, stderr, "the page prices web_search 2 ways")

    def test_a_free_tool_whose_sentence_is_gone(self) -> None:
        page = self.edited(self.pricing_page, "at <strong>no additional cost</strong>", "at <strong>$1 USD per 1,000 fetches</strong>")
        code, _, stderr = self.run_scrape(pricing=page)
        self.assert_failed(code, stderr, "no longer says how web_fetch is priced")

    def test_a_rate_in_another_unit(self) -> None:
        page = self.edited(self.pricing_page, "$0.08 USD per session-hour", "$0.08 USD per session-minute")
        code, _, stderr = self.run_scrape(pricing=page)
        self.assert_failed(code, stderr, "no longer says how managed_agents is priced")


class TestNotes(ScrapeTestCase):
    def test_a_priced_model_the_status_table_does_not_list(self) -> None:
        status = self.edited(self.status_page, status_row("claude-sonnet-5", "Active"),
                             status_row("claude-sonnet-five", "Active"))
        code, _, stderr = self.run_scrape(status=status)
        self.assertEqual(code, 0, stderr)
        self.assertIn("Claude Sonnet 5: priced, but the deprecations page's status table does not list it", self.notes())
        # Kept, marked absent, as for any entry the source stops offering.
        self.assertEqual(self.published()["claude-sonnet-5"]["absent_since"], FIXED_NOW[:10])

    def test_a_model_that_retires_is_kept_marked_absent(self) -> None:
        status = self.edited(self.status_page, status_row("claude-sonnet-4-5-20250929", "Deprecated"),
                             status_row("claude-sonnet-4-5-20250929", "Retired"))
        code, _, stderr = self.run_scrape(status=status)
        self.assertEqual(code, 0, stderr)
        models = self.published()
        self.assertEqual(models["claude-sonnet-4-5-20250929"]["absent_since"], FIXED_NOW[:10])
        self.assertNotIn("absent_since", models["claude-sonnet-4-6"])

    def test_a_row_whose_name_this_cannot_read(self) -> None:
        # Three tables name it: the model table, the batch table and the tool use table.
        page = self.edited(self.pricing_page, ">Claude Sonnet 5</a>", ">Sonnet Five</a>", count=3)
        code, _, stderr = self.run_scrape(pricing=page)
        self.assertEqual(code, 0, stderr)
        self.assertIn("model table: a row named 'Sonnet Five'", self.notes())
        self.assertIn("batch table: a row named 'Sonnet Five'", self.notes())
        self.assertEqual(self.published()["claude-sonnet-5"]["absent_since"], FIXED_NOW[:10])


class TestMapping(unittest.TestCase):
    def test_there_is_no_model_list(self) -> None:
        self.assertNotIn("models", MAPPING)

    def test_both_pages_are_named(self) -> None:
        self.assertEqual(MAPPING["sources"], [MAPPING["source"], MAPPING["status_source"]])

    def test_every_field_named_is_a_known_price_field(self) -> None:
        for table in MAPPING["tables"].values():
            for field in table.get("columns", {}).values():
                self.assertIn(field, validate.KNOWN_PRICE_FIELDS)
                if table is not MAPPING["tables"]["fast"]:
                    self.assertIn(f"{field}_long_context", validate.KNOWN_PRICE_FIELDS)
        for product in MAPPING["products"].values():
            if product["field"] is not None:
                self.assertIn(product["field"], validate.KNOWN_PRICE_FIELDS)


class TestPublishedFile(unittest.TestCase):
    """Run against the file about to be published, as every refresh does."""

    def models(self) -> dict[str, JSONDict]:
        return json.loads(PRICING_JSON.read_text(encoding="utf-8"))["providers"]["anthropic"]["models"]

    def test_the_block_matches_the_mapping(self) -> None:
        block = json.loads(PRICING_JSON.read_text(encoding="utf-8"))["providers"]["anthropic"]
        self.assertEqual((block["source"], block["sources"], block["currency"]),
                         (MAPPING["source"], MAPPING["sources"], MAPPING["currency"]))

    def test_every_dated_id_lists_its_alias_next(self) -> None:
        for model_id, entry in self.models().items():
            dated = re.fullmatch(r"(claude-.+)-[0-9]{8}", model_id)
            if dated:
                with self.subTest(model=model_id):
                    self.assertEqual(entry["api_ids"], [model_id, dated.group(1)])

    def test_every_long_context_price_has_its_threshold(self) -> None:
        for model_id, entry in self.models().items():
            if any(k.endswith("_long_context") for k in entry):
                with self.subTest(model=model_id):
                    self.assertGreater(entry["long_context_threshold"], 0)


if __name__ == "__main__":
    unittest.main()
