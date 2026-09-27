#!/usr/bin/env python3
"""
Copilot Gateway Demo — Interactive chat with full call-flow visibility.

Shows how any product talks to the gateway, and how the gateway
forwards to api.githubcopilot.com.  Every HTTP hop is logged to
the browser in real time.

Usage:
  python3 demo.py                     # assumes gateway.py running on 8787
  python3 demo.py --start-gateway     # auto-start gateway.py as subprocess
  python3 demo.py --no-start-gateway  # never start a gateway, even if unreachable
  python3 demo.py --port 8788         # custom port

Open http://localhost:8788 in your browser.

Account selection in the UI is demo-only (see demo_auth.py): it never changes
the gateway's account, `.gateway-token.json`, or the global gh login.
"""

from __future__ import annotations

import argparse
import collections
import hmac
import http.cookies
import http.client
import http.server
import ipaddress
import json
import logging
import pathlib
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

import demo_auth
from demo_auth import AuthError

# ─── Config ───────────────────────────────────────────────────────────────────

DEMO_HOST = "127.0.0.1"
DEMO_PORT = 8788
GATEWAY_URL = "http://127.0.0.1:8787"
HERE = pathlib.Path(__file__).parent
LOG_DIR = HERE / "logs"
AUTH_STORE = HERE / ".demo-auth.json"
LEGACY_TOKEN_FILE = HERE / ".gateway-token.json"

CSRF_HEADER = "X-Demo-CSRF"
AUTH_BODY_LIMIT = 16 * 1024
CHAT_BODY_LIMIT = 4 * 1024 * 1024
MAX_RESPONSES_BODY = 16 * 1024 * 1024
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,200}$")

logger = logging.getLogger("demo")


def configure_logging(log_dir: pathlib.Path):
    """File + stdout logging. Called from main() so importing has no side effects."""
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "demo.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )

# ─── SSE Event Bus ────────────────────────────────────────────────────────────

class EventBus:
    """Fan-out SSE event bus.  Each connected browser gets its own queue.

    Only non-secret chat flow events are published; account/auth payloads and
    device codes never go through the bus."""

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

# ─── Helpers ──────────────────────────────────────────────────────────────────

def mask_token(headers: dict) -> dict:
    """Return a copy with the Authorization value fully masked."""
    out = dict(headers)
    if "Authorization" in out:
        out["Authorization"] = "******"
    return out


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def pick_endpoint(model_id: str, catalog) -> tuple[str, str]:
    """Return (path, format) for a model.  format is 'anthropic', 'openai', or 'responses'."""
    for m in catalog:
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


def format_catalog(catalog) -> tuple[list, list]:
    formatted = []
    for m in catalog:
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
            "model_picker_price_category": m.get("model_picker_price_category"),
            "billing": m.get("billing"),
        })
    # Also collect models with no endpoints (listed but not callable)
    listed_only = []
    for m in catalog:
        endpoints = m.get("supported_endpoints", [])
        if endpoints:
            continue
        picker = m.get("model_picker_enabled", False)
        policy = m.get("policy", {}).get("state", "")
        if picker or policy == "enabled":
            listed_model = {
                "id": m.get("id", ""),
                "name": m.get("name", m.get("id", "")),
                "vendor": m.get("vendor", ""),
                "status": "listed but no endpoint",
            }
            if "model_picker_price_category" in m:
                listed_model["model_picker_price_category"] = m["model_picker_price_category"]
            if "billing" in m:
                listed_model["billing"] = m["billing"]
            listed_only.append(listed_model)
    return formatted, listed_only


def _stream_text(fmt: str, ev: dict) -> str:
    if fmt == "anthropic" and ev.get("type") == "content_block_delta":
        return ev.get("delta", {}).get("text", "")
    if fmt == "openai":
        ch = ev.get("choices", [])
        if ch:
            return ch[0].get("delta", {}).get("content", "") or ""
    return ""

# ─── Browser sessions (CSRF) ─────────────────────────────────────────────────

class SessionRegistry:
    """HttpOnly SameSite=Strict cookie -> CSRF token. Bounded, in memory."""

    def __init__(self, port: int, limit: int = 256):
        self.cookie_name = f"demo_session_{port}"
        self._sessions: collections.OrderedDict[str, str] = collections.OrderedDict()
        self._lock = threading.Lock()
        self._limit = limit

    def _sid(self, cookie_header):
        if not cookie_header:
            return None
        try:
            jar = http.cookies.SimpleCookie()
            jar.load(cookie_header)
        except http.cookies.CookieError:
            return None
        morsel = jar.get(self.cookie_name)
        return morsel.value if morsel else None

    def bootstrap(self, cookie_header) -> tuple[str, str, bool]:
        sid = self._sid(cookie_header)
        with self._lock:
            if sid and sid in self._sessions:
                self._sessions.move_to_end(sid)
                return sid, self._sessions[sid], False
            sid, csrf = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
            self._sessions[sid] = csrf
            while len(self._sessions) > self._limit:
                self._sessions.popitem(last=False)
            return sid, csrf, True

    def verify(self, cookie_header, csrf) -> str | None:
        sid = self._sid(cookie_header)
        with self._lock:
            expected = self._sessions.get(sid) if sid else None
        if expected and isinstance(csrf, str) and hmac.compare_digest(expected, csrf):
            return sid
        return None

# ─── Server ──────────────────────────────────────────────────────────────────

class DemoServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, auth: demo_auth.AuthManager, gateway_url: str = GATEWAY_URL):
        super().__init__(address, DemoHandler)
        self.auth = auth
        self.gateway_url = gateway_url
        self.bus = EventBus()
        self.sessions = SessionRegistry(self.server_address[1])


def create_server(host: str, port: int, auth: demo_auth.AuthManager,
                  gateway_url: str = GATEWAY_URL) -> DemoServer:
    return DemoServer((host, port), auth, gateway_url)


def _is_loopback(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback

# ─── HTTP Handler ─────────────────────────────────────────────────────────────

class DemoHandler(http.server.BaseHTTPRequestHandler):
    server: DemoServer

    def log_message(self, fmt, *args):
        pass

    # ── dispatch ──

    def do_GET(self):
        self._responded = False
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/" or path == "/index.html":
                self._serve_file("demo.html", "text/html")
            elif path == "/health":
                self._json_response(200, {"status": "ok"})
            elif not path.startswith("/api/"):
                self._json_response(404, {"error": "not found"})
            elif not self._guard(mutation=False):
                return
            elif path == "/api/session":
                self._handle_session()
            elif path == "/api/models":
                self._handle_models(self._query_mode(qs))
            elif path == "/api/auth/status":
                self._json_response(200, self.server.auth.status(self._query_mode(qs)))
            elif path == "/api/auth/accounts":
                self._json_response(200, self.server.auth.list_accounts())
            elif path == "/api/events":
                self._handle_sse()
            elif path == "/api/gateway/stats":
                self._proxy_gateway("/stats", "application/json")
            elif path == "/api/gateway/usage":
                # Forward only a validated decimal day count; ignore all other query params.
                days_val = qs.get("days", ["30"])[0]
                days = int(days_val) if days_val.isdecimal() else 30
                days = max(1, min(days, 3650))
                self._proxy_gateway(f"/usage?days={days}", "application/json")
            elif path == "/api/gateway/logs":
                # Forward ?n=<int> if present; default 200 (gateway clamps to 2000)
                n_val = qs.get("n", ["200"])[0]
                n = n_val if n_val.isdigit() else "200"
                self._proxy_gateway(f"/logs?n={n}", "text/plain; charset=utf-8")
            else:
                self._json_response(404, {"error": "not found"})
        except AuthError as e:
            self._error(e)
        except Exception:
            logger.exception("GET %s failed", path)
            self._internal_error()

    def do_POST(self):
        self._responded = False
        self._body_consumed = False
        path = urllib.parse.urlsplit(self.path).path
        auth = self.server.auth
        try:
            if not path.startswith("/api/"):
                self._json_response(404, {"error": "not found"})
                return
            if not self._guard(mutation=True):
                return
            if path == "/api/chat":
                self._handle_chat()
                return
            routes = {
                "/api/auth/select-gh": lambda d: auth.select_gh(
                    d.get("mode"), d.get("host"), d.get("login"), d.get("generation")),
                "/api/auth/device/start": lambda d: auth.start_device(
                    self._session_id, d.get("mode"), d.get("expected_login")),
                "/api/auth/device/poll": lambda d: auth.poll_device(
                    self._session_id, d.get("transaction_id")),
                "/api/auth/device/confirm": lambda d: auth.confirm_device(
                    self._session_id, d.get("transaction_id"), d.get("generation")),
                "/api/auth/device/cancel": lambda d: auth.cancel_device(
                    self._session_id, d.get("transaction_id")),
                "/api/auth/sign-out": lambda d: auth.sign_out(d.get("mode"), d.get("generation")),
                "/api/auth/use-default": lambda d: auth.use_default(
                    d.get("mode"), d.get("generation")),
                "/api/auth/reload": lambda d: auth.reload(d.get("mode"), d.get("generation")),
            }
            handler = routes.get(path)
            if handler is None:
                self._json_response(404, {"error": "not found"})
                return
            data = self._read_json(AUTH_BODY_LIMIT)
            self._json_response(200, handler(data))
        except AuthError as e:
            self._error(e)
        except Exception:
            logger.exception("POST %s failed", path)
            self._internal_error()

    def do_OPTIONS(self):
        self._responded = False
        # No CORS: cross-origin preflights are refused.
        self._json_response(405, {"error": "method not allowed"}, extra={"Allow": "GET, POST"})

    # ── boundary checks ──

    def _host_header(self) -> str:
        return (self.headers.get("Host") or "").strip().lower()

    def _trusted_host(self) -> bool:
        port = self.server.server_address[1]
        return self._host_header() in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _guard(self, *, mutation: bool) -> bool:
        """Loopback peer + trusted Host always; exact Origin, JSON and CSRF for mutations.
        Forwarded headers are never consulted."""
        port = self.server.server_address[1]
        if not (_is_loopback(self.client_address[0]) and self._trusted_host()):
            self._error(AuthError(
                "loopback_only",
                f"Account, model and chat features are available only on this computer "
                f"at http://127.0.0.1:{port}/."))
            return False
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if site and site not in ("same-origin", "none"):
            self._error(AuthError("forbidden", "Cross-site request refused."))
            return False
        if mutation:
            origin = (self.headers.get("Origin") or "").strip().lower()
            if origin != f"http://{self._host_header()}":
                self._error(AuthError("forbidden", "Cross-origin request refused."))
                return False
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._error(AuthError("unsupported_media_type", "JSON body required."))
                return False
            sid = self.server.sessions.verify(self.headers.get("Cookie"),
                                              self.headers.get(CSRF_HEADER))
            if not sid:
                self._error(AuthError("csrf_failed",
                                      "Browser session missing or expired; reload the page."))
                return False
            self._session_id = sid
        return True

    def _read_json(self, limit: int) -> dict:
        raw_len = self.headers.get("Content-Length")
        if raw_len is None or not raw_len.strip().isdigit():
            raise AuthError("invalid_request", "Content-Length required.", status=411)
        length = int(raw_len)
        if length > limit:
            raise AuthError("payload_too_large", "Request body too large.")
        self._body_consumed = True
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            raise AuthError("invalid_request", "Invalid JSON.") from None
        if not isinstance(data, dict):
            raise AuthError("invalid_request", "JSON object required.")
        return data

    @staticmethod
    def _query_mode(qs) -> str:
        mode = qs.get("mode", ["vscode"])[0]
        if mode not in demo_auth.MODES:
            raise AuthError("invalid_request", "Unknown mode.")
        return mode

    # ── /api/session ──

    def _handle_session(self):
        sessions = self.server.sessions
        sid, csrf, is_new = sessions.bootstrap(self.headers.get("Cookie"))
        extra = {}
        if is_new:
            extra["Set-Cookie"] = (f"{sessions.cookie_name}={sid}; HttpOnly; "
                                   "SameSite=Strict; Path=/")
        self._json_response(200, {"csrf": csrf, "port": self.server.server_address[1]},
                            extra=extra)

    # ── /api/models ──

    def _handle_models(self, mode: str):
        state = self.server.auth.ensure(mode)
        formatted, listed_only = format_catalog(state.catalog)
        if state.status != "ready":
            catalog_status = "unavailable"
        elif formatted:
            catalog_status = "ready"
        else:
            catalog_status = "empty"
        public = state.public()
        self._json_response(200, {
            "mode": mode,
            "generation": state.generation,
            "api_base": state.api_base,
            "integration_id": demo_auth.INTEGRATION_IDS[mode],
            "credential_source": public["source_label"],
            "auth": public,
            "catalog_status": catalog_status,
            "models": formatted,
            "listed_only": listed_only,
        })

    # ── /api/chat ──

    def _handle_chat(self):
        data = self._read_json(CHAT_BODY_LIMIT)
        mode = data.get("mode")
        model = data.get("model")
        messages = data.get("messages")
        if mode not in demo_auth.MODES:
            raise AuthError("invalid_request", "Unknown mode.")
        if not isinstance(model, str) or not MODEL_ID_RE.match(model):
            raise AuthError("invalid_request", "Invalid model.")
        if (not isinstance(messages, list) or not 1 <= len(messages) <= 500
                or not all(isinstance(m, dict) and m.get("role") in ("user", "assistant", "system")
                           and isinstance(m.get("content"), str) for m in messages)):
            raise AuthError("invalid_request", "Invalid messages.")
        messages = [{"role": m["role"], "content": m["content"]} for m in messages]

        # One immutable snapshot: credential, endpoint, integration and catalog.
        snap = self.server.auth.chat_snapshot(mode, data.get("generation"))
        callable_ids = {m.get("id") for m in snap.catalog if m.get("supported_endpoints")}
        if model not in callable_ids:
            raise AuthError("invalid_request", "That model is not callable for this account.")
        path, fmt = pick_endpoint(model, snap.catalog)
        if fmt == "anthropic":
            req_body = {"model": model, "max_tokens": 4096, "stream": True, "messages": messages}
            extra = {"anthropic-version": "2023-06-01"}
        elif fmt == "responses":
            # Responses API: use last user message as input
            req_body = {"model": model, "input": messages[-1]["content"]}
            extra = {}
        else:
            req_body = {"model": model, "max_tokens": 4096, "stream": True, "messages": messages}
            extra = {}
        is_stream = fmt != "responses"
        url = snap.api_base + path
        body_bytes = json.dumps(req_body).encode()
        headers = {
            "Authorization": "Bearer " + snap.secret.reveal(),
            "Content-Type": "application/json",
            "Copilot-Integration-Id": snap.integration_id,
        }
        headers.update(extra)
        tags = {"mode": mode, "generation": snap.generation}
        req_id = str(uuid.uuid4())[:8]
        bus = self.server.bus
        bus.publish("chat_start", {"id": req_id, "model": model, "format": fmt,
                                   "message_count": len(messages), **tags})
        bus.publish("request_sent", {"id": req_id, "step": f"demo → copilot API ({mode})",
                                     "method": "POST", "url": url,
                                     "headers": mask_token(headers), "body": req_body, **tags})
        t0 = time.time()
        try:
            resp = self.server.auth.provider.call("POST", url, demo_auth.COPILOT_API_ORIGINS,
                                                  headers, body_bytes, timeout=300)
        except AuthError as e:
            bus.publish("response_error", {"id": req_id, "status": 502, "body": e.message, **tags})
            raise
        if resp.status != 200:
            try:
                raw = resp.read(65536)
            except demo_auth.TransportError:
                raw = b""
            resp.close()
            msg = (demo_auth.sanitize_provider_message(raw, [snap.secret.reveal()])
                   or f"Copilot API returned HTTP {resp.status}.")
            bus.publish("response_error", {"id": req_id, "status": resp.status, "body": msg,
                                           "elapsed_ms": int((time.time() - t0) * 1000), **tags})
            raise AuthError("upstream_error", msg,
                            status=resp.status if 400 <= resp.status < 500 else 502,
                            upstream_status=resp.status)
        bus.publish("response_headers", {"id": req_id, "status": resp.status,
                                         "elapsed_ms": int((time.time() - t0) * 1000),
                                         "streaming": is_stream, **tags})
        if is_stream:
            self._relay_stream(resp, fmt, req_id, t0, tags)
            return
        try:
            result = resp.read(MAX_RESPONSES_BODY)
        except demo_auth.TransportError:
            raise AuthError("provider_unavailable", "Copilot response could not be read.") from None
        finally:
            resp.close()
        bus.publish("response_complete", {"id": req_id, "elapsed_ms": int((time.time() - t0) * 1000),
                                          "bytes": len(result), **tags})
        self._raw_response(200, "application/json", result)

    def _relay_stream(self, resp, fmt, req_id, t0, tags):
        bus = self.server.bus
        self._begin(200, {"Content-Type": "text/event-stream", "Cache-Control": "no-store"})
        chunk_count = 0
        total_bytes = 0
        interrupted = False
        try:
            for line in resp:
                total_bytes += len(line)
                try:
                    self.wfile.write(line)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return  # browser went away (e.g. account changed); discard the rest
                decoded = line.decode(errors="replace").strip()
                if decoded.startswith("data: ") and decoded != "data: [DONE]":
                    chunk_count += 1
                    try:
                        text = _stream_text(fmt, json.loads(decoded[6:]))
                    except (ValueError, AttributeError):
                        text = ""
                    if text:
                        bus.publish("response_chunk", {"id": req_id, "chunk_num": chunk_count,
                                                       "text": text, **tags})
        except (OSError, http.client.HTTPException):
            interrupted = True
        finally:
            resp.close()
        if interrupted:
            msg = "Upstream stream interrupted."
            bus.publish("response_error", {"id": req_id, "status": 502, "body": msg, **tags})
            try:
                self.wfile.write(f"data: {json.dumps({'error': {'message': msg}})}\n\n".encode())
            except OSError:
                pass
            return
        bus.publish("response_complete", {"id": req_id, "elapsed_ms": int((time.time() - t0) * 1000),
                                          "chunks": chunk_count, "bytes": total_bytes,
                                          "streaming": True, **tags})

    # ── /api/events (SSE) ──

    def _handle_sse(self):
        self._begin(200, {"Content-Type": "text/event-stream", "Cache-Control": "no-store"})
        q = self.server.bus.subscribe()
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
            self.server.bus.unsubscribe(q)

    # ── Helpers ──

    def _drain_unread_body(self):
        """Read a rejected request's bounded body so closing the socket does not
        reset the connection (Windows) before the client reads our response."""
        if self.command != "POST" or getattr(self, "_body_consumed", True):
            return
        self._body_consumed = True
        raw_len = (self.headers.get("Content-Length") or "").strip()
        if raw_len.isdigit() and int(raw_len) <= CHAT_BODY_LIMIT:
            try:
                self.rfile.read(int(raw_len))
            except OSError:
                pass

    def _begin(self, status: int, headers: dict):
        self._drain_unread_body()
        self._responded = True
        self.send_response(status)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if "Cache-Control" not in headers:
            self.send_header("Cache-Control", "no-store")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()

    def _serve_file(self, filename: str, content_type: str):
        filepath = HERE / filename
        if not filepath.exists():
            self._json_response(404, {"error": f"{filename} not found"})
            return
        data = filepath.read_bytes()
        self._begin(200, {"Content-Type": f"{content_type}; charset=utf-8",
                          "Content-Length": str(len(data))})
        self.wfile.write(data)

    def _json_response(self, status: int, data, extra: dict | None = None):
        body = json.dumps(data, indent=2, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        headers.update(extra or {})
        self._begin(status, headers)
        self.wfile.write(body)

    def _raw_response(self, status: int, content_type: str, data: bytes):
        self._begin(status, {"Content-Type": content_type, "Content-Length": str(len(data))})
        self.wfile.write(data)

    def _error(self, err: AuthError):
        if self._responded:
            return
        self._json_response(err.status, {"error": err.public()})

    def _internal_error(self):
        if not self._responded:
            try:
                self._json_response(500, {"error": {"category": "internal",
                                                    "message": "Internal demo error."}})
            except OSError:
                pass

    def _proxy_gateway(self, path: str, content_type: str):
        """Proxy a GET to the gateway and stream the response body back.
        Used for /stats and /logs to keep the dashboard self-contained
        (works even if the gateway later tightens its CORS posture)."""
        url = self.server.gateway_url + path
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = resp.read()
                self._raw_response(resp.status, content_type, data)
        except urllib.error.HTTPError as e:
            err = e.read() if hasattr(e, "read") else str(e).encode()
            self._raw_response(e.code, content_type, err)
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", type(e).__name__)
            body = json.dumps({"error": f"gateway unreachable: {reason}"}).encode()
            self._raw_response(502, "application/json", body)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    global GATEWAY_URL

    # Windows / redirected-pipe safety: gateway.py spawns us with stdout
    # redirected to logs/<session>/demo.log, so Python falls back to the locale
    # codec (cp1252 on Windows), which can't encode the Unicode box-drawing rule
    # in the banner below. That raised UnicodeEncodeError and killed the demo app
    # before it bound port 8788 — the gateway's log showed "demo app started" and
    # the UI was silently unreachable. Same failure mode as the gateway's own
    # banner crash; mirror its fix. Force UTF-8 with a lossy fallback so banner
    # prints can never crash startup.
    #
    # line_buffering=True because that same redirect makes stdout block-buffered
    # (~8KB), so the short banner would sit in the buffer and never reach
    # demo.log while the process runs — a healthy demo and one that never
    # started would both leave a 0-byte log. Before this fix the crash itself
    # flushed on interpreter shutdown, which is the only reason demo.log ever
    # had content; without line buffering, fixing the crash would silently
    # remove the only startup evidence the log ever carried.
    for _stream in (sys.stdout, sys.stderr):
        # In GUI/windowed contexts (pythonw.exe, some frozen exes) std streams
        # can be None — skip explicitly rather than lean on the except below.
        if _stream is None:
            continue
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace",
                                line_buffering=True)
        except (AttributeError, OSError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="Copilot Gateway Demo")
    parser.add_argument("--port", type=int, default=DEMO_PORT)
    parser.add_argument("--host", default=DEMO_HOST,
                        help="Bind address. Account, model and chat APIs stay loopback-only.")
    parser.add_argument("--gateway", default=GATEWAY_URL, help="Gateway URL")
    start = parser.add_mutually_exclusive_group()
    start.add_argument("--start-gateway", action="store_true",
                       help="Auto-start gateway.py as subprocess")
    start.add_argument("--no-start-gateway", action="store_true",
                       help="Never start gateway.py, even when it is unreachable")
    parser.add_argument("--auth-store", default=str(AUTH_STORE),
                        help="Demo account store (default: .demo-auth.json next to demo.py)")
    parser.add_argument("--legacy-token-file", default=str(LEGACY_TOKEN_FILE),
                        help="Read-only legacy VS Code token file (default: .gateway-token.json)")
    parser.add_argument("--log-dir", default=str(LOG_DIR), help="Directory for demo.log")
    args = parser.parse_args()

    configure_logging(pathlib.Path(args.log_dir))
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

    if args.no_start_gateway:
        if not gateway_running:
            print(f"[demo] Gateway not reachable at {GATEWAY_URL}; --no-start-gateway set, not starting it.")
    elif not gateway_running or args.start_gateway:
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

    auth = demo_auth.AuthManager(store=demo_auth.AuthStore(args.auth_store),
                                 legacy_token_file=args.legacy_token_file)
    server = create_server(args.host, args.port, auth, GATEWAY_URL)

    print()
    print(f"  Copilot Gateway Demo")
    print(f"  ────────────────────")
    print(f"  Demo UI:  http://{args.host}:{server.server_address[1]}")
    print(f"  Gateway:  {GATEWAY_URL}")
    if not _is_loopback(args.host):
        print(f"  Note: account, model and chat APIs answer only on "
              f"http://127.0.0.1:{server.server_address[1]}/")
    print()

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
