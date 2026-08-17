"""Tests for pricing_validate.py in isolation, independent of any one provider.

Standard library only: `python3 -m unittest discover tests`.

Deliberately synthetic: every document here is built by hand, never read from
pricing.json or a provider's own fixtures. This file exercises the *contract*
(schema_version, the providers.<name> nesting, the shared units and bounds), so it
must keep working unchanged as more providers are added -- it should never need to
know any provider's specific model ids.

Provider-specific tests -- does providers.mistral actually match Mistral's own
mapping.json, does providers.ovh match OVH's -- live in tests/scraper_tests/.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import pricing_validate as validate  # noqa: E402
from pricing_validate import JSONDict  # noqa: E402


def minimal_provider_block(**overrides: object) -> JSONDict:
    """A providers.<name> block that validate_document will accept as-is."""
    block: JSONDict = {
        "checked_utc": "2026-08-03T04:00:00Z",
        "updated": "2026-08-03",
        "source": "https://example.invalid/pricing",
        "currency": "USD",
        "models": {
            "some-model": {"in_per_mtok": 1.0, "out_per_mtok": 2.0, "display_name": "Some Model"},
        },
    }
    block.update(overrides)
    return block


def minimal_document(**providers: JSONDict) -> JSONDict:
    """A whole pricing.json-shaped document, one or more providers."""
    return {
        "schema_version": validate.SCHEMA_VERSION,
        "providers": providers or {"acme": minimal_provider_block()},
    }


class TestPriceBounds(unittest.TestCase):
    def test_bounds(self) -> None:
        for bad in (0, -1, 1000.01, 0.0001, float("nan"), float("inf")):
            with self.subTest(value=bad), self.assertRaises(validate.ValidationError):
                validate.check_price("m", "in_per_mtok", bad)
        for good in (0.001, 1.5, 1000.0):
            with self.subTest(value=good):
                validate.check_price("m", "in_per_mtok", good)

    def test_audio_prices_get_a_lower_floor_than_token_prices(self) -> None:
        """A minute of audio costs thousandths of a dollar. Under the global floor of
        0.001 an ordinary price cut would be refused as though the page had broken."""
        validate.check_price("m", "per_audio_minute", 0.0005)
        with self.assertRaises(validate.ValidationError):
            validate.check_price("m", "in_per_mtok", 0.0005)

        # The ceiling is not relaxed: letting a misparse through is the failure that matters.
        with self.assertRaises(validate.ValidationError):
            validate.check_price("m", "per_audio_minute", 1000.01)

    def test_a_boolean_is_not_a_price(self) -> None:
        with self.assertRaises(validate.ValidationError):
            validate.check_price("m", "in_per_mtok", True)

    def test_change_factor(self) -> None:
        validate.check_change("m", "in_per_mtok", 1.5, 7.5)  # exactly x5
        validate.check_change("m", "in_per_mtok", 7.5, 1.5)  # exactly /5
        with self.assertRaises(validate.ValidationError):
            validate.check_change("m", "in_per_mtok", 1.5, 7.51)
        with self.assertRaises(validate.ValidationError):
            validate.check_change("m", "in_per_mtok", 7.51, 1.5)


class TestDocumentShape(unittest.TestCase):
    def test_a_minimal_document_is_valid(self) -> None:
        validate.validate_document(minimal_document())

    def test_unknown_schema_version_is_refused(self) -> None:
        doc = minimal_document()
        doc["schema_version"] = validate.SCHEMA_VERSION - 1
        with self.assertRaises(validate.ValidationError):
            validate.validate_document(doc)

    def test_empty_providers_is_refused(self) -> None:
        with self.assertRaises(validate.ValidationError):
            validate.validate_document({"schema_version": validate.SCHEMA_VERSION, "providers": {}})

    def test_a_non_object_document_is_refused(self) -> None:
        bad_documents: tuple[object, ...] = (None, [], "pricing.json", 42)
        for bad in bad_documents:
            with self.subTest(value=bad), self.assertRaises(validate.ValidationError):
                validate.validate_document(bad)

    def test_generic_price_field_is_forbidden(self) -> None:
        doc = minimal_document(acme=minimal_provider_block())
        doc["providers"]["acme"]["models"]["some-model"]["price"] = 1.5
        with self.assertRaises(validate.ValidationError):
            validate.validate_document(doc)

    def test_a_model_with_no_price_field_is_refused(self) -> None:
        doc = minimal_document(acme=minimal_provider_block())
        doc["providers"]["acme"]["models"]["some-model"] = {"display_name": "Some Model"}
        with self.assertRaises(validate.ValidationError):
            validate.validate_document(doc)

    def test_a_model_needs_a_display_name(self) -> None:
        doc = minimal_document(acme=minimal_provider_block())
        del doc["providers"]["acme"]["models"]["some-model"]["display_name"]
        with self.assertRaises(validate.ValidationError):
            validate.validate_document(doc)

    def test_date_formats(self) -> None:
        doc = minimal_document(acme=minimal_provider_block(checked_utc="2026-07-30 04:00:00"))
        with self.assertRaises(validate.ValidationError):
            validate.validate_document(doc)

        doc = minimal_document(acme=minimal_provider_block(updated="30/07/2026"))
        with self.assertRaises(validate.ValidationError):
            validate.validate_document(doc)

    def test_a_provider_block_needs_its_own_metadata(self) -> None:
        for missing in ("checked_utc", "updated", "source", "currency"):
            block = minimal_provider_block()
            del block[missing]
            with self.subTest(missing=missing), self.assertRaises(validate.ValidationError):
                validate.validate_document(minimal_document(acme=block))


class TestFreeModels(unittest.TestCase):
    """A provider that gives a model away says so with `"free": true` and no price.

    The alternative -- publishing 0 -- cannot be told apart from what a parser reads
    off a page whose layout moved, which is the single failure `check_price`'s floor
    exists to catch. Keeping "free" out of the number space is what lets that floor
    stay strict.
    """

    def test_a_free_model_needs_no_price_field(self) -> None:
        doc = minimal_document(
            acme=minimal_provider_block(models={"gratis": {"free": True, "display_name": "Gratis"}})
        )
        validate.validate_document(doc)  # must not raise

    def test_free_and_a_price_together_is_refused(self) -> None:
        doc = minimal_document(
            acme=minimal_provider_block(
                models={"gratis": {"free": True, "in_per_mtok": 1.0, "display_name": "Gratis"}}
            )
        )
        with self.assertRaises(validate.ValidationError) as ctx:
            validate.validate_document(doc)
        self.assertIn("marked free", str(ctx.exception))

    def test_free_may_only_be_literal_true(self) -> None:
        """`"free": false` reads as "this model is not free", which is a statement the
        field cannot make -- a model that is not free carries a price instead."""
        for bad in (False, 0, None, "yes", 1):
            doc = minimal_document(
                acme=minimal_provider_block(
                    models={"gratis": {"free": bad, "in_per_mtok": 1.0, "display_name": "Gratis"}}
                )
            )
            with self.subTest(value=bad), self.assertRaises(validate.ValidationError):
                validate.validate_document(doc)

    def test_a_price_of_zero_is_still_refused(self) -> None:
        """The whole point of the marker: 0 remains an invalid price, free or not."""
        doc = minimal_document(
            acme=minimal_provider_block(models={"gratis": {"in_per_mtok": 0, "display_name": "Gratis"}})
        )
        with self.assertRaises(validate.ValidationError):
            validate.validate_document(doc)

    def test_a_free_model_renders_as_free_in_a_diff(self) -> None:
        old = {"m": {"in_per_mtok": 1.0, "display_name": "M"}}
        new = {"m": {"free": True, "display_name": "M"}}
        lines = validate.diff_models(old, new)
        self.assertTrue(lines, "a model becoming free is a change a reviewer must see")


class TestEntryKind(unittest.TestCase):
    """Mistral's page bills web search and image generation beside its models. They
    are published because a consumer estimating a bill needs them, and marked so that
    a consumer iterating `models` does not treat them as callable model ids."""

    def test_a_product_is_accepted(self) -> None:
        doc = minimal_document(
            acme=minimal_provider_block(
                models={"web-search": {"per_1k_calls": 30.0, "display_name": "Web Search", "kind": "product"}}
            )
        )
        validate.validate_document(doc)

    def test_an_unknown_kind_is_refused(self) -> None:
        doc = minimal_document(
            acme=minimal_provider_block(
                models={"thing": {"per_1k_calls": 30.0, "display_name": "Thing", "kind": "service"}}
            )
        )
        with self.assertRaises(validate.ValidationError) as ctx:
            validate.validate_document(doc)
        self.assertIn("kind", str(ctx.exception))

    def test_kind_is_optional_and_absent_means_model(self) -> None:
        doc = minimal_document()
        validate.validate_document(doc)
        self.assertNotIn("kind", doc["providers"]["acme"]["models"]["some-model"])


class TestAbsentSince(unittest.TestCase):
    """The marker that says: these prices are the last ones observed, not today's."""

    def entry(self, **overrides: object) -> JSONDict:
        entry: JSONDict = {"in_per_mtok": 1.0, "display_name": "Some Model"}
        entry.update(overrides)
        return entry

    def document(self, **overrides: object) -> JSONDict:
        return minimal_document(
            acme=minimal_provider_block(models={"some-model": self.entry(**overrides)})
        )

    def test_a_plain_day_is_accepted(self) -> None:
        validate.validate_document(self.document(absent_since="2026-08-17"))

    def test_absence_is_not_a_reason_to_drop_the_prices(self) -> None:
        """The whole point of the field: the entry stays priced. A consumer that reads
        the price without reading the date gets a stale figure, which is recoverable;
        one that finds nothing at all does not."""
        doc = self.document(absent_since="2026-08-17")
        validate.validate_document(doc)
        self.assertEqual(doc["providers"]["acme"]["models"]["some-model"]["in_per_mtok"], 1.0)

    def test_anything_that_is_not_a_day_is_refused(self) -> None:
        for bad in ("2026-08-17T04:00:00Z", "17/08/2026", "2026-8-17", "", True, 20260817, None):
            with self.subTest(value=bad), self.assertRaises(validate.ValidationError):
                validate.validate_document(self.document(absent_since=bad))

    def test_it_is_absent_by_default(self) -> None:
        """Most entries are on sale, and the common case stays unannotated -- the same
        rule `kind` follows."""
        doc = self.document()
        validate.validate_document(doc)
        self.assertNotIn("absent_since", doc["providers"]["acme"]["models"]["some-model"])

    def test_a_free_model_may_also_go_absent(self) -> None:
        doc = minimal_document(
            acme=minimal_provider_block(
                models={"gift": {"free": True, "display_name": "Gift", "absent_since": "2026-08-17"}}
            )
        )
        validate.validate_document(doc)

    def test_the_retention_window_is_a_year(self) -> None:
        """Stated here rather than only in the runner, because it is the promise the
        published file makes to whoever reads it."""
        self.assertEqual(validate.ABSENT_RETENTION_DAYS, 365)


class TestMultiProviderIsolation(unittest.TestCase):
    """The reason pricing.json nests under providers.<name> at all: two providers
    can diverge in currency and schedule, and a break in one must not hide in --
    or leak into -- the other."""

    def test_a_second_providers_block_is_still_validated(self) -> None:
        """A corruption in providers.ovh must be caught even on a run that only
        ever touches providers.mistral -- the whole document is checked, not just
        whichever block the caller happens to be updating."""
        doc = minimal_document(
            mistral=minimal_provider_block(),
            ovh=minimal_provider_block(currency=""),  # empty currency: invalid
        )
        with self.assertRaises(validate.ValidationError) as ctx:
            validate.validate_document(doc)
        self.assertIn("providers.ovh", str(ctx.exception))

    def test_two_providers_can_use_different_currencies(self) -> None:
        doc = minimal_document(
            mistral=minimal_provider_block(currency="USD"),
            ovh=minimal_provider_block(currency="EUR"),
        )
        validate.validate_document(doc)  # must not raise

    def test_the_same_model_id_at_two_providers_is_not_a_collision(self) -> None:
        """The whole point of nesting: 'small-model' from acme and 'small-model'
        from acme-two are unrelated prices, not a duplicate key."""
        doc = minimal_document(
            acme=minimal_provider_block(),
            acme_two=minimal_provider_block(
                models={"some-model": {"in_per_mtok": 99.0, "out_per_mtok": 100.0, "display_name": "Unrelated"}}
            ),
        )
        validate.validate_document(doc)  # must not raise despite the shared key


class TestDiff(unittest.TestCase):
    def test_diff_reports_additions_and_removals(self) -> None:
        old = {"a": {"in_per_mtok": 1.0, "display_name": "A"}}
        new = {"b": {"in_per_mtok": 2.0, "display_name": "B"}}
        lines = validate.diff_models(old, new)
        self.assertTrue(any(line.startswith("- a") for line in lines))
        self.assertTrue(any(line.startswith("+ b") for line in lines))

    def test_diff_is_empty_for_identical_models(self) -> None:
        models = minimal_provider_block()["models"]
        self.assertEqual(validate.diff_models(models, json.loads(json.dumps(models))), [])


if __name__ == "__main__":
    unittest.main()
