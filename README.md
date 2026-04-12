# Copilot LLM Gateway

A local LLM API gateway that lets **any product** access GitHub Copilot's models (Claude Opus/Sonnet, GPT-5.4, Gemini, MiniMax, Goldeneye, etc.) using standard OpenAI or Anthropic SDK formats. Clients connect with a dummy API key — the gateway handles all GitHub auth automatically.

## Why

GitHub Copilot subscription (via employee/enterprise plan) includes access to all major models — Claude Opus 4.6 (1M context), GPT-5.4, Gemini 3.1 Pro, etc. But the official Copilot CLI wraps these behind its own prompt system, agent framework, and tool layer. This gateway **bypasses the CLI** and gives direct model access, so any product can send prompts and get responses — like a self-hosted LLM provider backed by your Copilot subscription.

## Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│  Claude Code    │    │  AionUI         │    │  Any App/Bot    │
│  (Anthropic SDK)│    │  (OpenAI SDK)   │    │  (curl/fetch)   │
└───────┬─────────┘    └───────┬─────────┘    └───────┬─────────┘
        │ api_key="dummy"      │ api_key="dummy"      │
        │ base_url=:8787       │ base_url=:8787/v1    │
        └──────────┬───────────┴──────────┬───────────┘
                   ▼                      ▼
          ┌────────────────────────────────────┐
          │       Copilot LLM Gateway          │
          │       http://localhost:8787         │
          │                                    │
          │  • Strips client auth              │
          │  • Injects GitHub token + header   │
          │  • Auto-refreshes on 401           │
          │  • Routes paths correctly          │
          │  • Streams SSE responses           │
          │  • Caches model list (5 min TTL)   │
          │  • Two modes: CLI / VS Code        │
          └──────────────────┬─────────────────┘
                             │ Authorization: Bearer <token>
                             │ Copilot-Integration-Id: <mode>
                             ▼
              ┌──────────────────────────────┐
              │ api.enterprise.githubcopilot │
              │          .com               │
              │                             │
              │  Claude Opus/Sonnet (1M)    │
              │  GPT-5.4 / GPT-5.2         │
              │  Gemini 3.1 Pro             │
              │  MiniMax, Goldeneye         │
              └──────────────────────────────┘
```

## Quick Start

```bash
cd ~/Projects/copilot-gateway

# First time only: login with VS Code OAuth (opens browser)
python3 gateway.py --mode vscode

# ── After first login, use shell function/aliases: ──

cg        # Start gateway + demo + menu bar (backgrounds, returns prompt)
cgcc      # Claude Code through gateway (skip permissions, like cc)
cgca      # Claude Code through gateway (auto mode, safer than skip-all)

# Or without aliases:
python3 gateway.py    # starts gateway + demo UI + ⚡️CG menu bar
ANTHROPIC_AUTH_TOKEN=dummy ANTHROPIC_BASE_URL=http://localhost:8787 claude
```

Zero Python dependencies. Python 3.8+ stdlib only.

## Shell Function & Aliases

Add these to `~/.zshrc` (already configured on this machine):

```bash
# Copilot Gateway
cg() {
  cd ~/Projects/copilot-gateway || return 1
  local sid
  sid="$(date +%H%M%S)_$(head -c2 /dev/urandom | xxd -p)"
  local logdir="logs/$(date +%Y-%m-%d)/$sid"
  mkdir -p "$logdir" || return 1
  GATEWAY_SESSION_ID="$sid" nohup python3 gateway.py > "$logdir/console.log" 2>&1 &
  local pid=$!
  sleep 3
  if kill -0 "$pid" 2>/dev/null; then
    echo "⚡️ Gateway running (PID $pid) — logs: $logdir"
  else
    echo "❌ Gateway failed to start — check $logdir/console.log"
    return 1
  fi
}
alias cgcc="ANTHROPIC_AUTH_TOKEN=dummy ANTHROPIC_BASE_URL=http://localhost:8787 claude --dangerously-skip-permissions"
alias cgca="ANTHROPIC_AUTH_TOKEN=dummy ANTHROPIC_BASE_URL=http://localhost:8787 claude --enable-auto-mode"

# Claude Code (direct Anthropic API)
alias cc="claude --dangerously-skip-permissions"
alias ca="claude --enable-auto-mode"
```

| Command | What it does |
|---------|-------------|
| `cg` | Start gateway in background (+ demo UI on :8788, + ⚡️CG menu bar) with per-session logs |
| `cgcc` | Claude Code through gateway, skip all permissions |
| `cgca` | Claude Code through gateway, auto mode (safer, when available) |
| `cc` | Claude Code direct, skip all permissions |
| `ca` | Claude Code direct, auto mode |

**Workflow**: Run `cg` once, then `cgcc` in the same or any other terminal. Stop everything via ⚡️CG menu bar → "Stop Gateway & Quit".

## Two Auth Modes

The gateway supports two modes. **Same API URL**, different tokens and headers = different model access.

| | CLI Mode | VS Code Mode |
|---|---|---|
| **Token source** | `gh auth token` | OAuth device flow (saved to `.gateway-token.json`) |
| **Integration ID header** | `copilot-developer-cli` | `vscode-chat` |
| **Callable models** | 19 | 22 |
| **Re-auth needed?** | Never (uses gh) | Never (token persisted) |
| **Unique models** | — | Gemini 3.1 Pro, Fireworks routers |
| **Startup** | `python3 gateway.py --mode cli` | `python3 gateway.py --mode vscode` |

Both modes use the same API URL (`api.enterprise.githubcopilot.com`) and the same enterprise plan/quota.

### Critical finding: `Copilot-Integration-Id` header

The server controls model access via this header, not just the token. Without it, many models return 403 or "model_not_supported". The correct values:
- **CLI**: `Copilot-Integration-Id: copilot-developer-cli` (found in CLI logs)
- **VS Code**: `Copilot-Integration-Id: vscode-chat` (found in extension source)

## Available Models

### CLI Mode (19 callable, `copilot-developer-cli`)

| Model | Endpoint | Format |
|-------|----------|--------|
| `claude-opus-4.6` | `/v1/messages` | Anthropic Messages API |
| `claude-opus-4.6-1m` | `/v1/messages` | Anthropic Messages API (1M context) |
| `claude-sonnet-4.6` | `/v1/messages` | Anthropic Messages API |
| `claude-sonnet-4.5` | `/v1/messages` | Anthropic Messages API |
| `claude-opus-4.5` | `/v1/messages` | Anthropic Messages API |
| `claude-haiku-4.5` | `/v1/messages` | Anthropic Messages API |
| `claude-sonnet-4` | `/v1/messages` | Anthropic Messages API |
| `gpt-5.4` | `/v1/responses` | OpenAI Responses API |
| `gpt-5.4-mini` | `/v1/responses` | OpenAI Responses API |
| `gpt-5.2` | `/chat/completions` | OpenAI Chat API |
| `gpt-5.1` | `/chat/completions` | OpenAI Chat API |
| `gpt-5-mini` | `/chat/completions` | OpenAI Chat API |
| `goldeneye` | `/v1/responses` | OpenAI Responses API |
| `minimax-m2.5` | `/chat/completions` | OpenAI Chat API |

Note: The actual Copilot CLI shows only 18 models — it has a hardcoded allowlist (`WT` in app.js) that filters out `goldeneye` and `minimax-m2.5` even though the API makes them available.

### VS Code Mode (22 callable, `vscode-chat`, requires VS Code OAuth token)

All CLI models plus:
- `gemini-3.1-pro-preview` — Google Gemini 3.1 Pro
- `gpt-5.1-codex-mini` — OpenAI GPT-5.1 Codex Mini
- `accounts/msft/routers/mp3yn0h7` — Fireworks router
- `accounts/msft/routers/yaqq2gxh` — Fireworks router

Run `curl http://localhost:8787/v1/models` for the full live list with capabilities.

## Client Configuration

### Anthropic SDK (Python/Node.js)

```python
from anthropic import Anthropic
client = Anthropic(auth_token="dummy", base_url="http://localhost:8787")
msg = client.messages.create(
    model="claude-opus-4.6-1m",  # 1M context!
    max_tokens=4096,
    messages=[{"role": "user", "content": "Hello!"}],
)
```

### OpenAI SDK (Python/Node.js)

```python
from openai import OpenAI
client = OpenAI(api_key="dummy", base_url="http://localhost:8787/v1")
resp = client.chat.completions.create(
    model="claude-sonnet-4.6",  # Claude works via OpenAI format too
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}],
)
```

### Claude Code CLI

```bash
ANTHROPIC_AUTH_TOKEN=dummy ANTHROPIC_BASE_URL=http://localhost:8787 claude
```

Or in `~/.claude/settings.json`:
```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "dummy",
    "ANTHROPIC_BASE_URL": "http://localhost:8787"
  }
}
```

### curl

```bash
# Anthropic format
curl http://localhost:8787/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-opus-4.6-1m","max_tokens":100,"messages":[{"role":"user","content":"Hello"}]}'

# OpenAI format
curl http://localhost:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.2","max_tokens":100,"messages":[{"role":"user","content":"Hello"}]}'

# GPT-5.4 (Responses API)
curl http://localhost:8787/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.4","input":"Hello"}'

# Claude with extended thinking
curl http://localhost:8787/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-opus-4.6","max_tokens":8000,"thinking":{"type":"enabled","budget_tokens":4096},"messages":[{"role":"user","content":"Solve this step by step: what is 27*43?"}]}'
```

## Gateway Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/models` | List all available models with capabilities |
| `POST` | `/v1/messages` | Anthropic Messages API (Claude models) |
| `POST` | `/v1/chat/completions` | OpenAI Chat Completions API |
| `POST` | `/chat/completions` | OpenAI Chat Completions (alias) |
| `POST` | `/v1/responses` | OpenAI Responses API (GPT-5.4, Goldeneye) |
| `GET` | `/health` | Health check with token/upstream/mode status |

## Configuration

| Env Var | Default | Description |
|---------|---------|-------------|
| `GATEWAY_HOST` | `127.0.0.1` | Listen address |
| `GATEWAY_PORT` | `8787` | Listen port |
| `GATEWAY_UPSTREAM` | `https://api.githubcopilot.com` | Upstream API (auto-resolved to enterprise for enterprise plans) |
| `GATEWAY_SESSION_ID` | (auto-generated) | Override the session ID for log directory naming |
| `GITHUB_TOKEN` | (from `gh auth token`) | GitHub token override |

| CLI Flag | Description |
|----------|-------------|
| `--mode cli` | Use gh CLI token (19 models) |
| `--mode vscode` | Use VS Code OAuth token (22 models, first-time login required) |
| `--login` | Force re-authentication via OAuth device flow |

## Logging

Each gateway launch creates its own log directory under a dated folder. Multiple launches on the same day produce separate log sets.

```
logs/
├── latest -> 2026-04-09/143022_a3f1/    # symlink to newest session
├── 2026-04-09/
│   ├── 143022_a3f1/                     # 1st launch
│   │   ├── gateway.log                  # gateway request/response logs
│   │   ├── console.log                  # stdout/stderr (cg only)
│   │   └── demo.log                     # demo app output
│   └── 151500_b7e2/                     # 2nd launch
│       ├── gateway.log
│       ├── console.log
│       └── demo.log
└── 2026-04-10/
    └── ...
```

**Tailing the latest session:**
```bash
tail -f logs/latest/gateway.log          # live request log
tail -f logs/latest/console.log          # full console output (cg launch only)
```

**Cleanup:** logs are never auto-deleted. Remove old dated folders when needed:
```bash
rm -rf logs/2026-04-01/                  # delete a specific day
```

**Note:** When using `python3 gateway.py` directly (not via `cg`), gateway logs are written to the session directory and also printed to the terminal. There is no `console.log` in this case — it's only created by the `cg` shell function's redirect.

**Concurrent launches:** If multiple gateways start simultaneously, `logs/latest` points to whichever started last. Each launch still gets its own distinct session directory.

## What `python3 gateway.py` Launches

A single command starts three processes:

1. **Gateway** on `:8787` — the LLM API proxy
2. **Demo UI** on `:8788` — chat + call flow visualization
3. **⚡️CG menu bar** — macOS status bar indicator

The ⚡️CG menu bar shows:
- **⚡️CG** when running, **💤CG** when stopped (checks every 30s)
- **Open Demo UI** — opens `localhost:8788` in browser
- **Check Health** — shows gateway status popup
- **Copy Claude Code Command** — copies the `cgcc` env vars to clipboard
- **Stop Gateway & Quit** — kills gateway, demo, and menu bar in one click

All three are killed together via Ctrl+C or the menu bar stop option.

## Demo App

The demo UI at `localhost:8788` provides:
- **Left pane**: Chat with any model, model selector grouped by vendor
- **Right pane**: Real-time call flow log (request bodies, response headers, SSE chunks, timing)
- **Mode toggle**: VS Code / CLI switch — shows different model lists, API URLs, token types
- **Draggable split**: Resize panes by dragging the border
- **Info bar**: Shows API URL, token type, integration ID, model count for current mode

The demo calls the Copilot API directly (not through the gateway) so it can switch modes per-request.

## Files

| File | Purpose |
|------|---------|
| `gateway.py` | LLM gateway — dual mode, auto-auth, streaming, auto-launches demo + menu bar |
| `demo.py` | Demo web app with call-flow instrumentation |
| `demo.html` | Split-pane UI (chat + flow log + mode toggle) |
| `menubar.swift` | macOS menu bar indicator source (compile: `swiftc menubar.swift -o menubar -framework Cocoa`) |
| `mini-cli.py` | Lightweight terminal CLI (~100 lines) |
| `test-copilot-api.sh` | End-to-end test script |
| `docs/research.md` | How the Copilot API was discovered and how auth works |
| `docs/copilot-cli-internals.md` | Full reverse-engineering of the Copilot CLI |
| `docs/building-lightweight-cli.md` | Guide to building your own CLI |
| `docs/claude-code-integration.md` | Using the gateway with Claude Code CLI (backup/setup/restore) |

## How Auth Works

1. **API base resolution**: Gateway calls `api.github.com/copilot_internal/user` → gets enterprise endpoint URL + plan info
2. **Token**: `Authorization: Bearer <token>` (gh CLI token or VS Code OAuth token)
3. **Integration ID**: `Copilot-Integration-Id: copilot-developer-cli` or `vscode-chat` — this header controls which models are accessible
4. **Auto-refresh**: On 401, gateway re-resolves the token
5. **Persistence**: VS Code token saved to `.gateway-token.json`, auto-loaded on restart

## Caveats

- Requires active GitHub Copilot subscription (enterprise plan tested)
- Premium models (Opus 6x, Opus-1M 6x, GPT-5.4 1x) consume premium request quota
- `gho_*` OAuth tokens are long-lived but can be revoked — run `--login` to re-auth
- The Copilot CLI has a hardcoded model allowlist that hides some API-available models (goldeneye, minimax-m2.5)
- Rate limits are per your Copilot plan (enterprise = unlimited for this user)
- Gemini 3 Pro was deprecated March 26, 2026; replaced by Gemini 3.1 Pro (VS Code mode only)
