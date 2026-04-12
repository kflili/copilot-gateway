#!/usr/bin/env python3
"""
Copilot LLM Gateway — Local API gateway backed by GitHub Copilot.

Any product can call this like a normal LLM provider:
  - Anthropic SDK/clients → http://localhost:8787
  - OpenAI SDK/clients    → http://localhost:8787

Auth is handled automatically (GitHub token via `gh auth token`).
Clients can send any dummy API key or none at all.

Endpoints:
  GET  /v1/models              — list available models
  POST /v1/messages            — Anthropic Messages API
  POST /v1/chat/completions    — OpenAI Chat Completions API
  POST /chat/completions       — OpenAI Chat Completions API (alias)
  POST /v1/responses           — OpenAI Responses API (GPT-5.4)
  GET  /health                 — health check

Usage:
  python3 gateway.py                          # uses gh auth token
  python3 gateway.py --port 9000              # custom port
  GITHUB_TOKEN=gho_xxx python3 gateway.py     # explicit token
"""

import http.server
import json
import logging
import os
import pathlib
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

# ─── Config ───────────────────────────────────────────────────────────────────

LISTEN_HOST = os.environ.get("GATEWAY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("GATEWAY_PORT", "8787"))
UPSTREAM = os.environ.get("GATEWAY_UPSTREAM", "https://api.githubcopilot.com")
HERE = pathlib.Path(__file__).parent
LOG_DIR = HERE / "logs"

# ─── Per-Session Logging ─────────────────────────────────────────────────────

import re

gw_logger = logging.getLogger("gateway")
SESSION_LOG_DIR = None  # type: pathlib.Path | None  # set in setup_logging()

_VALID_SESSION_ID = re.compile(r'^[A-Za-z0-9_-]+$')


def _generate_session_id() -> str:
    """Generate a compact session ID: HHMMSS_<4-char-hex>."""
    ts = datetime.now().strftime("%H%M%S")
    suffix = secrets.token_hex(2)  # 4 hex chars
    return f"{ts}_{suffix}"


def setup_logging() -> pathlib.Path:
    """Create per-session log directory and configure logging.

    Returns the session log directory path.
    """
    global SESSION_LOG_DIR

    session_id = os.environ.get("GATEWAY_SESSION_ID", "")
    if not session_id or not _VALID_SESSION_ID.match(session_id):
        session_id = _generate_session_id()
    date_str = datetime.now().strftime("%Y-%m-%d")
    session_dir = LOG_DIR / date_str / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    SESSION_LOG_DIR = session_dir

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(session_dir / "gateway.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    # Update logs/latest symlink (best-effort, atomic)
    latest = LOG_DIR / "latest"
    relative_target = pathlib.Path(date_str) / session_id
    try:
        tmp_link = LOG_DIR / f".latest_tmp_{os.getpid()}"
        try:
            tmp_link.unlink()
        except FileNotFoundError:
            pass
        tmp_link.symlink_to(relative_target)
        tmp_link.rename(latest)
    except OSError:
        try:
            try:
                latest.unlink()
            except FileNotFoundError:
                pass
            latest.symlink_to(relative_target)
        except OSError:
            pass  # non-fatal — logs still work, just no convenience symlink

    return session_dir

# ─── Token Manager ────────────────────────────────────────────────────────────

class TokenManager:
    """Manages GitHub token with auto-refresh.

    Supports two modes:
      - "cli" (default): uses gh auth token → api.githubcopilot.com (fewer models)
      - "vscode": OAuth device flow with VS Code client ID → Copilot JWT
                  → api.enterprise.githubcopilot.com (all models incl Gemini, MiniMax, etc.)
    """

    VSCODE_CLIENT_ID = "01ab8ac9400c4e429b23"

    def __init__(self, mode: str = "cli"):
        self._token: str = ""
        self._gh_token: str = ""  # raw GitHub OAuth token (for JWT exchange)
        self._lock = threading.Lock()
        self._last_refresh: float = 0
        self._min_refresh_interval = 30
        self._jwt_expires: float = 0
        self._jwt_exchange_failed = False
        self.mode = mode
        self.api_base = ""  # set after first refresh
        self.refresh()

    @property
    def token(self) -> str:
        # For vscode mode with working JWT, auto-refresh before expiry
        if (self.mode == "vscode" and not self._jwt_exchange_failed
                and self._jwt_expires > 0
                and time.time() > self._jwt_expires - 60):
            self._refresh_jwt()
        return self._token

    def refresh(self) -> str:
        with self._lock:
            now = time.time()
            if now - self._last_refresh < self._min_refresh_interval:
                return self._token
            self._last_refresh = now

            # Resolve the GitHub OAuth token
            self._gh_token = self._resolve_gh_token()

            # Always resolve API base first (works for both modes)
            if not self.api_base:
                self._resolve_api_base()

            if self.mode == "vscode":
                # Try JWT exchange; if it fails, use raw token (still works)
                if not self._jwt_exchange_failed:
                    self._refresh_jwt_inner()
                else:
                    self._token = self._gh_token
            else:
                self._token = self._gh_token

            return self._token

    def _resolve_gh_token(self) -> str:
        # Try env vars first
        for var in ("GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN"):
            val = os.environ.get(var, "").strip()
            if val:
                return val

        # Fall back to gh CLI
        try:
            result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        if not self._gh_token:
            log("ERROR: no GitHub token. Set GITHUB_TOKEN or run 'gh auth login'.")
            sys.exit(1)
        return self._gh_token

    def _resolve_api_base(self):
        """Get the correct API base from copilot_internal/user."""
        try:
            req = urllib.request.Request(
                "https://api.github.com/copilot_internal/user",
                headers={"Authorization": f"token {self._gh_token}",
                         "Accept": "application/json"},
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
            self.api_base = resp.get("endpoints", {}).get("api", "")
            if self.api_base:
                log(f"API base from copilot_internal/user: {self.api_base}")
        except Exception as e:
            log(f"copilot_internal/user failed: {e}")

    def _refresh_jwt(self):
        with self._lock:
            self._refresh_jwt_inner()

    def _refresh_jwt_inner(self):
        """Exchange GitHub token for Copilot JWT."""
        try:
            req = urllib.request.Request(
                "https://api.github.com/copilot_internal/v2/token",
                headers={"Authorization": f"token {self._gh_token}",
                         "Accept": "application/json"},
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
            if "token" in resp:
                self._token = resp["token"]
                self._jwt_expires = resp.get("expires_at", time.time() + 1500)
                self.api_base = resp.get("endpoints", {}).get("api", self.api_base)
                log(f"JWT refreshed, expires in {int(self._jwt_expires - time.time())}s, api={self.api_base}")
            else:
                log(f"JWT exchange not available, using raw OAuth token (still works)")
                self._jwt_exchange_failed = True
                self._token = self._gh_token
        except Exception as e:
            log(f"JWT exchange not available ({e}), using raw OAuth token")
            self._jwt_exchange_failed = True
            self._token = self._gh_token

    def force_refresh(self) -> str:
        with self._lock:
            self._last_refresh = 0
        return self.refresh()

    @classmethod
    def do_device_flow(cls) -> str:
        """Run OAuth device flow with VS Code client ID. Returns the OAuth token."""
        import urllib.parse

        # Request device code
        data = urllib.parse.urlencode({
            "client_id": cls.VSCODE_CLIENT_ID,
            "scope": "read:user,user:email,repo,workflow",
        }).encode()
        req = urllib.request.Request(
            "https://github.com/login/device/code",
            data=data,
            headers={"Accept": "application/json"},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        user_code = resp["user_code"]
        device_code = resp["device_code"]
        interval = resp.get("interval", 5)

        print(f"\n  Open https://github.com/login/device")
        print(f"  Enter code: {user_code}\n")

        # Poll until authorized
        while True:
            time.sleep(interval)
            data = urllib.parse.urlencode({
                "client_id": cls.VSCODE_CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            }).encode()
            req = urllib.request.Request(
                "https://github.com/login/oauth/access_token",
                data=data,
                headers={"Accept": "application/json"},
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
            if "access_token" in resp:
                return resp["access_token"]
            if resp.get("error") == "expired_token":
                print("  Code expired. Please restart.")
                sys.exit(1)
            # else: authorization_pending, slow_down — keep polling


# Will be initialized in main()
token_mgr: TokenManager = None  # type: ignore

# ─── Models Cache ─────────────────────────────────────────────────────────────

class ModelsCache:
    """Caches the upstream model list, refreshes periodically."""

    def __init__(self):
        self._data: list = []
        self._lock = threading.Lock()
        self._last_fetch: float = 0
        self._ttl = 300  # 5 min cache

    def get(self) -> list:
        with self._lock:
            if time.time() - self._last_fetch > self._ttl:
                self._fetch()
            return self._data

    def _fetch(self):
        try:
            upstream = _get_upstream()
            headers = {"Authorization": f"Bearer {token_mgr.token}",
                       "Accept": "application/json"}
            if token_mgr.mode == "vscode":
                headers["Copilot-Integration-Id"] = "vscode-chat"
            else:
                headers["Copilot-Integration-Id"] = "copilot-developer-cli"
            req = urllib.request.Request(f"{upstream}/models", headers=headers)
            resp = urllib.request.urlopen(req, timeout=15)
            raw = json.loads(resp.read())
            self._data = raw.get("data", raw) if isinstance(raw, dict) else raw
            self._last_fetch = time.time()
            log(f"models cache refreshed: {len(self._data)} models from {upstream}")
        except Exception as e:
            log(f"models cache refresh failed: {e}")
            if not self._data:
                self._data = []


models_cache = ModelsCache()


def _get_upstream() -> str:
    """Return the correct upstream URL based on token mode."""
    if token_mgr and token_mgr.api_base:
        return token_mgr.api_base
    return UPSTREAM

# ─── Helpers ──────────────────────────────────────────────────────────────────

def log(msg: str):
    gw_logger.info(msg)


def masked_token(t: str) -> str:
    return t[:6] + "..." + t[-4:] if len(t) > 10 else "****"

# ─── Gateway Handler ──────────────────────────────────────────────────────────

class GatewayHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # we do our own logging

    # ── Route dispatch ──

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/v1/models", "/models"):
            self._handle_models()
        elif path == "/health":
            self._handle_health()
        else:
            self._forward()

    def do_POST(self):
        self._forward()

    def do_OPTIONS(self):
        self._send_cors_preflight()

    # ── /v1/models ──

    def _handle_models(self):
        models = models_cache.get()
        # Return in OpenAI-compatible list format (works for Anthropic clients too)
        body = json.dumps({
            "object": "list",
            "data": [self._format_model(m) for m in models],
        }, indent=2).encode()

        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        log(f"GET {self.path} → {len(models)} models")

    def _format_model(self, m: dict) -> dict:
        mid = m.get("id", "")
        vendor = m.get("vendor", "")
        endpoints = m.get("supported_endpoints", [])
        caps = m.get("capabilities", {})
        limits = caps.get("limits", {})
        supports = caps.get("supports", {})
        return {
            "id": mid,
            "object": "model",
            "created": 0,
            "owned_by": vendor.lower() if vendor else "github-copilot",
            "name": m.get("name", mid),
            "vendor": vendor,
            "supported_endpoints": endpoints,
            "context_window": limits.get("max_context_window_tokens", 0),
            "max_output_tokens": limits.get("max_output_tokens", 0),
            "supports_streaming": supports.get("streaming", False),
            "supports_tools": supports.get("tool_calls", False),
            "supports_vision": supports.get("vision", False),
        }

    # ── /health ──

    def _handle_health(self):
        body = json.dumps({
            "status": "ok",
            "upstream": _get_upstream(),
            "mode": token_mgr.mode if token_mgr else "?",
            "models_cached": len(models_cache.get()),
            "token_present": bool(token_mgr.token),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── Path mapping ──
    # Copilot API paths differ from standard SDK paths in some cases.
    PATH_MAP = {
        "/v1/chat/completions": "/chat/completions",  # OpenAI SDK sends /v1/, Copilot wants no /v1/
    }

    # ── Forward (the core proxy) ──

    def _forward(self):
        method = self.command
        path = self.PATH_MAP.get(self.path.split("?")[0], self.path)

        # Read request body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else None

        # Detect streaming
        is_stream = False
        if body:
            try:
                is_stream = json.loads(body).get("stream", False)
            except (json.JSONDecodeError, AttributeError):
                pass

        # Build upstream request
        url = _get_upstream() + path
        headers = self._upstream_headers(content_length)

        log(f"{method} {path} → {url} (stream={is_stream})")

        # Try the request, auto-refresh token on 401
        resp, error_body, error_code = self._do_upstream(method, url, headers, body)
        if error_code == 401:
            log("  ← 401, refreshing token...")
            token_mgr.force_refresh()
            headers["Authorization"] = f"Bearer {token_mgr.token}"
            resp, error_body, error_code = self._do_upstream(method, url, headers, body)

        if resp is None:
            # Error response
            self.send_response(error_code or 502)
            self._cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(error_body or b'{"error":"upstream unavailable"}')
            log(f"  ← {error_code or 502} error")
            return

        # Forward success response
        self.send_response(resp.status)
        self._cors_headers()
        for k, v in resp.headers.items():
            if k.lower() not in ("transfer-encoding", "connection", "keep-alive"):
                self.send_header(k, v)
        self.end_headers()

        if is_stream:
            total = 0
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                total += len(chunk)
            log(f"  ← {resp.status} streamed {total} bytes")
        else:
            data = resp.read()
            self.wfile.write(data)
            log(f"  ← {resp.status} ({len(data)} bytes)")

    def _do_upstream(self, method, url, headers, body):
        """Returns (response, None, None) on success or (None, error_body, status_code) on error."""
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=300)
            return resp, None, None
        except urllib.error.HTTPError as e:
            return None, e.read(), e.code
        except urllib.error.URLError as e:
            return None, json.dumps({"error": str(e.reason)}).encode(), 502

    def _upstream_headers(self, body_len: int) -> dict:
        headers = {}
        for key in self.headers:
            lk = key.lower()
            # Drop client auth and hop-by-hop headers — we supply our own auth
            if lk in ("host", "connection", "transfer-encoding",
                       "x-api-key", "authorization"):
                continue
            headers[key] = self.headers[key]
        headers["Authorization"] = f"Bearer {token_mgr.token}"
        if token_mgr.mode == "vscode":
            headers["Copilot-Integration-Id"] = "vscode-chat"
        else:
            headers["Copilot-Integration-Id"] = "copilot-developer-cli"
        headers.setdefault("Content-Type", "application/json")
        if body_len:
            headers["Content-Length"] = str(body_len)
        return headers

    # ── CORS ──

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, x-api-key, anthropic-version, openai-intent")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, PUT, DELETE, PATCH, OPTIONS")

    def _send_cors_preflight(self):
        self.send_response(200)
        self._cors_headers()
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()


# ─── Main ─────────────────────────────────────────────────────────────────────

TOKEN_FILE = HERE / ".gateway-token.json"


def _save_token(token: str, mode: str):
    """Persist the OAuth token so re-auth is never needed."""
    TOKEN_FILE.write_text(json.dumps({"token": token, "mode": mode}))
    log(f"Token saved to {TOKEN_FILE}")


def _load_token() -> tuple[str, str] | tuple[None, None]:
    """Load persisted token. Returns (token, mode) or (None, None)."""
    if TOKEN_FILE.exists():
        try:
            data = json.loads(TOKEN_FILE.read_text())
            return data["token"], data["mode"]
        except Exception:
            pass
    return None, None


def main():
    global token_mgr
    import argparse
    parser = argparse.ArgumentParser(description="Copilot LLM Gateway")
    parser.add_argument("--port", type=int, default=LISTEN_PORT)
    parser.add_argument("--host", default=LISTEN_HOST)
    parser.add_argument("--mode", choices=["cli", "vscode"], default=None,
                        help="Auth mode: 'cli' (gh token, fewer models) or "
                             "'vscode' (OAuth device flow, all models). "
                             "Default: auto-detect from saved token or 'cli'.")
    parser.add_argument("--login", action="store_true",
                        help="Force re-authentication (VS Code OAuth device flow)")
    args = parser.parse_args()

    host, port = args.host, args.port

    # Set up per-session logging (must happen before any log() calls)
    session_dir = setup_logging()

    # Resolve mode and token
    saved_token, saved_mode = _load_token()

    if args.login:
        # Force new device flow login
        print("[gateway] Starting VS Code OAuth device flow...")
        oauth_token = TokenManager.do_device_flow()
        _save_token(oauth_token, "vscode")
        os.environ["GITHUB_TOKEN"] = oauth_token
        mode = "vscode"
    elif args.mode:
        mode = args.mode
        if mode == "vscode" and saved_mode == "vscode" and saved_token:
            # Reuse saved VS Code token
            os.environ["GITHUB_TOKEN"] = saved_token
            log("Using saved VS Code OAuth token")
        elif mode == "vscode" and not saved_token:
            # Need to login first
            print("[gateway] VS Code mode requires OAuth login (first time only)...")
            oauth_token = TokenManager.do_device_flow()
            _save_token(oauth_token, "vscode")
            os.environ["GITHUB_TOKEN"] = oauth_token
    elif saved_mode == "vscode" and saved_token:
        # Auto-detect: use saved vscode token
        mode = "vscode"
        os.environ["GITHUB_TOKEN"] = saved_token
        log("Auto-detected saved VS Code token")
    else:
        mode = "cli"

    token_mgr = TokenManager(mode=mode)
    upstream = _get_upstream()

    mode_label = f"{mode} → {upstream}"
    print("╔══════════════════════════════════════════════════════════╗")
    print("║           Copilot LLM Gateway                           ║")
    print("╠══════════════════════════════════════════════════════════╣")
    print(f"║  Listening:  http://{host}:{port:<28}║")
    print(f"║  Mode:       {mode:<44}║")
    print(f"║  Upstream:   {upstream:<44}║")
    print(f"║  Token:      {masked_token(token_mgr.token):<44}║")
    print(f"║  Logs:       {str(session_dir.relative_to(HERE)):<44}║")
    print("╠══════════════════════════════════════════════════════════╣")
    print("║  Endpoints:                                              ║")
    print("║    GET  /v1/models           — list models               ║")
    print("║    POST /v1/messages         — Anthropic API             ║")
    print("║    POST /v1/chat/completions — OpenAI API                ║")
    print("║    POST /v1/responses        — OpenAI Responses API      ║")
    print("║    GET  /health              — health check              ║")
    print("╠══════════════════════════════════════════════════════════╣")
    print("║  Usage:  any client → http://localhost:8787              ║")
    print("║          api_key = \"dummy\"  (ignored)                   ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    # Pre-warm model cache
    models_cache.get()

    # Launch demo app (serves UI on port 8788)
    demo_proc = None
    demo_log_file = None
    demo_script = HERE / "demo.py"
    if demo_script.exists():
        try:
            demo_log_file = open(session_dir / "demo.log", "w")
            demo_proc = subprocess.Popen(
                [sys.executable, str(demo_script)],
                stdout=demo_log_file, stderr=subprocess.STDOUT,
            )
            log(f"demo app started (PID {demo_proc.pid}) → http://localhost:8788")
        except Exception as e:
            log(f"demo app failed: {e}")
            if demo_log_file:
                demo_log_file.close()
                demo_log_file = None

    # Launch menu bar indicator if binary exists
    menubar_proc = None
    menubar_bin = HERE / "menubar"
    if menubar_bin.exists():
        try:
            menubar_proc = subprocess.Popen([str(menubar_bin)])
            log(f"menu bar indicator started (PID {menubar_proc.pid})")
        except Exception as e:
            log(f"menu bar indicator failed: {e}")

    server = http.server.ThreadingHTTPServer((host, port), GatewayHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[gateway] shutting down.")
        server.server_close()
        if demo_proc:
            demo_proc.terminate()
        if demo_log_file:
            demo_log_file.close()
        if menubar_proc:
            menubar_proc.terminate()


if __name__ == "__main__":
    main()
