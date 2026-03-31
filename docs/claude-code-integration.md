# Using the Gateway with Claude Code CLI

This guide explains how to configure Claude Code CLI to use the Copilot Gateway as its backend, giving you access to Copilot's Claude models (including Opus 4.6 with 1M context and extended thinking) through your GitHub Copilot subscription.

## How It Works

Claude Code uses the Anthropic Messages API (`/v1/messages`). The gateway exposes this exact endpoint, so Claude Code treats it as a regular Anthropic API server. The gateway handles auth (GitHub token), routing, and streaming transparently.

```
Claude Code CLI
    │ ANTHROPIC_AUTH_TOKEN="dummy"
    │ ANTHROPIC_BASE_URL="http://localhost:8787"
    ▼
Copilot Gateway (localhost:8787)
    │ Authorization: Bearer <github-token>
    │ Copilot-Integration-Id: copilot-developer-cli
    ▼
api.enterprise.githubcopilot.com/v1/messages
    │
    ▼
Claude (via Amazon Bedrock)
```

## Step 1: Back Up Current Settings

Before changing anything, back up your current Claude Code configuration:

```bash
# Back up settings
cp ~/.claude/settings.json ~/.claude/settings.json.backup

# Back up local settings (if exists)
cp ~/.claude/settings.local.json ~/.claude/settings.local.json.backup 2>/dev/null

# Verify backup
cat ~/.claude/settings.json.backup | head -5
echo "Backup saved."
```

## Step 2: Start the Gateway

```bash
cd ~/Projects/copilot-gateway
python3 gateway.py
```

Wait for the startup banner — confirm it says the models are loaded.

## Step 3: Configure Claude Code

### Option A: Environment Variables (temporary, per-session)

This is the safest approach — only affects the current terminal session:

```bash
ANTHROPIC_AUTH_TOKEN=dummy ANTHROPIC_BASE_URL=http://localhost:8787 claude
```

When you close this terminal, Claude Code reverts to normal.

### Option B: Settings File (persistent)

Edit `~/.claude/settings.json` and add the `env` block:

```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "dummy",
    "ANTHROPIC_BASE_URL": "http://localhost:8787"
  }
}
```

**Important**: If you already have an `env` section, merge the keys — don't replace the entire block. If you have `ANTHROPIC_API_KEY` set, remove it (it would send `x-api-key` which the Copilot API rejects).

### Option C: Direct Connection (no gateway needed)

Skip the gateway entirely — point Claude Code straight at the Copilot API:

```bash
# Get your token
gh auth token
```

Then in `~/.claude/settings.json`:
```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "<paste gh auth token output>",
    "ANTHROPIC_BASE_URL": "https://api.enterprise.githubcopilot.com"
  }
}
```

**Caveat**: This doesn't send the `Copilot-Integration-Id` header, so some models may not work. The gateway adds this header automatically. Also, if your `gh auth token` expires, you need to update it manually. The gateway handles refresh automatically.

## Step 4: Verify It Works

```bash
# With gateway running + env vars set:
claude

# In Claude Code, try a simple prompt to verify the connection works
```

If it works, you'll see normal Claude Code behavior. The gateway logs will show the requests flowing through.

## Restoring Original Settings

### If you used Option A (env vars):
Nothing to restore — just close the terminal or start a new session.

### If you used Option B (settings file):
```bash
# Restore from backup
cp ~/.claude/settings.json.backup ~/.claude/settings.json

# Verify
cat ~/.claude/settings.json | head -10
echo "Restored."
```

### If you used Option C (direct):
Same as Option B — restore from backup.

### Nuclear option (if backup is lost):
Remove the env vars from settings:
```bash
# Edit ~/.claude/settings.json and remove these keys from "env":
#   "ANTHROPIC_AUTH_TOKEN"
#   "ANTHROPIC_BASE_URL"
# Or delete the entire "env" block if you didn't have one before.
```

## FAQ

### Do I need extra config for extended thinking?

**No.** Claude Code manages thinking internally — it decides when to use extended thinking based on the model and task. The `thinking` parameters are sent in the request body, and the gateway passes them through. Confirmed working with Opus 4.6 and Sonnet 4.6.

### What about Haiku for summaries? Do I need per-model URL config?

**No.** `ANTHROPIC_BASE_URL` applies to ALL model calls. Claude Code uses multiple models internally:
- **Primary**: your selected model (Opus, Sonnet, etc.)
- **Summary/compaction**: `claude-haiku-4-5-20251001`
- **Both go through the same URL** — Claude Code sends the model name in each request body

All model name formats are accepted by the Copilot API (tested):
| Claude Code sends | Copilot API accepts? | Responds as |
|---|---|---|
| `claude-sonnet-4-6-20250514` | Yes | `claude-sonnet-4-6` |
| `claude-haiku-4-5-20251001` | Yes | `claude-haiku-4-5-20251001` |
| `claude-opus-4-6-20250514` | Yes | `claude-opus-4-6` |
| `claude-opus-4.6-1m` | Yes | `claude-opus-4-6` |

### Can I override which model Claude Code uses?

Yes, via env vars (no settings file changes needed):
```bash
# Use Opus 4.6 as primary, haiku for summaries (default)
ANTHROPIC_AUTH_TOKEN=dummy ANTHROPIC_BASE_URL=http://localhost:8787 \
  ANTHROPIC_MODEL=claude-opus-4.6 claude

# Use Opus 4.6 1M context as primary
ANTHROPIC_AUTH_TOKEN=dummy ANTHROPIC_BASE_URL=http://localhost:8787 \
  ANTHROPIC_MODEL=claude-opus-4.6-1m claude
```

## Limitations

### What works
- Chat, multi-turn conversation
- Streaming responses
- Tool use (read/write files, bash, grep, etc.)
- Extended thinking (Opus 4.6, Sonnet 4.6 — budget 1K-32K tokens)
- All Claude models (Haiku, Sonnet, Opus, Opus-1M) through the same URL
- Date-suffixed model names (`claude-sonnet-4-6-20250514`) accepted

### What might not work
- **Token counting**: Claude Code's token budget calculations assume direct Anthropic API responses. The Copilot API returns slightly different `usage` fields (e.g., includes `copilot_usage`), which shouldn't cause issues but is worth noting.
- **Rate limits**: You're bound by your Copilot plan's quota, not Anthropic's rate limits. Enterprise plans typically have unlimited requests.
- **Anthropic-specific headers**: Some Anthropic beta features that require special headers (like `anthropic-beta`) may not be forwarded correctly through the Copilot proxy.

## Recommended Approach

**Use Option A (env vars) for testing first.** Only move to Option B after you've confirmed everything works. This way, if something breaks, just close the terminal — zero risk to your Claude Code setup.

```bash
# Test session — zero risk
ANTHROPIC_AUTH_TOKEN=dummy ANTHROPIC_BASE_URL=http://localhost:8787 claude

# If it works, make it persistent (Option B)
# If it doesn't, just close the terminal — nothing changed
```

## Switching Between Gateway and Direct Anthropic

If you want to quickly toggle between the gateway and your normal Anthropic API:

```bash
# Use gateway (Copilot-backed)
alias claude-copilot='ANTHROPIC_AUTH_TOKEN=dummy ANTHROPIC_BASE_URL=http://localhost:8787 claude'

# Use normal Anthropic API (default)
alias claude-direct='claude'
```

Add these to your `~/.zshrc` for convenience. Then:
```bash
claude-copilot   # → through gateway → Copilot API
claude           # → normal Anthropic API (default settings)
```
