"""Tests for the OVH coverage check: does the catalog page still show what OVH sells?

Standard library only: `python3 -m unittest discover tests`.

The check has no authority over any price, so what these tests guard is narrow and
specific: that it reports the right gaps, that it distinguishes "I found something"
from "I could not look", and that it stays quiet about the documented exceptions.
That last one matters most in practice -- a weekly check that reports the same
non-problem every Monday is a check nobody reads by March.
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

from providers.ovh import check_coverage  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ovh"
CATALOG_OK = FIXTURES / "catalog_ok.html"
MODELS_API_OK = FIXTURES / "models_api_ok.json"

OVH_DIR = REPO_ROOT / "scripts" / "providers" / "ovh"
MAPPING_JSON = OVH_DIR / "mapping.json"
COVERAGE_JSON = OVH_DIR / "coverage.json"

# The five OVH still serves but stopped listing on its catalog page between
# 2026-08-03 and 2026-08-04. The reason this check exists at all.
WITHDRAWN = (
    "Mistral-7B-Instruct-v0.3",
    "Mistral-Nemo-Instruct-2407",
    "Mistral-Small-3.2-24B-Instruct-2506",
    "Qwen3-32B",
    "Qwen3-Coder-30B-A3B-Instruct",
)


class CoverageTestCase(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def run_check(self, *, html: Path | None = None, models: Path | None = None) -> tuple[int, str, str]:
        argv = [
            "--mapping", str(MAPPING_JSON),
            "--coverage", str(COVERAGE_JSON),
            "--html", str(html or CATALOG_OK),
            "--models-json", str(models or MODELS_API_OK),
        ]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = check_coverage.main(argv)
        return code, out.getvalue(), err.getvalue()

    def write_models(self, ids: list[str]) -> Path:
        path = self.tmp / "models.json"
        payload = {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]}
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


class TestFindings(CoverageTestCase):
    def test_the_real_capture_reports_the_five_withdrawn_models(self) -> None:
        """Both fixtures are real captures taken hours apart. The gap between them is
        the exact thing this check was written for, so it must still be found."""
        code, stdout, _ = self.run_check()
        self.assertEqual(code, check_coverage.EXIT_GAP_FOUND)
        self.assertIn("SOLD BUT NOT ON THE CATALOG PAGE", stdout)
        for model_id in WITHDRAWN:
            self.assertIn(model_id, stdout)

    def test_no_gap_is_reported_when_the_two_sources_agree(self) -> None:
        catalog = check_coverage.catalog_model_names(CATALOG_OK.read_text(encoding="utf-8"))
        ignore = json.loads(COVERAGE_JSON.read_text(encoding="utf-8"))["ignore"]
        code, stdout, _ = self.run_check(models=self.write_models(sorted(catalog - set(ignore))))
        self.assertEqual(code, check_coverage.EXIT_OK)
        self.assertIn("No gap", stdout)

    def test_a_model_on_the_page_that_is_not_published_is_reported(self) -> None:
        """The other direction: OVH adding a model to the catalog. scrape.py is silent
        about it -- it only ever looks up ids the mapping already names."""
        findings = check_coverage.compare(
            catalog={"gpt-oss-120b", "brand-new-model"},
            api={"gpt-oss-120b", "brand-new-model"},
            mapped={"gpt-oss-120b"},
            ignore={},
        )
        self.assertEqual(len(findings), 1)
        self.assertIn("NOT PUBLISHED", findings[0])
        self.assertIn("brand-new-model", findings[0])

    def test_a_published_key_the_api_does_not_serve_is_reported(self) -> None:
        """Catches the failure mode that would follow from keying pricing.json off the
        catalog's CMS slug instead of its `name`: keys that resolve against nothing."""
        findings = check_coverage.compare(
            catalog={"qwen-3-6-27b"}, api={"Qwen3.6-27B"}, mapped={"qwen-3-6-27b"}, ignore={}
        )
        self.assertTrue(any("NOT IN THE API LIST" in f for f in findings))

    def test_the_ignore_list_silences_exactly_what_it_documents(self) -> None:
        """`ppl` and the upstream stabilityai/ alias are in every real API response and
        will never be on the catalog page. Without the ignore list this check would
        report a gap every single week and be worth nothing."""
        code, stdout, _ = self.run_check()
        # The run does report the five withdrawn models; what it must not do is drag
        # the documented exceptions along with them.
        self.assertEqual(code, check_coverage.EXIT_GAP_FOUND)
        self.assertNotIn("stabilityai/", stdout)
        for ignored in ("ppl", "nvr-tts-en-us", "nvr-tts-de-de", "nvr-tts-es-es", "nvr-tts-it-it"):
            self.assertNotIn(ignored, stdout.split("SOLD BUT", 1)[1])


class TestCouldNotLook(CoverageTestCase):
    """"I could not look" must never be reported as "I looked and found nothing"."""

    def test_malformed_model_list_is_a_failed_check_not_a_clean_result(self) -> None:
        bad = self.tmp / "bad.json"
        for content in ('{"data": []}', '{"data": "nope"}', '[]', '{"object": "list"}'):
            bad.write_text(content, encoding="utf-8")
            with self.subTest(content=content):
                code, _, stderr = self.run_check(models=bad)
                self.assertEqual(code, check_coverage.EXIT_CHECK_FAILED)
                self.assertIn("could not run", stderr)

    def test_an_entry_without_a_string_id_is_a_failed_check(self) -> None:
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps({"data": [{"id": 42}]}), encoding="utf-8")
        code, _, stderr = self.run_check(models=bad)
        self.assertEqual(code, check_coverage.EXIT_CHECK_FAILED)
        self.assertIn("could not run", stderr)

    def test_a_missing_file_is_a_failed_check(self) -> None:
        code, _, stderr = self.run_check(models=self.tmp / "does-not-exist.json")
        self.assertEqual(code, check_coverage.EXIT_CHECK_FAILED)
        self.assertIn("could not run", stderr)

    def test_an_unreadable_catalog_is_a_failed_check(self) -> None:
        empty = self.tmp / "empty.html"
        empty.write_text("<html><body></body></html>", encoding="utf-8")
        code, _, stderr = self.run_check(html=empty)
        self.assertEqual(code, check_coverage.EXIT_CHECK_FAILED)
        self.assertIn("could not read the catalog", stderr)

    def test_the_three_exit_codes_are_distinct(self) -> None:
        codes = (check_coverage.EXIT_OK, check_coverage.EXIT_CHECK_FAILED, check_coverage.EXIT_GAP_FOUND)
        self.assertEqual(len(set(codes)), 3)


class TestItTouchesNothing(CoverageTestCase):
    def test_pricing_json_is_never_written(self) -> None:
        """This check reads no price and may not change one. Guarded rather than
        assumed: it sits in the same directory as the scraper that does write."""
        published = REPO_ROOT / "pricing.json"
        before = published.read_bytes()
        self.run_check()
        self.assertEqual(published.read_bytes(), before)

    def test_the_ignore_list_gives_a_reason_for_every_entry(self) -> None:
        """An ignore entry is a claim a human made. Without a reason nobody can ever
        judge whether it still holds, and it becomes permanent by default."""
        ignore = json.loads(COVERAGE_JSON.read_text(encoding="utf-8"))["ignore"]
        self.assertTrue(ignore)
        for model_id, reason in ignore.items():
            with self.subTest(model=model_id):
                self.assertIsInstance(reason, str)
                self.assertGreater(len(reason), 30, "a reason must actually explain")


if __name__ == "__main__":
    unittest.main()
