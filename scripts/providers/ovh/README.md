<!-- @format -->

# OVH -- not built yet

This directory is reserved for OVH's scraper. It is deliberately empty of code:
there is no `scrape.py` or `mapping.json` here, because writing either without
having read OVH's actual pricing source first would mean guessing at numbers,
which is exactly what this whole repository exists to refuse to do.

## Before writing a line of code

1. Find out what OVH actually publishes. OVH AI Endpoints/AI Deploy may expose
   pricing through a structured, machine-readable source (a public catalog API,
   a JSON feed) rather than only a marketing page. If so, prefer it outright --
   it removes an entire class of failure (`scripts/providers/mistral/scrape.py`
   exists only because Mistral leaves no better option, per
   `docs/ai-pricing-source.md` in ProcraFiler). If OVH only has an HTML page like
   Mistral's, the same `PricingPageParser` pattern applies, adapted to OVH's
   actual markup -- do not assume it matches Mistral's.
2. Confirm whether OVH's catalog includes third-party models (e.g. Mistral
   models resold through OVH). If it does, `display_name` and the mapping must
   make unmistakably clear this is **OVH's price for running that model**, not
   Mistral's own -- the two can differ, and a consumer must never be able to
   confuse them. This is the entire reason pricing.json nests under
   `providers.<name>` instead of one flat `models` map.
3. Confirm OVH's billing currency. It is very likely EUR, not USD like Mistral --
   `providers.ovh.currency` is independent of `providers.mistral.currency`, and
   nothing here performs conversion.

## What "done" looks like

Mirror the Mistral provider directory as the reference implementation:

- `scripts/providers/ovh/scrape.py` -- reads OVH's real pricing source, matches
  it to API model ids through an **explicit, hand-committed mapping**, never
  fuzzy matching. Writes `providers.ovh` into a full pricing.json document,
  carrying every other provider's block through untouched, exactly as
  `scripts/providers/mistral/scrape.py` does via `merge_provider_block()`.
  It never writes `pricing.json` directly -- only candidate files, same as
  Mistral's.
- `scripts/providers/ovh/mapping.json` -- OVH's own page-name/label mapping,
  reviewed by a human, revisited whenever OVH's catalog changes.
- `tests/fixtures/ovh/` -- a real captured snapshot of whatever OVH actually
  publishes, with the same kind of decoy rows the Mistral fixture keeps
  (`libraries`' OCR row, the Voxtral realtime duplicate), if OVH's source has
  any place a naive by-label search could grab the wrong number.
- `tests/providers/test_ovh.py` -- the same scenarios `tests/providers/test_mistral.py`
  covers: unchanged, a normal price change, a layout that no longer parses, a
  model in the mapping missing from the source, an out-of-bounds figure. Every
  failure path must leave `pricing.json` untouched, proven the same way.
- `.github/workflows/refresh-ovh.yml` -- same three outcomes as
  `refresh-mistral.yml` (unchanged -> stamp only, changed -> pull request never
  auto-merged, failure -> issue and no write), same shared `concurrency` group
  so it queues rather than races with the Mistral workflow over the same file.

`scripts/pricing_validate.py` and `scripts/fetch.py` are already provider-agnostic
and need no changes to support this -- `validate_document()` already walks every
block under `providers`, and `fetch_page()` already has no Mistral-specific
assumptions baked in.
