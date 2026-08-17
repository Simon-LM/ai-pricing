"""Sanity checks applied to every scraped figure before it may be published.

The premise of these checks is that this repository reads a marketing page that
nobody promised to keep stable. A figure that lands far outside a plausible range,
or that jumps by an implausible factor, almost never means "the price moved" -- it
means the page changed shape and the parser latched onto the wrong number. In both
cases the correct answer is to refuse, report, and keep the old file.

pricing.json holds one block per provider, under `providers.<name>`, because the
same model name can mean two different prices at two different providers (a Mistral
model resold on OVH is not the same bill as calling Mistral directly). The units,
bounds and per-model checks in this file are provider-agnostic and shared by every
provider's own scraper; `validate_document` walks every provider block with the
same rules, so a corruption in one provider's block is caught even by a run that
only touched another provider's.

Standard library only. Imported by each provider's scrape.py and exercised
directly by the tests.

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
#
# `in_per_mtok`/`out_per_mtok` are specifically the two sides of a token price. A
# product billed at one flat per-million-token rate with no input/output split gets
# `per_mtok` instead, and one billed per million tokens for a named operation gets
# its own key (`index_per_mtok`, `train_per_mtok`) -- rather than being folded into
# `in_per_mtok`, which would quietly make "input tokens" mean four different things
# depending on the entry.
KNOWN_PRICE_FIELDS = (
    # token prices
    "in_per_mtok",
    "out_per_mtok",
    "per_mtok",
    "cache_read_per_mtok",
    "index_per_mtok",
    "train_per_mtok",
    "per_mchars",
    # document prices
    "per_1k_pages",
    "per_1k_chars",
    # audio prices
    "per_audio_minute",
    "per_audio_second",
    # per-operation prices
    "per_call",
    "per_1k_calls",
    "per_1k_images",
    "per_image",
    # subscription prices
    "per_model_month",
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
    # A second is roughly 1/60th of a minute, so its floor is lower again: OVH's
    # whisper-large-v3-turbo prices at ~0.0000128 per second, which the
    # per_audio_minute floor above would already reject outright.
    "per_audio_second": (0.000001, MAX_PLAUSIBLE),
}

# What an entry under `models` actually is. "model" is the default and is left
# unwritten in the file; "product" is a billable thing that is not a model and has no
# API model id (Mistral's web search, code execution, image generation and the like).
KNOWN_KINDS = ("model", "product")

# An entry whose source no longer offers it is NOT deleted on the spot. It keeps the
# last prices that were actually observed, frozen, and gains `absent_since` -- the day
# the source was first seen without it. Deleting immediately would be the automatic
# behaviour, and it is the wrong one: a consumer that still references the model would
# lose its price with no warning and no way to look up what it used to be, and a
# price series would develop a hole exactly where a comparison is most interesting.
#
# The entry is dropped once it has been absent for this long. A year rather than a
# few months on purpose: the projects reading this file are not all actively
# maintained, and one that quietly still names a withdrawn model should have a stale
# figure and an `absent_since` date to notice, for a good while, rather than a
# KeyError.
#
# A price carrying `absent_since` is a LAST KNOWN price, not a current one. Consumers
# must treat the two differently; that is the entire point of the field.
ABSENT_RETENTION_DAYS = 365

SCHEMA_VERSION = 2

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
            f"edit pricing.json by hand -- a jump this large is not published unreviewed."
        )


def validate_document(doc: Any) -> None:
    """Check a complete pricing.json-shaped document, independently of any scrape.

    `doc` is `Any`, not `JSONDict`, on purpose: this is the function that establishes
    the shape is a dict at all. It is normally handed the direct result of
    `json.loads()` on a file this repository does not fully trust even when it wrote
    it -- a corrupted commit, a bad hand-edit -- so the isinstance check below is
    load-bearing, not decoration.

    Every provider block is checked, not just the one a caller happens to be
    updating: a scrape that only touches `providers.mistral` still writes out the
    full document, and a corruption sitting untouched in `providers.ovh` must fail
    the same way a fresh one would, rather than ride along because nobody looked.
    """
    if not isinstance(doc, dict):
        raise ValidationError(f"document must be an object, got {type(doc).__name__}")
    doc = cast(JSONDict, doc)

    if doc.get("schema_version") != SCHEMA_VERSION:
        raise ValidationError(
            f"schema_version must be {SCHEMA_VERSION}, got {doc.get('schema_version')!r}"
        )

    providers = doc.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise ValidationError("providers must be a non-empty object")
    providers = cast("dict[str, Any]", providers)

    for provider_id, block in providers.items():
        if not isinstance(block, dict):
            raise ValidationError(f"providers.{provider_id}: entry must be an object")
        _validate_provider_block(provider_id, cast(JSONDict, block))


def _validate_provider_block(provider_id: str, block: JSONDict) -> None:
    """Check one `providers.<id>` block: its own metadata, then its own models.

    A provider's `checked_utc`/`updated`/`source`/`currency` are its own, not the
    file's: providers scrape on different schedules, from different pages, and
    sometimes in different currencies, so there is deliberately no file-wide
    equivalent of these four fields to fall back to.
    """
    for field in ("checked_utc", "updated", "source", "currency"):
        if not isinstance(block.get(field), str) or not block[field]:
            raise ValidationError(f"providers.{provider_id}: missing or empty required field: {field}")

    if not _ISO_UTC.match(block["checked_utc"]):
        raise ValidationError(
            f"providers.{provider_id}: checked_utc must be YYYY-MM-DDTHH:MM:SSZ, "
            f"got {block['checked_utc']!r}"
        )
    if not _ISO_DAY.match(block["updated"]):
        raise ValidationError(
            f"providers.{provider_id}: updated must be YYYY-MM-DD, got {block['updated']!r}"
        )

    models = block.get("models")
    if not isinstance(models, dict) or not models:
        raise ValidationError(f"providers.{provider_id}: models must be a non-empty object")
    models = cast("dict[str, Any]", models)

    for model_id, entry in models.items():
        full_id = f"{provider_id}/{model_id}"
        if not isinstance(entry, dict):
            raise ValidationError(f"{full_id}: entry must be an object")
        entry = cast(JSONDict, entry)

        prices = [k for k in entry if k in KNOWN_PRICE_FIELDS]
        free = entry.get("free")

        # A model that a provider deliberately gives away is published as
        # `"free": true` with no price field at all, never as a price of 0. The two
        # are not the same statement: 0 is also exactly what a broken parser reads
        # off a page whose layout moved, and check_price's floor exists to catch
        # precisely that. Keeping "free" out of the number space means the floor
        # never has to be relaxed to accommodate it.
        if "free" in entry and free is not True:
            raise ValidationError(
                f"{full_id}: 'free' may only be the literal true. A model that is not "
                f"free omits the field; false, 0 and null are all ways of writing "
                f"something this field cannot mean."
            )
        if free is True and prices:
            raise ValidationError(
                f"{full_id}: marked free but also carries price field(s) {prices}. "
                f"One of the two is wrong and nothing here can tell which."
            )
        if free is not True and not prices:
            raise ValidationError(
                f'{full_id}: no price field and not marked free. Expected one of '
                f'{list(KNOWN_PRICE_FIELDS)}, or "free": true.'
            )

        # Mistral's pricing page bills models and non-model products side by side --
        # web search, code execution and image generation are priced per call, not per
        # token, and have no API model id at all. They are published here because a
        # consumer estimating a bill needs them, but a consumer iterating "models"
        # must be able to tell them apart from something it can actually call as a
        # model. Absent means "model": the common case stays unannotated.
        kind = entry.get("kind")
        if kind is not None and kind not in KNOWN_KINDS:
            raise ValidationError(
                f"{full_id}: kind must be one of {list(KNOWN_KINDS)}, got {kind!r}"
            )

        # Set on the day the source was first seen without this entry, and cleared the
        # day it comes back. Its presence changes what every price beside it means --
        # last known rather than current -- so the format is pinned as tightly as
        # `updated` is, and a value that is not a plain day is refused rather than
        # published as something a consumer would have to guess at.
        if "absent_since" in entry:
            absent_since = entry["absent_since"]
            if not isinstance(absent_since, str) or not _ISO_DAY.match(absent_since):
                raise ValidationError(
                    f"{full_id}: absent_since must be a plain day, YYYY-MM-DD, got "
                    f"{absent_since!r}. An entry the source still offers omits the field; "
                    f"null and false are ways of writing something it cannot mean."
                )

        unknown = [
            k
            for k in entry
            if k not in KNOWN_PRICE_FIELDS
            and k not in ("display_name", "free", "kind", "absent_since")
        ]
        if unknown:
            raise ValidationError(f"{full_id}: unknown field(s) {unknown}")

        if "price" in entry:
            raise ValidationError(
                f"{full_id}: generic 'price' field is forbidden -- the unit must be in the key name"
            )

        if not isinstance(entry.get("display_name"), str) or not entry["display_name"]:
            raise ValidationError(f"{full_id}: missing or empty display_name")

        for field in prices:
            check_price(full_id, field, entry[field])


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
    if entry.get("free") is True:
        return "free"
    return ", ".join(f"{k}={v}" for k, v in sorted(entry.items()) if k in KNOWN_PRICE_FIELDS)
