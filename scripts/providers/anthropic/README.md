# Anthropic

`scrape.py` reads two of Anthropic's public documentation pages once a week (Mondays,
04:30 UTC, `.github/workflows/refresh-anthropic.yml`) and works out what
`providers.anthropic` should say. No API key: both pages are public.

| what | source | read as |
| --- | --- | --- |
| every price | <https://platform.claude.com/docs/en/about-claude/pricing> | its tables, found by their header rows, and three sentences |
| each model's API id, and whether it is still sold | <https://platform.claude.com/docs/en/about-claude/model-deprecations> | its status table |

## The prices: the pricing page's tables

The pricing page is rendered on the server, so its tables are in the HTML a fetch gets.
Each table is found by its header rows, word for word, as `mapping.json`'s `tables`
states them. A table that is missing, or found twice, fails the run.

| table | columns | published as |
| --- | --- | --- |
| model pricing | Input, Output, 5m writes, 1h writes, Hits and refreshes | `in_per_mtok`, `out_per_mtok`, `cache_write_5m_per_mtok`, `cache_write_1h_per_mtok`, `cache_read_per_mtok` |
| batch processing | Input, Output | `in_per_mtok_batch`, `out_per_mtok_batch` |
| fast mode | Input, Output | a second entry, `<id> (fast)`, with `in_per_mtok` and `out_per_mtok` |
| Claude Managed Agents | the "Session runtime" row | `managed_agents`, `per_session_hour`, `"kind": "product"` |

Every price cell states its unit, `$N / MTok` (`$N USD / MTok` in the fast mode table).
A cell in any other form fails the run: a figure whose unit may have changed is not
published.

**The batch prices are read, not worked out.** The page states them in a table of their
own, model by model: half the standard rates on 2026-11-10.

**Prompt caching has three prices**: writing to the cache for 5 minutes (1.25 times the
input rate), writing to it for 1 hour (2 times), and reading it back. The page states
all three as figures, model by model, and all three are published. The read rate is
not the same multiple for every model: 0.1 times the input rate for most, 0.05 for
Claude Opus 5.5 and Sonnet 5.5, 0.025 for Claude Fable 5.2 and Fable 5.1.

**Claude Haiku 5.5 is priced by prompt length.** Its rows say "for prompts up to
100,000 tokens" and "for prompts over 100,000 tokens": every price, cache and batch
included, is higher past that size. The second row is published in the `_long_context`
twin of each field, with `long_context_threshold` 100001: a prompt of exactly 100,000
tokens is still billed at the lower prices. The page says the whole request is billed
at the higher prices once its prompt is over the threshold, cache reads and writes
included. On 2026-11-10 no other model was priced this way: Claude 4.6 and later models
bill their whole 1M-token context at the standard rates.

**Fast mode** is a faster output at a higher price, for a few models: $8 / $40 for
Claude Opus 5.5 instead of $4 / $20 on 2026-11-10. It is published as an entry of its
own, `claude-opus-5-5 (fast)`, whose `api_ids` is the model's id: fast mode is a
setting of the request, not another model. The page says the prompt caching and data
residency multipliers apply on top of fast mode prices, and states no figure for them,
so none is published. Fast mode is not available in batch.

## The tools

| key | price on 2026-11-10 | source on the page |
| --- | --- | --- |
| `web_search` | $10 per 1,000 searches, `per_1k_calls` | a sentence |
| `code_execution` | $0.05 per container hour, `per_container_hour` | a sentence |
| `web_fetch` | free, `"free": true` | a sentence |
| `managed_agents` | $0.08 per session hour, `per_session_hour` | a table |

Each sentence is matched by a pattern in `mapping.json`'s `products`. A sentence that is
gone, or found twice with two different figures, fails the run.

**Code execution** lets Claude run code (Python, for one) in an isolated container on
Anthropic's servers, billed by the time the container runs. Two things the format has
no field for, so they are only written here:

- every organisation gets **1,550 free hours a month**; the $0.05 an hour applies
  beyond that;
- code execution is **free when web search or web fetch** (their 2026-02-09 versions or
  later) is in the same request: only the tokens are billed.

A run of code execution is billed for at least 5 minutes, and is billed even if the
tool is never called when files are attached, because they are loaded into the
container.

## The ids: the deprecations page's status table

The pricing page names models ("Claude Opus 4.5") and states no id. The deprecations
page's status table states every current and recently retired model's API id, and its
state: Active, Deprecated or Retired.

A name is matched to its id by the format Anthropic's
[Model IDs and versions](https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions)
page states: `claude-{name}-{major}[-{minor}]`, followed by `-{YYYYMMDD}` for models
before the 4.6 generation. "Claude Opus 4.5" is `claude-opus-4-5-20251101`; "Claude
Opus 5.5" is `claude-opus-5-5`.

**Keys are the API ids the status table states.** A dated id also lists, in `api_ids`,
the shorter alias the same page says the Claude API accepts for it, and which points to
the most recent dated snapshot of that minor version:

```json
"claude-opus-4-5-20251101": {
  "in_per_mtok": 5.0,
  "display_name": "Claude Opus 4.5",
  "api_ids": ["claude-opus-4-5-20251101", "claude-opus-4-5"]
}
```

From the 4.6 generation on, the dateless id is the model itself, not an alias, and the
entry carries no `api_ids`.

**A Retired model is not published**, even when the pricing page still prices it. On
2026-11-10 it priced Claude Opus 4.1, Opus 4 and Sonnet 4, which the status table calls
Retired. A Deprecated model is still callable and is published, until the status table
calls it Retired: the run then marks it `absent_since`, like any entry a source stops
offering. A state `mapping.json`'s `states` does not name fails the run. A priced model
the status table does not list is reported in the run's notes and not published.

## What is not published, and why

- **The models sold only after verification.** The pricing page badges them "(Verification
  required)": on 2026-11-10, Claude Mythos 5, 5.1 and 5.2, which Anthropic sells only to
  organisations it has verified. They are not published, in any table. The badge is read
  from the page, as `restricted_badge` in `mapping.json` names it, not from a list of
  models: a badge taken off publishes the model on the next run, a badge put on holds it
  back, and the run then marks its entry `absent_since`.
- **The 1.1x multiplier for US-only inference.** With `inference_geo: "us"`, Claude 4.6
  and later models cost 1.1 times every price above, cache and the long-context prices
  of Claude Haiku 5.5 included. The default, global routing, costs the prices
  published. A multiplier is a rule, not a price, and doubling every entry to state it
  would add nothing a reader cannot work out from this line. Claude Platform on AWS,
  Claude Models on Google Cloud (Anthropic managed) and Claude in Microsoft Foundry
  (US Data Zone Standard deployments) apply the same 1.1x.
- **The partner clouds** (Amazon Bedrock, Claude Models on Google Cloud managed by
  Google), which set their own prices on their own pages, with a 10% premium for
  regional endpoints. The platforms Anthropic operates (Claude Platform on AWS, Claude
  in Microsoft Foundry) bill at the prices published here.
- **The prompt caching multipliers on fast mode**, stated as a rule with no figure.
- **Tokens added by tools** (the tool use system prompt, the bash and text editor tool
  definitions): they are billed at the model's input price, which is published.

## Checked on 2026-11-10

Against the page, by a human: every model row of the model table, the batch table, the
fast mode table, the four tools. The tests check every flat row of the model table
against what is published, and Claude Haiku 5.5's two rows field by field.

Perplexity resells seven of these models through its Agent API; on 2026-11-10 each had
the same input, output and cache read prices there as here (`providers.perplexity`, keys
`anthropic/<id>`).
