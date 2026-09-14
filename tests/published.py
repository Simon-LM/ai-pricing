"""Which file counts as "the published one", and what its blocks may carry.

Every provider's test file asks pricing.json the same question: are the price fields
actually published ones this provider's mapping could have produced? The answer has
three branches, and until 2026-09-14 four separate copies of it had two.

That day the Hugging Face block gained its first `unpriced_since` entries -- routes
the router sells but declines to price -- and the copy in test_huggingface.py knew
only "free" and "priced", so it called them a bug. main went red on a file that was
perfectly valid, and the next weekly refresh would have opened a failure issue and
published nothing. The three other copies were one unpriced model away from exactly
the same week-long stall, in a provider each.

So the rule lives here now, once. A fifth provider gets it by calling this, and a
fourth state -- if there is ever one -- is added in a single place rather than in
four that drift.

The same day exposed a second gap, and `published_file()` closes it. Every test that
checks "the published file" used to hardcode the committed pricing.json -- the file
as it was *before* the scrape. What a refresh actually commits is a candidate built
after it, and nothing ever tested that. A refresh now points AI_PRICING_FILE at the
candidate and runs these same tests against it, so the question asked is about the
bytes that are about to be published rather than the ones being replaced.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import pricing_validate as validate  # noqa: E402
from pricing_validate import JSONDict  # noqa: E402


# A refresh sets this to the candidate it is about to promote. Unset -- which is
# every local run and every CI run on a push -- it means the committed file.
PUBLISHED_FILE_ENV = "AI_PRICING_FILE"


def published_file() -> Path:
    """The pricing.json under test: the committed one, or a candidate to be published.

    Read at import time by each test module, so a refresh exports the variable for
    the whole `python3 -m unittest` process rather than per test.
    """
    override = os.environ.get(PUBLISHED_FILE_ENV)
    if not override:
        return REPO_ROOT / "pricing.json"
    path = Path(override)
    if not path.is_file():
        raise FileNotFoundError(
            f"{PUBLISHED_FILE_ENV}={override} does not name a file. A refresh sets this "
            f"to the candidate it is about to publish; an empty or wrong path here would "
            f"otherwise silently test the committed file instead and prove nothing."
        )
    return path


def assert_price_fields_are_producible(
    case: unittest.TestCase, models: JSONDict, producible: set[str]
) -> None:
    """Check every entry of a published block against the mapping that produced it.

    An entry is in exactly one of three states, and `producible` is the set of price
    fields this provider's mapping can emit:

    `free`             the source gives it away, so it carries no price at all.
    `unpriced_since`   the source still sells it but will not say what it costs.
                       Any prices left on it are the last ones actually observed, so
                       they must still be producible -- but there may be none, and
                       for a model that was never priced there will be none.
    otherwise          it is on sale at a stated price, so it carries at least one.

    In all three states a field the mapping could not have produced means the block
    and the mapping have drifted apart. That is the thing worth catching, and it is
    checked before the branch so it holds in every state.
    """
    for model_id, entry in models.items():
        fields = {k for k in entry if k in validate.KNOWN_PRICE_FIELDS}
        with case.subTest(model=model_id):
            case.assertLessEqual(
                fields,
                producible,
                f"{model_id} carries a price field this mapping cannot produce",
            )
            if entry.get("free") is True:
                case.assertEqual(
                    fields, set(), f"{model_id} is free and priced at once"
                )
            elif "unpriced_since" not in entry:
                case.assertTrue(
                    fields, f"{model_id} is neither free, nor unpriced, nor priced"
                )
