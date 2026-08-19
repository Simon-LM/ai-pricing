"""Tests for the Mistral scraper, and for what happens when things break.

Standard library only: `python3 -m unittest discover tests`.

Every scenario asserts the same thing at the end, through `assert_current_untouched`:
pricing.json is byte-for-byte what it was. That is the property the whole design
exists to protect -- a stale price whose date the user can see is far better than a
wrong one they cannot.

This provider reads TWO sources, and the split is the thing most worth testing.
Models come from docs.mistral.ai, which states a machine-readable price object and an
isRetired flag per model; the billable non-models come from mistral.ai/pricing/api,
which is the only place they are priced. Reading only the second, as an earlier
version did, silently missed four priced models -- including one the pricing page had
dropped while Mistral was still selling it.

Every scenario runs against committed fixtures through `--offline`, which serves each
URL from disk and refuses any URL the manifest does not name. A test that forgets a
fixture therefore fails loudly instead of quietly reaching the network and passing for
the wrong reason.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import unittest
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# Imported via the providers.mistral package, not a flat `import scrape` off a
# directly-inserted directory: OVH's scraper is also a module literally named
# scrape.py, and a flat import would collide in sys.modules the moment both test
# files run in the same process (as `unittest discover` does) -- whichever loads
# first wins the name, and the other file silently gets the wrong module.
from providers.mistral import scrape  # noqa: E402
import pricing_validate as validate  # noqa: E402
from pricing_validate import JSONDict  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "mistral"

# The scrape scenarios compare the fixtures against a baseline pinned to that same
# capture, never against the live pricing.json. Otherwise every genuine price change
# would fail the test suite until somebody re-captured the fixtures, which would put
# friction on exactly the path that must stay quick: a price correction reaching
# consumers. The two only move together when the fixtures are deliberately re-captured.
BASELINE_JSON = FIXTURES / "baseline.json"

PRICING_JSON = REPO_ROOT / "pricing.json"
MAPPING_JSON = REPO_ROOT / "scripts" / "providers" / "mistral" / "mapping.json"

FIXED_NOW = "2026-08-18T04:00:00Z"

DOCS_INDEX = "https://docs.mistral.ai/models"
PRICING_PAGE = "https://mistral.ai/pricing/api"


def mutate(source: str, old: str, new: str, expected_count: int | None = 1) -> str:
    """Replace `old` with `new`, checking first that there was something to replace.

    `expected_count` is an exact number where uniqueness is the point of the test --
    a price mutation that silently hit two rows would prove nothing. Pass None for
    structural mutations that sweep a whole page, where the count is incidental; those
    still refuse to run against zero matches, which is the failure that would leave a
    test testing nothing.
    """
    found = source.count(old)
    if found == 0 or (expected_count is not None and found != expected_count):
        raise AssertionError(
            f"fixture drift: expected {expected_count or 'at least one'} occurrence(s) of "
            f"{old!r}, found {found}. Re-capture the fixtures under tests/fixtures/mistral/."
        )
    return source.replace(old, new)


class ScrapeTestCase(unittest.TestCase):
    """Base class: each test gets its own copy of every fixture, free to mutate."""

    maxDiff = None

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        self.sources = self.tmp / "sources"
        shutil.copytree(FIXTURES, self.sources)
        self.manifest = json.loads((self.sources / "offline.json").read_text(encoding="utf-8"))

        self.out_dir = self.tmp / "out"
        self.current = self.tmp / "pricing.json"
        shutil.copy2(BASELINE_JSON, self.current)
        self.current_bytes = self.current.read_bytes()

    # -- fixture helpers -----------------------------------------------------------

    def path_for(self, url: str) -> Path:
        return self.sources / self.manifest[url]

    def edit(self, url: str, old: str, new: str, expected_count: int | None = 1) -> None:
        """Rewrite one served fixture in place."""
        path = self.path_for(url)
        path.write_text(
            mutate(path.read_text(encoding="utf-8"), old, new, expected_count), encoding="utf-8"
        )

    def docs_url(self, slug: str) -> str:
        return f"https://docs.mistral.ai/models/{slug}"

    def withdraw_from_index(self, slug: str) -> None:
        """Remove one model's card from the docs index, as if Mistral had retired it."""
        index = self.path_for(DOCS_INDEX)
        text = index.read_text(encoding="utf-8")
        kept = [line for line in text.splitlines() if f'href="/models/{slug}"' not in line]
        self.assertEqual(len(kept), len(text.splitlines()) - 1, f"{slug} is not one line of the index")
        index.write_text("\n".join(kept) + "\n", encoding="utf-8")

    # -- running -------------------------------------------------------------------

    def run_scrape(self, now: str = FIXED_NOW) -> tuple[int, str, str]:
        """Run the script end to end against the (possibly mutated) fixtures."""
        argv = [
            "--out-dir", str(self.out_dir),
            "--current", str(self.current),
            "--mapping", str(MAPPING_JSON),
            "--offline", str(self.sources / "offline.json"),
            "--now", now,
        ]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scrape.main(argv)
        return code, out.getvalue(), err.getvalue()

    # -- assertions ----------------------------------------------------------------

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
        return self.candidate(name)["providers"]["mistral"]

    def models(self, name: str = "updated.json") -> JSONDict:
        return self.candidate_block(name)["models"]

    def notes(self) -> str:
        return (self.out_dir / "notes.txt").read_text(encoding="utf-8")


# ======================================================================================
# Outcome 1: figures unchanged
# ======================================================================================


class TestUnchanged(ScrapeTestCase):
    def test_unchanged_sources_stamp_and_publish_nothing_new(self) -> None:
        code, stdout, _ = self.run_scrape()

        self.assertEqual(code, 0)
        self.assertIn("unchanged", stdout)
        self.assertFalse((self.out_dir / "updated.json").exists())

        stamped = self.candidate_block("stamped.json")
        published = json.loads(self.current_bytes)["providers"]["mistral"]

        self.assertEqual(stamped["checked_utc"], FIXED_NOW, "checked_utc was not refreshed")
        self.assertEqual(stamped["updated"], published["updated"], "updated moved with no figure")
        self.assertEqual(stamped["models"], published["models"])
        self.assert_current_untouched()

    def test_the_source_field_names_the_docs_site(self) -> None:
        """Consumers follow `source` to check a figure by hand, so it has to point at
        where the models actually come from -- not at the page the products come from."""
        self.run_scrape()
        self.assertEqual(self.candidate_block("stamped.json")["source"], DOCS_INDEX)

    def test_models_come_from_the_docs_site(self) -> None:
        self.run_scrape()
        models = self.models("stamped.json")
        self.assertEqual(models["mistral medium 3.5"]["in_per_mtok"], 1.5)
        self.assertEqual(models["mistral medium 3.5"]["out_per_mtok"], 7.5)
        self.assertNotIn("kind", models["mistral medium 3.5"], "a model is not a product")

    def test_the_models_the_pricing_page_omits_are_published(self) -> None:
        """The reason this provider reads two sources at all. The pricing page shows no
        card for any of these three, and Mistral sells all of them."""
        models = self.models("stamped.json") if not self.run_scrape()[0] else {}
        self.assertEqual(models["ocr 4.0"]["per_1k_pages"], 4.0)
        self.assertEqual(models["ocr 3"]["per_1k_pages"], 2.0)
        self.assertIs(models["leanstral 1.5"]["free"], True)

    def test_one_model_priced_twice_in_the_same_unit_keeps_both_figures(self) -> None:
        """OCR bills annotated pages at a higher rate than plain ones. Two per-1000-page
        prices on one model, which is why annotated pages have a field of their own --
        an entry has one of each field, and dropping either would understate a bill."""
        self.run_scrape()
        entry = self.models("stamped.json")["ocr 4.1"]
        self.assertEqual(entry["per_1k_pages"], 4.0)
        self.assertEqual(entry["per_1k_annotated_pages"], 5.0)

    def test_a_cached_input_price_is_published_under_its_own_field(self) -> None:
        """GLM 5.2 states two figures denominated '/M Tokens' on the input side, and only
        their labels tell them apart. Folding the cached rate into in_per_mtok would
        understate every ordinary input token by a factor of ten."""
        self.run_scrape()
        entry = self.models("stamped.json")["z.ai glm 5.2"]
        self.assertEqual(entry["in_per_mtok"], 1.4)
        self.assertEqual(entry["cache_read_per_mtok"], 0.14)
        self.assertEqual(entry["out_per_mtok"], 4.4)

    def test_a_model_billed_in_two_units_keeps_both(self) -> None:
        """Voxtral Small is billed per minute of audio AND per million tokens of text.
        Code that assumes one unit per model truncates it, and the truncation is silent."""
        self.run_scrape()
        entry = self.models("stamped.json")["voxtral small"]
        self.assertEqual(entry["per_audio_minute"], 0.004)
        self.assertEqual(entry["in_per_mtok"], 0.1)
        self.assertEqual(entry["out_per_mtok"], 0.4)

    def test_a_free_model_is_published_free_and_never_as_zero(self) -> None:
        """This source says `free` in the data, so it is taken as said rather than
        inferred from a figure of 0 -- which is also what a broken parser reads."""
        self.run_scrape()
        entry = self.models("stamped.json")["leanstral 1.5"]
        self.assertIs(entry["free"], True)
        self.assertEqual([k for k in entry if k in validate.KNOWN_PRICE_FIELDS], [])

    def test_an_unbilled_side_is_simply_absent(self) -> None:
        """Voxtral TTS charges for the audio it generates and nothing for the text it is
        given. The zero on the input side is not published -- there is no price -- and
        the model is not free either."""
        self.run_scrape()
        entry = self.models("stamped.json")["voxtral tts"]
        self.assertEqual(entry["per_mchars"], 16.0)
        self.assertNotIn("free", entry)

    def test_models_publish_the_identifiers_a_caller_passes(self) -> None:
        """The reason for reading the docs site rather than only the pricing page's card
        names: the pages state what to actually call, and the pricing page never did."""
        self.run_scrape()
        models = self.models("stamped.json")
        self.assertEqual(
            models["ocr 4.1"]["api_ids"], ["mistral-ocr-4-1", "mistral-ocr-4", "mistral-ocr-latest"]
        )
        self.assertEqual(models["mistral medium 3.5"]["api_ids"][0], "mistral-medium-3-5")

    def test_the_identifier_order_the_source_gives_is_kept(self) -> None:
        """Most specific first, moving alias last. Sorting them would destroy the only
        thing telling a consumer which one is safe to pin a price to."""
        self.run_scrape()
        ids = self.models("stamped.json")["mistral medium 3.5"]["api_ids"]
        self.assertEqual(ids, ["mistral-medium-3-5", "mistral-medium-3", "mistral-medium-latest"])

    def test_products_carry_no_identifier_because_there_is_nothing_to_call(self) -> None:
        self.run_scrape()
        self.assertNotIn("api_ids", self.models("stamped.json")["web search"])

    def test_a_model_whose_page_states_no_identifier_is_still_priced_and_reported(self) -> None:
        """A missing identifier is not a reason to withhold a correct price -- but a
        consumer has nothing to call, so it is worth a human's attention."""
        self.edit(self.docs_url("ocr-4-1"), '\\"names\\":', '\\"labels\\":', expected_count=None)
        code, _, _ = self.run_scrape()
        self.assertEqual(code, 0)
        entry = self.models()["ocr 4.1"]
        self.assertEqual(entry["per_1k_pages"], 4.0)
        self.assertNotIn("api_ids", entry)
        self.assertIn("no callable identifier", self.notes())

    def test_products_come_from_the_pricing_page(self) -> None:
        self.run_scrape()
        models = self.models("stamped.json")
        self.assertEqual(models["web search"]["per_1k_calls"], 30.0)
        self.assertEqual(models["web search"]["kind"], "product")

    def test_the_pricing_pages_own_model_cards_are_ignored(self) -> None:
        """That page still carries a card for every model, at prices of its own. Reading
        them too would give each model two sources of truth, and one of them would
        silently win -- which is how a model the pricing page had dropped came to be
        published as withdrawn while Mistral was still selling it.

        Moving the figure on the pricing page's own 'codestral' card must therefore
        change nothing: that model's price comes from the docs site."""
        page = self.path_for(PRICING_PAGE)
        text = page.read_text(encoding="utf-8")
        start, end = scrape_card_span(text, "codestral")
        card = text[start:end].replace("&quot;priceUsd&quot;:0.3", "&quot;priceUsd&quot;:9.9")
        self.assertNotEqual(card, text[start:end], "fixture drift: the codestral card no longer reads 0.3")
        page.write_text(text[:start] + card + text[end:], encoding="utf-8")

        code, stdout, _ = self.run_scrape()
        self.assertEqual(code, 0)
        self.assertIn("unchanged", stdout)
        self.assertEqual(self.models("stamped.json")["codestral"]["in_per_mtok"], 0.3)

    def test_the_libraries_ocr_row_is_not_the_ocr_models_price(self) -> None:
        """The decoy: 'libraries' carries a row labelled 'OCR (per 1K pages)' at $3,
        which is not the OCR model's own $4. They are now not even read from the same
        source, and they must still not be confused."""
        self.run_scrape()
        models = self.models("stamped.json")
        self.assertEqual(models["libraries"]["per_1k_pages"], 3.0)
        self.assertEqual(models["ocr 4.1"]["per_1k_pages"], 4.0)

    def test_every_current_model_on_the_index_reaches_the_output(self) -> None:
        """There is no list to reconcile: every model the index lists and prices is
        published. One dropping out between the source and the file is the failure mode
        this repository must not have."""
        self.run_scrape()
        published = set(self.models("stamped.json"))
        index = scrape.parse_index(self.path_for(DOCS_INDEX).read_text(encoding="utf-8"))
        # Shieldstral is on the index and states no price at all, so there is nothing
        # to publish for it; every other listed model is here.
        self.assertEqual({n.lower() for n in index.values()} - published, {"shieldstral 1.0"})

    def test_other_providers_blocks_are_never_touched(self) -> None:
        """The entire reason pricing.json nests under providers.<name>: a run that
        scrapes Mistral must carry any other provider's block through byte-for-byte,
        never read it, never rewrite it -- not even to reformat it."""
        doc = json.loads(self.current_bytes)
        doc["providers"]["acme"] = {
            "checked_utc": "2020-01-01T00:00:00Z",
            "updated": "2020-01-01",
            "source": "https://example.invalid",
            "currency": "JPY",
            "models": {"weird-model": {"in_per_mtok": 42.0, "display_name": "Weird"}},
        }
        self.current.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()

        self.run_scrape()

        providers = self.candidate("stamped.json")["providers"]
        self.assertEqual(providers["acme"], doc["providers"]["acme"])
        # Order matters too, not just value equality: a merge that rebuilt the providers
        # dict as "everyone else, then mistral" would leave every value equal while
        # turning ["mistral", "acme"] into ["acme", "mistral"], which shows up as the
        # whole acme block being removed-and-re-added in the diff a human reviews.
        self.assertEqual(list(providers), list(doc["providers"]))


# ======================================================================================
# Outcome 2: a figure changed
# ======================================================================================


class TestPriceChange(ScrapeTestCase):
    def test_a_model_price_change_produces_a_candidate_not_a_publication(self) -> None:
        self.edit(self.docs_url("mistral-medium-3-5-26-04"),
                  '\\"price\\":1.5', '\\"price\\":2.25')

        code, stdout, _ = self.run_scrape()

        self.assertEqual(code, 0)
        self.assertIn("changed", stdout)
        self.assertIn("mistral medium 3.5.in_per_mtok", stdout)

        updated = self.candidate_block("updated.json")
        self.assertEqual(updated["models"]["mistral medium 3.5"]["in_per_mtok"], 2.25)
        self.assertEqual(updated["checked_utc"], FIXED_NOW)
        self.assertEqual(updated["updated"], FIXED_NOW[:10], "updated must move with the figure")
        self.assertEqual(
            updated["models"]["mistral medium 3.5"]["out_per_mtok"], 7.5,
            "an unrelated figure was disturbed",
        )

        # The stamp is still produced: the keepalive commit happens on every run.
        stamped = self.candidate_block("stamped.json")
        self.assertEqual(stamped["models"], json.loads(self.current_bytes)["providers"]["mistral"]["models"])
        self.assert_current_untouched()

    def test_a_product_price_change_is_picked_up_too(self) -> None:
        """Both sources are live inputs; neither is a fallback for the other."""
        page = self.path_for(PRICING_PAGE).read_text(encoding="utf-8")
        start, end = scrape_card_span(page, "web search")
        card = page[start:end].replace("&quot;priceUsd&quot;:30", "&quot;priceUsd&quot;:45")
        self.path_for(PRICING_PAGE).write_text(page[:start] + card + page[end:], encoding="utf-8")

        code, stdout, _ = self.run_scrape()
        self.assertEqual(code, 0)
        self.assertIn("web search.per_1k_calls", stdout)
        self.assertEqual(self.models()["web search"]["per_1k_calls"], 45.0)
        self.assert_current_untouched()

    def test_a_price_going_down_is_also_a_change(self) -> None:
        self.edit(self.docs_url("ocr-4-1"), '\\"price\\":4,', '\\"price\\":2,')
        code, stdout, _ = self.run_scrape()
        self.assertEqual(code, 0)
        self.assertIn("ocr 4.1.per_1k_pages", stdout)
        self.assertEqual(self.models()["ocr 4.1"]["per_1k_pages"], 2.0)
        self.assert_current_untouched()

    def test_a_renamed_model_publishes_the_new_name_and_keeps_the_old(self) -> None:
        """The key is the name the source states, so a rename is two facts at once: a
        model exists under a new name, and nothing answers to the old one. Both are
        published -- the new key with today's price, the old one frozen and dated."""
        self.edit(self.docs_url("ocr-4-1"), 'OCR 4.1', 'OCR 4.2', expected_count=None)

        code, _, _ = self.run_scrape()
        self.assertEqual(code, 0)

        models = self.models()
        self.assertEqual(models["ocr 4.2"]["per_1k_pages"], 4.0)
        self.assertNotIn("absent_since", models["ocr 4.2"])
        self.assertEqual(models["ocr 4.1"]["absent_since"], FIXED_NOW[:10])
        self.assertEqual(models["ocr 4.1"]["per_1k_pages"], 4.0)
        self.assertIn("no longer offered", self.notes())
        self.assert_current_untouched()


# ======================================================================================
# A model that goes away
# ======================================================================================


class TestWithdrawnModel(ScrapeTestCase):
    """This is the case that used to fail the whole job, blocking correct prices for
    everything else until a human edited a list."""

    SLUG = "codestral-25-08"

    def test_it_is_kept_with_its_last_prices_and_a_date(self) -> None:
        self.withdraw_from_index(self.SLUG)
        code, _, _ = self.run_scrape()
        self.assertEqual(code, 0)

        entry = self.models()["codestral"]
        published = json.loads(self.current_bytes)["providers"]["mistral"]["models"]["codestral"]
        self.assertEqual(entry["absent_since"], FIXED_NOW[:10])
        self.assertEqual(entry["in_per_mtok"], published["in_per_mtok"])
        self.assertEqual(entry["out_per_mtok"], published["out_per_mtok"])

    def test_a_model_marked_retired_is_treated_the_same_way(self) -> None:
        """Still linked from the index, but the page itself says it is retired. A price
        for something Mistral has withdrawn is not a current price."""
        self.edit(self.docs_url(self.SLUG), '\\"isRetired\\":false', '\\"isRetired\\":true',
                  expected_count=None)
        code, _, _ = self.run_scrape()
        self.assertEqual(code, 0)
        self.assertEqual(self.models()["codestral"]["absent_since"], FIXED_NOW[:10])

    def test_every_other_model_is_still_published(self) -> None:
        """The point of the whole design: one model going away is not a reason to
        withhold every other price."""
        self.withdraw_from_index(self.SLUG)
        self.run_scrape()
        models = self.models()
        still_offered = [m for m in models if "absent_since" not in models[m]]
        self.assertEqual(len(still_offered), 26)
        self.assertEqual(models["mistral medium 3.5"]["in_per_mtok"], 1.5)

    def test_it_is_reported_once_and_not_again(self) -> None:
        """The run that first sees it gone says so. A run a week later, with the stamp
        already in place, says nothing: a channel that repeats itself gets muted, and
        this one has to still work the day something real happens."""
        self.withdraw_from_index(self.SLUG)
        self.run_scrape()
        self.assertIn("codestral", self.notes())
        self.assertIn("365 days", self.notes())

        self.current.write_text(
            (self.out_dir / "updated.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.current_bytes = self.current.read_bytes()
        code, stdout, _ = self.run_scrape(now="2026-08-25T04:00:00Z")

        self.assertEqual(code, 0)
        self.assertIn("unchanged", stdout)
        self.assertEqual(self.notes(), "", "the same disappearance was reported twice")

    def test_it_is_dropped_after_a_year_and_not_before(self) -> None:
        self.withdraw_from_index(self.SLUG)
        self.run_scrape()
        aged = json.loads((self.out_dir / "updated.json").read_text(encoding="utf-8"))
        aged["providers"]["mistral"]["models"]["codestral"]["absent_since"] = "2025-08-18"
        self.current.write_text(json.dumps(aged, indent=2) + "\n", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()

        self.run_scrape(now="2026-08-17T04:00:00Z")
        self.assertIn("codestral", self.models("stamped.json"))

        code, _, _ = self.run_scrape(now="2026-08-19T04:00:00Z")
        self.assertEqual(code, 0)
        self.assertNotIn("codestral", self.models())
        self.assertIn("Dropped from the file", self.notes())

    def test_a_model_that_comes_back_loses_the_marker(self) -> None:
        self.withdraw_from_index(self.SLUG)
        self.run_scrape()
        self.current.write_text(
            (self.out_dir / "updated.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.current_bytes = self.current.read_bytes()

        # Restore the index, then run again.
        shutil.copy2(FIXTURES / "docs_index.html", self.path_for(DOCS_INDEX))
        code, _, _ = self.run_scrape(now="2026-08-25T04:00:00Z")

        self.assertEqual(code, 0)
        entry = self.models()["codestral"]
        self.assertNotIn("absent_since", entry)
        self.assertEqual(entry["in_per_mtok"], 0.3)
        self.assertIn("offered again", self.notes())


# ======================================================================================
# Outcome 3: fetch or parse failed
# ======================================================================================


class TestLayoutFailures(ScrapeTestCase):
    def test_docs_index_without_model_cards(self) -> None:
        self.path_for(DOCS_INDEX).write_text("<html><body>nothing</body></html>", encoding="utf-8")
        code, _, stderr = self.run_scrape()
        self.assert_failed(code, stderr, "no model card found on the docs index")

    def test_a_docs_page_without_a_model_name(self) -> None:
        self.edit(self.docs_url("ocr-4-1"), '\\"currentModelName\\"', '\\"someOtherKey\\"')
        code, _, stderr = self.run_scrape()
        self.assert_failed(code, stderr, "states no currentModelName")

    def test_a_source_the_manifest_does_not_serve_is_never_fetched(self) -> None:
        """An offline run that silently reached the network would pass for the wrong
        reason, and would make the tests depend on a live page."""
        self.edit(DOCS_INDEX, 'href="/models/ocr-4-1"', 'href="/models/ocr-9-9"')
        code, _, stderr = self.run_scrape()
        self.assert_failed(code, stderr, "which no fixture serves")

    def test_pricing_page_whose_layout_no_longer_parses(self) -> None:
        self.edit(PRICING_PAGE, 'class="model-item', 'class="product-tile', expected_count=None)
        code, _, stderr = self.run_scrape()
        self.assert_failed(code, stderr, "no model card found")

    def test_pricing_page_price_markup_replaced_by_plain_text(self) -> None:
        self.edit(PRICING_PAGE, " data-prices=", " data-figures=", expected_count=None)
        code, _, stderr = self.run_scrape()
        self.assert_failed(code, stderr, "without data-prices")

    def test_pricing_page_unparseable_price_json(self) -> None:
        self.edit(
            PRICING_PAGE,
            "&quot;priceEur&quot;:1.25,&quot;priceUsd&quot;:1.5",
            "&quot;priceEur&quot;:1.25 &quot;priceUsd&quot;:1.5",
        )
        code, _, stderr = self.run_scrape()
        self.assert_failed(code, stderr, "not JSON")


class TestUnreadableFigures(ScrapeTestCase):
    """Things this file will not guess at. None of them stop the other prices."""

    def test_an_unknown_denominator_is_reported_and_never_guessed_at(self) -> None:
        """There is no honest field for a figure whose unit this repository cannot name,
        so it is not published -- but a unit changing on one model says nothing about
        whether the others were read correctly."""
        self.edit(self.docs_url("ocr-4-1"), '/1000 Pages\\"', '/100 Pages\\"')
        code, _, _ = self.run_scrape()
        self.assertEqual(code, 0)

        models = self.models()
        self.assertIn("/100 Pages", self.notes())
        self.assertIn("has no field for", self.notes())
        self.assertNotIn("per_1k_pages", models["ocr 4.1"])
        self.assertEqual(models["ocr 4.1"]["per_1k_annotated_pages"], 5.0)
        self.assertEqual(models["mistral medium 3.5"]["in_per_mtok"], 1.5)

    def test_a_model_whose_every_figure_is_unreadable_goes_absent(self) -> None:
        self.edit(self.docs_url("ocr-4-1"), '/1000 ', '/100 ', expected_count=None)
        code, _, _ = self.run_scrape()
        self.assertEqual(code, 0)
        self.assertIn("no price on this page could be published", self.notes())
        self.assertEqual(self.models()["ocr 4.1"]["absent_since"], FIXED_NOW[:10])

    def test_an_unrecognised_product_row_label_is_reported(self) -> None:
        self.edit(PRICING_PAGE, "Price (per 1K calls)", "Cost (per 1K calls)", expected_count=None)
        code, _, _ = self.run_scrape()
        self.assertEqual(code, 0)
        self.assertIn("Cost (per 1K calls)", self.notes())
        self.assertIn("has no field for that label", self.notes())
        self.assertEqual(self.models()["web search"]["absent_since"], FIXED_NOW[:10])


class TestSanityBounds(ScrapeTestCase):
    def test_out_of_bounds_figure_is_refused(self) -> None:
        self.edit(self.docs_url("mistral-medium-3-5-26-04"), '\\"price\\":1.5', '\\"price\\":4000')
        code, _, stderr = self.run_scrape()
        self.assert_failed(code, stderr, "outside the plausible range")

    def test_plausible_figure_that_moved_implausibly_is_refused(self) -> None:
        """20 is a perfectly ordinary price. Going 1.5 -> 20 in one week is not."""
        self.edit(self.docs_url("mistral-medium-3-5-26-04"), '\\"price\\":1.5', '\\"price\\":20')
        code, _, stderr = self.run_scrape()
        self.assert_failed(code, stderr, "factor of", "Refusing to publish")

    def test_a_change_just_under_the_factor_limit_is_allowed_through(self) -> None:
        self.edit(self.docs_url("mistral-medium-3-5-26-04"), '\\"price\\":1.5', '\\"price\\":6')
        code, stdout, _ = self.run_scrape()
        self.assertEqual(code, 0)
        self.assertIn("changed", stdout)
        self.assert_current_untouched()

    def test_a_key_claimed_by_both_sources_is_refused(self) -> None:
        """One key cannot be two things, and nothing here can tell which source is
        right, so the run stops rather than letting one silently win.

        Reaching this needs the mapping to call a card that is also a model a product,
        which is why it is set up by hand: the committed mapping does not, and a test
        that could not construct the collision would not be testing the guard."""
        # Renamed to a docs model the pricing page has no card of its own for, so that
        # the collision under test is the cross-source one and not a duplicate card.
        self.edit(PRICING_PAGE, 'data-name="libraries"', 'data-name="ocr 4.0"')

        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
        mapping["products"] = ["ocr 4.0"]
        patched = self.tmp / "mapping.json"
        patched.write_text(json.dumps(mapping), encoding="utf-8")

        argv = [
            "--out-dir", str(self.out_dir), "--current", str(self.current),
            "--mapping", str(patched), "--offline", str(self.sources / "offline.json"),
            "--now", FIXED_NOW,
        ]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scrape.main(argv)
        self.assert_failed(code, err.getvalue(), "both as a model", "as a product")


class TestCorruptInputs(ScrapeTestCase):
    def test_invalid_committed_pricing_json_stops_everything(self) -> None:
        self.current.write_text(
            json.dumps({"schema_version": validate.SCHEMA_VERSION, "providers": {}}), encoding="utf-8"
        )
        self.current_bytes = self.current.read_bytes()
        code, _, stderr = self.run_scrape()
        self.assert_failed(code, stderr, "committed pricing.json is invalid")

    def test_unparseable_committed_pricing_json_stops_everything(self) -> None:
        self.current.write_text("{ not json", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()
        code, _, stderr = self.run_scrape()
        self.assert_failed(code, stderr, "not valid JSON")

    def test_a_missing_mistral_block_stops_everything(self) -> None:
        """This job updates an existing provider's figures; it does not decide, on its
        own, to start publishing a provider that was never seeded by a human."""
        doc = json.loads(self.current_bytes)
        del doc["providers"]["mistral"]
        doc["providers"]["placeholder"] = {
            "checked_utc": "2026-08-18T04:00:00Z",
            "updated": "2026-08-18",
            "source": "https://example.invalid",
            "currency": "USD",
            "models": {"x": {"in_per_mtok": 1.0, "display_name": "X"}},
        }
        self.current.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()

        code, _, stderr = self.run_scrape()
        self.assert_failed(code, stderr, "no providers.mistral block")

    def test_a_published_model_neither_source_shows_is_kept_not_dropped(self) -> None:
        """Something in the file that neither source has heard of is not deleted on
        sight. It is dated and kept for a year, like anything else that goes away."""
        published = json.loads(self.current_bytes)
        published["providers"]["mistral"]["models"]["mistral retired legacy"] = {
            "in_per_mtok": 0.5,
            "out_per_mtok": 1.5,
            "display_name": "mistral retired legacy",
        }
        self.current.write_text(json.dumps(published, indent=2) + "\n", encoding="utf-8")
        self.current_bytes = self.current.read_bytes()

        code, _, _ = self.run_scrape()
        self.assertEqual(code, 0)

        entry = self.models()["mistral retired legacy"]
        self.assertEqual(entry["absent_since"], FIXED_NOW[:10])
        self.assertEqual(entry["in_per_mtok"], 0.5)
        self.assert_current_untouched()


# ======================================================================================
# Cross-file consistency: does providers.mistral agree with Mistral's own files?
# ======================================================================================


class TestPublishedFileMatchesMistral(unittest.TestCase):
    maxDiff = None

    def base(self) -> JSONDict:
        return json.loads(PRICING_JSON.read_text(encoding="utf-8"))

    def mistral_block(self) -> JSONDict:
        return self.base()["providers"]["mistral"]

    def test_the_committed_file_is_valid(self) -> None:
        validate.validate_document(self.base())

    def test_the_fixture_baseline_is_valid(self) -> None:
        validate.validate_document(json.loads(BASELINE_JSON.read_text(encoding="utf-8")))

    def test_the_baseline_has_the_same_shape_as_the_published_file(self) -> None:
        """Figures may differ -- the baseline is pinned to its own capture -- but the set
        of entries still on sale, and the units they are priced in, may not drift apart
        unnoticed. Entries marked `absent_since` are excluded on both sides: the
        published file keeps a withdrawn model for a year and the fixtures, being a
        single capture, have no way to know about one."""
        published = {k: v for k, v in self.mistral_block()["models"].items() if "absent_since" not in v}
        baseline = json.loads(BASELINE_JSON.read_text(encoding="utf-8"))["providers"]["mistral"]["models"]
        baseline = {k: v for k, v in baseline.items() if "absent_since" not in v}

        self.assertEqual(set(published), set(baseline))
        for model_id, entry in published.items():
            self.assertEqual(
                {k for k in entry if k in validate.KNOWN_PRICE_FIELDS},
                {k for k in baseline[model_id] if k in validate.KNOWN_PRICE_FIELDS},
                model_id,
            )

    def test_the_committed_file_matches_the_mapping(self) -> None:
        """There is no model list to agree with, so what is checked is what the mapping
        still decides: where the figures come from, what currency they are in, and which
        price fields its two translation tables can produce."""
        published = self.mistral_block()
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))

        self.assertEqual(published["source"], mapping["source"])
        self.assertEqual(published["currency"], mapping["currency"])

        producible = set(mapping["labels"].values())
        for side in mapping["denominators"].values():
            producible |= set(side.values())
        producible |= {spec["field"] for spec in mapping["rows"].values()}

        for model_id, entry in published["models"].items():
            fields = {k for k in entry if k in validate.KNOWN_PRICE_FIELDS}
            if entry.get("free") is True:
                self.assertEqual(fields, set(), f"{model_id} is free and priced at once")
            else:
                self.assertTrue(fields, model_id)
                self.assertLessEqual(fields, producible, model_id)

    def test_only_products_carry_a_kind(self) -> None:
        mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
        products = set(mapping["products"])
        for model_id, entry in self.mistral_block()["models"].items():
            if "kind" in entry:
                self.assertEqual(entry["kind"], "product", model_id)
                self.assertIn(model_id, products, model_id)

    def test_every_key_is_a_name_a_source_states(self) -> None:
        """The keys are names the sources state, deliberately not API model ids. An
        API-shaped key creeping back in would mean somebody had started translating
        names by hand again, which is the thing that made this block need a human every
        week."""
        for model_id, entry in self.mistral_block()["models"].items():
            self.assertEqual(model_id, entry["display_name"], model_id)
            self.assertEqual(model_id, model_id.lower(), model_id)

    def test_the_published_figures_have_room_to_fall(self) -> None:
        """Guards the floor against the figures actually published: if a price ever sits
        too close to it, a real price cut starts failing the job instead of being
        published. Caught here rather than on the Monday it happens."""
        for model_id, entry in self.mistral_block()["models"].items():
            for field, value in entry.items():
                if field not in validate.KNOWN_PRICE_FIELDS:
                    continue
                low, _ = validate.PRICE_BOUNDS.get(
                    field, (validate.MIN_PLAUSIBLE, validate.MAX_PLAUSIBLE)
                )
                self.assertGreaterEqual(
                    value / low,
                    validate.MAX_CHANGE_FACTOR,
                    f"{model_id}.{field} = {value} sits less than a factor of "
                    f"{validate.MAX_CHANGE_FACTOR} above its floor of {low}; a legitimate "
                    f"price cut would be refused as out of bounds. Lower the floor for "
                    f"this unit in pricing_validate.PRICE_BOUNDS.",
                )


# ======================================================================================
# The mapping is a document a human maintains, so check it stays legible
# ======================================================================================


class TestMapping(unittest.TestCase):
    def mapping(self) -> JSONDict:
        return json.loads(MAPPING_JSON.read_text(encoding="utf-8"))

    def test_there_is_no_model_list(self) -> None:
        """Load-bearing, not pedantry. A hand-written model list is exactly what made a
        week of renames block twenty-two correct prices, and re-adding one would bring
        that back without anything else in the tests noticing."""
        self.assertNotIn("models", self.mapping())

    def test_the_two_sources_are_distinct(self) -> None:
        mapping = self.mapping()
        self.assertNotEqual(mapping["source"], mapping["products_source"])

    def test_both_sources_are_mistrals_own(self) -> None:
        """This block must be built from Mistral and from nothing else. A reseller can
        confirm that a string is a real Mistral id, since it sells against that id, but
        never that Mistral still offers it: resellers lag a retirement by however long
        their own catalogue takes to notice, and can hold a contract that keeps a model
        callable through them for months after it has left the public API. Taking either
        the inventory or the ids from one would date a withdrawal late, or never."""
        mapping = self.mapping()
        for key in ("source", "products_source"):
            host = urlsplit(mapping[key]).netloc
            self.assertTrue(
                host == "mistral.ai" or host.endswith(".mistral.ai"),
                f"{key} points at {host!r}, which is not Mistral's own site.",
            )

    def test_the_offline_manifest_serves_only_mistral(self) -> None:
        """Makes the rule above structural rather than a promise. An offline run refuses
        any URL the manifest does not name, so a scraper that started reading somebody
        else's catalogue fails here instead of quietly publishing from it."""
        manifest = json.loads((FIXTURES / "offline.json").read_text(encoding="utf-8"))
        for url in manifest:
            host = urlsplit(url).netloc
            self.assertTrue(
                host == "mistral.ai" or host.endswith(".mistral.ai"),
                f"the manifest serves {url}, which is not Mistral's own site.",
            )

    def test_every_mapped_denominator_names_a_known_price_field(self) -> None:
        for side, table in self.mapping()["denominators"].items():
            for denominator, field in table.items():
                self.assertIn(field, validate.KNOWN_PRICE_FIELDS, f"{side} {denominator}")

    def test_no_two_denominators_share_a_field_on_one_side(self) -> None:
        """Two units mapping to one field on the same side would make an entry
        order-dependent, and one of the two figures would silently win."""
        for side, table in self.mapping()["denominators"].items():
            fields = list(table.values())
            self.assertEqual(len(fields), len(set(fields)), side)

    def test_every_mapped_row_names_a_known_price_field(self) -> None:
        for label, spec in self.mapping()["rows"].items():
            self.assertIn(spec["field"], validate.KNOWN_PRICE_FIELDS, label)

    def test_every_denominator_the_docs_state_is_mapped(self) -> None:
        """Catches the gap on the day the fixtures are re-captured rather than on the
        Monday a price silently stops being published."""
        mapping = self.mapping()
        manifest = json.loads((FIXTURES / "offline.json").read_text(encoding="utf-8"))
        known = {d for table in mapping["denominators"].values() for d in table}

        for url, rel in manifest.items():
            if not url.startswith("https://docs.mistral.ai/models/"):
                continue
            _, _, pricing, _ = scrape.parse_model_page(
                (FIXTURES / rel).read_text(encoding="utf-8", errors="replace"), url
            )
            if not pricing:
                continue
            rows = (pricing.get("input") or []) + (pricing.get("output") or [])
            if not rows and "denominator" in pricing:
                rows = [pricing]
            for row in rows:
                if row.get("price") == 0:
                    continue
                self.assertIn(row.get("denominator"), known, f"{url}: {row}")

    def test_every_product_row_label_on_the_page_is_mapped(self) -> None:
        mapping = self.mapping()
        cards = scrape.parse_page((FIXTURES / "page_ok.html").read_text(encoding="utf-8"))
        for card_name in mapping["products"]:
            for label, _ in cards.get(card_name, []):
                self.assertIsNotNone(
                    scrape.resolve_field(label, mapping["rows"]), f"{card_name}: {label!r}"
                )

    def test_products_are_card_names_that_exist_on_the_pricing_page(self) -> None:
        """The list gates which cards are read, so a stale name in it silently stops
        publishing that product."""
        cards = set(scrape.parse_page((FIXTURES / "page_ok.html").read_text(encoding="utf-8")))
        self.assertLessEqual(set(self.mapping()["products"]), cards)


def scrape_card_span(source: str, data_name: str) -> tuple[int, int]:
    """The exact character range one pricing-page model card occupies."""
    anchor = source.index(f'data-name="{data_name}"')
    start = source.rindex('<div class="model-item', 0, anchor)
    end = source.index("</mistral-block-card-model>", start) + len("</mistral-block-card-model>")
    return start, source.index("</div>", end) + len("</div>")


if __name__ == "__main__":
    unittest.main()
