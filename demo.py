#!/usr/bin/env python3
"""
Copilot Gateway Demo — Interactive chat with full call-flow visibility.

Shows how any product talks to the gateway, and how the gateway
forwards to api.githubcopilot.com.  Every HTTP hop is logged to
the browser in real time.

Usage:
  python3 demo.py                     # assumes gateway.py running on 8787
  python3 demo.py --start-gateway     # auto-start gateway.py as subprocess
  python3 demo.py --port 8788         # custom port

Open http://localhost:8788 in your browser.
"""

import argparse
import http.server
import json
import logging
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from datetime import datetime

# ─── Config ───────────────────────────────────────────────────────────────────

DEMO_HOST = "127.0.0.1"
DEMO_PORT = 8788
GATEWAY_URL = "http://127.0.0.1:8787"
GATEWAY_UPSTREAM = "https://api.githubcopilot.com"
HERE = pathlib.Path(__file__).parent
LOG_DIR = HERE / "logs"

# ─── File Logging ─────────────────────────────────────────────────────────────

LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "demo.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("demo")

# ─── SSE Event Bus ────────────────────────────────────────────────────────────

class EventBus:
    """Fan-out SSE event bus.  Each connected browser gets its own queue."""

    def __init__(self):
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s is not q]

    def publish(self, event_type: str, data: dict):
        data["_ts"] = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        payload = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        logger.debug("event: %s %s", event_type, json.dumps(data, ensure_ascii=False)[:200])
        with self._lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    pass  # slow consumer, drop


bus = EventBus()

# ─── Helpers ──────────────────────────────────────────────────────────────────

def mask_token(headers: dict) -> dict:
    """Return a copy with Authorization token masked."""
    out = dict(headers)
    auth = out.get("Authorization", "")
    if auth.startswith("Bearer ") and len(auth) > 20:
        token = auth[7:]
        out["Authorization"] = f"Bearer {token[:6]}...{token[-4:]}"
    return out


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

# ─── Models metadata (cached) ────────────────────────────────────────────────

_models_cache: dict[str, list] = {}  # mode -> models
_models_lock = threading.Lock()


def _resolve_cli_token() -> str:
    """Get the gh CLI token."""
    for var in ("GH_TOKEN",):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


def _resolve_vscode_token() -> str:
    """Get saved VS Code OAuth token."""
    token_file = HERE / ".gateway-token.json"
    if token_file.exists():
        try:
            data = json.loads(token_file.read_text())
            if data.get("mode") == "vscode":
                return data["token"]
        except Exception:
            pass
    return ""


def _get_copilot_api_base(token: str) -> str:
    """Resolve the Copilot API base URL for a token."""
    try:
        req = urllib.request.Request(
            "https://api.github.com/copilot_internal/user",
            headers={"Authorization": f"token {token}", "Accept": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return resp.get("endpoints", {}).get("api", "https://api.githubcopilot.com")
    except Exception:
        return "https://api.githubcopilot.com"


# Cached per-mode API bases and tokens
_mode_config: dict[str, dict] = {}


def _ensure_mode_config(mode: str):
    """Initialize token + API base for a mode."""
    if mode in _mode_config:
        return
    if mode == "vscode":
        token = _resolve_vscode_token()
        if not token:
            logger.warning("No saved VS Code token, vscode mode unavailable")
            return
        api_base = _get_copilot_api_base(token)
        _mode_config["vscode"] = {"token": token, "api_base": api_base}
        logger.info(f"vscode mode: api={api_base}")
    else:
        token = _resolve_cli_token()
        if not token:
            logger.warning("No CLI token available")
            return
        api_base = _get_copilot_api_base(token)
        _mode_config["cli"] = {"token": token, "api_base": api_base}
        logger.info(f"cli mode: api={api_base}")


def get_models(mode: str = "vscode") -> list:
    with _models_lock:
        if mode in _models_cache:
            return _models_cache[mode]

    _ensure_mode_config(mode)
    cfg = _mode_config.get(mode)
    if not cfg:
        return []

    try:
        headers = {"Authorization": f"Bearer {cfg['token']}", "Accept": "application/json"}
        headers["Copilot-Integration-Id"] = "vscode-chat" if mode == "vscode" else "copilot-developer-cli"
        req = urllib.request.Request(f"{cfg['api_base']}/models", headers=headers)
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        models = resp.get("data", resp) if isinstance(resp, dict) else resp
        with _models_lock:
            _models_cache[mode] = models
        logger.info(f"models cache [{mode}]: {len(models)} models")
        return models
    except Exception as e:
        logger.error(f"models fetch [{mode}] failed: {e}")
        return []


def get_mode_token_and_base(mode: str) -> tuple[str, str]:
    """Return (token, api_base) for a mode, for direct API calls."""
    _ensure_mode_config(mode)
    cfg = _mode_config.get(mode, {})
    return cfg.get("token", ""), cfg.get("api_base", GATEWAY_UPSTREAM)


def pick_endpoint(model_id: str, mode: str = "vscode") -> tuple[str, str]:
    """Return (path, format) for a model.  format is 'anthropic', 'openai', or 'responses'."""
    models = get_models(mode)
    for m in models:
        if m.get("id") == model_id:
            eps = m.get("supported_endpoints", [])
            if "/v1/messages" in eps:
                return "/v1/messages", "anthropic"
            if "/v1/responses" in eps or "/responses" in eps:
                return "/v1/responses", "responses"
            if "/chat/completions" in eps:
                return "/chat/completions", "openai"
    # fallback: Claude models use /v1/messages
    if "claude" in model_id:
        return "/v1/messages", "anthropic"
    return "/chat/completions", "openai"

# ─── Proxy + Instrumentation ─────────────────────────────────────────────────

def proxy_chat(model: str, messages: list, req_id: str) -> bytes:
    """Proxy a chat request through the gateway with full instrumentation.
    Streams SSE back as bytes to send to the browser."""

    path, fmt = pick_endpoint(model)

    # Build request body based on format
    if fmt == "anthropic":
        body = {"model": model, "max_tokens": 4096, "stream": True, "messages": messages}
        extra_headers = {"anthropic-version": "2023-06-01"}
    elif fmt == "responses":
        # Responses API: use last user message as input
        last_msg = messages[-1]["content"] if messages else ""
        body = {"model": model, "input": last_msg}
        extra_headers = {}
    else:
        body = {"model": model, "max_tokens": 4096, "stream": True, "messages": messages}
        extra_headers = {}

    is_stream = body.get("stream", False)
    url = GATEWAY_URL + path
    body_bytes = json.dumps(body).encode()

    headers = {"Content-Type": "application/json", "Content-Length": str(len(body_bytes))}
    headers.update(extra_headers)

    # ── Event: request_sent ──
    bus.publish("request_sent", {
        "id": req_id,
        "step": "demo → gateway",
        "method": "POST",
        "url": url,
        "headers": headers,
        "body": body,
    })
    bus.publish("request_sent", {
        "id": req_id,
        "step": "gateway → copilot API",
        "method": "POST",
        "url": GATEWAY_UPSTREAM + path,
        "headers": mask_token({"Authorization": "Bearer <github-token>", **extra_headers,
                                "Content-Type": "application/json"}),
        "body": body,
        "note": "(gateway replaces auth header with real GitHub token)",
    })

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    t0 = time.time()

    try:
        resp = urllib.request.urlopen(req, timeout=300)
    except urllib.error.HTTPError as e:
        elapsed = time.time() - t0
        err_body = e.read().decode(errors="replace")
        bus.publish("response_error", {
            "id": req_id, "status": e.code, "body": err_body,
            "elapsed_ms": int(elapsed * 1000),
        })
        return json.dumps({"error": err_body, "status": e.code}).encode()
    except urllib.error.URLError as e:
        bus.publish("response_error", {
            "id": req_id, "status": 502, "body": str(e.reason),
        })
        return json.dumps({"error": str(e.reason), "status": 502}).encode()

    # ── Event: response_headers ──
    resp_headers = dict(resp.headers)
    bus.publish("response_headers", {
        "id": req_id, "status": resp.status,
        "headers": {k: v for k, v in resp_headers.items()
                     if k.lower() not in ("set-cookie",)},
        "elapsed_ms": int((time.time() - t0) * 1000),
        "streaming": is_stream,
    })

    if is_stream:
        return _stream_response(resp, req_id, t0, fmt)
    else:
        data = resp.read()
        elapsed = time.time() - t0
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            parsed = data.decode(errors="replace")
        bus.publish("response_complete", {
            "id": req_id, "body": parsed,
            "elapsed_ms": int(elapsed * 1000),
            "bytes": len(data),
        })
        return data


def _stream_response(resp, req_id: str, t0: float, fmt: str) -> bytes:
    """Read streaming response, publish events, return accumulated bytes."""
    chunks = []
    chunk_count = 0
    total_bytes = 0

    for line in resp:
        chunks.append(line)
        total_bytes += len(line)
        decoded = line.decode(errors="replace").strip()

        if decoded.startswith("data: ") and decoded != "data: [DONE]":
            chunk_count += 1
            try:
                event_data = json.loads(decoded[6:])
                # Extract text for display
                text = ""
                if fmt == "anthropic":
                    if event_data.get("type") == "content_block_delta":
                        text = event_data.get("delta", {}).get("text", "")
                elif fmt == "openai":
                    choices = event_data.get("choices", [])
                    if choices:
                        text = choices[0].get("delta", {}).get("content", "")

                bus.publish("response_chunk", {
                    "id": req_id,
                    "chunk_num": chunk_count,
                    "text": text,
                    "raw_type": event_data.get("type", event_data.get("object", "")),
                })
            except json.JSONDecodeError:
                pass

    elapsed = time.time() - t0
    bus.publish("response_complete", {
        "id": req_id,
        "elapsed_ms": int(elapsed * 1000),
        "chunks": chunk_count,
        "bytes": total_bytes,
        "streaming": True,
    })

    return b"".join(chunks)

# ─── HTTP Handler ─────────────────────────────────────────────────────────────

class DemoHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/" or path == "/index.html":
                self._serve_file("demo.html", "text/html")
            elif path == "/api/models":
                self._handle_models()
            elif path == "/api/events":
                self._handle_sse()
            elif path == "/api/gateway/stats":
                self._proxy_gateway("/stats", "application/json")
            elif path == "/api/gateway/logs":
                # Forward ?n=<int> if present; default 200 (gateway clamps to 2000)
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                n_val = qs.get("n", ["200"])[0]
                n = n_val if n_val.isdigit() else "200"
                self._proxy_gateway(f"/logs?n={n}", "text/plain; charset=utf-8")
            elif path == "/health":
                self._json_response(200, {"status": "ok"})
            else:
                self._json_response(404, {"error": "not found"})
        except Exception:
            logger.exception("GET %s failed", path)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/chat":
                self._handle_chat()
            else:
                self._json_response(404, {"error": "not found"})
        except Exception:
            logger.exception("POST %s failed", path)

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    # ── /api/models ──

    def _parse_mode(self) -> str:
        """Extract mode from ?mode=cli|vscode query param."""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        return qs.get("mode", ["vscode"])[0]

    def _handle_models(self):
        mode = self._parse_mode()
        models = get_models(mode)
        formatted = []
        for m in models:
            endpoints = m.get("supported_endpoints", [])
            if not endpoints:
                continue
            caps = m.get("capabilities", {})
            formatted.append({
                "id": m.get("id", ""),
                "name": m.get("name", m.get("id", "")),
                "vendor": m.get("vendor", ""),
                "endpoints": endpoints,
                "policy": m.get("policy", {}).get("state", ""),
                "context_window": caps.get("limits", {}).get("max_context_window_tokens", 0),
                "max_output": caps.get("limits", {}).get("max_output_tokens", 0),
                "supports_streaming": caps.get("supports", {}).get("streaming", False),
                "supports_tools": caps.get("supports", {}).get("tool_calls", False),
                "supports_vision": caps.get("supports", {}).get("vision", False),
            })
        # Also collect models with no endpoints (listed but not callable)
        listed_only = []
        for m in models:
            endpoints = m.get("supported_endpoints", [])
            if endpoints:
                continue
            picker = m.get("model_picker_enabled", False)
            policy = m.get("policy", {}).get("state", "")
            if picker or policy == "enabled":
                listed_only.append({
                    "id": m.get("id", ""),
                    "name": m.get("name", m.get("id", "")),
                    "vendor": m.get("vendor", ""),
                    "status": "listed but no endpoint",
                })

        token, api_base = get_mode_token_and_base(mode)
        self._json_response(200, {
            "mode": mode,
            "api_base": api_base,
            "token_type": "VS Code OAuth" if mode == "vscode" else "gh CLI OAuth",
            "token_prefix": token[:10] + "..." if token else "none",
            "integration_id": "vscode-chat" if mode == "vscode" else "copilot-developer-cli",
            "models": formatted,
            "listed_only": listed_only,
        })

    # ── /api/chat ──

    def _handle_chat(self):
        body = self._read_body()
        if not body:
            self._json_response(400, {"error": "empty body"})
            return
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._json_response(400, {"error": "invalid JSON"})
            return

        model = data.get("model", "claude-sonnet-4.6")
        messages = data.get("messages", [])
        mode = data.get("mode", "vscode")
        req_id = str(uuid.uuid4())[:8]

        _, fmt = pick_endpoint(model, mode)
        is_stream = fmt not in ("responses",)

        # Get token and API base for this mode
        token, api_base = get_mode_token_and_base(mode)

        bus.publish("chat_start", {
            "id": req_id, "model": model, "format": fmt,
            "mode": mode, "message_count": len(messages),
        })

        # Run proxy in this thread — stream back to browser
        if is_stream:
            # Stream SSE to browser
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            path, fmt = pick_endpoint(model, mode)
            if fmt == "anthropic":
                req_body = {"model": model, "max_tokens": 4096, "stream": True, "messages": messages}
                extra = {"anthropic-version": "2023-06-01"}
            else:
                req_body = {"model": model, "max_tokens": 4096, "stream": True, "messages": messages}
                extra = {}

            url = api_base + path
            body_bytes = json.dumps(req_body).encode()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Content-Length": str(len(body_bytes)),
            }
            headers["Copilot-Integration-Id"] = "vscode-chat" if mode == "vscode" else "copilot-developer-cli"
            headers.update(extra)

            bus.publish("request_sent", {
                "id": req_id, "step": f"demo → copilot API ({mode})",
                "method": "POST", "url": url,
                "headers": mask_token(dict(headers)),
                "body": req_body,
            })

            req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
            t0 = time.time()

            try:
                resp = urllib.request.urlopen(req, timeout=300)
            except urllib.error.HTTPError as e:
                err = e.read().decode(errors="replace")
                bus.publish("response_error", {"id": req_id, "status": e.code, "body": err})
                self.wfile.write(f"data: {json.dumps({'error': err})}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                return
            except urllib.error.URLError as e:
                bus.publish("response_error", {"id": req_id, "status": 502, "body": str(e.reason)})
                self.wfile.write(f"data: {json.dumps({'error': str(e.reason)})}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                return

            bus.publish("response_headers", {
                "id": req_id, "status": resp.status,
                "elapsed_ms": int((time.time() - t0) * 1000),
                "streaming": True,
            })

            chunk_count = 0
            total_bytes = 0
            for line in resp:
                total_bytes += len(line)
                try:
                    self.wfile.write(line)
                    self.wfile.flush()
                except BrokenPipeError:
                    break
                decoded = line.decode(errors="replace").strip()
                if decoded.startswith("data: ") and decoded != "data: [DONE]":
                    chunk_count += 1
                    try:
                        ev = json.loads(decoded[6:])
                        text = ""
                        if fmt == "anthropic" and ev.get("type") == "content_block_delta":
                            text = ev.get("delta", {}).get("text", "")
                        elif fmt == "openai":
                            ch = ev.get("choices", [])
                            if ch:
                                text = ch[0].get("delta", {}).get("content", "")
                        if text:
                            bus.publish("response_chunk", {
                                "id": req_id, "chunk_num": chunk_count, "text": text,
                            })
                    except json.JSONDecodeError:
                        pass

            elapsed = time.time() - t0
            bus.publish("response_complete", {
                "id": req_id, "elapsed_ms": int(elapsed * 1000),
                "chunks": chunk_count, "bytes": total_bytes, "streaming": True,
            })
        else:
            # Non-streaming (Responses API)
            path, _ = pick_endpoint(model, mode)
            last_msg = messages[-1]["content"] if messages else ""
            req_body = {"model": model, "input": last_msg}
            url = api_base + path
            body_bytes = json.dumps(req_body).encode()
            req_headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Content-Length": str(len(body_bytes)),
            }
            if mode == "vscode":
                req_headers["Copilot-Integration-Id"] = "vscode-chat"

            bus.publish("request_sent", {
                "id": req_id, "step": f"demo → copilot API ({mode})",
                "method": "POST", "url": url,
                "headers": mask_token(dict(req_headers)),
                "body": req_body,
            })

            t0 = time.time()
            try:
                req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method="POST")
                resp = urllib.request.urlopen(req, timeout=300)
                result = resp.read()
                bus.publish("response_complete", {
                    "id": req_id, "elapsed_ms": int((time.time() - t0) * 1000),
                    "bytes": len(result),
                })
                self._raw_response(200, "application/json", result)
            except urllib.error.HTTPError as e:
                err = e.read()
                bus.publish("response_error", {"id": req_id, "status": e.code, "body": err.decode(errors="replace")})
                self._raw_response(e.code, "application/json", err)
            except urllib.error.URLError as e:
                bus.publish("response_error", {"id": req_id, "status": 502, "body": str(e.reason)})
                self._json_response(502, {"error": str(e.reason)})

    # ── /api/events (SSE) ──

    def _handle_sse(self):
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        q = bus.subscribe()
        try:
            while True:
                try:
                    payload = q.get(timeout=30)
                    self.wfile.write(payload.encode())
                    self.wfile.flush()
                except queue.Empty:
                    # keepalive
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            bus.unsubscribe(q)

    # ── Helpers ──

    def _serve_file(self, filename: str, content_type: str):
        filepath = HERE / filename
        if not filepath.exists():
            self._json_response(404, {"error": f"{filename} not found"})
            return
        data = filepath.read_bytes()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _json_response(self, status: int, data):
        body = json.dumps(data, indent=2, ensure_ascii=False).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _raw_response(self, status: int, content_type: str, data: bytes):
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _proxy_gateway(self, path: str, content_type: str):
        """Proxy a GET to the gateway and stream the response body back.
        Used for /stats and /logs to keep the dashboard self-contained
        (works even if the gateway later tightens its CORS posture)."""
        url = GATEWAY_URL + path
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = resp.read()
                self._raw_response(resp.status, content_type, data)
        except urllib.error.HTTPError as e:
            err = e.read() if hasattr(e, "read") else str(e).encode()
            self._raw_response(e.code, content_type, err)
        except urllib.error.URLError as e:
            body = json.dumps({"error": f"gateway unreachable: {e.reason}"}).encode()
            self._raw_response(502, "application/json", body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, OPTIONS")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    global GATEWAY_URL

    parser = argparse.ArgumentParser(description="Copilot Gateway Demo")
    parser.add_argument("--port", type=int, default=DEMO_PORT)
    parser.add_argument("--host", default=DEMO_HOST)
    parser.add_argument("--gateway", default=GATEWAY_URL, help="Gateway URL")
    parser.add_argument("--start-gateway", action="store_true",
                        help="Auto-start gateway.py as subprocess")
    args = parser.parse_args()

    GATEWAY_URL = args.gateway

    # Auto-detect if gateway is already running
    gateway_proc = None
    gateway_running = False
    try:
        req = urllib.request.Request(f"{GATEWAY_URL}/health")
        urllib.request.urlopen(req, timeout=3)
        gateway_running = True
        print(f"[demo] Gateway already running at {GATEWAY_URL}")
    except Exception:
        pass

    if not gateway_running or args.start_gateway:
        gateway_script = HERE / "gateway.py"
        if gateway_script.exists() and not gateway_running:
            print(f"[demo] Starting gateway: python3 {gateway_script}")
            gateway_proc = subprocess.Popen(
                [sys.executable, str(gateway_script)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            time.sleep(2)  # let it start
            print(f"[demo] Gateway started (PID {gateway_proc.pid})")
        elif not gateway_running:
            print(f"[demo] WARNING: gateway not running and {HERE / 'gateway.py'} not found")
            print(f"[demo] Start it manually: python3 gateway.py")

    print()
    print(f"  Copilot Gateway Demo")
    print(f"  ────────────────────")
    print(f"  Demo UI:  http://{args.host}:{args.port}")
    print(f"  Gateway:  {GATEWAY_URL}")
    print()

    server = http.server.ThreadingHTTPServer((args.host, args.port), DemoHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[demo] shutting down.")
        server.server_close()
        if gateway_proc:
            gateway_proc.terminate()
            gateway_proc.wait(timeout=5)


if __name__ == "__main__":
    main()
