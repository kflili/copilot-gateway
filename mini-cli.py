#!/usr/bin/env python3
"""
Lightweight CLI for GitHub Copilot models. Zero dependencies.

Usage:
  python3 mini-cli.py                      # chat with claude-sonnet-4.6
  python3 mini-cli.py claude-opus-4.6      # chat with opus
  python3 mini-cli.py gpt-5.4             # chat with GPT-5.4 (uses /v1/responses)
  python3 mini-cli.py list                 # list available models

Requires: gh CLI authenticated (gh auth login)
Or: set GITHUB_TOKEN / GH_TOKEN env var
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

API = os.environ.get("COPILOT_API_URL", "https://api.githubcopilot.com")


def get_token():
    for v in ("GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN"):
        if os.environ.get(v):
            return os.environ[v].strip()
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except FileNotFoundError:
        pass
    print("Error: no GitHub token. Run 'gh auth login' or set GITHUB_TOKEN.", file=sys.stderr)
    sys.exit(1)


def list_models(token):
    req = urllib.request.Request(
        f"{API}/models",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    data = json.loads(urllib.request.urlopen(req, timeout=15).read())
    models = data.get("data", data) if isinstance(data, dict) else data
    for m in models:
        mid = m.get("id", "")
        vendor = m.get("vendor", "")
        endpoints = ", ".join(m.get("supported_endpoints", []))
        print(f"  {mid:30s}  {vendor:12s}  [{endpoints}]")


def chat_anthropic(model, token):
    """Multi-turn chat using Anthropic Messages API (/v1/messages)."""
    messages = []
    print(f"Model: {model}  (Anthropic format, type 'quit' to exit)\n")

    while True:
        try:
            user_input = input("> ")
        except (EOFError, KeyboardInterrupt):
            break
        if user_input.strip().lower() in ("quit", "exit", "q"):
            break
        if not user_input.strip():
            continue

        messages.append({"role": "user", "content": user_input})

        req = urllib.request.Request(
            f"{API}/v1/messages",
            data=json.dumps({
                "model": model,
                "max_tokens": 4096,
                "stream": True,
                "messages": messages,
            }).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            },
        )

        try:
            resp = urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as e:
            print(f"Error {e.code}: {e.read().decode()[:200]}")
            messages.pop()
            continue

        full_response = ""
        print()
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
        print("\n")
        if full_response:
            messages.append({"role": "assistant", "content": full_response})


def chat_openai_responses(model, token):
    """Single-turn using OpenAI Responses API (/v1/responses) for GPT-5.4."""
    print(f"Model: {model}  (Responses API, type 'quit' to exit)\n")

    while True:
        try:
            user_input = input("> ")
        except (EOFError, KeyboardInterrupt):
            break
        if user_input.strip().lower() in ("quit", "exit", "q"):
            break
        if not user_input.strip():
            continue

        req = urllib.request.Request(
            f"{API}/v1/responses",
            data=json.dumps({"model": model, "input": user_input}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

        try:
            resp = json.loads(urllib.request.urlopen(req, timeout=300).read())
            text = resp.get("output_text") or resp["output"][0]["content"][0]["text"]
            print(f"\n{text}\n")
        except urllib.error.HTTPError as e:
            print(f"Error {e.code}: {e.read().decode()[:200]}")


# Models that only support the Responses API
RESPONSES_ONLY = {"gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.2-codex"}


if __name__ == "__main__":
    token = get_token()
    model = sys.argv[1] if len(sys.argv) > 1 else "claude-sonnet-4.6"

    if model in ("--list", "-l", "list"):
        list_models(token)
    elif model in RESPONSES_ONLY:
        chat_openai_responses(model, token)
    else:
        chat_anthropic(model, token)
