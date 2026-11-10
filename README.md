# ai-pricing

Machine-readable prices of AI models and APIs, as each provider publishes them: one
JSON file at one URL, with its JSON Schema beside it, checked every day.

```text
https://raw.githubusercontent.com/Simon-LM/ai-pricing/main/pricing.json
https://raw.githubusercontent.com/Simon-LM/ai-pricing/main/pricing.schema.json
```

Prices are grouped by **provider**, not by model, because the same model is a different
bill depending on who you pay: DeepSeek V4.1 Flash is $0.30 / $1.20 per million tokens
at Mistral and €0.60 / €1.80 at Scaleway. Both figures are in the file, each under its
own provider.

## Providers

| key | what it covers | currency | read from | details |
| --- | --- | --- | --- | --- |
| `anthropic` | Claude models, fast mode, tools | USD | pricing and model deprecations pages | [README](scripts/providers/anthropic/README.md) |
| `openai` | GPT models at every speed, audio, images, tools | USD | pricing page, as Markdown | [README](scripts/providers/openai/README.md) |
| `google` | Gemini API: Gemini, Gemma, Lyria, grounding | USD | pricing page, as Markdown | [README](scripts/providers/google/README.md) |
| `googlecloud` | Google Cloud Speech-to-Text | USD | pricing page | [README](scripts/providers/googlecloud/README.md) |
| `mistral` | Mistral models, Voice Agents | USD | documentation site | [README](scripts/providers/mistral/README.md) |
| `ovh` | OVHcloud AI Endpoints | EUR | catalog page | [README](scripts/providers/ovh/README.md) |
| `scaleway` | Scaleway Generative APIs | EUR | public catalogue API | [README](scripts/providers/scaleway/README.md) |
| `xai` | Grok, image, video and voice models, tools | USD | pricing page | [README](scripts/providers/xai/README.md) |
| `perplexity` | Agent API (with resold models), Search API, tools | USD | pricing and models pages | [README](scripts/providers/perplexity/README.md) |
| `edenai` | models resold by Eden AI | USD | public API | [README](scripts/providers/edenai/README.md) |
| `huggingface` | models routed by Hugging Face to OVHcloud | USD | public router API | [README](scripts/providers/huggingface/README.md) |

Every provider is read from its own public pages or API, with no API key.

## Reading a price

A price is read by provider, then by the key that provider's source states, usually the
API model id:

```json
"anthropic": {
  "checked_utc": "2026-11-10T12:25:30Z",
  "currency": "USD",
  "models": {
    "claude-sonnet-5-5": {
      "in_per_mtok": 2.0,
      "out_per_mtok": 10.0,
      "cache_read_per_mtok": 0.1,
      "cache_write_5m_per_mtok": 2.5,
      "cache_write_1h_per_mtok": 4.0,
      "in_per_mtok_batch": 1.0,
      "out_per_mtok_batch": 5.0,
      "display_name": "Claude Sonnet 5.5"
    }
  }
}
```

```python
import json, urllib.request

URL = "https://raw.githubusercontent.com/Simon-LM/ai-pricing/main/pricing.json"
block = json.load(urllib.request.urlopen(URL))["providers"]["anthropic"]
model = block["models"]["claude-sonnet-5-5"]
print(model["in_per_mtok"], block["currency"], "per million input tokens, as of", block["checked_utc"])
```

What to know before using one:

- **The unit is in the field name.** `in_per_mtok` is per million input tokens,
  `per_audio_minute` per minute of audio. A model can carry several: read every field
  you find. There is no generic `price` field.
- **The currency is the block's.** Nothing here converts one currency to another.
- **A price has a date.** Show it with the price: "≈ $2.00 (Anthropic rates of
  2026-11-10)", never a bare "$2.00".
- **Some entries are no longer live quotes.** `absent_since`: the provider stopped
  selling it, and the price beside it is the last one seen. `unpriced_since`: still sold,
  but no price is stated. `disputed_since`: the source states two prices, a human must
  pick. Leave these out of a list of models to choose from.
- **A free model is `"free": true`**, never a price of 0.
- **An alias can change price without changing name.** `mistral-small-latest` points to
  whatever model Mistral decides, at that model's price.

Every field, unit and key is described in [docs/format.md](docs/format.md).

## How it stays current

Each provider has its own GitHub workflow, which reads its pages every day, between
02:30 and 07:30 UTC. A price that changed is published that morning. A model the
provider added is published, one it withdrew is kept with its last price and marked
`absent_since`. A run that cannot read its source, or reads a figure that looks wrong,
publishes nothing and opens an issue instead. Every Monday, a report says which
providers were checked.

How it works, and why it refuses rather than guesses:
[docs/how-it-works.md](docs/how-it-works.md).

## Before you trust a number

- **Most figures are read from public web pages**, not from an official feed; two come
  from public JSON APIs. A page can change faster than this file notices.
- **There is no guarantee of any kind**: not of accuracy, freshness or continued
  existence. If money depends on the answer, check the page each block names in
  `source`.
- **This project is not affiliated with any provider.**

## Documentation

| document | for |
| --- | --- |
| [docs/format.md](docs/format.md) | reading the file: fields, units, keys, markers, the schema |
| [docs/how-it-works.md](docs/how-it-works.md) | how the daily checks run, publish, refuse and report |
| [docs/contributing.md](docs/contributing.md) | running it yourself, reporting an error, proposing a provider or a field |
| `scripts/providers/<key>/README.md` | what each provider's pages say, and what is not published |

## Contributing

A wrong price or a mistake in the documentation can be fixed directly in a pull request.
For a new provider, a new field or a new unit, please open an issue first: each one
becomes part of a public format other projects rely on, and talking it over first
avoids work that cannot be merged. See [docs/contributing.md](docs/contributing.md).

## Licence

[MIT](LICENSE.md).

The prices themselves are facts about other companies' products, published by their
respective providers. They are reproduced here for interoperability, and this
repository claims nothing over them.
