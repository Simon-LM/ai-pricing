# How it works

How `pricing.json` is kept current: where each price is read, what a daily run does,
when it refuses to publish, and how it tells a human.

## One block, one reader, per provider

Each provider has, and keeps to itself:

- a scraper, `scripts/providers/<key>/scrape.py`, which reads only that provider's
  public pages or API and works out only `providers.<key>`;
- a mapping, `scripts/providers/<key>/mapping.json`, written by a human: how the
  source states its units, its tables, its sentences, translated into this file's
  fields. **It holds no list of models**: the source says which models exist, every day;
- a workflow, `.github/workflows/refresh-<key>.yml`, which runs that scraper every day;
- its own `checked_utc`, `updated`, `source` and `currency` in the file.

What all of them share is written once, in
[`scripts/provider_runner.py`](../scripts/provider_runner.py): reading the published
file, comparing, keeping withdrawn entries, writing the candidates. A scraper supplies
only how to read its source. A run only ever rewrites its own block; every other block
is carried through byte for byte, in the same order, so a diff shows only what moved.

No scraper is given an API key, ever. Every source is public:

| how the prices are stated | providers |
| --- | --- |
| a public JSON API | Eden AI, Hugging Face's router, Scaleway's product catalogue |
| data embedded in a page (a Next.js or Mintlify payload) | Mistral's documentation, OVH's catalog, xAI's pricing page, Perplexity's pricing calculator |
| a page's Markdown version | OpenAI, Google's Gemini API |
| tables and sentences of a rendered page | Anthropic, Google Cloud, Perplexity's models page |

Each provider's own README says which page, what is read on it, and what is not
published and why.

## A day's run

Each workflow runs every day at its own time, half an hour apart so that no two start in
the same minute: Google Cloud at 02:30 UTC, Scaleway 03:00, Google 03:30, Mistral 04:00,
Anthropic 04:30, OVH 05:00, OpenAI 05:30, Eden AI 06:00, Perplexity 06:30, Hugging Face
07:00, xAI 07:30. All of them also run on demand, from the Actions tab. All of them share
one concurrency group, `pricing-json-writes`, since they write the same file: a second
run waits for the first instead of racing it.

A run has three outcomes, never a fourth:

| outcome | what happens |
| --- | --- |
| nothing changed | nothing is committed, except on Mondays and on a run started by hand, when the block's `checked_utc` stamp is committed |
| something changed | the new file is committed to `main`, the same morning |
| a check failed | nothing is written, the job fails, and an issue is opened |

"Something changed" is wider than "a price moved": a model added, withdrawn or renamed
publishes too. **There is no review gate.** The provider already changed its price;
holding the new file back would only publish a figure known to be stale. What a reviewer
would catch, a misread figure, is caught earlier and harder, below.

## When a run refuses to publish

A run publishes nothing, and fails, whenever it cannot trust what it read:

- the page cannot be fetched, or no longer has the shape the scraper reads: a table is
  gone, a column was renamed, a sentence was reworded;
- a figure is stated in a unit the mapping does not know, or in a form the scraper
  cannot place;
- a figure lies outside the plausible range for its unit, or moved by more than a factor
  of 5 since the last run;
- an entry is priced twice where the source states one price, so which figure is real
  is unknown (a source known to list some models twice, as Eden AI does, holds that
  entry instead, below);
- the block's currency changed;
- the file about to be committed fails the tests that describe a published file. These
  run on the candidate, after the scrape and before the commit: on 2026-09-14, testing
  the old file instead let a block its own tests reject reach `main`.

**A model appearing or disappearing is not on that list.** It says nothing about whether
the other prices were read correctly, so it never holds them back: it is published, and
reported.

**What a run cannot read alone is held, not guessed.** A model listed twice at two prices
keeps its last published price under `disputed_since` until a human picks; a figure in a
unit the mapping does not know is reported and left out; the rest of the block publishes.

Every figure that reaches `main` is a dated commit: `git log -- pricing.json` is the
history of every price move, with the figures in the diff.

## How a human hears about it

Every message is an issue assigned to the repository owner, which GitHub sends by email
without anyone watching the repository.

| when | what |
| --- | --- |
| a run fails | one issue per outage and per provider, commented on each day it keeps failing; `pricing.json` untouched |
| what a source offers changes | one issue naming what appeared, disappeared, went unpriced or was dropped after a year, raised once by the run that sees it |
| every Monday at 08:00 UTC | one report, whether or not anything is wrong, saying which providers were checked that morning; closed when every one was |

**The Monday report exists because silence proves nothing.** A workflow that never ran,
disabled by GitHub after 60 idle days, broken by an edit to its own YAML or never
scheduled, raises no failure: the file just stops moving. A report that arrives every
Monday is the alarm for that case, because a report that does not arrive is itself the
alarm. [`scripts/weekly_report.py`](../scripts/weekly_report.py) reads the stamps and
nothing else.

**The Monday stamp keeps the schedules alive.** GitHub disables a scheduled workflow
after 60 days without a commit, and only commits reset that timer. Each block's stamp,
committed every Monday, is both a statement to consumers ("verified unchanged today")
and what keeps its workflow running. On the other days an unchanged run commits nothing,
so the history holds one stamp a week per provider rather than seven.

**A missing model is looked for too, where a second list exists.** A scraper sees a model
disappear from its page, but not a model the provider sells that its page never names.
OVH's workflow also compares its catalog page with OVH's public list of models and
reports what one has and the other does not, without holding back any price.

## Why a repository, and not a scraper in each program

Someone has to read a provider's page; the question is where. A program that read the
page itself would not crash when the page was redesigned: it would read the **wrong
number** and show it, in every installation at once. Here, the page is read in one
place, checked against bounds that stop a misread figure from being committed, and every
change is a dated line a human can read back.

It also makes a price a public fact rather than one project's private one, turns
`git log` on one JSON file into a price history across providers, and lets a correction
reach every consumer without anyone shipping a release.

The rationale and the contract this file is bound by are written up in
[ProcraFiler's `docs/ai-pricing-source.md`](https://github.com/Simon-LM/ProcraFiler/blob/main/docs/ai-pricing-source.md),
the first program to read it.

## What this repository does not do

- **Convert currencies.** A block is in the currency its provider states.
- **Estimate a bill.** It knows prices, not volumes or token counts.
- **Touch anyone's account.** It reads public pages only, and holds no key.
