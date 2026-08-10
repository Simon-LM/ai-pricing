# ai-pricing

Machine-readable AI model prices, one provider's block per key, published as one
file at one URL:

```text
https://raw.githubusercontent.com/Simon-LM/ai-pricing/main/pricing.json
```

Today that means two providers called directly — Mistral and OVH's AI Endpoints
catalog — and two aggregators that resell them and others, Eden AI and Hugging Face's
Inference Providers router. That is exactly why prices are grouped by **provider**,
not just by model: the same model is a different bill depending on who you actually
paid. `codestral-latest` is $0.30/$0.90 from Mistral and $1.00/$3.00 through Eden AI;
`Qwen3.6-27B` is €0.40 from OVH directly and $0.47 through Hugging Face. Every one of
those numbers is in this file.

| provider | entries | currency | what it is |
| --- | --- | --- | --- |
| `mistral` | 30 | USD | called directly |
| `ovh` | 19 | EUR | called directly |
| `edenai` | 103 | USD | resold, 5 upstreams |
| `huggingface` | 15 | USD | routed, 2 partners |

`huggingface` is keyed by `<model>:<partner>` rather than by model, because the
router serves one model through several partners at several prices — `gpt-oss-120b`
through eleven of them. The others are covered in full; `huggingface` covers the two
partners asked for.

## Read this before you trust a number in it

**Most of these figures are scraped from public marketing pages**, one scraper per
provider. Mistral, specifically, publishes no machine-readable pricing: there is no
pricing endpoint in the API, and the one usage endpoint that returns a `prices`
field requires an *admin* API key that an ordinary user of an open-source tool does
not have and should never be asked for. So Mistral's numbers here come from reading
<https://mistral.ai/pricing/api>, a page with no contract behind it and no
versioning. OVH's are read the same way, from their public AI Endpoints catalog --
a Next.js app that embeds its model list and pricing as JSON inside the page rather
than rendering scrapeable card markup the way Mistral's does.

Eden AI is the exception and the sturdiest source here: a real public JSON API,
<https://api.edenai.run/v3/models>, stating an input and an output price per model
under explicit field names, with no label to string-match and no layout to latch
onto. Being an aggregator, its prices are what **Eden** charges to forward a call,
so they differ from the same model's price under `providers.mistral` or
`providers.ovh` — by design, not by error.

Each provider's own `README.md` under `scripts/providers/<name>/` explains that
provider's specific situation.

**Price changes publish automatically.** The weekly job for each provider commits
whatever it read, straight to `main` -- a moved price included. There is no review
gate, because a price moving is not a decision anyone makes: the provider already
changed it, and holding the new figure back only means shipping one known to be
stale.

What a reviewer would actually have caught is caught earlier and harder, before
anything is written: a figure outside the plausible range for its unit, or one that
moved by more than a factor of 5, fails the job and publishes nothing. Everything
that reaches `main` has cleared those. `git log -- pricing.json` is the record of
every move, dated, with the figures in the diff.

**There is no guarantee of any kind.** Not of accuracy, not of freshness, not of
continued existence. If money depends on the answer, read the authoritative page,
linked in `providers.<name>.source` below.

Every provider's block carries the date it was verified, precisely so that you can
show it to whoever is reading your number. Display it:
`≈ $0.80 (Mistral rates of 2026-07-30)`, never a bare `$0.80`.

## The file

```json
{
  "schema_version": 2,
  "providers": {
    "mistral": {
      "checked_utc": "2026-08-03T05:11:31Z",
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
  }
}
```

| field | meaning |
| --- | --- |
| `schema_version` | A consumer that does not recognise this number must ignore the file and fall back, rather than misread it. Bumped on any breaking change. |
| `providers` | Keyed by provider id (`mistral`, `ovh`). Never flatten this: the same model name at two providers is two different prices, not a collision to resolve. |
| `providers.<name>.checked_utc` | When that provider's scraper last **verified** its figures. "Confirmed unchanged today" is a much stronger statement than "last edited in May", which is why this is separate from `updated`. Each provider has its own -- they scrape on their own schedule. |
| `providers.<name>.updated` | When that provider's figures last actually **changed**. |
| `providers.<name>.source` | The page that provider's numbers came from, so a human can check in one click. |
| `providers.<name>.currency` | Never assume it, and never assume it matches another provider's. Nothing here performs conversion. |
| `providers.<name>.models` | Keyed by **API model id**, never by marketing name. |
| `display_name` | The marketing name, kept only so that a diff is readable by a human. Never use it for matching. |
| `free` | Present, and always `true`, when the provider gives the model away. The entry then carries **no price field at all** -- see below. |
| `kind` | Absent on a model, which is the normal case. `"product"` marks a billable thing that is **not** a model and has no API model id: Mistral's web search, code execution and image generation are priced on the same page as its models. Filter on it if you are listing models to call. |

The unit is part of the key name, so that a consumer cannot silently apply a
per-token price to a per-page model. There is deliberately no generic `price` field.

| unit key | billed |
| --- | --- |
| `in_per_mtok` | per million input tokens |
| `out_per_mtok` | per million output tokens |
| `per_mtok` | per million tokens, with no input/output split |
| `cache_read_per_mtok` | per million tokens read back from a prompt cache |
| `index_per_mtok` | per million tokens indexed |
| `train_per_mtok` | per million tokens trained on, one-off |
| `per_1k_pages` | per thousand pages |
| `per_1k_chars` | per thousand characters |
| `per_audio_minute` | per minute of audio sent |
| `per_audio_second` | per second of audio sent |
| `per_call` | per API call |
| `per_1k_calls` | per thousand API calls |
| `per_1k_images` | per thousand images |
| `per_model_month` | per month, per stored model |

**A free model is `"free": true`, never a price of `0`.** Zero is also exactly what a
broken parser reads off a page whose layout moved, and this repository refuses a
figure of 0 for that reason. Publishing free models as a marker rather than a number
is what lets that check stay strict. A consumer must treat an entry with `free` as
costing nothing, not as missing data.

**A model may carry several of them at once.** `voxtral-small-latest` above is billed
both per minute of audio and per million tokens of text, and reading only one of the
two undercounts a bill without ever looking wrong. Do not assume one unit per model:
iterate the keys you find.

**Within a `schema_version`, no field is ever removed.** An old client may fetch this
file at any time. Fields get added -- `per_audio_minute` and later `per_audio_second`
were added this way, and so was the whole `providers` layer this file now has -- and
when something must break in a way addition cannot cover, the version is bumped, as
it was when `providers` arrived.

## Consuming it

Ship a copy of the file with your package, so a machine with no network still has
dated figures. Refresh from the URL above at most weekly, cache it locally, never
block on it, and let the user turn it off. Convert at display time only, and always
attach the date.

To price a call, you need to know both the provider you called and the model id, and
read `providers.<that provider>.models.<that model>` -- there is no flat, provider-less
lookup, on purpose.

## The trap worth knowing about

`mistral-medium-latest` is an **alias**. Today it resolves to Mistral Medium 3.5;
tomorrow it will resolve to something else, at a different price, with no change
anywhere in this file.

Each provider's scraper matches marketing names to API ids through an explicit,
hand-maintained mapping -- Mistral's lives at
[`scripts/providers/mistral/mapping.json`](scripts/providers/mistral/mapping.json) --
never by fuzzy matching, never by lowercasing and hoping. A name the mapping does not
recognise is reported as a failure, not guessed at.

That mapping is an assumption a human wrote down, and it is the thing most likely to
be quietly wrong. It is written down explicitly so that it *can* be checked.

## Multiple providers, one file

`providers.<name>` exists because the same model can be called through more than one
API, at more than one price. A Mistral model resold through OVH is not the same bill
as calling Mistral directly, and nothing about a model's own name says which API you
actually paid.

Each provider is fully independent in practice, even though they share one file:

- its own scraper, under `scripts/providers/<name>/`, reading only that provider's
  source and writing only `providers.<name>`;
- its own explicit mapping, reviewed by a human;
- its own weekly workflow, `.github/workflows/refresh-<name>.yml`, with the same
  three outcomes as Mistral's (unchanged, changed -> PR, failure -> issue);
- its own `checked_utc`, `updated`, `source` and `currency`.

A scraper only ever reads and rewrites its own block. Every other provider's block is
carried through candidate files byte-for-byte -- a Mistral run never so much as parses
`providers.ovh`, and an OVH run never touches `providers.mistral`. Key order is
preserved too, not just values: a merge that rebuilt the whole `providers` dict would
leave every untouched provider's block looking removed-and-re-added in the diff a
human is about to review, which is exactly the kind of noise this design exists to
avoid. `validate_document()` still checks every provider's block on every run, so a
corruption sitting untouched in one provider is caught by any run, not only by that
provider's own.

The plumbing that makes all of this true -- reading, merging, writing, the three
outcomes, the run()/main() control flow -- is written once, in
[`scripts/provider_runner.py`](scripts/provider_runner.py), and shared by every
provider's own `scrape.py`. A provider's own file supplies only what is genuinely
its own: how to fetch and parse its page. Compare
[`scripts/providers/mistral/scrape.py`](scripts/providers/mistral/scrape.py) (reads
server-rendered card markup) against
[`scripts/providers/ovh/scrape.py`](scripts/providers/ovh/scrape.py) (reads a
framework's embedded JSON data chunk) for how differently "read the page" can look
between two providers, and how little of either file is about anything else.

## How the weekly job behaves

Each provider has its own workflow:
[`.github/workflows/refresh-mistral.yml`](.github/workflows/refresh-mistral.yml) runs
Mondays at 04:00 UTC, and
[`.github/workflows/refresh-ovh.yml`](.github/workflows/refresh-ovh.yml) an hour
later at 05:00, and
[`.github/workflows/refresh-edenai.yml`](.github/workflows/refresh-edenai.yml) at
06:00 and
[`.github/workflows/refresh-huggingface.yml`](.github/workflows/refresh-huggingface.yml)
at 07:00 -- offset on purpose so the scheduled runs don't land in the same minute and
immediately queue behind each other every single week. All also run on demand. Every
provider's workflow has three outcomes and never a fourth:

| outcome | what happens |
| --- | --- |
| figures unchanged | that provider's `checked_utc` stamp is committed straight to `main` |
| a figure changed | the new figures are committed straight to `main`, with a `::notice::` on the run |
| fetch or parse failed | an issue is opened **and assigned to the repository owner**, the job fails, and `pricing.json` is left **untouched** |

A stale price whose date you can see is far better than a wrong one you cannot. So
each job refuses rather than guesses whenever:

- the page cannot be fetched, or no longer parses;
- a model in its mapping is absent from the page, or has been renamed;
- a figure lands outside the plausible range for its unit, or moves by more than a
  factor of 5;
- the unit shown next to a price changes;
- the page stops publishing a figure in the currency that provider's block declares.

All providers' workflows share one `concurrency` group, `pricing-json-writes`, since
they all commit to the same file: a second provider's run queues behind the first
rather than racing it.

**Refusing to publish a wrong number is only half the job.** A scraper fails loudly
when a model it maps disappears from a page, and is completely silent about a model
the provider sells that its page never mentions -- an absent row looks exactly like a
model that does not exist. OVH's catalog page dropped five models it was still
serving on 2026-08-04, and nothing in the scrape could have noticed. So OVH's
workflow also runs
[`scripts/providers/ovh/check_coverage.py`](scripts/providers/ovh/check_coverage.py),
which compares the catalog page against OVH's own public model list and reports what
each one has that the other does not. It reads no price, never touches
`pricing.json`, and a gap is a warning rather than a failed job: refusing to publish
nineteen correct prices because a twentieth is missing would also suppress the
`checked_utc` stamp that keeps the schedule alive.

Being assigned is what actually delivers the alert: it notifies through a different
channel than watching the repository, one that is on by default, and it subscribes
the owner to the issue so that the follow-up comment on each repeated failure lands
too. Note the one case neither covers -- if the workflow is *disabled* rather than
failing, nothing runs, no issue is opened and no mail is sent. The only detector for
that is a consumer watching `checked_utc` go stale.

The stamp commit on every run is not only informative. GitHub disables a scheduled
workflow after 60 days without repository activity, and **only new commits reset that
timer** -- not tags, not issues, nothing but commits. A repository whose content
changes every few months is exactly the profile that gets silently switched off.
Committing `checked_utc` weekly, per provider, is both a real statement to consumers
and the thing that keeps each schedule alive.

## Running it yourself

No dependencies beyond the Python standard library, and no API key of any kind -- a
scraper reads a public page and must never be given a credential.

```sh
python3 -m unittest discover -s tests -v                          # 164 tests
python3 scripts/providers/mistral/scrape.py --out-dir .ci-out     # read the live page
python3 scripts/providers/mistral/scrape.py --out-dir .ci-out --html tests/fixtures/mistral/page_ok.html

python3 scripts/providers/ovh/scrape.py --out-dir .ci-out         # read the live catalog
python3 scripts/providers/ovh/scrape.py --out-dir .ci-out --html tests/fixtures/ovh/catalog_ok.html
```

A provider's `scrape.py` **never writes `pricing.json`.** It writes candidates into
`--out-dir` and reports which one, if any, should be promoted, merging its own block
into a full copy of the current file and leaving every other provider's block
untouched. "Leave the published file alone when anything goes wrong" is therefore a
property of the design rather than a branch of code somebody has to remember to get
right.

## Adding a model

1. Look the **API model id** up at the provider, not on the pricing page. The page's
   card name is marketing copy and is regularly not the id: Mistral's `voxtral tts`
   card is `voxtral-mini-tts-latest`, its `ministral 3 - 3b` card is
   `ministral-3b-latest`, and OVH's catalog entry with `id: "qwen-3-6-27b"` is called
   as `Qwen3.6-27B`. Mistral publishes ids on each model's card at
   `docs.mistral.ai/models/model-cards/<slug>`; OVH puts the id in the catalog's own
   `name` field, cross-checkable against its public model list at
   `https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/models`. Guessing here publishes
   a key that resolves against nothing while looking perfectly reasonable.
2. Add an entry to that provider's `mapping.json` -- Mistral's keys a card by
   `page_name` and a price row by `label`, copied byte-for-byte from the page;
   OVH's keys a catalog entry by `catalog_id` **and** `catalog_name`, and a price by
   `price_unit`, copied byte-for-byte from its embedded data. Whatever the provider's
   own convention, list every unit the model is billed in, not just the obvious one.
   A model the provider gives away gets `"free": true` instead of price fields, and a
   billable thing that is not a model at all gets `"kind": "product"`.
3. Add the same model to `pricing.json`, under that provider's block, with the
   figures you read on the page. The job refuses to run against a `pricing.json` that
   publishes a key the mapping does not know, so removing or renaming a key is
   deliberately a hand edit, never something a scheduled run can do by itself.
4. If the model introduces a **new unit**, add it to `KNOWN_PRICE_FIELDS` in
   [`scripts/pricing_validate.py`](scripts/pricing_validate.py) -- shared by every
   provider -- and check whether the default floor of 0.001 still makes sense for
   it. A unit whose prices are naturally small needs its own entry in
   `PRICE_BOUNDS`, or an ordinary price cut will be refused as a parsing accident.
   A test enforces that every published figure keeps a factor of 5 of room above its
   floor, so you will be told rather than left to find out on a Monday morning.
5. Re-capture that provider's fixture (Mistral's is `tests/fixtures/mistral/page_ok.html`,
   OVH's is `tests/fixtures/ovh/catalog_ok.html`) so it contains the new entry, and
   regenerate its `baseline.json` from it.
6. Run the tests. `tests/test_pricing_validate.py` checks the shared schema; each
   provider's `tests/scraper_tests/test_<name>.py` has its own
   `TestPublishedFileMatches<Name>` and `TestMapping` classes that check that
   provider's own files agree with each other.

Do not add currency conversion, token estimation, or anything that reads a user's
account. This repository knows prices. It does not know volumes, and it never
touches anyone's account at any provider.

## Adding a provider

Confirm the real pricing source first -- read it yourself, work out whether it is
scrapeable card markup (like Mistral's) or embedded framework data (like OVH's),
confirm the currency, check whether the catalog resells third-party models under
a name that could be confused with theirs. Never guess at any of this.

Then mirror whichever of the two existing providers reads more like your new
one -- [`scripts/providers/mistral/scrape.py`](scripts/providers/mistral/scrape.py)
or [`scripts/providers/ovh/scrape.py`](scripts/providers/ovh/scrape.py) -- for the
page-reading half, and use [`scripts/provider_runner.py`](scripts/provider_runner.py)
for everything else: it already handles reading, merging, writing, and the three
outcomes, and only needs an `extract_new_models(html_text, mapping)` callback and a
provider id. Write an explicit mapping, and give the new provider its own
`refresh-<name>.yml` sharing the `pricing-json-writes` concurrency group.

One naming trap worth knowing before you start: give the new provider's test file
a distinct import path (`from providers.<name> import scrape`, not a bare
`import scrape` off a directly-inserted directory). Two providers' `scrape.py`
files share the same filename by convention, and Python's module cache is keyed
by name alone -- a flat import works when only one test file runs, and silently
imports the wrong provider's module the moment both run in the same process, which
`unittest discover` always does.

## Why this is a separate repository

Somebody has to read a web page. The design question is *where*, and the answer is:
not on the user's machine. A consumer that scraped the page itself would not crash
when the page was redesigned -- it would read the **wrong number** and cheerfully
announce €0.80 for a run that costs €8, in every installation at once. Here, a human
sees the diff before anything reaches anyone.

It also means the price of `mistral-medium-latest` is not one project's private fact,
`git log` on a single JSON file becomes a price history nobody else publishes -- across
every provider, not just one -- and a correction does not require cutting a release
of anything.

The full rationale, and the contract this file is bound by, is
[`docs/ai-pricing-source.md`](https://github.com/Simon-LM/ProcraFiler/blob/main/docs/ai-pricing-source.md)
in [ProcraFiler](https://github.com/Simon-LM/ProcraFiler), the first consumer.

## Licence

[MIT](LICENSE.md).

The prices themselves are facts about other companies' products, published by their
respective providers. They are reproduced here for interoperability, and this
repository claims nothing over them.
