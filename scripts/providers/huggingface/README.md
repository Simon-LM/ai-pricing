# Hugging Face (Inference Providers router)

`scrape.py` reads Hugging Face's public router listing every day (07:00 UTC,
`.github/workflows/refresh-huggingface.yml`) and works out what `providers.huggingface`
should say. No API key: the listing is public.

| what | source | read as |
| --- | --- | --- |
| every route the router serves, with its price | <https://router.huggingface.co/v1/models> | JSON: model id, partner, status, input and output price |

`mapping.json` names the partners covered, `partners`, and the price fields; nothing
else. A route a covered partner starts serving is published on the next run.

## A route, not a model

Hugging Face does not serve models itself: it routes a call to a partner that does, and
one model can be served by several partners at different prices. So a key is a
`<model id>:<partner>` pair, the exact string the router takes:

```json
"openai/gpt-oss-120b:ovhcloud": { "in_per_mtok": 0.09, "out_per_mtok": 0.47 }
```

**These are prices through the router**, not the partner's own. OVHcloud's catalog
prices `Qwen3.6-27B` at €0.40 per million input tokens (`providers.ovh`); the router
quotes $0.47 for the same model on the same partner. Both are correct: they are two
bills.

**USD, per million tokens**, as the router states them; no scaling is applied. The
`ovhcloud` routes track OVH's own EUR catalog at one exchange rate (0.40 → 0.47,
0.91 → 1.01, 0.60 → 0.71), which pins both the unit and the currency, and a test checks
that band so a silent change to either breaks the build rather than the file.

## Partners covered

| partner | state on 2026-11-10 |
| --- | --- |
| `ovhcloud` | served: its routes are published |
| `scaleway` | not served since 2026-11-09 |

**The router stopped routing to Scaleway on 2026-11-09.** A call answers HTTP 402,
"Pay-as-you go is not enabled for provider scaleway yet", and the listing holds no
Scaleway route. Those routes stay in the file, marked `absent_since`, so a program
that still names one finds out; they are dropped a year later. Scaleway's own prices are
under `providers.scaleway`. The partner stays in `mapping.json`: if the router serves
it again, its routes come back on the next run.

## Routes that change

- **A route no longer served**, or no longer marked `live`, keeps its last prices and
  gains `absent_since`: a price for a call that cannot be served is not a current price.
- **A route the router will not price** is published with `unpriced_since` and no price:
  no `pricing` object, a missing `input` or `output`, a price that is not a number, or a
  `0` on a route the router itself marks `is_free: false`. Input and output are two
  halves of one price, so one without the other publishes nothing. The marker goes the
  day the router quotes a usable price.
- **A route the router marks `is_free`** is published `"free": true`, with no price.
- **If every route is unpriceable, the run fails**: that is a listing this scraper no
  longer understands, not one bad route.

A figure out of its unit's bounds, or one that moved by more than a factor of 5, still
fails the run: an unpriceable route is never a way around that guard.

## What is not published, and why

- **Partners not covered** (every partner but the two above): their routes are ignored.
  Adding one is a one-line change to `partners`.

## Running it yourself

```sh
python3 scripts/providers/huggingface/scrape.py --out-dir .ci-out          # live
python3 scripts/providers/huggingface/scrape.py --out-dir .ci-out --html tests/fixtures/huggingface/models_ok.json
```
