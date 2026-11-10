"""Tests for scripts/providers/perplexity/scrape.py.

Two fixtures, both as served on 2026-11-10 and trimmed to what these tests read:

- `pricing_ok.html`, the pricing page: its eight tables, which a human reads, and the
  Mintlify template that carries the compiled page, whose calculator rates the scraper
  reads for everything but the models;
- `models_ok.html`, the Agent API models page: its model tables, which the scraper
  reads for the models.

Every figure the pricing page's tables state is checked against what is published,
and so is every model rate the calculator carries.
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
from providers.perplexity import scrape  # noqa: E402
import pricing_validate as validate  # noqa: E402
from pricing_validate import JSONDict  # noqa: E402
from provider_runner import ScrapeError  # noqa: E402
from tests.published import published_file  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "perplexity"
PRICING_OK = FIXTURES / "pricing_ok.html"
MODELS_OK = FIXTURES / "models_ok.html"
BASELINE_JSON = FIXTURES / "baseline.json"
MAPPING_JSON = REPO_ROOT / "scripts" / "providers" / "perplexity" / "mapping.json"
MAPPING = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
PRICING_JSON = published_file()

FIXED_NOW = "2026-11-10T10:00:00Z"

# In the pricing page, the compiled source is a JSON string, so its double quotes are
# written \" -- this builds the exact bytes a replacement has to match.
Q = '\\"'


def key(name: str) -> str:
    return f"{Q}{name}{Q}:"


def tables(page: str) -> list[list[list[str]]]:
    """A page's tables, as rows of cell texts."""
    found = []
    for table in re.findall(r"<table.*?</table>", page, re.S):
        rows = []
        for row in re.findall(r"<tr.*?</tr>", table, re.S):
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
            rows.append([" ".join(html.unescape(re.sub(r"<[^>]+>", " ", c)).split()) for c in cells])
        found.append(rows)
    return found


def dollars(text: str) -> float:
    match = re.search(r"\$([0-9][0-9,]*\.?[0-9]*)", text)
    assert match, f"no dollar figure in {text!r}"
    return float(match.group(1).replace(",", ""))


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
        self.models_page = MODELS_OK.read_text(encoding="utf-8")

    def run_scrape(self, pricing: str | None = None, models: str | None = None,
                   mapping: Path = MAPPING_JSON) -> tuple[int, str, str]:
        (self.tmp / "pricing.html").write_text(self.pricing_page if pricing is None else pricing, encoding="utf-8")
        (self.tmp / "models.html").write_text(self.models_page if models is None else models, encoding="utf-8")
        manifest = self.tmp / "offline.json"
        manifest.write_text(json.dumps({MAPPING["source"]: "pricing.html", MAPPING["models_source"]: "models.html"}),
                            encoding="utf-8")
        argv = ["--out-dir", str(self.out_dir), "--current", str(self.current), "--mapping", str(mapping),
                "--offline", str(manifest), "--now", FIXED_NOW]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scrape.main(argv)
        return code, out.getvalue(), err.getvalue()

    def published(self) -> dict[str, JSONDict]:
        name = "updated.json" if (self.out_dir / "updated.json").exists() else "stamped.json"
        return json.loads((self.out_dir / name).read_text(encoding="utf-8"))["providers"]["perplexity"]["models"]

    def notes(self) -> str:
        return (self.out_dir / "notes.txt").read_text(encoding="utf-8")

    def pricing_with(self, old: str, new: str) -> str:
        self.assertEqual(self.pricing_page.count(old), 1, f"fixture drift: {old!r} is not in the page exactly once")
        return self.pricing_page.replace(old, new)

    def model_row(self, model_id: str) -> str:
        rows = [r for r in re.findall(r"<tr.*?</tr>", self.models_page, re.S) if f">{model_id}<" in r]
        self.assertEqual(len(rows), 1, f"fixture drift: no single row for {model_id}")
        return rows[0]

    def models_with_row(self, model_id: str, *cells: str) -> str:
        """The models page with one model's input, output and cache cells replaced."""
        row = self.model_row(model_id)
        tds = re.findall(r"<td[^>]*>.*?</td>", row, re.S)
        new_row = row
        for old_td, text in zip(tds[1:4], cells):
            new_row = new_row.replace(old_td, f"<td>{text}</td>", 1)
        return self.models_page.replace(row, new_row)

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

    def test_the_committed_manifest_serves_both_pages(self) -> None:
        manifest = json.loads((FIXTURES / "offline.json").read_text(encoding="utf-8"))
        self.assertEqual(set(manifest), set(MAPPING["sources"]))


class TestJsObject(unittest.TestCase):
    """The calculator's rates are a JavaScript literal; these are the forms it uses."""

    def read(self, literal: str) -> JSONDict:
        return scrape.js_object(scrape.RATES_MARKER + literal + "));const x=1;")

    def test_numbers_without_a_leading_zero_and_in_exponent_form(self) -> None:
        self.assertEqual(self.read('{"a":.25,"b":[.5,-.75],"c":5e-4,"d":1e3}'),
                         {"a": 0.25, "b": [0.5, -0.75], "c": 0.0005, "d": 1000.0})

    def test_backtick_strings_and_braces_inside_strings(self) -> None:
        self.assertEqual(self.read('{"id":`xai/grok-4.7`,"note":"a {brace} and a `tick`"}'),
                         {"id": "xai/grok-4.7", "note": "a {brace} and a `tick`"})

    def test_a_placeholder_in_a_backtick_string_is_refused(self) -> None:
        with self.assertRaisesRegex(ScrapeError, "placeholder"):
            self.read("{\"id\":`${model}`}")

    def test_an_unclosed_object_is_refused(self) -> None:
        with self.assertRaisesRegex(ScrapeError, "object literal"):
            scrape.js_object(scrape.RATES_MARKER + '{"a":{"b":1}')


class TestModels(ScrapeTestCase):
    """The models page, as it read on 2026-11-10."""

    def setUp(self) -> None:
        super().setUp()
        code, _, stderr = self.run_scrape()
        self.assertEqual(code, 0, stderr)
        self.models = self.published()

    def test_a_model_keyed_by_the_id_a_request_names(self) -> None:
        sonar = self.models["perplexity/sonar"]
        self.assertEqual((sonar["in_per_mtok"], sonar["out_per_mtok"], sonar["cache_read_per_mtok"]),
                         (0.25, 2.5, 0.0625))
        self.assertNotIn("long_context_threshold", sonar)

    def test_twenty_eight_models(self) -> None:
        self.assertEqual(len([k for k in self.models if "/" in k]), 28)

    def test_a_threshold_written_below_and_from(self) -> None:
        """'2.00 (<200k) 4.00 (≥200k)': the 200000th token is billed high."""
        grok = self.models["xai/grok-4.7"]
        self.assertEqual((grok["in_per_mtok"], grok["in_per_mtok_long_context"]), (2.0, 4.0))
        self.assertEqual((grok["out_per_mtok"], grok["out_per_mtok_long_context"]), (6.0, 12.0))
        self.assertEqual((grok["cache_read_per_mtok"], grok["cache_read_per_mtok_long_context"]), (0.5, 1.0))
        self.assertEqual(grok["long_context_threshold"], 200000)

    def test_a_threshold_written_up_to_and_past(self) -> None:
        """'0.10 (≤272k) 0.20 (>272k)': the 272001st token is."""
        luna = self.models["openai/gpt-6-luna"]
        self.assertEqual((luna["in_per_mtok"], luna["in_per_mtok_long_context"]), (0.1, 0.2))
        self.assertEqual(luna["long_context_threshold"], 272001)

    def test_a_cache_price_written_as_a_share_off_the_input_follows_both_tiers(self) -> None:
        """'90% off input' on gpt-6-luna, '95% off input' on gpt-6.1-sol."""
        luna = self.models["openai/gpt-6-luna"]
        self.assertEqual((luna["cache_read_per_mtok"], luna["cache_read_per_mtok_long_context"]), (0.01, 0.02))
        sol = self.models["openai/gpt-6.1-sol"]
        self.assertEqual((sol["cache_read_per_mtok"], sol["cache_read_per_mtok_long_context"]), (0.1, 0.2))

    def test_the_model_the_calculator_lacks_is_published(self) -> None:
        self.assertEqual(self.models["perplexity/glm-5.3-flash"],
                         {"in_per_mtok": 0.15, "out_per_mtok": 0.5, "cache_read_per_mtok": 0.03,
                          "display_name": "perplexity/glm-5.3-flash"})

    def test_a_dash_means_no_cache_price(self) -> None:
        code, _, stderr = self.run_scrape(models=self.models_with_row("perplexity/sonar", "0.25", "2.50", "—"))
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("cache_read_per_mtok", self.published()["perplexity/sonar"])


class TestAgainstTheCalculator(ScrapeTestCase):
    """The calculator's model rates are not read; they must still agree."""

    def test_every_model_the_calculator_prices_agrees_with_the_models_page(self) -> None:
        code, _, stderr = self.run_scrape()
        self.assertEqual(code, 0, stderr)
        models = self.published()
        rates = scrape.js_object(scrape.page_source(self.pricing_page))
        calculator = {m["id"]: m for m in rates["agent"]["models"]}
        self.assertEqual(len(calculator), 27)
        self.assertEqual(set(m for m in models if "/" in m) - set(calculator), {"perplexity/glm-5.3-flash"})
        for model_id, rate in calculator.items():
            entry = models[model_id]
            for source, field in (("input", "in_per_mtok"), ("output", "out_per_mtok"), ("cache", "cache_read_per_mtok")):
                with self.subTest(model=model_id, field=field):
                    value = rate[source]
                    if isinstance(value, dict):
                        self.assertAlmostEqual(entry[field], value["low"])
                        self.assertAlmostEqual(entry[f"{field}_long_context"], value["high"])
                        self.assertEqual(entry["long_context_threshold"], rate["tierThreshold"] + 1)
                    else:
                        self.assertAlmostEqual(entry[field], value)


class TestProducts(ScrapeTestCase):
    def setUp(self) -> None:
        super().setUp()
        code, _, stderr = self.run_scrape()
        self.assertEqual(code, 0, stderr)
        self.models = self.published()

    def test_the_sandbox_is_billed_per_session_and_per_search(self) -> None:
        self.assertEqual(self.models["sandbox"],
                         {"per_session": 0.03, "per_1k_calls": 2.5, "display_name": "sandbox", "kind": "product"})

    def test_the_effort_levels_are_not_published(self) -> None:
        """Their token and tool counts are median examples, not prices."""
        for preset in ("fast", "low", "medium", "high", "xhigh"):
            self.assertNotIn(preset, self.models)

    def test_embeddings_and_the_decisions_api_are_billed_on_input_only(self) -> None:
        for model_id in ("pplx-embed-v1-4b", "pplx-decider-v1.2"):
            with self.subTest(model=model_id):
                self.assertEqual(set(self.models[model_id]), {"in_per_mtok", "display_name"})


class TestAgainstThePricingTables(ScrapeTestCase):
    """Every figure a human reads on the pricing page, against what is published.

    The scraper reads the calculator's rates, not these tables; this is the check
    that the two agree, as they did on 2026-11-10.
    """

    def setUp(self) -> None:
        super().setUp()
        code, _, stderr = self.run_scrape()
        self.assertEqual(code, 0, stderr)
        self.models = self.published()
        self.tables = tables(self.pricing_page)
        self.by_head = {rows[0][0]: rows for rows in self.tables if rows and rows[0]}

    def test_the_tools_table(self) -> None:
        rows = self.by_head["Tool"][1:]
        self.assertEqual(len(rows), 7)
        keys = {"web_search (standard)": "web_search", "web_search (Fast Search)": "web_search (fast)"}
        for name, price, description in rows:
            with self.subTest(tool=name):
                if name == "sandbox":
                    self.assertEqual(dollars(price), self.models["sandbox"]["per_session"])
                    self.assertIn("per session", price)
                    search = re.search(r"billed at (\$[0-9.]+) per request", description)
                    assert search is not None
                    self.assertAlmostEqual(dollars(search.group(1)) * 1000, self.models["sandbox"]["per_1k_calls"])
                    continue
                self.assertRegex(price, r"per (successful )?invocation")
                self.assertAlmostEqual(dollars(price) * 1000, self.models[keys.get(name, name)]["per_1k_calls"])

    def test_the_search_api_table(self) -> None:
        rows = {row[0]: row[1] for row in self.by_head["API"][1:]}
        self.assertEqual(dollars(rows["Search API"]), self.models["search_api"]["per_1k_calls"])
        self.assertEqual(dollars(rows["Search API with Fast Search"]), self.models["search_api (fast)"]["per_1k_calls"])

    def test_the_embeddings_tables(self) -> None:
        rows = [row for t in self.tables if t[0][0] == "Model" and "Dimensions" in t[0] for row in t[1:]]
        self.assertEqual(len(rows), 4)
        for model_id, _, price in rows:
            with self.subTest(model=model_id):
                self.assertEqual(dollars(price), self.models[model_id]["in_per_mtok"])

    def test_the_decisions_api_table(self) -> None:
        rows = [row for t in self.tables if t[0][0] == "Model" and "Output tokens" in t[0] for row in t[1:]]
        self.assertEqual(len(rows), 2)
        for model_id, price, output, fee in rows:
            with self.subTest(model=model_id):
                self.assertEqual(dollars(price), self.models[model_id]["in_per_mtok"])
                self.assertEqual((output, fee), ("Free", "None"))


class TestFailures(ScrapeTestCase):
    def test_a_pricing_page_without_the_rates(self) -> None:
        code, _, stderr = self.run_scrape(pricing=self.pricing_page.replace(scrape.RATES_MARKER, "const[x]=useState(()=>("))
        self.assert_failed(code, stderr, "no pricing-calculator rates")

    def test_a_section_that_changes_unit(self) -> None:
        code, _, stderr = self.run_scrape(pricing=self.pricing_with("`$ per invocation`", "`$ per 1,000 invocations`"))
        self.assert_failed(code, stderr, "'tools'")

    def test_a_rate_that_is_not_a_number(self) -> None:
        old = f"{key('fetch_url')}5e-4"
        code, _, stderr = self.run_scrape(pricing=self.pricing_with(old, f"{key('fetch_url')}`cheap`"))
        self.assert_failed(code, stderr, "tools.fetch_url")

    def test_a_models_page_whose_tables_change_unit(self) -> None:
        models = self.models_page.replace("Input ($/1M)", "Input ($/1K)")
        self.assertNotEqual(models, self.models_page)
        code, _, stderr = self.run_scrape(models=models)
        self.assert_failed(code, stderr, "no table headed")

    def test_a_price_form_nothing_here_reads(self) -> None:
        code, _, stderr = self.run_scrape(models=self.models_with_row("perplexity/sonar", "0.25", "2.50", "half price"))
        self.assert_failed(code, stderr, "not a price form this can read")

    def test_two_tiers_that_do_not_meet(self) -> None:
        code, _, stderr = self.run_scrape(
            models=self.models_with_row("perplexity/sonar", "0.25 (<200k) 0.50 (≥272k)", "2.50", "0.0625"))
        self.assert_failed(code, stderr, "do not meet")

    def test_tiers_that_change_at_different_sizes(self) -> None:
        code, _, stderr = self.run_scrape(models=self.models_with_row(
            "perplexity/sonar", "0.25 (<200k) 0.50 (≥200k)", "2.50 (≤272k) 5.00 (>272k)", "0.0625"))
        self.assert_failed(code, stderr, "change tier at different sizes")

    def test_a_model_listed_twice(self) -> None:
        row = self.model_row("perplexity/sonar")
        code, _, stderr = self.run_scrape(models=self.models_page.replace(row, row + row))
        self.assert_failed(code, stderr, "listed twice")


class TestNotes(ScrapeTestCase):
    def test_a_section_nothing_here_reads_is_noted_and_everything_else_publishes(self) -> None:
        """The calculator still has code for a `sonar` section, the old Sonar API."""
        code, _, stderr = self.run_scrape(pricing=self.pricing_with(f"{key('agent')}{{", f"{key('sonar')}{{}},{key('agent')}{{"))
        self.assertEqual(code, 0, stderr)
        self.assertIn("a section 'sonar'", self.notes())
        self.assertEqual(self.published()["perplexity/sonar"]["in_per_mtok"], 0.25)

    def test_a_unit_for_a_section_nothing_here_reads_is_noted(self) -> None:
        old = f"{key('search.per1k')}`$ per 1,000 requests`"
        code, _, stderr = self.run_scrape(pricing=self.pricing_with(old, f"{old},{key('sonar.request')}`$ per 1,000 requests`"))
        self.assertEqual(code, 0, stderr)
        self.assertIn("a unit for 'sonar.request'", self.notes())


class TestPublishedFile(unittest.TestCase):
    """Run against the file about to be published, as every refresh does."""

    def setUp(self) -> None:
        self.models = json.loads(PRICING_JSON.read_text(encoding="utf-8"))["providers"]["perplexity"]["models"]

    def test_every_long_context_price_has_its_threshold(self) -> None:
        for model_id, entry in self.models.items():
            if any(k.endswith("_long_context") for k in entry):
                with self.subTest(model=model_id):
                    self.assertGreater(entry["long_context_threshold"], 0)

    def test_every_entry_is_a_model_keyed_provider_slash_model_or_says_what_it_is(self) -> None:
        for model_id, entry in self.models.items():
            with self.subTest(entry=model_id):
                self.assertTrue("/" in model_id or entry.get("kind") == "product" or model_id.startswith("pplx-"))


if __name__ == "__main__":
    unittest.main()
