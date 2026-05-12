# GitHub Copilot CLI Internals

Full reverse-engineering of the Copilot CLI (v1.0.14-0) architecture, code paths, and implementation details. Useful for understanding how it works and for building lightweight alternatives.

## Installation & Launch

**Install**: `brew install --cask copilot-cli` → `/opt/homebrew/bin/copilot`

**Launch chain**:
1. `copilot` → `npm-loader.js` (852 bytes) — sets `COPILOT_RUN_APP=1`, spawns Node
2. `index.js` (4.8 KB) — bootstrap/module loader
3. `app.js` (16.9 MB) — the entire application in one minified bundle

**Runtime**: Node.js, ES Modules (ES2022), Git commit `d0a4613`

## Directory Structure

```
~/.copilot/                              # 3.2 GB total
├── config.json                          # User settings
├── command-history-state.json           # Command history (38 KB)
├── session-store.db                     # SQLite session database (2.6 MB)
├── logs/                                # Process logs (335+ files, ~1.1 GB)
├── session-state/                       # 269+ session state dirs with metadata
├── ide/                                 # IDE integration locks
└── pkg/universal/1.0.14-0/              # Main package (128 MB)
    ├── app.js                           # Application bundle (16.9 MB)
    ├── index.js                         # Module loader (4.8 KB)
    ├── npm-loader.js                    # Entry script (852 B)
    ├── package.json                     # @github/copilot v1.0.14-0
    ├── schemas/
    │   └── api.schema.json              # JSON-RPC API schema (352 KB)
    ├── definitions/                     # Agent YAML configs
    │   ├── code-review.agent.yaml       # Code review agent (claude-sonnet-4.5)
    │   ├── explore.agent.yaml           # Codebase explorer (claude-haiku-4.5)
    │   ├── research.agent.yaml          # Research agent (claude-sonnet-4.6)
    │   ├── task.agent.yaml              # Task runner (claude-haiku-4.5)
    │   └── configure-copilot.agent.yaml # MCP config agent (claude-haiku-4.5)
    ├── sdk/                             # TypeScript SDK (14 MB)
    ├── copilot-sdk/                     # SDK docs & extensions (628 KB)
    ├── ripgrep/                         # Bundled rg binary (29 MB)
    ├── sharp/                           # Image processing (11 MB)
    ├── clipboard/                       # Clipboard access (8.7 MB)
    ├── prebuilds/                       # Native bindings per-platform (25 MB)
    ├── queries/                         # Tree-sitter syntax queries (100 KB)
    ├── tree-sitter-*.wasm               # 19 language parsers
    └── worker/                          # Worker thread support
```

## API Client (CAPI)

The core API client is class `C8`, which **extends the OpenAI SDK** (`Ti`/`OpenAI`):

```
C8 extends OpenAI (Ti)
├── C8.createWithOAuthToken(logger, baseUrl, integrationId, token, ...)
│   → Sets Authorization: Bearer {token}
├── C8.createWithHmac(logger, baseUrl, integrationId, hmacKey, ...)
│   → Sets Request-HMAC header
├── baseHeaders (always sent):
│   Content-Type: application/json
│   Accept: application/json
│   Openai-Intent: conversation-agent
│   X-Initiator: user
│   X-GitHub-Api-Version: 2026-01-09
│   Copilot-Integration-Id: copilot-developer-cli  ← CRITICAL HEADER
│   X-Interaction-Id: {sessionId}
│   User-Agent: copilot/{version} (darwin) term/{terminal}
└── Endpoints used:
    {baseURL}/models              → GET model list
    {baseURL}/models/{id}/policy  → PUT enable model
    {baseURL}/chat/completions    → POST (GPT, Claude via OpenAI format)
    {baseURL}/v1/messages         → POST (Claude via Anthropic format)
    {baseURL}/v1/responses        → POST (GPT-5.x via Responses API)
    {baseURL}/mcp                 → MCP protocol
```

**The `Copilot-Integration-Id` header is essential.** Without it, many models return 403 or "model_not_supported". The CLI uses `copilot-developer-cli`; VS Code uses `vscode-chat`. The server uses this header to determine which models a client is allowed to access.

## Auth Flow

Priority order for token resolution:

| Priority | Method | Source | Token Type |
|----------|--------|--------|------------|
| 1 | SDK Token | `config.authTokenEnvVar` env var | Any |
| 2 | HMAC | `CAPI_HMAC_KEY` / `COPILOT_HMAC_KEY` | HMAC key |
| 3 | API Key | Server-to-server config | API key |
| 4 | Copilot API Token | `GITHUB_COPILOT_API_TOKEN` + `COPILOT_API_URL` | Direct token |
| 5 | GitHub Token | `COPILOT_GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_TOKEN` | OAuth |
| 6 | Stored Login | macOS Keychain / `~/.copilot/copilot-state.json` | OAuth |
| 7 | GH CLI | `gh auth token --hostname {host}` | OAuth |

**Token validation**: Calls `GET https://api.github.com/copilot_internal/user` with the token. Response includes:
- `endpoints.api` — the Copilot API URL (e.g., `https://api.enterprise.githubcopilot.com` for enterprise)
- `copilot_plan` — plan type (enterprise, business, individual)
- `organization_login_list` — org memberships
- `quota_snapshots` — remaining premium requests

**Classic PATs (`ghp_*`) are rejected** — only OAuth tokens (`gho_*`) and fine-grained tokens work.

### VS Code Auth (different from CLI)

VS Code uses a different OAuth app (client ID `01ab8ac9400c4e429b23`) and sends `Copilot-Integration-Id: vscode-chat`. This unlocks additional models (22 vs CLI's 19), including Gemini 3.1 Pro. VS Code also attempts a JWT exchange via `copilot_internal/v2/token`, but the raw OAuth token works directly with the API. The VS Code extension also uses A/B experiment flags (`copilotchat.showInModelPicker`) to control model visibility per user cohort.

## Model Routing

All models go through the same API endpoint. For enterprise plans, this is `api.enterprise.githubcopilot.com` (resolved via `copilot_internal/user`). The server handles routing to actual providers (Bedrock for Claude, Azure for GPT). Model name is sent in the request body.

### Model List (Hardcoded in `WT`)

The CLI has a hardcoded allowlist of 18 models that it shows in the model picker. This is **not** the same as what the API returns — the API returns more models (19 callable with `copilot-developer-cli` header), but the CLI filters to only its known list. Models like `goldeneye` and `minimax-m2.5` are callable via the API but hidden from the CLI's UI.

| Model | Context | Max Output | Billing Multiplier |
|-------|---------|------------|-------------------|
| claude-haiku-4.5 | 200K | 64K | 0.333x |
| claude-sonnet-4.5 | 200K | 32K | 1x |
| claude-sonnet-4.6 | 200K | 32K | 1x |
| claude-opus-4.5 | 200K | 32K | 3x |
| claude-opus-4.6 | 200K | 32K | 3x |
| claude-opus-4.6-1m | 1M | 32K | 6x |
| claude-opus-4.6-fast | 200K | 32K | 30x |
| gpt-4.1 | 128K | 16K | 0x (free) |
| gpt-5-mini | 264K | 64K | 0x (free) |
| gpt-5.1 | 264K | 64K | ? |
| gpt-5.1-codex | 400K | 128K | ? |
| gpt-5.2 | 400K | 128K | ? |
| gpt-5.4 | 400K | 128K | ? |
| gpt-5.4-mini | 400K | 128K | ? |
| gemini-2.5-pro | 128K | 64K | ? |

### Wire API Selection

- **Chat Completions** (`/chat/completions`) — Default for most models
- **Responses API** (`/v1/responses`) — For GPT-5.x series when `supported_endpoints` includes `/responses`
- **WebSocket Responses** (`ws:/responses`) — Feature-flagged for GPT-5.x. The Codex CLI attempts this first via `GET /v1/responses` with `Upgrade: websocket`. The gateway does not proxy WebSocket; it returns `HTTP/1.1 405 Method Not Allowed` (`Allow: POST, OPTIONS`, empty body), which causes the CLI to fall back to HTTPS POST immediately. See the `do_GET` handler for `/v1/responses` in `gateway.py`.
- **Anthropic Messages** (`/v1/messages`) — For Claude models when using native format

### Model Config Inheritance

```
qh (base)
├── tool_choice, parallel_tool_calls, vision, splitEditingTools:true
│
├── $ye (Claude non-thinking) = qh + tool_choice:false
│   └── nq (Claude thinking) = $ye + defaultReasoningEffort:"high", maxOutputTokens:32000
│
├── S_e (GPT-5) = qh + customTools:true
│
├── E1 (GPT codex) = qh + customTools:true
│
└── AKr (thinking variant) = qh + thinkingMode:true, splitEditingTools:false
```

## BYOK (Bring Your Own Key)

When `COPILOT_PROVIDER_BASE_URL` is set, the CLI bypasses GitHub auth entirely and calls the custom provider directly.

| Env Var | Description |
|---------|-------------|
| `COPILOT_PROVIDER_BASE_URL` | Provider URL (required to activate) |
| `COPILOT_PROVIDER_TYPE` | `"openai"` (default), `"azure"`, `"anthropic"` |
| `COPILOT_PROVIDER_API_KEY` | API key |
| `COPILOT_PROVIDER_BEARER_TOKEN` | Bearer token (precedence over API key) |
| `COPILOT_PROVIDER_WIRE_API` | `"completions"` or `"responses"` |
| `COPILOT_MODEL` | Model name (required for BYOK) |
| `COPILOT_PROVIDER_MAX_PROMPT_TOKENS` | Override max prompt tokens |
| `COPILOT_PROVIDER_MAX_OUTPUT_TOKENS` | Override max output tokens |

Provider client creation (`Dzs` function):
- `type: "anthropic"` → Creates Anthropic SDK client (`new Anthropic({apiKey, baseURL})`)
- `type: "azure"` → Creates Azure OpenAI client with `api-key` header
- `type: "openai"` → Creates OpenAI client with custom baseURL

## Built-in Tools

| Tool | Internal Name | Description |
|------|--------------|-------------|
| Shell | `bash` / `powershell` | Execute shell commands |
| View | `view` (or `str_replace_editor` unified) | Read file contents |
| Edit | `edit` / `apply_patch` | Modify files |
| Create | `create` | Create new files |
| Search | `search` (grep) | ripgrep-based code search |
| Glob | `glob` | File pattern matching |
| Fetch | `fetch_url` | Web page fetching |
| Shell Sessions | `read_shell` / `write_shell` / `stop_shell` | Persistent shell sessions |
| MCP | `validate_mcp_config` / `mcp_reload` | MCP server management |
| Subagent | `search_code_subagent` | Semantic code search |
| Follow-up | `propose_follow_up` | Suggest next actions |
| Docs | `get_documentation` | Self-documentation |

Tools can be split or unified: when `splitEditingTools: true` (default for most models), file operations are separate tools (`view`, `create`, `edit`). When false, they're merged into `str_replace_editor`.

## Slash Commands

Full list of interactive commands:

**Core**: `/help`, `/model`, `/compact`, `/context`, `/usage`, `/version`, `/update`
**Session**: `/new`, `/clear`, `/session`, `/sessions`, `/rename`, `/restart`
**History**: `/rewind`, `/undo`, `/copy`, `/share`, `/diff`
**Agents**: `/agent`, `/review`, `/research`, `/plan`, `/tasks`, `/fleet`
**Config**: `/mcp`, `/lsp`, `/skills`, `/extension`, `/plugin`, `/sandbox`, `/instructions`
**Permissions**: `/allow-all` (alias `/yolo`), `/reset-allowed-tools`
**Navigation**: `/cd`, `/cwd`, `/add-dir`, `/list-dirs`
**Dev**: `/pr`, `/init`, `/ide`, `/remote`
**Debug**: `/feedback`, `/collect-debug-logs`, `/diagnose`, `/changelog`
**Display**: `/theme`, `/streamer-mode`, `/terminal-setup`

## Agent Definitions

5 built-in agents in `definitions/`:

| Agent | Model | Purpose |
|-------|-------|---------|
| `code-review` | claude-sonnet-4.5 | Bugs, security, logic errors only. "Silence > noise" |
| `explore` | claude-haiku-4.5 | Fast codebase exploration, parallel tool calls |
| `research` | claude-sonnet-4.6 | Deep research with GitHub + web, full markdown reports |
| `task` | claude-haiku-4.5 | Run tests/builds/lints, minimal output |
| `configure-copilot` | claude-haiku-4.5 | MCP server configuration |

## SDK (Programmatic Access)

The SDK at `sdk/` exposes:
- `CopilotClient` — Connect to running CLI server
- `CopilotSession` — Manage conversations
- `defineTool`, `approveAll` — Tool helpers
- Custom agent configs, MCP server configs, permission handlers
- Hook system: `onUserPromptSubmitted`, `onPreToolUse`, `onPostToolUse`, `onSessionStart`, `onSessionEnd`

Extensions must be `.mjs` files (no TypeScript). Stdout is reserved for JSON-RPC.

## MCP (Model Context Protocol)

Extensive MCP support with built-in tool allowlists (107 tools):
- GitHub tools: `get_pull_request`, `list_issues`, `search_code`, etc.
- Playwright: 20+ browser automation tools
- Azure: AKS, Cosmos, KeyVault tools
- Config: `~/.copilot/mcp-config.json`, `.mcp.json`, `.vscode/mcp.json`
- Endpoints: `https://api.githubcopilot.com/mcp`

## Reasoning & Effort

| Level | Thinking Budget | Models |
|-------|----------------|--------|
| low | 1024 tokens | Claude, GPT-5.x |
| medium | 2048 tokens | Claude, GPT-5.x |
| high | 4096 tokens | Claude, GPT-5.x |
| xhigh | (max) | GPT-5.3-codex, GPT-5.4 only |

Claude models use `thinking_budget` parameter. GPT models use `reasoning_effort`.

## Billing

- Tracked in **nano-AIU** (AI Units): `total_nano_aiu` per request
- Multiplier 0x = free tier, <=0.5x = cheap, >=2x = premium
- Quota fields: `completions_remaining`, `completions_overage_count`, `completions_unlimited`
- Check usage: https://github.com/settings/copilot

## Environment Variables (Key Ones)

| Variable | Purpose |
|----------|---------|
| `COPILOT_API_URL` | Override API base URL |
| `COPILOT_MODEL` | Override default model |
| `COPILOT_GITHUB_TOKEN` | Auth token (highest priority) |
| `COPILOT_ALLOW_ALL` | Bypass permission prompts |
| `COPILOT_OFFLINE` | Offline mode |
| `COPILOT_CUSTOM_AGENT` | Custom agent config path |
| `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` | Custom instruction directories |
| `COPILOT_EXPERIMENTS` | Feature flags |
| `COPILOT_OTEL_ENABLED` | OpenTelemetry tracing |

See full list of 93+ `COPILOT_*` and `GITHUB_*` variables in the app.js analysis.
