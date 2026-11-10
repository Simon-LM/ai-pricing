"""Tests for scripts/providers/googlecloud/scrape.py.

The fixture is Google Cloud's Speech-to-Text pricing page as served on 2026-11-10, with
scripts, styles and icons stripped and its pricing part kept as served. The figures
asserted below were read off that page by a human.
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
from providers.googlecloud import scrape  # noqa: E402
import pricing_validate as validate  # noqa: E402
from pricing_validate import JSONDict  # noqa: E402
from tests.published import published_file  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "googlecloud"
PRICING_OK = FIXTURES / "pricing_ok.html"
BASELINE_JSON = FIXTURES / "baseline.json"
MAPPING_JSON = REPO_ROOT / "scripts" / "providers" / "googlecloud" / "mapping.json"
MAPPING = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
PRICING_JSON = published_file()

FIXED_NOW = "2026-11-10T10:00:00Z"
V2 = "speech-to-text v2 recognition standard"


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

    def run_scrape(self, page: str | None = None) -> tuple[int, str, str]:
        source = self.tmp / "pricing.html"
        source.write_text(self.page if page is None else page, encoding="utf-8")
        argv = ["--out-dir", str(self.out_dir), "--current", str(self.current), "--mapping", str(MAPPING_JSON),
                "--html", str(source), "--now", FIXED_NOW]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scrape.main(argv)
        return code, out.getvalue(), err.getvalue()

    def published(self) -> dict[str, JSONDict]:
        name = "updated.json" if (self.out_dir / "updated.json").exists() else "stamped.json"
        return json.loads((self.out_dir / name).read_text(encoding="utf-8"))["providers"]["googlecloud"]["models"]

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

    def test_one_entry_per_volume_tier_each_a_single_price(self) -> None:
        tiers = {k: e["per_audio_minute"] for k, e in self.models.items() if k.startswith(V2 + " (")}
        self.assertEqual(tiers, {
            f"{V2} (0 to 500,000 min/month)": 0.016,
            f"{V2} (500,000 to 1,000,000 min/month)": 0.01,
            f"{V2} (1,000,000 to 2,000,000 min/month)": 0.008,
            f"{V2} (2,000,000+ min/month)": 0.004,
        })
        for key in tiers:
            self.assertEqual(set(self.models[key]), {"per_audio_minute", "display_name"}, key)

    def test_the_commitment_plans_are_not_read(self) -> None:
        """$0.0144 and $0.0128 are the 1-year and 3-year plan prices of the first tier."""
        prices = {e["per_audio_minute"] for e in self.models.values()}
        self.assertFalse(prices & {0.0144, 0.0128, 0.0027, 0.0024})

    def test_dynamic_batch(self) -> None:
        self.assertEqual(self.models["speech-to-text v2 dynamic batch recognition standard"]["per_audio_minute"], 0.003)

    def test_a_free_tier_is_not_an_entry(self) -> None:
        """V1: "0 minute to 60 minute $0.00 (Free)", then "60 minute and above $0.016"."""
        logged = "speech-to-text v1 speech recognition with data logging standard"
        self.assertEqual(self.models[logged]["per_audio_minute"], 0.016)
        self.assertEqual(self.models["speech-to-text v1 speech recognition without data logging standard"]
                         ["per_audio_minute"], 0.024)
        self.assertFalse([k for k in self.models if "60" in k])

    def test_the_medical_models(self) -> None:
        self.assertEqual(self.models["speech-to-text v1 medical dictation"]["per_audio_minute"], 0.078)
        self.assertEqual(self.models["speech-to-text v1 medical conversation"]["per_audio_minute"], 0.078)

    def test_nine_entries(self) -> None:
        self.assertEqual(len(self.models), 9)


class TestFailures(ScrapeTestCase):
    def test_a_price_in_another_unit(self) -> None:
        page = self.edited("$0.003 / 1 minute</td>", "$0.18 / 1 hour</td>")
        code, _, stderr = self.run_scrape(page)
        self.assert_failed(code, stderr, "is not a price per minute this can read")

    def test_a_range_this_cannot_read(self) -> None:
        page = self.edited("2,000,000 minute and above", "2,000,000 minute or more", count=3)
        code, _, stderr = self.run_scrape(page)
        self.assert_failed(code, stderr, "is not a range of minutes this can read")

    def test_a_table_under_no_version_heading(self) -> None:
        page = self.page.replace("Speech-to-Text V2 API", "Speech-to-Text API (current)")
        page = page.replace("Speech-to-Text V1 API", "Speech-to-Text API (legacy)")
        code, _, stderr = self.run_scrape(page)
        self.assert_failed(code, stderr, "under no 'Speech-to-Text V… API' heading")

    def test_too_few_entries(self) -> None:
        page = self.page.replace("<p>Price (USD)</p>", "<p>Cost (USD)</p>")
        code, _, stderr = self.run_scrape(page)
        self.assert_failed(code, stderr, "refusing to publish so few")


class TestNotes(ScrapeTestCase):
    def test_a_table_headed_another_way(self) -> None:
        page = self.page.replace("<p>Price (USD)</p>", "<p>Cost (USD)</p>", 1)
        code, _, stderr = self.run_scrape(page)
        self.assertEqual(code, 0, stderr)
        self.assertIn("not ['Category', 'Model', 'Price (USD)']; not read", self.notes())
        self.assertEqual(self.published()[f"{V2} (0 to 500,000 min/month)"]["absent_since"], FIXED_NOW[:10])


class TestPublishedFile(unittest.TestCase):
    """Run against the file about to be published, as every refresh does."""

    def test_the_block_matches_the_mapping(self) -> None:
        block = json.loads(PRICING_JSON.read_text(encoding="utf-8"))["providers"]["googlecloud"]
        self.assertEqual((block["source"], block["sources"], block["currency"]),
                         (MAPPING["source"], MAPPING["sources"], MAPPING["currency"]))

    def test_every_entry_is_one_price_per_minute(self) -> None:
        block = json.loads(PRICING_JSON.read_text(encoding="utf-8"))["providers"]["googlecloud"]
        for key, entry in block["models"].items():
            with self.subTest(entry=key):
                self.assertIn("per_audio_minute", entry)


if __name__ == "__main__":
    unittest.main()
