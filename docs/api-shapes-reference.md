# Copilot Gateway — API Shapes Quick Reference

A stable cheat-sheet for developers and AI agents working with this gateway.
For dated probe results / raw measurements see the `docs/YYYY-MM-DD-*.md` notes.

The gateway listens on `http://localhost:8787` and proxies to
`api.enterprise.githubcopilot.com`. It exposes **three API shapes
simultaneously**; the client picks the shape, the gateway forwards as-is.

---

## TL;DR mental model

`/chat/completions` and `/responses` are **two API envelopes in front of the
same model fleet**. The differences are historical, not about the models
themselves:

- `/chat/completions` — OpenAI's original (2023) chat API. Stateless,
  `messages[]` in / `choices[]` out. Became the de-facto cross-vendor
  standard, so almost every provider (OpenAI, Anthropic, Google, Cerebras,
  Fireworks, …) implements it. Lowest-common-denominator control surface.
- `/responses` — OpenAI's newer (2024) Responses API. Designed after
  reasoning models and agentic tool use existed, so it has first-class slots
  for reasoning effort, reasoning summaries, server-side built-in tools,
  typed streaming events, and optional server-side conversation state.
- `/v1/messages` — Anthropic's native Messages API. Predates and exceeds
  chat-completions for Claude (cache_control, thinking blocks, tool_use
  blocks, etc.). Used by Claude Code and the Anthropic SDK.

Same weights run behind each shape. Pick the envelope that exposes the knobs
you need **and** that the model is actually wired into.

---

## Endpoints exposed by the gateway

| Local path                                      | API shape                  | Forwarded to upstream      | Typical client                       |
|-------------------------------------------------|----------------------------|----------------------------|--------------------------------------|
| `POST /v1/messages` (+ `/count_tokens`)         | Anthropic Messages         | `/v1/messages`             | Claude SDK, Claude Code              |
| `POST /v1/chat/completions` (alias `/chat/completions`) | OpenAI Chat Completions | `/chat/completions`        | OpenAI SDK, most third-party tools   |
| `POST /v1/responses` (alias `/responses`)       | OpenAI Responses           | `/responses`               | GPT-5.x with `output_config.effort`  |
| `GET  /v1/models` (alias `/models`)             | model listing              | `/models`                  | discovery                            |

Notes:
- `/v1/` prefix is stripped before forwarding for chat / responses (Copilot
  upstream wants no prefix on those).
- The gateway does **not** steer requests per model; the client must call the
  shape that the model supports.
- `output_config.effort` and `reasoning_effort` are forwarded unchanged —
  there is no per-model allowlist. Upstream rejects unsupported combinations
  with a 400; the gateway augments `invalid_reasoning_effort` errors with a
  "Run /effort to pick a supported level" hint
  (`gateway.py` ~lines 728–782).

---

## Capability matrix: `/chat/completions` vs `/responses`

| Capability                                       | `/chat/completions` (legacy) | `/responses` (newer) |
|--------------------------------------------------|:----------------------------:|:--------------------:|
| Stateless single turn                            | ✅                           | ✅                   |
| Server-side conversation state                   | ❌                           | ✅                   |
| Reasoning effort                                 | top-level `reasoning_effort` (model-dependent) | `output_config.effort` (low/medium/high + summaries) |
| Reasoning summaries / encrypted reasoning items  | ❌                           | ✅                   |
| Built-in server tools (web_search, file_search, computer_use, image_generation) | ❌ | ✅ |
| Function calling                                 | ✅                           | ✅                   |
| Streaming                                        | text deltas only             | typed events (`response.output_item.added`, `reasoning.delta`, …) |
| Multi-modal output / structured refusals         | limited                      | first-class          |

Rule of thumb: if the model supports both, prefer `/responses` when you need
reasoning controls or built-in tools; use `/chat/completions` for portability.

---

## Per-model endpoint support (from `/models` → `supported_endpoints`)

Snapshot — re-check with `curl -s http://localhost:8787/models`.

| Model family                                         | `/chat/completions` | `/responses` | `/v1/messages` |
|------------------------------------------------------|:-------------------:|:------------:|:--------------:|
| `claude-*` (sonnet/opus/haiku, all versions)         | ✅                  | ❌           | ✅             |
| `gemini-3.1-pro-preview`, `gemini-3-flash-preview`, `gemini-2.5-pro` | ✅ | ❌       | ❌             |
| `gpt-5.4`, `gpt-5.2`, `gpt-5-mini`                   | ✅                  | ✅           | ❌             |
| `gpt-5.5`, `gpt-5.4-mini`, `gpt-5.2-codex`, `gpt-5.3-codex` | ❌           | ✅           | ❌             |
| `minimax-m2.5`, Fireworks routers                    | ✅                  | ❌           | ❌             |

Implications:
- **Some GPT-5 variants are Responses-only** — calling `gpt-5.5`,
  `gpt-5.4-mini`, or the Codex variants on `/chat/completions` returns 400.
- **Gemini on Copilot is `/chat/completions` only** — Responses API is
  unavailable, so `output_config.effort` is unreachable for Gemini.
- **Claude is dual-exposed**, but using it through `/chat/completions` loses
  Anthropic-specific features (cache_control, thinking blocks, etc.). Prefer
  `/v1/messages` for Claude.

---

## Reasoning-effort support (current findings)

| Model                       | Endpoint to use     | Field                       | Supported levels      |
|-----------------------------|---------------------|-----------------------------|-----------------------|
| `gpt-5.4`, `gpt-5.2`, `gpt-5-mini` | `/responses`  | `output_config.effort`      | low / medium / high   |
| `gpt-5.5`, `gpt-5.4-mini`, `gpt-5.*-codex` | `/responses` | `output_config.effort`  | low / medium / high   |
| `gemini-3-flash-preview`    | `/chat/completions` | top-level `reasoning_effort`| low / medium / high   |
| `gemini-3.1-pro-preview`    | `/chat/completions` | top-level `reasoning_effort`| low / medium / high   |
| `gemini-2.5-pro`            | `/chat/completions` | —                           | **not supported** (400) |
| `claude-opus-4.7`           | `/v1/messages`      | Anthropic `thinking` block  | (Anthropic-native; not OpenAI-style "effort") |
| `claude-opus-4.6`           | `/v1/messages`      | `output_config.effort` accepts only `medium` historically (re-verify) | medium |

Re-verify any rejection with the 400 message — upstream is the source of truth.

---

## Built-in server tools (web_search, file_search, etc.)

Both OpenAI's `/responses` and Anthropic's `/v1/messages` define server-side
"built-in" tools that the model service runs itself (no client-side execution).
The gateway forwards request bodies with minimal normalization (strips
`context_management`, `cache_control.scope`, and unsupported tool types;
rewrites a small allowlist of Claude Opus 4.x base model IDs to their
upstream-required variants — `claude-opus-4.7` / `-4-7` → `-1m-internal`,
`claude-opus-4.6` / `-4-6` → `-1m`; converts `thinking.type:enabled→adaptive`
for Opus 4.7 — see `gateway.py` for the full list), so server-tool support
is gated entirely by what GitHub Copilot's enterprise upstream chooses to
honor — which is **asymmetric**.

| Tool                                  | Where it lives                | Through Copilot? |
|---------------------------------------|-------------------------------|:----------------:|
| OpenAI `web_search` (Responses tool)  | `tools:[{type:"web_search"}]` on `/v1/responses` for GPT-5.x | ✅ honored |
| Anthropic `web_search_20250305`        | `tools:[{type:"web_search_20250305", name:"web_search"}]` on `/v1/messages` for Claude | ❌ 400 — `"The use of the web search tool is not supported."` |
| OpenAI `file_search`, `computer_use`, `image_generation` | `/v1/responses` | not yet probed end-to-end |

Verified empirically against `https://api.enterprise.githubcopilot.com` using
this gateway. Successful GPT-5.4 + `web_search` calls return `web_search_call`
output items with `action.queries[]` plus a `message` containing
`url_citation` annotations — i.e. the exact OpenAI Responses-API shape.

**Implication for Claude Code via the gateway:** Claude can't reach Copilot's
search directly. The practical workaround is to invoke the `gpt` skill
(`copilot` CLI) for ad-hoc web research; see
`docs/claude-code-integration.md` § *Web Search*.

---

## Decision guide for picking an endpoint

1. **Anthropic / Claude model?** → `POST /v1/messages` (use Anthropic shape;
   it's the most expressive for Claude).
2. **OpenAI GPT-5.x, and you need reasoning effort, summaries, or built-in
   tools?** → `POST /v1/responses` with `output_config.effort`.
3. **OpenAI GPT-5.x and you only need plain chat?** → either works; prefer
   `/chat/completions` for portability across clients.
4. **Gemini, MiniMax, Fireworks router?** → `POST /chat/completions` (it's
   the only shape they support). For Gemini reasoning, set top-level
   `reasoning_effort` (only on flash-preview / 3.1-pro-preview).
5. **If you get HTTP 400 with `not supported via Responses API` or
   `does not support reasoning effort`** → switch endpoints / drop the
   field per the matrix above.

---

## See also

- `gateway.py` — request routing (`PATH_MAP`), effort forwarding, and the
  `invalid_reasoning_effort` error augmentation.
- `docs/2026-04-26-gemini-effort-and-api-shapes.md` — raw probe results that
  produced this reference.
- `docs/copilot-cli-internals.md`, `docs/claude-code-integration.md` — how
  upstream clients use each shape in practice.
