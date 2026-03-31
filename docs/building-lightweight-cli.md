# Building a Lightweight CLI

Guide to building your own lightweight AI CLI using the Copilot API directly, without the 128 MB Copilot CLI and its heavy features.

## What the Copilot CLI includes that you probably don't need

The full Copilot CLI is 128 MB because it bundles:
- 19 tree-sitter WASM parsers (29 MB) — syntax highlighting
- ripgrep binary (29 MB) — code search
- sharp image processing (11 MB) — screenshots
- clipboard library (8.7 MB) — clipboard ops
- Native prebuilds for 6 platforms (25 MB)
- Full SDK with TypeScript types (14 MB)
- 5 agent definitions, MCP framework, session database

For a lightweight CLI, you just need: HTTP client + terminal I/O.

## Minimum viable implementation

### The API is simple

All you need is one HTTP POST call. The two critical headers are:
- `Authorization: Bearer <token>` — your GitHub OAuth token
- `Copilot-Integration-Id: copilot-developer-cli` — **required** to unlock full model access

```python
import json, urllib.request

def ask(model, prompt, token):
    req = urllib.request.Request(
        "https://api.enterprise.githubcopilot.com/v1/messages",
        data=json.dumps({
            "model": model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Copilot-Integration-Id": "copilot-developer-cli",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        },
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=120).read())
    return resp["content"][0]["text"]
```

Without the `Copilot-Integration-Id` header, many models will return 403 or "model_not_supported".

### Streaming version

For real-time output:

```python
import json, urllib.request

def ask_stream(model, prompt, token):
    req = urllib.request.Request(
        "https://api.enterprise.githubcopilot.com/v1/messages",
        data=json.dumps({
            "model": model,
            "max_tokens": 4096,
            "stream": True,
            "messages": [{"role": "user", "content": prompt}],
        }).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Copilot-Integration-Id": "copilot-developer-cli",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        },
    )
    resp = urllib.request.urlopen(req, timeout=300)
    for line in resp:
        line = line.decode().strip()
        if line.startswith("data: ") and line != "data: [DONE]":
            event = json.loads(line[6:])
            if event.get("type") == "content_block_delta":
                text = event.get("delta", {}).get("text", "")
                print(text, end="", flush=True)
    print()
```

### Complete lightweight CLI (Python, ~80 lines)

```python
#!/usr/bin/env python3
"""Lightweight CLI for GitHub Copilot models. Zero dependencies."""

import json, os, subprocess, sys, urllib.request

API = "https://api.enterprise.githubcopilot.com"  # or resolve via copilot_internal/user

def get_token():
    for v in ("GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN"):
        if os.environ.get(v):
            return os.environ[v].strip()
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return r.stdout.strip()
    except FileNotFoundError:
        pass
    print("Error: no GitHub token. Run 'gh auth login'.", file=sys.stderr)
    sys.exit(1)

def list_models(token):
    req = urllib.request.Request(f"{API}/models",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "Copilot-Integration-Id": "copilot-developer-cli"})
    data = json.loads(urllib.request.urlopen(req, timeout=15).read())
    models = data.get("data", data) if isinstance(data, dict) else data
    for m in models:
        mid = m.get("id", "")
        vendor = m.get("vendor", "")
        endpoints = m.get("supported_endpoints", [])
        print(f"  {mid:30s}  {vendor:12s}  {endpoints}")

def chat(model, token):
    messages = []
    print(f"Model: {model}  (type 'quit' to exit)")
    while True:
        try:
            user_input = input("\n> ")
        except (EOFError, KeyboardInterrupt):
            break
        if user_input.strip().lower() in ("quit", "exit", "q"):
            break
        messages.append({"role": "user", "content": user_input})

        req = urllib.request.Request(f"{API}/v1/messages",
            data=json.dumps({
                "model": model, "max_tokens": 4096, "stream": True,
                "messages": messages,
            }).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Copilot-Integration-Id": "copilot-developer-cli",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            })

        print()
        full_response = ""
        resp = urllib.request.urlopen(req, timeout=300)
        for line in resp:
            line = line.decode().strip()
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    event = json.loads(line[6:])
                    if event.get("type") == "content_block_delta":
                        text = event.get("delta", {}).get("text", "")
                        print(text, end="", flush=True)
                        full_response += text
                except json.JSONDecodeError:
                    pass
        print()
        messages.append({"role": "assistant", "content": full_response})

if __name__ == "__main__":
    token = get_token()
    model = sys.argv[1] if len(sys.argv) > 1 else "claude-sonnet-4.6"

    if model in ("--list", "-l", "list"):
        list_models(token)
    else:
        chat(model, token)
```

Usage:
```bash
python3 mini-cli.py                      # chat with claude-sonnet-4.6
python3 mini-cli.py claude-opus-4.6      # chat with opus
python3 mini-cli.py list                 # list available models
```

### Node.js version (~60 lines)

```javascript
#!/usr/bin/env node
// Lightweight CLI for GitHub Copilot models. Zero dependencies.

const https = require('https');
const { execSync } = require('child_process');
const readline = require('readline');

const API_HOST = 'api.enterprise.githubcopilot.com';
const token = process.env.GITHUB_TOKEN
  || process.env.GH_TOKEN
  || execSync('gh auth token', { encoding: 'utf8' }).trim();
const model = process.argv[2] || 'claude-sonnet-4.6';

const messages = [];
const rl = readline.createInterface({ input: process.stdin, output: process.stdout });

function ask() {
  rl.question('\n> ', (input) => {
    if (['quit', 'exit', 'q'].includes(input.trim().toLowerCase())) {
      rl.close();
      return;
    }
    messages.push({ role: 'user', content: input });

    const body = JSON.stringify({
      model, max_tokens: 4096, stream: true, messages,
    });

    const req = https.request({
      hostname: API_HOST, path: '/v1/messages', method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Copilot-Integration-Id': 'copilot-developer-cli',
        'Content-Type': 'application/json',
        'anthropic-version': '2023-06-01',
        'Content-Length': Buffer.byteLength(body),
      },
    }, (res) => {
      let fullResponse = '';
      let buffer = '';
      process.stdout.write('\n');
      res.on('data', (chunk) => {
        buffer += chunk.toString();
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
          if (line.startsWith('data: ') && line !== 'data: [DONE]') {
            try {
              const event = JSON.parse(line.slice(6));
              if (event.type === 'content_block_delta') {
                const text = event.delta?.text || '';
                process.stdout.write(text);
                fullResponse += text;
              }
            } catch {}
          }
        }
      });
      res.on('end', () => {
        process.stdout.write('\n');
        messages.push({ role: 'assistant', content: fullResponse });
        ask();
      });
    });
    req.write(body);
    req.end();
  });
}

console.log(`Model: ${model}  (type 'quit' to exit)`);
ask();
```

## Adding tool use

If you want your lightweight CLI to use tools (like reading files or running commands), add tools to the request:

```python
tools = [
    {
        "name": "read_file",
        "description": "Read a file's contents",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]

# Add to request body:
body = {
    "model": model,
    "max_tokens": 4096,
    "tools": tools,
    "messages": messages,
}

# When response has stop_reason="tool_use", execute the tool and send result back:
for block in response["content"]:
    if block["type"] == "tool_use":
        tool_name = block["name"]
        tool_input = block["input"]
        tool_id = block["id"]

        if tool_name == "read_file":
            result = open(tool_input["path"]).read()
        elif tool_name == "run_command":
            result = subprocess.run(tool_input["command"], shell=True,
                                    capture_output=True, text=True).stdout

        messages.append({"role": "assistant", "content": response["content"]})
        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": result}],
        })
        # Then send another request to get the model's response
```

## Using the gateway instead of direct API

If you run the gateway (`python3 gateway.py`), your lightweight CLI doesn't even need to handle auth:

```python
API = "http://localhost:8787"  # instead of "https://api.githubcopilot.com"
# No token needed — gateway handles it
headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
```

## Feature comparison

| Feature | Copilot CLI (128 MB) | Lightweight CLI (~5 KB) | Gateway + CLI |
|---------|---------------------|------------------------|---------------|
| Chat with models | Yes | Yes | Yes |
| Streaming | Yes | Yes | Yes |
| Tool use | Yes (12+ tools) | Add your own | Add your own |
| Syntax highlighting | Yes (19 parsers) | No (use terminal) | No |
| File search (ripgrep) | Bundled | Use system rg | Use system rg |
| Image processing | Yes (sharp) | No | No |
| MCP servers | Yes | No (add if needed) | No |
| Session persistence | SQLite DB | In-memory | In-memory |
| Auth management | Full OAuth flow | `gh auth token` | Gateway handles |
| Multiple platforms | 6 platforms | Anywhere with Python/Node | Anywhere |
| Dependencies | Node.js + natives | Python stdlib only | Python stdlib |
| Size | 128 MB | ~5 KB | ~15 KB |

## OpenAI format (for GPT models)

For GPT-5.x models, use the chat completions or responses format. Remember to include the `Copilot-Integration-Id` header:

```python
headers = {
    "Authorization": f"Bearer {token}",
    "Copilot-Integration-Id": "copilot-developer-cli",
    "Content-Type": "application/json",
}

# Chat completions (GPT-5.2, GPT-5.1, GPT-5-mini)
body = {
    "model": "gpt-5.2",
    "max_tokens": 4096,
    "messages": [{"role": "user", "content": prompt}],
}
url = f"{API}/chat/completions"  # note: no /v1 prefix for Copilot API

# Responses API (GPT-5.4, GPT-5.3-codex, GPT-5.2-codex, goldeneye)
body = {
    "model": "gpt-5.4",
    "input": prompt,
}
url = f"{API}/v1/responses"
```

## Key insight

The Copilot CLI is essentially:
1. **Auth layer** → resolve GitHub token, validate with `/copilot_internal/user` to get API endpoint
2. **CAPI client** → OpenAI SDK extended with `Copilot-Integration-Id: copilot-developer-cli` header, pointed at `api.enterprise.githubcopilot.com`
3. **Agent loop** → system prompt + tools + user input → model → tool execution → repeat
4. **UI layer** → terminal rendering, markdown, syntax highlighting
5. **Model filter** → hardcoded allowlist of 18 models (hides some API-available models like goldeneye, minimax)

For a lightweight alternative, you only need #1 and #2. The gateway handles both for you, so you really just need an HTTP client.

### Two auth modes for different model access

| Mode | Header | Models | How to get token |
|---|---|---|---|
| CLI | `copilot-developer-cli` | 19 | `gh auth token` (already have it) |
| VS Code | `vscode-chat` | 22 (+Gemini 3.1 Pro) | OAuth device flow with client ID `01ab8ac9400c4e429b23` |

Same API URL, same billing. The header determines access. For most use cases, CLI mode is sufficient — VS Code mode adds Gemini 3.1 Pro and a few Fireworks router models.
