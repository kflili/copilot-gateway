# 2026-04-26 — Gemini reasoning_effort support & gateway API shapes

Recorded findings from a live probe against the local gateway
(`http://localhost:8787`, forwarding to `api.enterprise.githubcopilot.com`).

## 1. Gateway API surface

The gateway exposes **three API shapes** simultaneously on a single port and
forwards to the matching Copilot upstream path. `/v1/` is stripped before
forwarding for chat/responses (Copilot wants no prefix there).

| Local path                                | API shape                       | Typical client                          |
|-------------------------------------------|---------------------------------|-----------------------------------------|
| `POST /v1/messages` (+ `/count_tokens`)   | Anthropic Messages API          | Claude SDK, Claude Code                 |
| `POST /v1/chat/completions` (alias `/chat/completions`) | OpenAI Chat Completions | OpenAI SDK, most third-party tooling    |
| `POST /v1/responses` (alias `/responses`) | OpenAI Responses API            | GPT-5.x with `output_config.effort`     |
| `GET  /v1/models` (alias `/models`)       | model listing                   | discovery                               |

The gateway does **not** steer requests per model — the client picks the API
shape that matches the model.

## 2. Gemini `reasoning_effort` support

### `/v1/responses` (Responses API)

All Gemini models tested return HTTP 400:

```
gemini-2.5-pro            → "model gemini-2.5-pro is not supported via Responses API."
gemini-3-flash-preview    → "model gemini-3-flash-preview is not supported via Responses API."
gemini-3.1-pro-preview    → "model gemini-3.1-pro-preview does not support Responses API."
```

So `output_config.effort` (the Responses-API field) is not reachable for Gemini.

### `/chat/completions` with `reasoning_effort: "low" | "medium" | "high"`

| Model                     | low | medium | high | no effort |
|---------------------------|:---:|:------:|:----:|:---------:|
| `gemini-2.5-pro`          | ❌  | ❌     | ❌   | ✅        |
| `gemini-3-flash-preview`  | ✅  | ✅     | ✅   | ✅        |
| `gemini-3.1-pro-preview`  | ✅  | ✅     | ✅   | ✅        |

`gemini-2.5-pro` rejects with:
```
reasoning_effort "<level>" was provided, but model gemini-2.5-pro does not
support reasoning effort
```

The gateway forwards `reasoning_effort` / `output_config.effort` unchanged
(no per-model allowlist) and augments upstream `invalid_reasoning_effort`
errors with a "Run /effort to pick a supported level" hint
(`gateway.py` ~lines 728–782).

## 3. Real-world usage (from `logs/2026-04-*`, ~10k requests)

Endpoint mix:

| Endpoint                       | Calls |
|--------------------------------|------:|
| `/v1/messages`                 | 8 849 |
| `/v1/messages/count_tokens`    |   782 |
| `/responses`                   |    12 |
| `/chat/completions`            |    12 |

Model mix:

| Model                       | Calls | API used        |
|-----------------------------|------:|-----------------|
| `claude-opus-4-6`           | 6 948 | `/v1/messages`  |
| `claude-sonnet-4-6`         | 1 625 | `/v1/messages`  |
| `claude-haiku-4-5-20251001` |   981 | `/v1/messages`  |
| `claude-opus-4-7`           |    76 | `/v1/messages`  |
| `gpt-5.4`                   |     3 | `/responses`    |
| Gemini variants             |   7 ea| mixed (probes)  |

## 4. Practical guidance

- **Claude models** → use Anthropic shape: `POST /v1/messages` (streaming
  supported via `?beta=true`). This is how Claude Code and Anthropic SDK
  clients hit the gateway, and it accounts for the vast majority of traffic.
- **Gemini** → use OpenAI Chat Completions shape: `POST /chat/completions`.
  For reasoning effort, set top-level `reasoning_effort` (not
  `output_config.effort`), and only on `gemini-3-flash-preview` /
  `gemini-3.1-pro-preview`. `gemini-2.5-pro` does not support it.
- **GPT-5.x** → use Responses shape: `POST /v1/responses` with
  `output_config.effort`.
