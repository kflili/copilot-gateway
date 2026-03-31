#!/usr/bin/env bash
#
# test-copilot-api.sh - Demonstrate direct GitHub Copilot API usage
#
# Requires: gh CLI authenticated (gh auth login)
#
# Tests:
#   1. List available models
#   2. Claude Sonnet via /v1/messages (non-streaming)
#   3. Claude Opus via /v1/messages (streaming / SSE)
#   4. GPT-5.4 via /v1/responses
#
set -euo pipefail

API="https://api.githubcopilot.com"
TOKEN="${GITHUB_TOKEN:-$(gh auth token)}"

if [[ -z "$TOKEN" ]]; then
  echo "ERROR: No GitHub token. Run 'gh auth login' or set GITHUB_TOKEN." >&2
  exit 1
fi

AUTH="Authorization: Bearer $TOKEN"
CT="Content-Type: application/json"

divider() {
  echo ""
  echo "================================================================"
  echo "  $1"
  echo "================================================================"
  echo ""
}

# ---------- 1. List models ----------

divider "1. List Available Models"

echo "GET $API/models"
echo ""
curl -s "$API/models" \
  -H "$AUTH" | python3 -m json.tool 2>/dev/null || echo "(raw output above)"

# ---------- 2. Claude Sonnet - non-streaming ----------

divider "2. Claude Sonnet 4 via /v1/messages (non-streaming)"

echo "POST $API/v1/messages"
echo ""
curl -s "$API/v1/messages" \
  -H "$AUTH" \
  -H "$CT" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-sonnet-4.6",
    "max_tokens": 256,
    "messages": [
      {"role": "user", "content": "What is the GitHub Copilot API? Answer in one sentence."}
    ]
  }' | python3 -m json.tool 2>/dev/null || echo "(raw output above)"

# ---------- 3. Claude Opus - streaming ----------

divider "3. Claude Opus 4 via /v1/messages (streaming SSE)"

echo "POST $API/v1/messages  (stream=true)"
echo ""
curl -sN "$API/v1/messages" \
  -H "$AUTH" \
  -H "$CT" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-opus-4.6",
    "max_tokens": 256,
    "stream": true,
    "messages": [
      {"role": "user", "content": "Name three benefits of using AI coding assistants. Be brief."}
    ]
  }'

echo ""

# ---------- 4. GPT via /v1/responses ----------

divider "4. GPT-5.4 via /v1/responses"

echo "POST $API/v1/responses"
echo ""
curl -s "$API/v1/responses" \
  -H "$AUTH" \
  -H "$CT" \
  -d '{
    "model": "gpt-5.4",
    "input": "Explain what GitHub Copilot is in one sentence."
  }' | python3 -m json.tool 2>/dev/null || echo "(raw output above)"

divider "Done"
echo "All tests completed."
