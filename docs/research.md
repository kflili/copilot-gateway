# Research: How the Copilot API Works

This documents how we discovered and validated that GitHub Copilot exposes a usable LLM API, and how authentication works end-to-end.

## Discovery Process

### Step 1: Finding the API endpoint

The GitHub Copilot CLI (`copilot` command, installed via `brew install --cask copilot-cli`) is a Node.js application. Its main bundle is at:

```
~/.copilot/pkg/universal/1.0.14-0/app.js  (16.9 MB minified)
```

By grepping the bundle for URL patterns, we found the core API base URL:

```
https://api.githubcopilot.com
```

This is hardcoded as constant `Hzo` in app.js. It can be overridden via `COPILOT_API_URL` env var. For enterprise users, the actual endpoint is resolved dynamically via `copilot_internal/user`:

```
https://api.enterprise.githubcopilot.com
```

### Step 2: Understanding authentication

The Copilot CLI authenticates using your GitHub token. The token resolution order:

1. `COPILOT_GITHUB_TOKEN` env var
2. `GH_TOKEN` env var
3. `GITHUB_TOKEN` env var
4. macOS Keychain (stored login tokens via `keytar`)
5. `gh auth token` (GitHub CLI)

The token is sent as `Authorization: Bearer <token>` on all API requests.

**Important**: Classic PATs (`ghp_*`) are rejected. You need an OAuth token (`gho_*`) from `gh auth login` or a fine-grained PAT.

### Step 3: The critical `Copilot-Integration-Id` header

**This was the biggest discovery.** The Copilot API uses this header to control model access per client type. Without it, many models return 403 or "model_not_supported". We found the correct values by:

- **CLI**: Grepping the CLI logs (`~/.copilot/logs/`) revealed `"Creating copilot-client for integration ID copilot-developer-cli"`. Using `Copilot-Integration-Id: copilot-developer-cli` unlocks all CLI models.
- **VS Code**: Grepping the extension source at `~/.vscode/extensions/github.copilot-chat-*/dist/extension.js` revealed `Copilot-Integration-Id: vscode-chat`.

| Integration ID | Token Source | Callable Models |
|---|---|---|
| *(none)* | `gh auth token` | 15 (many 403s) |
| `copilot-developer-cli` | `gh auth token` | 19 (matches real CLI) |
| `vscode-chat` | VS Code OAuth token | 22 (matches real VS Code) |

### Step 4: Enterprise endpoint resolution

Calling `GET api.github.com/copilot_internal/user` with any valid token returns the user's Copilot config:

```json
{
  "copilot_plan": "enterprise",
  "endpoints": {
    "api": "https://api.enterprise.githubcopilot.com",
    "proxy": "https://proxy.enterprise.githubcopilot.com"
  },
  "organization_login_list": ["microsoft", "MicrosoftCopilot"],
  "quota_snapshots": {
    "premium_interactions": { "unlimited": true }
  }
}
```

Both CLI and VS Code tokens resolve to the **same API URL** (`api.enterprise.githubcopilot.com`). The endpoint doesn't change — only the header + token combination changes what models are accessible.

### Step 5: Validating the API supports standard formats

We tested with curl and confirmed all three API formats work:

**Anthropic Messages API** — `POST /v1/messages`
```bash
curl -X POST https://api.enterprise.githubcopilot.com/v1/messages \
  -H "Authorization: Bearer $(gh auth token)" \
  -H "Copilot-Integration-Id: copilot-developer-cli" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-opus-4.6-1m","max_tokens":50,"messages":[{"role":"user","content":"Hello"}]}'
```

**OpenAI Chat Completions** — `POST /chat/completions`
```bash
curl -X POST https://api.enterprise.githubcopilot.com/chat/completions \
  -H "Authorization: Bearer $(gh auth token)" \
  -H "Copilot-Integration-Id: copilot-developer-cli" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.2","max_tokens":50,"messages":[{"role":"user","content":"Hello"}]}'
```

**OpenAI Responses API** — `POST /v1/responses`
```bash
curl -X POST https://api.enterprise.githubcopilot.com/v1/responses \
  -H "Authorization: Bearer $(gh auth token)" \
  -H "Copilot-Integration-Id: copilot-developer-cli" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.4","input":"Hello"}'
```

### Step 6: Confirming advanced features

**Streaming (SSE)**: Works. Adding `"stream": true` returns standard Anthropic SSE events.

**Tool use**: Works. Proper `tool_use` content blocks with `stop_reason: "tool_use"`.

**Vision**: Supported for models that list `vision: true` in capabilities.

### Step 7: VS Code OAuth for more models

VS Code uses its own OAuth app (client ID `01ab8ac9400c4e429b23`) which gives access to 22 models vs CLI's 19. We replicated this via OAuth device flow:

1. `POST github.com/login/device/code` with VS Code's client ID → get user code
2. User visits `github.com/login/device`, enters code, approves
3. Poll `github.com/login/oauth/access_token` → get `gho_*` token
4. Token is saved to `.gateway-token.json` — no re-auth needed

This token + `Copilot-Integration-Id: vscode-chat` unlocks Gemini 3.1 Pro, Fireworks routers, and more.

## Auth Header Format

The Copilot API **only** accepts `Authorization: Bearer <token>`. It **rejects** `x-api-key: <token>`.

This matters for Claude Code integration:
- `ANTHROPIC_API_KEY` → sends `x-api-key` (won't work)
- `ANTHROPIC_AUTH_TOKEN` → sends `Authorization: Bearer` (works!)

## Two Separate GitHub APIs

### 1. Copilot API (`api.enterprise.githubcopilot.com`)
- Requires Copilot subscription
- 19-22 callable models (depending on mode)
- Supports Anthropic, OpenAI Chat, and OpenAI Responses formats
- Auth: GitHub OAuth token + `Copilot-Integration-Id` header

### 2. GitHub Models API (`models.github.ai`)
- Free tier available to all GitHub users
- 43 models (GPT, Llama, DeepSeek, Grok, Mistral — no Claude)
- OpenAI-compatible format only
- Auth: GitHub PAT with `models:read` scope

The gateway uses the Copilot API because it has Claude, more models, and enterprise-grade access.

## Token Lifecycle

1. **CLI token**: `gh auth login` → `gho_*` stored in macOS Keychain → retrieved via `gh auth token`. Long-lived, no expiry unless revoked.
2. **VS Code token**: OAuth device flow with client ID `01ab8ac9400c4e429b23` → `gho_*` saved to `.gateway-token.json`. Long-lived, auto-detected on gateway restart.
3. Both tokens are validated via `copilot_internal/user` which returns the enterprise API endpoint.
4. The gateway auto-refreshes on 401.

## Model Listing and Filtering

`GET /models` returns all models, but the Copilot CLI applies a **hardcoded allowlist** (`WT` constant in app.js) of 18 models. This means:
- `goldeneye` and `minimax-m2.5` are callable via the API but hidden from the CLI's model picker
- `gpt-4.1` is in the CLI's hardcoded list but has no API endpoint
- Our gateway shows the **real API availability**, which is more accurate than the CLI

## Backend Infrastructure

From response headers and CLI source:
- **Amazon Bedrock** for Claude models (response includes `amazon-bedrock-invocationMetrics`)
- **Azure OpenAI** for GPT models
- **Cerebras** hosts MiniMax M2.5
- **Fireworks** hosts router models
- Proxy infrastructure: `copilot-proxy.githubusercontent.com`, `proxy.enterprise.githubcopilot.com`

## Gemini Model Availability (March 2026)

- **Gemini 3 Pro**: Deprecated March 26, 2026. Removed from CLI v1.0.13+.
- **Gemini 3.1 Pro**: Available in VS Code mode only (not in CLI mode)
- **Gemini 3 Flash**: Listed in API but no callable endpoint
- **Gemini 2.5 Pro**: Listed in API but no callable endpoint
- Known CLI/VS Code parity gap tracked at github/copilot-cli#1703
