"""Tests for scripts/trim_fixture.py, the tool that cuts a provider's test fixture out of
a page.

A fixture is what a provider's scraper is tested against, so the tool's own test is mostly
one thing: every committed Anthropic fixture, cut again from what its comment records,
comes back byte for byte. A fixture edited by hand stops matching, and the fix is to cut
it again, not to correct the test.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

import trim_fixture  # noqa: E402

ANTHROPIC = REPO_ROOT / "tests" / "fixtures" / "anthropic"

# Each fixture, and the heading the scraper reads first: what the fixture starts at.
COMMITTED = (
    (ANTHROPIC / "pricing_ok.html", "Model pricing"),
    (ANTHROPIC / "deprecations_ok.html", "Overview"),
)

PAGE = """<!DOCTYPE html>
<html lang="en"><head><title>Pricing - Example</title><style>td{color:red}</style></head><body>
<nav class="site"><a href="/">Home</a></nav>
<h2 id="prices">Model pricing<span><button aria-label="Copy link to Model pricing"></button></span></h2>
<p>Prices are per million tokens.</p>
<table class="docs-table"><tbody><tr><td rowSpan="2"><div><a href="/docs/models/opus-5-5">Claude Opus 5.5</a></div></td>
<td><div>$4<!-- --> <small>/ MTok</small></div></td></tr></tbody></table>
<footer><img src="/logo.svg" alt="logo"><script>track()</script><svg><path d="M0 0"/></svg></footer>
</body></html>
"""


def cut(page: str, **overrides: object) -> str:
    settings: dict[str, object] = {
        "url": "https://example.com/pricing", "served": "2026-11-15",
        "from_heading": "Model pricing", "comment": "the text and the tables kept.",
    }
    settings.update(overrides)
    return trim_fixture.trim(page, **settings)  # type: ignore[arg-type]


class TestTheCommittedFixtures(unittest.TestCase):
    def test_every_committed_anthropic_fixture_is_what_this_tool_cuts(self) -> None:
        """A fixture's comment states the page and the day: cutting it again from those
        facts, and from the heading its tests read, gives the same bytes back."""
        for path, heading in COMMITTED:
            with self.subTest(fixture=path.name):
                fixture = path.read_text(encoding="utf-8")
                url, served, comment = trim_fixture.recorded_comment(fixture)
                self.assertEqual(trim_fixture.trim(fixture, url, served, heading, comment), fixture)

    def test_what_the_fixtures_keep_is_what_the_scraper_reads(self) -> None:
        """The three things the Anthropic scraper reads are still in the fixture it is
        tested against: a cell's own element boundaries, a header row's words, and what a
        badge says to a screen reader. A trim that lost one would pass its own test and
        make the provider's test pass for the wrong reason."""
        fixture = (ANTHROPIC / "pricing_ok.html").read_text(encoding="utf-8")
        self.assertIn("$0.10<!-- --> <small>/ MTok</small><small>for prompts up to 100,000 tokens</small>", fixture)
        self.assertIn("<button>5m writes</button>", fixture)
        self.assertIn('aria-label="Claude Mythos 5.2 (Verification required)"', fixture)


class TestWhatItKeepsAndDrops(unittest.TestCase):
    def test_the_attributes_the_fixtures_do_not_carry_are_dropped(self) -> None:
        fixture = cut(PAGE)
        self.assertIn('href="/docs/models/opus-5-5"', fixture)
        self.assertIn('aria-label="Copy link to Model pricing"', fixture)
        self.assertNotIn("class=", fixture)
        self.assertNotIn("id=", fixture)
        self.assertNotIn("src=", fixture)
        self.assertIn("<img>", fixture)

    def test_an_attribute_the_page_writes_in_camel_case_keeps_its_case(self) -> None:
        """`rowspan` is read whatever its case, but a fixture that rewrote it would be a
        fixture that lies about the page."""
        self.assertIn('<td rowSpan="2">', cut(PAGE))

    def test_the_icons_the_styles_and_the_scripts_take_their_text_with_them(self) -> None:
        fixture = cut(PAGE)
        self.assertNotIn("track()", fixture)
        self.assertNotIn("td{color:red}", fixture)
        self.assertNotIn("M0 0", fixture)
        self.assertNotIn("<svg", fixture)

    def test_an_element_that_carries_no_text_is_kept_without_it(self) -> None:
        """The elements a page wraps a price in are the scraper's business: <small> stays,
        which is what makes a fixture able to catch a page that changed it."""
        self.assertIn("<small>/ MTok</small>", cut(PAGE))


class TestTheCutItself(unittest.TestCase):
    def test_the_chrome_before_the_heading_is_dropped_and_the_head_states_the_source(self) -> None:
        fixture = cut(PAGE)
        self.assertEqual(
            fixture[:100],
            "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\"><title>Pricing - Example</title></head><body>\n"[:100],
        )
        self.assertIn('<body>\n<!-- https://example.com/pricing as served on 2026-11-15:', fixture)
        self.assertTrue(fixture.startswith("<!DOCTYPE html>"), fixture[:80])
        self.assertIn("<h2>Model pricing", fixture)
        self.assertNotIn("<nav", fixture)
        self.assertNotIn("Home", fixture)
        self.assertIn("</body></html>", fixture)

    def test_a_heading_the_page_does_not_carry_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            cut(PAGE, from_heading="Batch pricing")

    def test_a_page_without_a_lang_or_a_title_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            cut("<h2>Model pricing</h2>")

    def test_a_whole_page_is_kept_when_no_heading_is_named(self) -> None:
        fixture = cut(PAGE, from_heading=None)
        self.assertIn("<nav>", fixture)
        self.assertIn("Home", fixture)

    def test_the_date_is_a_date(self) -> None:
        with self.assertRaises(SystemExit):
            trim_fixture.main(["--url", "https://example.com", "--served", "yesterday", "--out", "/dev/null"])


class TestReadingAFixtureBack(unittest.TestCase):
    def test_the_comment_states_the_page_the_day_and_what_was_stripped(self) -> None:
        url, served, comment = trim_fixture.recorded_comment(cut(PAGE))
        self.assertEqual((url, served), ("https://example.com/pricing", "2026-11-15"))
        self.assertEqual(comment, "the text and the tables kept.")

    def test_a_fixture_without_that_comment_says_so(self) -> None:
        with self.assertRaises(ValueError):
            trim_fixture.recorded_comment("<html><body>no comment</body></html>")


if __name__ == "__main__":
    unittest.main()
