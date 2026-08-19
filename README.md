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

| provider | entries | on sale | currency | what it is |
| --- | --- | --- | --- | --- |
| `mistral` | 34 | 27 | USD | called directly |
| `ovh` | 19 | 19 | EUR | called directly |
| `edenai` | 105 | 104 | USD | resold, 5 upstreams |
| `huggingface` | 15 | 15 | USD | routed, 2 partners |

The difference between the two counts is entries carrying `absent_since`: models the
source has stopped offering, kept for a year with the last prices actually observed.
See [Models that go away](#models-that-go-away).

`huggingface` is keyed by `<model>:<partner>` rather than by model, because the router
serves the same model through both covered partners at different prices —
`gpt-oss-120b` is $0.09 per million input tokens via OVHcloud and $0.171 via Scaleway.
`huggingface` covers those two partners; the other three providers are covered in
full.

## Read this before you trust a number in it

**Most of these figures are scraped from public web pages**, one scraper per provider.
There is no pricing endpoint in Mistral's API, and the one usage endpoint that returns
a `prices` field requires an *admin* API key that an ordinary user of an open-source
tool does not have and should never be asked for. So Mistral's models are read from
its documentation site, <https://docs.mistral.ai/models>, which does at least state a
machine-readable price object per model; its billable non-models are read from
<https://mistral.ai/pricing/api>, which is the only place they are priced. OVH's
catalog is read the same way -- a Next.js app that embeds its model list and pricing
as JSON inside the page.

**One provider, two sources, on purpose.** Mistral's pricing page had dropped Voxtral
Mini Transcribe 2 while Mistral was still selling it, and this file published it as
withdrawn until the documentation site was read alongside. A marketing page is not an
inventory. See [scripts/providers/mistral/README.md](scripts/providers/mistral/README.md).

Eden AI is the exception and the sturdiest source here: a real public JSON API,
<https://api.edenai.run/v3/models>, stating an input and an output price per model
under explicit field names, with no label to string-match and no layout to latch
onto. Being an aggregator, its prices are what **Eden** charges to forward a call,
so they differ from the same model's price under `providers.mistral` or
`providers.ovh` — by design, not by error.

Each provider's own `README.md` under `scripts/providers/<name>/` explains that
provider's specific situation.

**Everything publishes automatically.** The weekly job for each provider commits
whatever it read, straight to `main`: a moved price, a model the source has added, a
model it has withdrawn. There is no review gate and no hand-written list of models to
reconcile first, because none of that is a decision anyone makes -- the provider
already changed it, and holding the new file back only means shipping one known to be
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
        "mistral medium 3.5": {
          "in_per_mtok": 1.5,
          "out_per_mtok": 7.5,
          "display_name": "mistral medium 3.5"
        },
        "voxtral small": {
          "in_per_mtok": 0.1,
          "out_per_mtok": 0.4,
          "per_audio_minute": 0.004,
          "display_name": "voxtral small"
        },
        "mixtral 8x7b": {
          "in_per_mtok": 0.7,
          "out_per_mtok": 0.7,
          "display_name": "mixtral 8x7b",
          "absent_since": "2026-08-17"
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
| `providers.<name>.models` | Keyed by **whatever identifies the entry at that source**, which is not the same thing at every provider -- see below. |
| `display_name` | A human-readable label, kept only so that a diff is readable. Never use it for matching. |
| `free` | Present, and always `true`, when the provider gives the model away. The entry then carries **no price field at all** -- see below. |
| `kind` | Absent on a model, which is the normal case. `"product"` marks a billable thing that is **not** a model: Mistral's web search, code execution and image generation are priced on the same page as its models. Filter on it if you are listing models to call. |
| `absent_since` | Absent on a model still on sale, which is the normal case. When present, the source has stopped offering this entry as of that day, and **every price beside it is the last one observed, not a current one**. See below. |

**The key is whatever the source itself states, per provider.** There is no single
convention because there is no single source:

| provider | key | example |
| --- | --- | --- |
| `ovh` | the callable API model id, from the catalog's own `name` | `Qwen3.6-27B` |
| `edenai` | Eden's model id, upstream prefix included | `mistral/codestral-latest` |
| `huggingface` | the exact string the router takes | `openai/gpt-oss-120b:ovhcloud` |
| `mistral` | **the name its source states**, lowercased | `mistral medium 3.5`, `ocr 4.0` |

Mistral is the odd one. Its documentation pages do carry the callable ids, alongside
their aliases (`"names": ["mistral-ocr-4-1", "mistral-ocr-4", "mistral-ocr-latest"]`),
but publishing them is a separate decision that has not been taken, so this block is
keyed the way its sources name things. If you call Mistral's API, resolve the id
yourself at `docs.mistral.ai/models/<slug>` -- and note that every `-latest` id is an
alias whose meaning changes without warning.

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
| `per_1k_annotated_pages` | per thousand pages that are also annotated (OCR bills these higher) |
| `per_1k_chars` | per thousand characters |
| `per_audio_minute` | per minute of audio sent |
| `per_audio_second` | per second of audio sent |
| `per_call` | per API call |
| `per_1k_calls` | per thousand API calls |
| `per_1k_images` | per thousand images |
| `per_image` | per image |
| `per_mchars` | per million input characters |
| `per_model_month` | per month, per stored model |

**A free model is `"free": true`, never a price of `0`.** Zero is also exactly what a
broken parser reads off a page whose layout moved, and this repository refuses a
figure of 0 for that reason. Publishing free models as a marker rather than a number
is what lets that check stay strict. A consumer must treat an entry with `free` as
costing nothing, not as missing data.

**A model may carry several of them at once.** `voxtral small` above is billed both
per minute of audio and per million tokens of text, and reading only one of the two
undercounts a bill without ever looking wrong. Do not assume one unit per model:
iterate the keys you find.

## Models that go away

Sources retire models constantly. When one disappears, its entry is **not** deleted:
it keeps the last prices that were actually observed, unchanged, and gains

```json
"absent_since": "2026-08-17"
```

the day the source was first seen without it. The entry is dropped a year later. If
the source starts offering it again, the field disappears and the prices are refreshed
like any other.

Deleting on sight would be the obvious automatic behaviour and it is the wrong one: a
consumer still naming that model would lose its price with no warning and no way to
look up what it used to be, and a price history would develop a hole exactly where a
comparison is most interesting. A year rather than a few weeks because not every
project reading this file is actively maintained.

**A price under `absent_since` is a last known price, not a current one.** That is the
entire point of the field, and a consumer that ignores it will quote a figure nobody
sells any more. Two things worth doing with it: exclude those entries when listing
models a user can pick, and show the date when displaying a price you found under one.

A disappearance also sends one email, on the run that first observes it — see
[How the weekly job behaves](#how-the-weekly-job-behaves).

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

**An id can keep its name and change its price.** `mistral-medium-latest` at Mistral,
`grok-latest` at Eden AI and every other `-latest` id is an **alias**: today it
resolves to one model, tomorrow to a different one at a different price. Eden AI's
`xai/grok-latest` moved from Grok 4.5 to Grok 4.6 in a single week, and in this file
that shows up as a price change on an unchanged key -- which is exactly what it is.
Nothing here can warn you that the model behind an alias is not the one you tested.

Each provider's `mapping.json` still holds one hand-written thing, and it is worth
knowing which: **not a list of models** -- those are read from the source every week --
but a translation table for the things the source states in prose rather than in data.
OVH's maps `million_input_tokens` to `in_per_mtok`; Mistral's maps the row label
`Input (/M tokens)` to the same field. A unit or a label the table does not recognise
is reported and its figure left unpublished, never guessed at, because the unit is
part of the field name and there is no honest field to put an unrecognised figure in.

Those tables are assumptions a human wrote down, and they are the thing most likely to
be quietly wrong. They are written down explicitly so that they *can* be checked.

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
  three outcomes as Mistral's (unchanged -> stamp, changed -> commit, failure -> issue);
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
| anything changed | the new file is committed straight to `main`, with a `::notice::` on the run |
| fetch or parse failed | an issue is opened **and assigned to the repository owner**, the job fails, and `pricing.json` is left **untouched** |

"Anything changed" is deliberately wider than "a price moved": a model added, a model
withdrawn and a model renamed all land in the same outcome and all publish. The one
thing that stops a run is not knowing whether the figures can be trusted, so each job
refuses rather than guesses whenever:

- the page cannot be fetched, or no longer parses;
- a figure lands outside the plausible range for its unit, or moves by more than a
  factor of 5;
- the same identifier appears twice, so which figure is the real one is ambiguous;
- the page stops publishing a figure in the currency that provider's block declares.

Note what is **not** on that list: the set of models. A model appearing or vanishing
says nothing about whether the other prices were read correctly, so it never withholds
them. It is published, and it sends an email.

### The three emails

All of them arrive by the same mechanism -- an issue assigned to the repository owner,
which notifies through a channel that is on by default and does not depend on watching
the repository.

| when | what |
| --- | --- |
| a scrape fails | an issue per outage, per provider, commented on rather than duplicated on repeated failure. `pricing.json` untouched. |
| a source's inventory changes | an issue naming what appeared, disappeared or was dropped after a year. Raised **once**, by the run that observes it, never repeated while the entry sits there marked. Nothing is broken and nothing needs fixing; it is a notification. |
| every Monday at 08:00 UTC | one report, whether or not anything is wrong, saying which providers refreshed and which did not. Closed automatically when all four did. |

The weekly report exists because the other two report by exception, and no exception
can be raised by a workflow that never ran -- disabled by GitHub after 60 idle days,
broken by a bad edit to its own YAML, or silently skipped. `pricing.json` would just
stop moving while looking exactly as it always did. A receipt that arrives every Monday
is worth more than silence, because a receipt that *doesn't* arrive is itself the
alarm, and that is the one alarm no code in this repository could raise.
[`scripts/weekly_report.py`](scripts/weekly_report.py) reads the four `checked_utc`
stamps and nothing else; it writes no file and has no permission to.

All four providers' workflows share one `concurrency` group, `pricing-json-writes`,
since they all commit to the same file: a second provider's run queues behind the first
rather than racing it. The weekly report is deliberately not in that group -- it writes
nothing, so it has no reason to queue behind a scrape and every reason to still run if
one is stuck.

**Refusing to publish a wrong number is only half the job.** A scraper notices when a
model it used to publish disappears from a page, and is completely silent about a model
the provider sells that its page never mentions -- to a scraper reading one source, an
absent row looks exactly like a model that does not exist. OVH's catalog page dropped
five models it was still serving on 2026-08-04, and nothing in the scrape could have
noticed. So OVH's
workflow also runs
[`scripts/providers/ovh/check_coverage.py`](scripts/providers/ovh/check_coverage.py),
which compares the catalog page against OVH's own public model list and reports what
each one has that the other does not. It reads no price, never touches
`pricing.json`, and a gap is a warning rather than a failed job: refusing to publish
nineteen correct prices because a twentieth is missing would also suppress the
`checked_utc` stamp that keeps the schedule alive.

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
python3 -m unittest discover -s tests -v                          # 231 tests
python3 scripts/providers/mistral/scrape.py --out-dir .ci-out     # read the live sources
python3 scripts/providers/mistral/scrape.py --out-dir .ci-out --offline tests/fixtures/mistral/offline.json

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

**You don't.** A model a source has started offering is published by that source's next
weekly run, without anyone doing anything. That is the whole design: a hand-written
model list is what used to make a single retired model block every other price in its
block, and there is no longer one to add to.

What still needs a human is a source stating something the file has no way to express:

1. **A new unit.** If the source prices something per week, or per gigabyte, the run
   reports it and publishes nothing for that one figure. Add the unit to
   `KNOWN_PRICE_FIELDS` in
   [`scripts/pricing_validate.py`](scripts/pricing_validate.py) -- shared by every
   provider -- and check whether the default floor of 0.001 still makes sense for it.
   A unit whose prices are naturally small needs its own entry in `PRICE_BOUNDS`, or an
   ordinary price cut will be refused as a parsing accident. A test enforces that every
   published figure keeps a factor of 5 of room above its floor, so you will be told
   rather than left to find out on a Monday morning.
2. **A new way of naming that unit at one source.** Add it to that provider's
   `mapping.json`: OVH's `units` maps a catalog `price_unit` to a field, Mistral's
   `denominators` maps a docs unit string to one and its `rows` maps a pricing-page row
   label, Eden AI's and Hugging Face's `fields` map an API field name. Copy the string
   byte-for-byte from the source.
3. Re-capture that provider's fixtures (Mistral's are under `tests/fixtures/mistral/`,
   listed by `offline.json`; OVH's is `tests/fixtures/ovh/catalog_ok.html`) so they
   contain the new shape, and regenerate its `baseline.json` from them.
4. Run the tests. `tests/test_pricing_validate.py` checks the shared schema,
   `tests/test_provider_runner.py` the reconciliation every provider shares,
   `tests/test_nextjs_flight.py` the payload reader the two Next.js sources share, and each
   provider's `tests/scraper_tests/test_<name>.py` has `TestPublishedFileMatches<Name>`
   and `TestMapping` classes that check that provider's own files agree with each
   other -- including that its mapping still contains no model list.

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
for everything else: it already handles reading, merging, writing, the three outcomes,
and keeping withdrawn entries for a year. It only needs a provider id and an
`extract_new_models(fetch, mapping)` callback returning `(models, notes)` -- `fetch`
being a url-to-text callable, so a provider that needs several pages (as Mistral does)
reads them all through the same offline-testable path --
`models` being everything the source offers *today*, with no reference to what is
already published. Write a mapping that names units and labels, never models, and give
the new provider its own `refresh-<name>.yml` sharing the `pricing-json-writes`
concurrency group.

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
announce €0.80 for a run that costs €8, in every installation at once. Here, the page
is read in one place, checked against bounds that stop a misparse from ever being
committed, and every change is a dated line in `git log` that a human can go back and
read.

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
