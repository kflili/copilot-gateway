# Copilot LLM Gateway

A local LLM API gateway that lets **any product** access GitHub Copilot's models (Claude Opus/Sonnet, GPT-5.4, Gemini, MiniMax, Goldeneye, etc.) using standard OpenAI or Anthropic SDK formats. Clients connect with a dummy API key — the gateway handles all GitHub auth automatically. Tracks token usage and shows live stats in the macOS menu bar.

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

## Mac vs Windows Integration

Same gateway binary, different wiring on each platform. The table below shows
where they diverge — useful when a Windows sub-session asks why a
Mac-documented step has no Windows analogue (or vice versa).

| Concern | Mac | Windows |
|---|---|---|
| Route Claude through gateway | Manual: `cgcc` alias in `~/.zshrc` or `env` block in `~/.claude/settings.json` | Tray app one-click: **Enable for Windows** toggle |
| Route a sibling Linux env | N/A (no Mac↔Linux automation) | **Enable for WSL** submenu — writes env into chosen distro with dynamic Windows-host-IP resolution |
| Verify routing works | Run `cgcc` and try a prompt | **[Test]** button per toggle → green/red toast |
| Live stats / control surface | ⚡️CG menu bar (Swift binary or `CopilotGateway.app`) | Tray app shows stats inline + per-host breakdown |

**Why the asymmetry**: Mac users on this codebase self-select into a CLI-heavy
workflow (`cg` to start, `cgcc` in any terminal, edit `~/.zshrc` to taste), so
a one-click installer would add friction more than it removes. Windows users
are a more diverse set — including WSL sub-session investigators who don't
edit Windows env vars by hand — so the tray app does that wiring for them.
Neither path is "better"; they fit their respective audiences.

## Quick Start

```bash
cd ~/Projects/copilot-gateway

# First time only: login with VS Code OAuth (opens browser)
python3 gateway.py --mode vscode

# ── After first login, use shell function/aliases: ──

cg        # Start gateway + demo + menu bar (backgrounds, returns prompt)
cgcc      # Claude Code through gateway (skip permissions, like cc)
cgca      # Claude Code through gateway (auto mode, safer than skip-all)
cgcx      # Codex CLI through gateway (workspace-write, on-request approvals)

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
alias cgcx="OPENAI_API_KEY=dummy codex -c openai_base_url=http://localhost:8787/v1 -a on-request -s workspace-write -c approvals_reviewer=\"auto_review\""

# Claude Code (direct Anthropic API)
alias cc="claude --dangerously-skip-permissions"
alias ca="claude --enable-auto-mode"
```

| Command | What it does |
|---------|-------------|
| `cg` | Start gateway in background (+ demo UI on :8788, + ⚡️CG menu bar) with per-session logs |
| `cgcc` | Claude Code through gateway on Claude Opus 4.8 (xhigh effort, native 1M context), skip all permissions |
| `cgcc48` | Same as `cgcc` |
| `cgcc47` | Claude Code through gateway on Claude Opus 4.7 (1M context), skip all permissions |
| `cgca` | Claude Code through gateway, auto mode (safer, when available) |
| `cgcx` | Codex CLI through gateway (gpt-5.5/5.4 etc., workspace-write sandbox) |
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

### Codex CLI (OpenAI)

```bash
OPENAI_API_KEY=dummy codex -c openai_base_url=http://localhost:8787/v1
```

Or use the `cgcx` alias (above). Picks up the model from `~/.codex/config.toml` — set `model = "gpt-5.5"` (or any model the gateway lists at `/v1/models`).

**Expected behavior on first prompt:** the Codex CLI tries a WebSocket transport against `/v1/responses` first, the gateway returns `405 Method Not Allowed` (it doesn't proxy WebSocket), and the CLI prints:

```
⚠ Falling back from WebSockets to HTTPS transport. unexpected status 405 Method Not Allowed
```

The 405 response itself completes in <1 s. The user-observable delay on the first `cgcx` prompt is ~5 s end-to-end, since the CLI also does its WebSocket-handshake setup and HTTPS-transport reconnect on top of the round-trip. Every subsequent prompt in the same session goes directly over HTTPS POST with no overhead. The 405 response is intentional — the `do_GET` handler for `/v1/responses` in `gateway.py` rejects the WebSocket upgrade cleanly so the CLI falls back immediately instead of running its full retry loop (which would take 1-2 min).

### AionUi (Auto-detection)

AionUi automatically detects the gateway when spawning Claude Code sessions (both Rich UI and Terminal modes). No manual configuration needed — just start the gateway and launch a Claude session in AionUi.

**How it works:** Before each Claude spawn, AionUi probes `http://localhost:8787/health` (300ms timeout). If the gateway responds with `{"status":"ok"}`, it injects `ANTHROPIC_BASE_URL` and a dummy auth token into the spawned process. If the gateway isn't running, Claude uses the default API path.

**Settings toggle:** In AionUi, go to **Settings → Agent CLI → Copilot Gateway** to enable/disable auto-detection (enabled by default).

**Known limitation:** The Copilot API does not support Anthropic's `context_management` (server-side compaction) feature. The gateway automatically strips this field from requests. Claude Code handles context management client-side, so this has no practical impact.

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
| `GET` | `/health` | Health check with token/upstream/mode/request count |
| `GET` | `/stats` | Token usage stats (requests, tokens, per-model breakdown) |
| `GET` | `/logs` | Recent gateway log lines (text, ?n=100 for line count) |

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

A single command starts up to three processes:

1. **Gateway** on `:8787` — the LLM API proxy
2. **Demo UI** on `:8788` — chat + call flow visualization
3. **⚡️CG menu bar** — macOS status bar indicator (skipped if one is already drawing the icon — either a standalone `menubar` from a previous gateway or the bundled `CopilotGateway.app`, which has its own menu-bar UI)

The ⚡️CG menu bar shows:
- **⚡️CG 42↗ 170K** — live request count and total tokens (updates every 30s)
- **💤CG** when stopped
- **📊 Stats** — requests, premium requests, input/output token breakdown, uptime
- **Open Demo UI** — opens `localhost:8788` in browser
- **Check Health** — shows gateway status popup
- **View Logs** — opens a floating log viewer panel with color-coded live tail (⌘L)
- **Copy Claude Code Command** — copies the `cgcc` env vars to clipboard
- **Stop Gateway & Quit** — kills gateway, demo, and menu bar in one click

**Ctrl+C** terminates the gateway, demo, and any menu bar this gateway spawned. An adopted menu bar (standalone `menubar` or `CopilotGateway.app`) keeps running on the Ctrl+C path — `gateway.py` has no PID handle to it. The **Stop Gateway & Quit** menu option kills the gateway and demo processes *and* quits the menu-bar process itself, so the menu-driven shutdown fully cleans up either way.

## Demo App

The demo UI at `localhost:8788` provides:
- **Left pane**: Chat with any model, model selector grouped by vendor
- **Right pane**: Real-time call flow log (request bodies, response headers, SSE chunks, timing)
- **Mode toggle**: VS Code / CLI switch — shows different model lists, API URLs, token types
- **Draggable split**: Resize panes by dragging the border
- **Info bar**: Shows API URL, token type, integration ID, model count for current mode
- **Gateway panel** (collapsible, top strip): per-host (Windows | WSL | Total) stats table — requests, tokens in/out, top model, last request — plus a live `/logs` tail with `[WIN]` (blue) / `[WSL]` (green) / `[OTHER]` (gray) prefixes. Polls the gateway every 3s via `/api/gateway/stats` + `/api/gateway/logs`.

The demo calls the Copilot API directly (not through the gateway) so it can switch modes per-request.

## Windows

Two ways to run on Windows: directly from source (currently the working path)
or via a packaged single-`.exe` (experimental — see Known limitation below).

### Option A — from source (recommended)

`tray_app.py` is a Windows-targeted system-tray UI that mirrors the macOS
`menubar.swift` feature surface and adds two toggles unique to this platform:
**Enable for Windows** (writes `ANTHROPIC_*` to user env via `setx` and
`%USERPROFILE%\.claude\settings.json`) and **Enable for WSL** (writes a
shell-function wrapper into the chosen distro's rc-file so the Windows host IP
is resolved at every shell start).

Install (Windows):

```powershell
pip install pystray pillow
python tray_app.py
```

Menu items: Stats (per-origin breakdown from the `per_origin` field of
`/stats`), View logs (color-coded by origin from `/logs`), Copy claude /
codex command, Enable for Windows + [Test], Enable for WSL submenu (one
entry per distro) + per-distro [Test], Stop & quit.

### After pulling updates on Windows

After `git pull`, restart the tray app and run **Enable for Windows** again.
The toggle writes gateway routing env plus the Claude Code default model to
user scope via `setx` and merges the same values into
`%USERPROFILE%\.claude\settings.json`.

Current Claude default written by the tray:

```text
ANTHROPIC_MODEL=claude-opus-4-8
ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-8
ANTHROPIC_DEFAULT_OPUS_MODEL_NAME=Opus 4.8 via Gateway
ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES=effort,xhigh_effort,thinking,adaptive_thinking,interleaved_thinking
```

The tray also sets `"model": "claude-opus-4-8"` and
`"effortLevel": "xhigh"` in Claude Code settings. Restart open terminals,
IDE windows, and Claude Code sessions after enabling because `setx` does not
change already-running processes.

For WSL, run **Enable for WSL** for each distro you use. It rewrites the
distro rc-file block and, when a stable Windows-host URL is available, merges
the same 4.8/xhigh defaults into that distro's `~/.claude/settings.json`.

Bind safety: by default the spawned gateway listens on `127.0.0.1` (loopback —
not reachable from WSL or LAN). To make it reachable from WSL distros,
launch with `python tray_app.py --host 0.0.0.0` and accept the
LAN-exposure posture shown in the Stats popup. The Enable-for-WSL toggle
writes env into the chosen distro but does NOT re-bind the running
gateway at runtime — that's deferred (see
`docs/design/windows-app/plan.md` § Out of Scope).

Mac dev box: full tray rendering targets Windows; on macOS, run
`python3 tray_app.py --smoke` to exercise the platform / dependency probes
without entering the pystray run loop.

### Option B — packaged `.exe` (experimental, not yet functional end-to-end)

Build once, distribute the resulting `.exe` to any Windows host. Requires
Python 3.10+ and PowerShell:

```powershell
.\build-windows.ps1
.\dist\copilot-gateway.exe
```

The script installs PyInstaller + `pystray` + `pillow` + `zstandard`, then
invokes `pyinstaller pyinstaller.spec` to produce `dist\copilot-gateway.exe` —
a single-file bundle of `tray_app.py` (entry), `gateway.py`, `demo.py`, and
`demo.html`. No Python install needed on the target machine.

Flags: `-SkipDeps` skips `pip install` if deps are already present;
`-Clean` removes `.\build\` and `.\dist\` before building.

**Known limitation (blocks end-to-end use)**: `tray_app.py:197` spawns
`gateway.py` via `subprocess.Popen([sys.executable, ...])`, which in a frozen
onefile build resolves `sys.executable` to the bootloader `.exe` (not a
Python interpreter). The tray UI launches, but the gateway subprocess will
not start until a follow-up PR teaches `tray_app.py` to detect
`getattr(sys, 'frozen', False)` and either re-spawn with a sub-command
sentinel or run gateway in-process. **Until that follow-up lands, use
Option A (from source) for a working setup**; this PR ships only the build
infrastructure. Tracked in `docs/design/windows-app/plan.md` § Out of Scope.

### Build artifacts

`build-windows.ps1` and `pyinstaller.spec` live at the repo root.
PyInstaller drops the executable in `dist\copilot-gateway.exe` and
intermediate work in `build\`. The script provisions a local `.venv\` for
build + runtime deps so it doesn't pollute your system Python. All three
directories (`.venv\`, `build\`, `dist\`) are gitignored.

## Files

| File | Purpose |
|------|---------|
| `gateway.py` | LLM gateway — dual mode, auto-auth, streaming, auto-launches demo + menu bar |
| `demo.py` | Demo web app with call-flow instrumentation |
| `demo.html` | Split-pane UI (chat + flow log + mode toggle) |
| `menubar.swift` | macOS menu bar indicator source (compile: `swiftc menubar.swift -o menubar -framework Cocoa`) |
| `tray_app.py` | Windows system-tray UI: stats / logs / Win + WSL toggles. Install: `pip install pystray pillow` |
| `mini-cli.py` | Lightweight terminal CLI (~100 lines) |
| `test-copilot-api.sh` | End-to-end test script |
| `docs/research.md` | How the Copilot API was discovered and how auth works |
| `docs/copilot-cli-internals.md` | Full reverse-engineering of the Copilot CLI |
| `docs/building-lightweight-cli.md` | Guide to building your own CLI |
| `docs/claude-code-integration.md` | Using the gateway with Claude Code CLI (backup/setup/restore) |
| `docs/api-shapes-reference.md` | Endpoint × model × built-in-tool capability matrix |

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
- **Server-side tool asymmetry**: Copilot honors OpenAI's `web_search` server tool on `/v1/responses` for GPT-5.x but rejects Anthropic's `web_search_20250305` on `/v1/messages` for Claude. See `docs/api-shapes-reference.md` § *Built-in server tools*. Workaround for Claude Code: invoke the `gpt` skill (copilot CLI) for ad-hoc web research instead of relying on Anthropic's `WebSearch`.
- **Codex CLI WebSocket fallback**: First prompt of each `cgcx` session shows a one-time `Falling back from WebSockets to HTTPS transport` message and ~5s delay. The Codex CLI tries WebSocket transport at `/v1/responses` first; the gateway returns `405 Method Not Allowed` to force immediate fallback. Subsequent prompts in the same session are direct HTTPS POST with no overhead.
