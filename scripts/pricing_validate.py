"""Sanity checks applied to every scraped figure before it may be published.

The premise of these checks is that this repository reads a marketing page that
nobody promised to keep stable. A figure that lands far outside a plausible range,
or that jumps by an implausible factor, almost never means "the price moved" -- it
means the page changed shape and the parser latched onto the wrong number. In both
cases the correct answer is to refuse, report, and keep the old file.

Standard library only. Imported by scrape.py and exercised directly by the tests.

Named `pricing_validate`, not `validate`: a package literally called `validate`
ships in Debian/Ubuntu's system Python (`/usr/lib/python3/dist-packages/validate`,
a configobj compatibility shim). `sys.path.insert(0, ...)` in scrape.py makes this
file win at runtime regardless of name, but a static analyzer does not execute that
line, so a same-named local module is silently shadowed by the unrelated system
package during static analysis, and would be shadowed at runtime too in any
invocation that does not happen to run through scrape.py's own sys.path trick.
"""

from __future__ import annotations

import re
from typing import Any, TypeAlias, cast

# A pricing.json-shaped object, or one of its "models" entries. Named for what it is
# rather than left as a bare `dict`, so a type checker can tell an entry apart from,
# say, the mapping's field spec, instead of collapsing everything to Unknown.
JSONDict: TypeAlias = dict[str, Any]

# A price outside this range, in the file's currency unit, is treated as a parsing
# accident rather than a price. Suggested by the specification (section 4.3).
MIN_PLAUSIBLE = 0.001
MAX_PLAUSIBLE = 1000.0

# A figure that moves by more than this factor in either direction is refused for
# the same reason. Mistral has never moved a price this far in one step; a parser
# that reads the wrong column easily does.
MAX_CHANGE_FACTOR = 5.0

# The units a price field may carry. The unit is part of the key name so that a
# consumer cannot silently apply a per-token price to a per-page model, which also
# means an unknown key here is a contract violation, not a new feature.
KNOWN_PRICE_FIELDS = (
    "in_per_mtok",
    "out_per_mtok",
    "per_1k_pages",
    "per_audio_minute",
)

# "Plausible" is a statement about a unit, not about a number. One minute of audio
# costs three thousandths of a dollar, so the range that catches a misparse for
# per_audio_minute is not the range that catches one for per_1k_pages. Keeping a
# single global floor of 0.001 would leave audio prices barely a factor of 3 above
# it, and would refuse an ordinary price cut as though the page had broken.
#
# Only floors are relaxed here. The ceiling stays where the specification put it:
# raising it would let a real misparse through, which is the failure that matters.
PRICE_BOUNDS = {
    "per_audio_minute": (0.0001, MAX_PLAUSIBLE),
}

SCHEMA_VERSION = 1

_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class ValidationError(Exception):
    """A scraped value may not be published. Always fatal: never downgraded to a warning."""


def check_price(model_id: str, field: str, value: object) -> float:
    """Return `value` as a float, or raise if it cannot be a real price."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(
            f"{model_id}.{field}: expected a number, got {type(value).__name__} ({value!r})"
        )
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        raise ValidationError(f"{model_id}.{field}: not a finite number ({value!r})")

    low, high = PRICE_BOUNDS.get(field, (MIN_PLAUSIBLE, MAX_PLAUSIBLE))
    if not (low <= value <= high):
        raise ValidationError(
            f"{model_id}.{field}: {value} is outside the plausible range "
            f"[{low}, {high}] for this unit. This normally means the page changed "
            f"shape and the wrong number was read, not that the price moved."
        )
    return value


def check_change(model_id: str, field: str, old: float, new: float) -> None:
    """Raise if a figure moved further than a real price plausibly moves."""
    if old <= 0 or new <= 0:
        raise ValidationError(f"{model_id}.{field}: non-positive price ({old} -> {new})")
    factor = new / old if new > old else old / new
    if factor > MAX_CHANGE_FACTOR:
        raise ValidationError(
            f"{model_id}.{field}: {old} -> {new} is a factor of {factor:.1f}, over the "
            f"limit of {MAX_CHANGE_FACTOR}. Refusing to publish. If this change is real, "
            f"edit pricing.json by hand in a reviewed pull request."
        )


def validate_document(doc: Any) -> None:
    """Check a complete pricing.json-shaped document, independently of any scrape.

    `doc` is `Any`, not `JSONDict`, on purpose: this is the function that establishes
    the shape is a dict at all. It is normally handed the direct result of
    `json.loads()` on a file this repository does not fully trust even when it wrote
    it -- a corrupted commit, a bad hand-edit -- so the isinstance check below is
    load-bearing, not decoration.
    """
    if not isinstance(doc, dict):
        raise ValidationError(f"document must be an object, got {type(doc).__name__}")
    doc = cast(JSONDict, doc)

    if doc.get("schema_version") != SCHEMA_VERSION:
        raise ValidationError(
            f"schema_version must be {SCHEMA_VERSION}, got {doc.get('schema_version')!r}"
        )

    for field in ("checked_utc", "updated", "source", "currency"):
        if not isinstance(doc.get(field), str) or not doc[field]:
            raise ValidationError(f"missing or empty required field: {field}")

    if not _ISO_UTC.match(doc["checked_utc"]):
        raise ValidationError(
            f"checked_utc must be YYYY-MM-DDTHH:MM:SSZ, got {doc['checked_utc']!r}"
        )
    if not _ISO_DAY.match(doc["updated"]):
        raise ValidationError(f"updated must be YYYY-MM-DD, got {doc['updated']!r}")

    models = doc.get("models")
    if not isinstance(models, dict) or not models:
        raise ValidationError("models must be a non-empty object")
    models = cast("dict[str, Any]", models)

    for model_id, entry in models.items():
        if not isinstance(entry, dict):
            raise ValidationError(f"{model_id}: entry must be an object")
        entry = cast(JSONDict, entry)

        prices = [k for k in entry if k in KNOWN_PRICE_FIELDS]
        if not prices:
            raise ValidationError(
                f"{model_id}: no price field. Expected one of {list(KNOWN_PRICE_FIELDS)}"
            )

        unknown = [k for k in entry if k not in KNOWN_PRICE_FIELDS and k != "display_name"]
        if unknown:
            raise ValidationError(f"{model_id}: unknown field(s) {unknown}")

        if "price" in entry:
            raise ValidationError(
                f"{model_id}: generic 'price' field is forbidden -- the unit must be in the key name"
            )

        if not isinstance(entry.get("display_name"), str) or not entry["display_name"]:
            raise ValidationError(f"{model_id}: missing or empty display_name")

        for field in prices:
            check_price(model_id, field, entry[field])


def diff_models(old_models: dict[str, JSONDict], new_models: dict[str, JSONDict]) -> list[str]:
    """Return a human-readable list of the differences between two `models` blocks.

    Empty list means the figures are identical and only `checked_utc` needs committing.
    """
    lines: list[str] = []

    for model_id in sorted(set(old_models) | set(new_models)):
        old = old_models.get(model_id)
        new = new_models.get(model_id)

        if old is None:
            lines.append(f"+ {model_id}: added ({_render(new)})")
            continue
        if new is None:
            lines.append(f"- {model_id}: removed (was {_render(old)})")
            continue

        for field in sorted(set(old) | set(new)):
            before, after = old.get(field), new.get(field)
            if before != after:
                lines.append(f"~ {model_id}.{field}: {before!r} -> {after!r}")

    return lines


def _render(entry: JSONDict | None) -> str:
    # entry is only ever None here if a model_id from the union of both key sets
    # somehow belongs to neither dict, which cannot happen; None is accepted so the
    # type checker does not have to take that on faith.
    if entry is None:
        return "(missing)"
    return ", ".join(f"{k}={v}" for k, v in sorted(entry.items()) if k in KNOWN_PRICE_FIELDS)
