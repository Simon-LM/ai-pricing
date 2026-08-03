# ai-pricing

Machine-readable prices for the AI models a handful of small projects actually use,
published as one file at one URL:

```text
https://raw.githubusercontent.com/Simon-LM/ai-pricing/main/pricing.json
```

## Read this before you trust a number in it

**These figures are scraped from a public marketing page.** Mistral publishes no
machine-readable pricing: there is no pricing endpoint in the API, and the one usage
endpoint that returns a `prices` field requires an *admin* API key that an ordinary
user of an open-source tool does not have and should never be asked for. So the
numbers here come from reading <https://mistral.ai/pricing/api>, a page with no
contract behind it and no versioning.

**A human reviews every figure before it is published.** The weekly job may commit
the "we checked, nothing moved" timestamp on its own. It may never change a price on
its own: a price change opens a pull request that a person reads and merges.

**There is no guarantee of any kind.** Not of accuracy, not of freshness, not of
continued existence. If money depends on the answer, read the authoritative page:

> <https://mistral.ai/pricing/api>

Every figure carries the date it was verified, precisely so that you can show it to
whoever is reading your number. Display it: `≈ $0.80 (rates of 2026-07-30)`, never a
bare `$0.80`.

## The file

```json
{
  "schema_version": 1,
  "checked_utc": "2026-08-02T22:37:46Z",
  "updated": "2026-08-03",
  "source": "https://mistral.ai/pricing/api",
  "currency": "USD",
  "models": {
    "mistral-medium-latest": {
      "in_per_mtok": 1.5,
      "out_per_mtok": 7.5,
      "display_name": "Mistral Medium 3.5"
    },
    "voxtral-small-latest": {
      "in_per_mtok": 0.1,
      "out_per_mtok": 0.4,
      "per_audio_minute": 0.004,
      "display_name": "Voxtral Small"
    }
  }
}
```

| field | meaning |
| --- | --- |
| `schema_version` | A consumer that does not recognise this number must ignore the file and fall back, rather than misread it. Bumped on any breaking change. |
| `checked_utc` | When the scraper last **verified** the figures. "Confirmed unchanged today" is a much stronger statement than "last edited in May", which is why this is separate from `updated`. |
| `updated` | When a figure last actually **changed**. |
| `source` | The page the numbers came from, so a human can check in one click. |
| `currency` | Never assume it. Prices are published in USD and converted by nobody here. |
| `models` | Keyed by **API model id**, never by marketing name. |
| `display_name` | The marketing name, kept only so that a diff is readable by a human. Never use it for matching. |

The unit is part of the key name, so that a consumer cannot silently apply a
per-token price to a per-page model. There is deliberately no generic `price` field.

| unit key | billed |
| --- | --- |
| `in_per_mtok` | per million input tokens |
| `out_per_mtok` | per million output tokens |
| `per_1k_pages` | per thousand pages |
| `per_audio_minute` | per minute of audio sent |

**A model may carry several of them at once.** `voxtral-small-latest` above is billed
both per minute of audio and per million tokens of text, and reading only one of the
two undercounts a bill without ever looking wrong. Do not assume one unit per model:
iterate the keys you find.

**Within a `schema_version`, no field is ever removed.** An old client may fetch this
file at any time. Fields get added — `per_audio_minute` was added this way — and when
something must break, the version is bumped.

## Consuming it

Ship a copy of the file with your package, so a machine with no network still has
dated figures. Refresh from the URL above at most weekly, cache it locally, never
block on it, and let the user turn it off. Convert at display time only, and always
attach the date.

## The trap worth knowing about

`mistral-medium-latest` is an **alias**. Today it resolves to Mistral Medium 3.5;
tomorrow it will resolve to something else, at a different price, with no change
anywhere in this file.

The scraper therefore matches marketing names to API ids through an explicit,
hand-maintained mapping in [`scripts/mapping.json`](scripts/mapping.json) — never by
fuzzy matching, never by lowercasing and hoping. A name the mapping does not
recognise is reported as a failure, not guessed at.

That mapping is an assumption a human wrote down, and it is the thing most likely to
be quietly wrong. It is written down explicitly so that it *can* be checked.

## How the weekly job behaves

`.github/workflows/refresh.yml` runs on Mondays at 04:00 UTC, and on demand. It has
three outcomes and never a fourth:

| outcome | what happens |
| --- | --- |
| figures unchanged | the `checked_utc` stamp is committed straight to `main` |
| a figure changed | the stamp is committed, then a **pull request** is opened. Never auto-merged. |
| fetch or parse failed | an issue is opened, the job fails, and `pricing.json` is left **untouched** |

A stale price whose date you can see is far better than a wrong one you cannot. So
the job refuses rather than guesses whenever:

- the page cannot be fetched, or no longer parses;
- a model in the mapping is absent from the page, or has been renamed;
- a figure lands outside the plausible range for its unit, or moves by more than a
  factor of 5;
- the unit shown next to a price changes;
- the page stops publishing a USD figure.

The stamp commit on every run is not only informative. GitHub disables a scheduled
workflow after 60 days without repository activity, and **only new commits reset that
timer** — not tags, not issues, not merged pull requests. A repository whose content
changes every few months is exactly the profile that gets silently switched off.
Committing `checked_utc` weekly is both a real statement to consumers and the thing
that keeps the schedule alive.

## Running it yourself

No dependencies beyond the Python standard library, and no API key of any kind — the
scraper reads a public page and must never be given a credential.

```sh
python3 -m unittest discover -s tests -v          # 42 tests
python3 scripts/scrape.py --out-dir .ci-out       # read the live page
python3 scripts/scrape.py --out-dir .ci-out --html tests/fixtures/page_ok.html
```

`scrape.py` **never writes `pricing.json`.** It writes candidates into `--out-dir`
and reports which one, if any, should be promoted. "Leave the published file
untouched when anything goes wrong" is therefore a property of the design rather
than a branch of code somebody has to remember to get right.

## Adding a model

1. Add an entry to `scripts/mapping.json`, with `page_name` copied byte-for-byte from
   the card's `data-name` attribute on the pricing page, and each `label` copied
   byte-for-byte from the row label. List every unit the model is billed in, not just
   the obvious one.
2. Add the same model to `pricing.json` with the figures you read on the page.
3. If the model introduces a **new unit**, add it to `KNOWN_PRICE_FIELDS` in
   `scripts/pricing_validate.py`, and check whether the default floor of 0.001 still makes
   sense for it. A unit whose prices are naturally small needs its own entry in
   `PRICE_BOUNDS`, or an ordinary price cut will be refused as a parsing accident.
   A test enforces that every published figure keeps a factor of 5 of room above its
   floor, so you will be told rather than left to find out on a Monday morning.
4. Re-capture `tests/fixtures/page_ok.html` so the fixture contains the new card, and
   regenerate `tests/fixtures/baseline.json` from it.
5. Run the tests. `TestValidator` and `TestMapping` check that the files agree.

Do not add currency conversion, token estimation, or anything that reads a user's
account. This repository knows prices. It does not know volumes, and it never
touches anyone's Mistral account.

## Why this is a separate repository

Somebody has to read a web page. The design question is *where*, and the answer is:
not on the user's machine. A consumer that scraped the page itself would not crash
when the page was redesigned — it would read the **wrong number** and cheerfully
announce €0.80 for a run that costs €8, in every installation at once. Here, a human
sees the diff before anything reaches anyone.

It also means the price of `mistral-medium-latest` is not one project's private fact,
`git log` on a single JSON file becomes a price history nobody else publishes, and a
correction does not require cutting a release of anything.

The full rationale, and the contract this file is bound by, is
[`docs/ai-pricing-source.md`](https://github.com/Simon-LM/ProcraFiler/blob/main/docs/ai-pricing-source.md)
in [ProcraFiler](https://github.com/Simon-LM/ProcraFiler), the first consumer.

## Licence

[MIT](LICENSE.md).

The prices themselves are facts about somebody else's products, published by Mistral
AI. They are reproduced here for interoperability, and this repository claims nothing
over them.
