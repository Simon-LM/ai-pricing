# Contributing

Thank you for helping. This page says what to open first, an issue or a pull request,
so that no one spends time on work that cannot be merged, then how to run the project
and how to add to it.

## Open an issue first, or a pull request directly?

**Open an issue first** for anything that adds to the format or to what is read:

- a new provider;
- a new field or a new unit;
- a change to the keys, the markers or the schema;
- reading a provider from another page than today's.

Every field and every provider becomes part of a public format that other programs read
and rely on: once published, it cannot simply be taken back. Talking it over first
takes a few minutes, puts everyone on the same page, and avoids a finished pull request
that has to be refused or redone. The issue templates ask for what is needed to decide:
the provider's pricing page, an example of the price, what it would be used for.

**A pull request directly is welcome** for:

- a mistake in the documentation;
- a scraper that broke because a provider changed its page, with its fixture and tests
  updated to the new page;
- a price this file states wrong, when the cause is in how it is read (the scraper or
  the mapping). Please link the provider's page that shows the right figure.

**Do not edit `pricing.json` by hand.** The next daily run rewrites each block from its
source: a hand-made fix would be overwritten, or hide a reader that needs fixing. If you
cannot find the cause, open an issue with the "Wrong or missing price" template.

## Running it yourself

No dependencies beyond the Python standard library, and no API key of any kind.

```sh
python3 -m unittest discover -s tests -v          # the whole test suite
npx pyright                                       # types, as the CI checks them
python3 scripts/build_schema.py --check           # is pricing.schema.json current?
python3 scripts/build_schema.py                   # regenerate it

# one provider, live: reads the public source, writes candidates into .ci-out
python3 scripts/providers/openai/scrape.py --out-dir .ci-out

# one provider, offline, from its committed fixture
python3 scripts/providers/openai/scrape.py --out-dir .ci-out --html tests/fixtures/openai/pricing_ok.md
python3 scripts/providers/mistral/scrape.py --out-dir .ci-out --offline tests/fixtures/mistral/offline.json
```

A scraper **never writes `pricing.json`**. It writes candidate files into `--out-dir`
(`stamped.json` if nothing changed, `updated.json` if something did, `error.txt` if it
failed) and says which one the workflow should promote. `--now` sets the date of the run,
for a source that states prices by date.

## Adding a field or a unit

After the issue is agreed:

1. Add the field to `KNOWN_PRICE_FIELDS` in
   [`scripts/pricing_validate.py`](../scripts/pricing_validate.py), with a comment
   saying what it bills and which provider states it. The field name carries the unit:
   `per_<unit>`, or `<kind>_<side>_per_mtok` for tokens.
2. If its prices are naturally small, give it a floor in `PRICE_BOUNDS`: a test requires
   every published figure to sit a factor of 5 above its floor, so that an ordinary
   price cut is never refused as a misread.
3. Regenerate the schema, `python3 scripts/build_schema.py`.
4. Map the source's wording to the field in that provider's `mapping.json`, copied
   byte for byte from the source.
5. Add it to the units table of [docs/format.md](format.md).

## Adding a provider

After the issue is agreed, and after reading the provider's pricing pages yourself:

1. **Find the source a program can read reliably**: a public JSON API first, then data
   embedded in a page, a page's Markdown version, and only then a page's tables or
   sentences. Confirm the currency. Never use a page that needs an account or a key.
2. **Copy the provider closest to yours.** Scaleway or Eden AI for a JSON API, OpenAI
   for a Markdown page, Anthropic for HTML tables, Google for prices written as
   sentences. A scraper supplies one function, `extract_new_models(fetch, mapping)`,
   returning everything the source offers today and the notes a human should read;
   [`scripts/provider_runner.py`](../scripts/provider_runner.py) does the rest.
3. **Write a mapping that names units, tables and sentences, never models.** Declare
   `format`: `json` for an API, `markdown` for a Markdown page, `html` for a page.
4. **Refuse rather than guess.** A figure in a form the scraper does not know fails the
   run, or is reported and left out; it is never placed by guess.
5. **Test it.** A fixture of the real page under `tests/fixtures/<key>/`, a
   `baseline.json` built from it, and `tests/scraper_tests/test_<key>.py` checking the
   figures as a human reads them on the page, and failing when the page changes shape.
   Import the scraper as `from providers.<key> import scrape`: every scraper is named
   `scrape.py`, and a bare `import scrape` loads the wrong one when the suite runs
   together.
6. **Schedule it**: `.github/workflows/refresh-<key>.yml`, copied from another provider's,
   at a free half hour before 08:00 UTC; add it to the `workflow_run` list of
   `tests.yml` and to `PROVIDER_MODULES` in `tests/test_workflows.py`.
7. **Document it**: `scripts/providers/<key>/README.md` (what is read, the keys, what is
   not published and why), a row in the README's providers table, and a row in the keys
   table of [docs/format.md](format.md).
8. **Seed its block.** A run updates a block; it does not start one. The first block is
   built from a live run and committed with the provider.

## Rules that do not bend

- No API key, no account, no login: every source is public.
- No hand-written list of models: the source says what exists, every day.
- No currency conversion, no estimate of a bill: this file states prices, as stated.
- A figure that cannot be trusted is not published.

## Pull requests

- One subject per pull request, linked to its issue when one was needed.
- The test suite, the type check and the schema check pass.
- The description says which page was read, and on what date.
