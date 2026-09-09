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
  GET  /stats                  — in-memory token usage stats
  GET  /usage?days=N           — durable exact usage ledger
  GET  /logs                   — recent gateway log lines

Usage:
  python3 gateway.py                          # uses gh auth token
  python3 gateway.py --port 9000              # custom port
  GITHUB_TOKEN=gho_xxx python3 gateway.py     # explicit token
"""

from __future__ import annotations  # defer annotation evaluation so PEP 604 unions and lowercase generics work under the README's stated Python 3.8+ support window

import gzip
import http.server
import ipaddress
import json
import logging
import os
import pathlib
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

try:
    import zstandard as _zstd
    def _zstd_decompress(data: bytes) -> bytes:
        return _zstd.ZstdDecompressor().decompress(data, max_output_size=64 * 1024 * 1024)
except ImportError:
    _zstd = None
    def _zstd_decompress(data: bytes) -> bytes:
        raise RuntimeError("zstandard module not installed — run: pip install zstandard")

# ─── Config ───────────────────────────────────────────────────────────────────

GATEWAY_VERSION = "1.3.0"  # Codex CLI 405 fallback, HTTP/1.1 correctness fixes, header dedup
LISTEN_HOST = os.environ.get("GATEWAY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("GATEWAY_PORT", "8787"))
UPSTREAM = os.environ.get("GATEWAY_UPSTREAM", "https://api.githubcopilot.com")
COPILOT_MODELS_API_VERSION = os.environ.get(
    "COPILOT_MODELS_API_VERSION", "2026-08-01"
)
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
            logging.FileHandler(session_dir / "gateway.log", encoding="utf-8"),
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
        # _refresh_cv wraps _lock so callers can wait for an in-flight refresh
        # instead of firing a duplicate upstream call (herd collapse). The
        # upstream round-trip itself runs WITHOUT the lock held — see refresh().
        self._refresh_cv = threading.Condition(self._lock)
        self._refreshing = False  # True while one thread performs the upstream refresh
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

    def has_token(self) -> bool:
        """Lock-free, refresh-free liveness read for /health.

        Returns whether a token is currently cached without acquiring _lock or
        triggering a refresh, so the health probe never blocks behind an
        in-flight upstream refresh. Reports last-known cached state.
        """
        return bool(self._token)

    def refresh(self) -> str:
        with self._refresh_cv:
            if self._refreshing:
                # A refresh is in flight — wait for it and reuse its fresh token
                # rather than returning one that is mid-refresh. Checking _refreshing
                # BEFORE the throttle is what makes a concurrent caller reuse the
                # refreshed value instead of the stale token (single-flight).
                self._refresh_cv.wait_for(lambda: not self._refreshing, timeout=20)
                return self._token
            now = time.time()
            if now - self._last_refresh < self._min_refresh_interval:
                return self._token          # no refresh in flight + recently refreshed → current token is fresh
            # Claim the slot, then release the lock before the network round-trip.
            self._refreshing = True
            self._last_refresh = now

        # Upstream work runs WITHOUT _lock held, so concurrent /health probes and
        # throttled callers never block behind this network round-trip. Each write
        # below is a single atomic attribute rebind, so a concurrent reader of
        # .token always sees a complete (old or new) token, never a torn one.
        try:
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
        finally:
            with self._refresh_cv:
                self._refreshing = False
                self._refresh_cv.notify_all()

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
        # Single-flight, lock-free upstream JWT exchange. After waiting for OR
        # performing a refresh, re-check freshness and retry if the token is still
        # expired — a refresh that failed must not leave the caller of .token holding
        # a stale token that 401s the next request (gemini #15). Bounded to 2 own
        # attempts so a persistently-failing exchange can't spin; the inner exchange
        # also sets _jwt_exchange_failed on failure, which exits the loop immediately.
        attempts = 0
        while True:
            with self._refresh_cv:
                if self._jwt_exchange_failed:
                    return                      # JWT disabled; raw token in use
                if self._jwt_expires > 0 and time.time() <= self._jwt_expires - 60:
                    return                      # fresh — we or another thread refreshed
                if self._refreshing:
                    # Another thread is refreshing; wait, then re-check freshness above.
                    if not self._refresh_cv.wait_for(lambda: not self._refreshing, timeout=20):
                        return                  # refresher stuck >20s; caller retries
                    continue
                if attempts >= 2:
                    return                      # gave it our best effort; avoid spinning
                self._refreshing = True         # token still due, nobody refreshing → claim
            attempts += 1
            try:
                self._refresh_jwt_inner()
            finally:
                with self._refresh_cv:
                    self._refreshing = False
                    self._refresh_cv.notify_all()
            # loop: re-check freshness; retry our own refresh if it left the token stale

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
        with self._refresh_cv:
            if self._refreshing:
                # Wait for the in-flight refresh instead of racing it; its new
                # token is exactly what the 401 retry path needs.
                self._refresh_cv.wait_for(lambda: not self._refreshing, timeout=20)
                return self._token
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

    def count_cached(self) -> int:
        """Lock-free, refresh-free count of the last-known model list.

        Used by /health so the probe reports cached model count without
        acquiring _lock or triggering an upstream fetch.
        """
        return len(self._data)

    def _fetch(self):
        try:
            upstream = _get_upstream()
            headers = {"Authorization": f"Bearer {token_mgr.token}",
                       "Accept": "application/json",
                       "X-GitHub-Api-Version": COPILOT_MODELS_API_VERSION}
            if token_mgr.mode == "vscode":
                headers["Copilot-Integration-Id"] = "vscode-chat"
            else:
                headers["Copilot-Integration-Id"] = "copilot-developer-cli"
            models_url = f"{upstream}/models"
            req = urllib.request.Request(models_url, headers=headers)
            try:
                resp = urllib.request.urlopen(req, timeout=15)
            except urllib.error.HTTPError as e:
                if e.code not in (400, 406):
                    raise
                e.close()
                log(f"models API version {COPILOT_MODELS_API_VERSION} rejected "
                    f"with HTTP {e.code}; retrying unversioned catalog")
                fallback_headers = dict(headers)
                fallback_headers.pop("X-GitHub-Api-Version", None)
                req = urllib.request.Request(models_url, headers=fallback_headers)
                resp = urllib.request.urlopen(req, timeout=15)
            with resp:
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


# ─── Origin classification ────────────────────────────────────────────────────
# Tag each request by where the client lives so per-host usage is visible in
# stats/logs. The X-Gateway-Origin header overrides IP-based classification —
# WSL2 in `networkingMode=mirrored` appears from loopback (would otherwise
# classify as "windows"), and the 172.16.0.0/12 RFC1918 range collides with
# corporate LANs/VPNs/Docker if the gateway is ever bound to 0.0.0.0. The WSL
# toggle in Item 3 writes the header into the WSL-side env so WSL clients
# self-identify; the IP fallback covers users on the pre-toggle path.

ORIGINS = ("windows", "wsl", "other")
_WSL2_NET = ipaddress.ip_network("172.16.0.0/12")

def _classify_origin(client_ip: str, header_value: str | None = None) -> str:
    """Return 'windows' | 'wsl' | 'other' for a client. Header wins when set
    to a known origin; otherwise classify by IP. Unknown header values fall
    through to IP-based classification (prevents user-controlled stats-dict
    pollution from arbitrary header values)."""
    if header_value:
        v = header_value.strip().lower()
        if v in ORIGINS:
            return v
    # Strip IPv6 zone-ID (RFC 6874 scope, e.g. `::1%lo0`, `fe80::1%eth0`) —
    # Python's ipaddress.ip_address only learned to parse these in 3.9, but
    # the README declares 3.8+ support.
    if isinstance(client_ip, str) and "%" in client_ip:
        client_ip = client_ip.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(client_ip)
    except (ValueError, TypeError):
        return "other"
    # Unwrap IPv4-mapped IPv6 (e.g. `::ffff:127.0.0.1` from a loopback client
    # or `::ffff:172.16.0.1` from a WSL2 client reaching a dual-stack
    # `::`-bound gateway) so both the is_loopback and WSL-range checks below
    # see the IPv4 form. Python 3.12+ IPv6Address.is_loopback recognizes
    # IPv4-mapped loopback natively (cpython#103193), but the README declares
    # 3.8+ support and pre-3.12 returns False for `::ffff:127.0.0.1` —
    # the unwrap makes the behavior uniform across the supported window.
    # NOTE: ip.ipv4_mapped is Python 3.3+ (present since the ipaddress module
    # was introduced; see https://docs.python.org/3/library/ipaddress.html#ipaddress.IPv6Address.ipv4_mapped
    # — no "Added in version" annotation). Some review bots have flagged
    # this as requiring 3.9+ or 3.10+; that is incorrect. The 3.13 addition
    # is `IPv4Address.ipv6_mapped` (the reverse-direction property).
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return "windows"
    if ip.version == 4 and ip in _WSL2_NET:
        return "wsl"
    return "other"


# ─── Request Stats Tracker ────────────────────────────────────────────────────

# Legacy request-level estimates from Copilot CLI internals (nano-AIU based).
# These are independent of the live per-token billing metadata in /models.
BILLING_MULTIPLIERS = {
    "claude-haiku-4.5": 0.333,
    "claude-haiku-4-5-20251001": 0.333,
    "claude-sonnet-4.5": 1.0,
    "claude-sonnet-4-5-20250514": 1.0,
    "claude-sonnet-4.6": 1.0,
    "claude-sonnet-4-6-20250514": 1.0,
    "claude-sonnet-4": 1.0,
    "claude-opus-4.5": 3.0,
    "claude-opus-4.6": 3.0,
    "claude-opus-4-6-20250514": 3.0,
    "claude-opus-4.6-1m": 6.0,
    "claude-opus-4.6-fast": 30.0,
    "gpt-4.1": 0.0,
    "gpt-5-mini": 0.0,
    "gpt-5.4-mini": 0.0,
}


class RequestStats:
    """Thread-safe accumulator for request and token usage stats."""

    def __init__(self):
        self._lock = threading.Lock()
        self._start_time = time.time()
        self._requests_succeeded = 0
        self._requests_failed = 0
        self._usage_parse_failures = 0
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._estimated_premium = 0.0
        self._models = {}  # model -> {requests, input_tokens, output_tokens, premium_requests}
        self._last_request_at = None  # type: str | None
        # Per-origin breakdown — keyed by ORIGINS values. Caller is responsible
        # for passing a validated origin (see _classify_origin), so the key set
        # stays bounded.
        self._per_origin = {o: {"requests": 0, "requests_failed": 0,
                                "input_tokens": 0, "output_tokens": 0,
                                "last_request_at": None} for o in ORIGINS}

    def record_success(self, model: str, input_tokens: int, output_tokens: int,
                       nano_aiu: int = 0, origin: str = "other"):
        """Record a successful request with usage data."""
        multiplier = BILLING_MULTIPLIERS.get(model, 1.0)
        premium = nano_aiu / 1_000_000_000 if nano_aiu else multiplier
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        with self._lock:
            self._requests_succeeded += 1
            self._total_input_tokens += input_tokens
            self._total_output_tokens += output_tokens
            self._estimated_premium += premium
            self._last_request_at = now

            if model not in self._models:
                self._models[model] = {
                    "requests": 0, "input_tokens": 0,
                    "output_tokens": 0, "premium_requests": 0.0,
                }
            m = self._models[model]
            m["requests"] += 1
            m["input_tokens"] += input_tokens
            m["output_tokens"] += output_tokens
            m["premium_requests"] += premium

            po = self._per_origin.get(origin) or self._per_origin["other"]
            po["requests"] += 1
            po["input_tokens"] += input_tokens
            po["output_tokens"] += output_tokens
            po["last_request_at"] = now

    def record_failure(self, origin: str = "other"):
        with self._lock:
            self._requests_failed += 1
            po = self._per_origin.get(origin) or self._per_origin["other"]
            po["requests_failed"] += 1

    def record_parse_failure(self):
        with self._lock:
            self._usage_parse_failures += 1

    def snapshot(self) -> dict:
        with self._lock:
            total_tokens = self._total_input_tokens + self._total_output_tokens
            return {
                "session_start": datetime.utcfromtimestamp(self._start_time).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"),
                "uptime_seconds": int(time.time() - self._start_time),
                "requests_succeeded": self._requests_succeeded,
                "requests_failed": self._requests_failed,
                "usage_parse_failures": self._usage_parse_failures,
                "total_input_tokens": self._total_input_tokens,
                "total_output_tokens": self._total_output_tokens,
                "total_tokens": total_tokens,
                "estimated_premium_requests": round(self._estimated_premium, 2),
                "models": {k: dict(v) for k, v in self._models.items()},
                "per_origin": {k: dict(v) for k, v in self._per_origin.items()},
                "last_request_at": self._last_request_at,
            }


# Global stats — initialized in main()
request_stats = None  # type: RequestStats | None


# ─── Durable Usage Ledger ─────────────────────────────────────────────────────

USAGE_DB = LOG_DIR / "usage.sqlite3"


def _nonnegative_int(value, default=0):
    """Return an exact non-negative integer for untrusted usage fields."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        parsed = Decimal(str(value))
        if (not parsed.is_finite() or parsed < 0
                or parsed != parsed.to_integral_value()):
            return default
        return int(parsed)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return default


def _optional_nonnegative_int(value):
    if value is None:
        return None
    return _nonnegative_int(value, default=None)


def _decimal(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            return Decimal(0)
        return result
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _usage_endpoint(path: str) -> str:
    """Persist only known API endpoint names, never arbitrary client paths."""
    clean = path.split("?", 1)[0]
    if clean in ("/v1/messages", "/messages"):
        return "/v1/messages"
    if clean in ("/v1/chat/completions", "/chat/completions"):
        return "/v1/chat/completions"
    if clean in ("/v1/responses", "/responses"):
        return "/v1/responses"
    return "other"


def _merge_token_detail_snapshots(current: list, incoming: list) -> list:
    """Merge cumulative token-detail snapshots without counting repeats twice.

    Copilot can repeat the complete copilot_usage object in multiple SSE events.
    Within each model/type/rate bucket, retain the greatest cumulative count
    observed instead of summing snapshots.
    """
    merged = {}
    for source in (current, incoming):
        snapshot = {}
        if not isinstance(source, list):
            continue
        for item in source:
            if not isinstance(item, dict):
                continue
            model = str(item.get("model") or "unknown")
            token_type = str(item.get("token_type") or "unknown")
            batch_size = _nonnegative_int(item.get("batch_size"), 0)
            # Numeric spellings such as 10 and 10.0 describe the same rate;
            # normalize them so repeated snapshots cannot create two buckets.
            cost = format(_decimal(item.get("cost_per_batch")), "f")
            key = (model, token_type, batch_size, cost)
            count = _nonnegative_int(item.get("token_count"), 0)
            snapshot[key] = snapshot.get(key, 0) + count
        for key, count in snapshot.items():
            if key not in merged or count > merged[key]:
                merged[key] = count
    return [
        {
            "model": key[0],
            "token_type": key[1],
            "batch_size": key[2],
            "cost_per_batch": key[3],
            "token_count": count,
        }
        for key, count in sorted(merged.items())
    ]


class UsageStore:
    """Thread-safe, best-effort SQLite ledger for exact Copilot usage."""

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS requests (
            request_id TEXT PRIMARY KEY,
            timestamp_utc TEXT NOT NULL,
            day_utc TEXT NOT NULL,
            requested_model TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            origin TEXT NOT NULL,
            http_success INTEGER NOT NULL,
            http_status INTEGER,
            fallback_input_tokens INTEGER NOT NULL,
            fallback_output_tokens INTEGER NOT NULL,
            total_nano_aiu INTEGER,
            ai_credits REAL
        );

        CREATE TABLE IF NOT EXISTS request_model_usage (
            request_id TEXT NOT NULL,
            actual_model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cache_read_tokens INTEGER NOT NULL,
            cache_write_tokens INTEGER NOT NULL,
            cache_write_1h_tokens INTEGER NOT NULL,
            raw_nano_aiu TEXT,
            allocated_nano_aiu INTEGER,
            allocated_ai_credits REAL,
            PRIMARY KEY (request_id, actual_model),
            FOREIGN KEY (request_id) REFERENCES requests(request_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_requests_day ON requests(day_utc);
        CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp_utc);
        CREATE INDEX IF NOT EXISTS idx_requests_requested_model
            ON requests(requested_model);
        CREATE INDEX IF NOT EXISTS idx_request_model_usage_model
            ON request_model_usage(actual_model);
    """

    _TOKEN_COLUMNS = {
        "input": "input_tokens",
        "input_tokens": "input_tokens",
        "prompt": "input_tokens",
        "prompt_tokens": "input_tokens",
        "output": "output_tokens",
        "output_tokens": "output_tokens",
        "completion": "output_tokens",
        "completion_tokens": "output_tokens",
        "cache_read": "cache_read_tokens",
        "cache_read_tokens": "cache_read_tokens",
        "cache_write": "cache_write_tokens",
        "cache_write_tokens": "cache_write_tokens",
        "cache_write_1h": "cache_write_1h_tokens",
        "cache_write_1h_tokens": "cache_write_1h_tokens",
    }

    def __init__(self, path: pathlib.Path):
        self.path = path
        self._lock = threading.Lock()
        self._conn = None
        conn = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(self._SCHEMA)
            conn.commit()
            self._conn = conn
            log(f"usage ledger initialized: {path}")
        except Exception as e:
            log(f"ERROR: usage ledger initialization failed: {e}")
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def close(self):
        """Close the ledger connection during an orderly gateway shutdown."""
        with self._lock:
            connection = self._conn
            self._conn = None
            if connection is not None:
                connection.close()

    @staticmethod
    def _model_rows(requested_model: str, fallback_input: int,
                    fallback_output: int, token_details: list,
                    total_nano_aiu):
        rows = {}
        has_details = isinstance(token_details, list) and bool(token_details)
        if has_details:
            for item in token_details:
                if not isinstance(item, dict):
                    continue
                model = str(item.get("model") or requested_model or "unknown")
                row = rows.setdefault(model, {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "cache_write_1h_tokens": 0,
                    "raw_nano_aiu": Decimal(0),
                })
                count = _nonnegative_int(item.get("token_count"), 0)
                column = UsageStore._TOKEN_COLUMNS.get(
                    str(item.get("token_type") or "").lower()
                )
                if column:
                    row[column] += count
                batch_size = _nonnegative_int(item.get("batch_size"), 0)
                if batch_size:
                    row["raw_nano_aiu"] += (
                        Decimal(count) * _decimal(item.get("cost_per_batch"))
                        / Decimal(batch_size)
                    )

        # Without actual-model details, retain useful model/token attribution
        # under the requested model. This also gives failed requests a model row.
        if not rows:
            rows[str(requested_model or "unknown")] = {
                "input_tokens": fallback_input,
                "output_tokens": fallback_output,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "cache_write_1h_tokens": 0,
                "raw_nano_aiu": Decimal(0),
            }

        allocations = {model: None for model in rows}
        if total_nano_aiu is not None:
            total = _nonnegative_int(total_nano_aiu, 0)
            if len(rows) == 1:
                allocations[next(iter(rows))] = total
            else:
                weights = {m: row["raw_nano_aiu"] for m, row in rows.items()}
                weight_total = sum(weights.values(), Decimal(0))
                if weight_total <= 0:
                    weights = {
                        m: Decimal(sum(row[c] for c in (
                            "input_tokens", "output_tokens", "cache_read_tokens",
                            "cache_write_tokens", "cache_write_1h_tokens",
                        )))
                        for m, row in rows.items()
                    }
                    weight_total = sum(weights.values(), Decimal(0))
                if weight_total <= 0:
                    weights = {m: Decimal(1) for m in rows}
                    weight_total = Decimal(len(rows))

                exact = {m: Decimal(total) * weights[m] / weight_total for m in rows}
                allocations = {
                    m: int(value.to_integral_value(rounding=ROUND_FLOOR))
                    for m, value in exact.items()
                }
                remainder = total - sum(allocations.values())
                order = sorted(rows, key=lambda m: (-(exact[m] - allocations[m]), m))
                for model in order[:remainder]:
                    allocations[model] += 1

        result = []
        for model, row in rows.items():
            allocated = allocations[model]
            result.append((
                model,
                row["input_tokens"], row["output_tokens"],
                row["cache_read_tokens"], row["cache_write_tokens"],
                row["cache_write_1h_tokens"], format(row["raw_nano_aiu"], "f"),
                allocated,
                allocated / 1_000_000_000 if allocated is not None else None,
            ))
        return result

    def record(self, request_id: str, requested_model: str, endpoint: str,
               origin: str, success: bool, http_status, fallback_input_tokens=0,
               fallback_output_tokens=0, total_nano_aiu=None,
               token_details=None):
        """Persist one request; failures are logged and deliberately swallowed."""
        if self._conn is None:
            log("ERROR: usage ledger write skipped: store is unavailable")
            return
        try:
            now = datetime.utcnow()
            timestamp = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            day = now.strftime("%Y-%m-%d")
            fallback_input = _nonnegative_int(fallback_input_tokens, 0)
            fallback_output = _nonnegative_int(fallback_output_tokens, 0)
            total_nano = _optional_nonnegative_int(total_nano_aiu)
            status = _optional_nonnegative_int(http_status)
            rows = self._model_rows(
                str(requested_model or "unknown"), fallback_input,
                fallback_output, token_details or [], total_nano,
            )
            with self._lock:
                with self._conn:
                    self._conn.execute(
                        """INSERT INTO requests (
                               request_id, timestamp_utc, day_utc, requested_model,
                               endpoint, origin, http_success, http_status,
                               fallback_input_tokens, fallback_output_tokens,
                               total_nano_aiu, ai_credits
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            request_id, timestamp, day,
                            str(requested_model or "unknown"), str(endpoint or ""),
                            origin if origin in ORIGINS else "other",
                            1 if success else 0, status, fallback_input,
                            fallback_output, total_nano,
                            total_nano / 1_000_000_000
                            if total_nano is not None else None,
                        ),
                    )
                    self._conn.executemany(
                        """INSERT INTO request_model_usage (
                               request_id, actual_model, input_tokens, output_tokens,
                               cache_read_tokens, cache_write_tokens,
                               cache_write_1h_tokens, raw_nano_aiu,
                               allocated_nano_aiu, allocated_ai_credits
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        [(request_id,) + row for row in rows],
                    )
        except Exception as e:
            log(f"ERROR: usage ledger write failed for {request_id}: {e}")

    @staticmethod
    def _metrics(row):
        known = int(row["requests_with_actual_cost"] or 0)
        requests = int(row["requests"] or 0)
        nano = row["known_nano_aiu"]
        return {
            "requests": requests,
            "succeeded": int(row["succeeded"] or 0),
            "failed": int(row["failed"] or 0),
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "cache_read_tokens": int(row["cache_read_tokens"] or 0),
            "cache_write_tokens": int(row["cache_write_tokens"] or 0),
            "cache_write_1h_tokens": int(row["cache_write_1h_tokens"] or 0),
            "ai_credits": int(nano) / 1_000_000_000 if nano is not None else None,
            "requests_with_actual_cost": known,
            "requests_without_actual_cost": requests - known,
        }

    def aggregate(self, days: int):
        """Return UTC request/day/model aggregates, or None on read failure."""
        if self._conn is None:
            log("ERROR: usage ledger read skipped: store is unavailable")
            return None
        try:
            today = datetime.utcnow().date()
            from_day = today - timedelta(days=days - 1)
            start = from_day.isoformat()
            end = today.isoformat()
            request_select = """
                COUNT(*) AS requests,
                COALESCE(SUM(http_success), 0) AS succeeded,
                COALESCE(SUM(CASE WHEN http_success = 0 THEN 1 ELSE 0 END), 0) AS failed,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(cache_write_1h_tokens), 0) AS cache_write_1h_tokens,
                SUM(total_nano_aiu) AS known_nano_aiu,
                COUNT(total_nano_aiu) AS requests_with_actual_cost
            """
            request_cte = """
                WITH model_per_request AS (
                    SELECT request_id,
                           SUM(input_tokens) AS input_tokens,
                           SUM(output_tokens) AS output_tokens,
                           SUM(cache_read_tokens) AS cache_read_tokens,
                           SUM(cache_write_tokens) AS cache_write_tokens,
                           SUM(cache_write_1h_tokens) AS cache_write_1h_tokens
                    FROM request_model_usage GROUP BY request_id
                ), ranged AS (
                    SELECT r.request_id, r.timestamp_utc, r.day_utc,
                           r.requested_model, r.endpoint, r.origin,
                           r.http_success, r.http_status, r.total_nano_aiu,
                           r.ai_credits,
                           COALESCE(m.input_tokens, r.fallback_input_tokens) AS input_tokens,
                           COALESCE(m.output_tokens, r.fallback_output_tokens) AS output_tokens,
                           COALESCE(m.cache_read_tokens, 0) AS cache_read_tokens,
                           COALESCE(m.cache_write_tokens, 0) AS cache_write_tokens,
                           COALESCE(m.cache_write_1h_tokens, 0) AS cache_write_1h_tokens
                    FROM requests r
                    LEFT JOIN model_per_request m ON m.request_id = r.request_id
                    WHERE r.day_utc BETWEEN ? AND ?
                )
            """
            model_select = """
                COUNT(*) AS requests,
                COALESCE(SUM(r.http_success), 0) AS succeeded,
                COALESCE(SUM(CASE WHEN r.http_success = 0 THEN 1 ELSE 0 END), 0) AS failed,
                COALESCE(SUM(m.input_tokens), 0) AS input_tokens,
                COALESCE(SUM(m.output_tokens), 0) AS output_tokens,
                COALESCE(SUM(m.cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(m.cache_write_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(m.cache_write_1h_tokens), 0) AS cache_write_1h_tokens,
                SUM(m.allocated_nano_aiu) AS known_nano_aiu,
                COUNT(m.allocated_nano_aiu) AS requests_with_actual_cost
            """
            with self._lock:
                self._conn.row_factory = sqlite3.Row
                total_row = self._conn.execute(
                    request_cte + "SELECT " + request_select + " FROM ranged",
                    (start, end),
                ).fetchone()
                daily_rows = self._conn.execute(
                    request_cte + "SELECT day_utc AS date, " + request_select
                    + " FROM ranged GROUP BY day_utc ORDER BY day_utc ASC",
                    (start, end),
                ).fetchall()
                by_model_rows = self._conn.execute(
                    "SELECT m.actual_model AS model, " + model_select
                    + " FROM request_model_usage m JOIN requests r "
                      "ON r.request_id = m.request_id "
                      "WHERE r.day_utc BETWEEN ? AND ? GROUP BY m.actual_model "
                      "ORDER BY known_nano_aiu DESC, model ASC",
                    (start, end),
                ).fetchall()
                daily_model_rows = self._conn.execute(
                    "SELECT r.day_utc AS date, m.actual_model AS model, "
                    + model_select
                    + " FROM request_model_usage m JOIN requests r "
                      "ON r.request_id = m.request_id "
                      "WHERE r.day_utc BETWEEN ? AND ? "
                      "GROUP BY r.day_utc, m.actual_model "
                      "ORDER BY date DESC, known_nano_aiu DESC, model ASC",
                    (start, end),
                ).fetchall()

            def shaped(rows, dimensions):
                result = []
                for row in rows:
                    item = {name: row[name] for name in dimensions}
                    item.update(self._metrics(row))
                    result.append(item)
                return result

            return {
                "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "timezone": "UTC",
                "range": {"days": days, "from": start, "to": end},
                "totals": self._metrics(total_row),
                "daily": shaped(daily_rows, ("date",)),
                "by_model": shaped(by_model_rows, ("model",)),
                "daily_models": shaped(daily_model_rows, ("date", "model")),
            }
        except Exception as e:
            log(f"ERROR: usage ledger read failed: {e}")
            return None


# Global durable ledger — initialized in main().
usage_store = None  # type: UsageStore | None

# ─── Usage Extraction ─────────────────────────────────────────────────────────

def _copilot_usage_fields(container: dict) -> tuple:
    """Return (exact nano-AIU or None, token details) without inventing cost."""
    copilot_usage = container.get("copilot_usage", {})
    if not isinstance(copilot_usage, dict):
        return None, []
    nano_aiu = _optional_nonnegative_int(copilot_usage.get("total_nano_aiu"))
    details = copilot_usage.get("token_details", [])
    return nano_aiu, details if isinstance(details, list) else []


def _extract_usage_from_response(resp_json: dict, path: str) -> tuple:
    """Extract fallback tokens plus exact Copilot cost details."""
    nano_aiu, token_details = _copilot_usage_fields(resp_json)

    if "/messages" in path:
        # Anthropic format
        usage = resp_json.get("usage", {})
        return (usage.get("input_tokens", 0),
                usage.get("output_tokens", 0), nano_aiu, token_details)
    elif "/responses" in path:
        # OpenAI Responses format
        usage = resp_json.get("usage", {})
        return (usage.get("input_tokens", usage.get("prompt_tokens", 0)),
                usage.get("output_tokens", usage.get("completion_tokens", 0)),
                nano_aiu, token_details)
    else:
        # OpenAI Chat Completions format
        usage = resp_json.get("usage", {})
        return (usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0), nano_aiu, token_details)


def _extract_usage_from_event(event: dict, path: str) -> tuple:
    """Extract one cumulative SSE usage snapshot, including token details."""
    nano_aiu, token_details = _copilot_usage_fields(event)

    if "/messages" in path:
        # Anthropic SSE: message_start has input, message_delta has output
        evt_type = event.get("type", "")
        if evt_type == "message_start":
            msg = event.get("message", {})
            if nano_aiu is None:
                nano_aiu, token_details = _copilot_usage_fields(msg)
            usage = msg.get("usage", {})
            return (usage.get("input_tokens", 0), 0, nano_aiu, token_details)
        elif evt_type == "message_delta":
            usage = event.get("usage", {})
            return (0, usage.get("output_tokens", 0), nano_aiu, token_details)
    elif "/responses" in path:
        # OpenAI Responses SSE: response.completed has usage
        evt_type = event.get("type", "")
        if evt_type == "response.completed":
            resp = event.get("response", {})
            if nano_aiu is None:
                nano_aiu, token_details = _copilot_usage_fields(resp)
            usage = resp.get("usage", {})
            return (usage.get("input_tokens", usage.get("prompt_tokens", 0)),
                    usage.get("output_tokens", usage.get("completion_tokens", 0)),
                    nano_aiu, token_details)
    else:
        # OpenAI Chat SSE: final chunks can repeat cumulative usage.
        usage = event.get("usage", {})
        if usage:
            return (usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0), nano_aiu, token_details)

    return (0, 0, nano_aiu, token_details)


# ─── Gateway Handler ──────────────────────────────────────────────────────────

class GatewayHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Headers we either emit ourselves or that would create client-visible
    # duplicates if forwarded from upstream. Browsers reject multi-valued
    # Access-Control-Allow-* per CORS spec; date/server are emitted by
    # send_response; transfer-encoding is irrelevant (urllib de-chunks);
    # connection/keep-alive/content-length are managed per-response below.
    _SKIP_FORWARDED_HEADERS = frozenset({
        "transfer-encoding", "connection", "keep-alive", "content-length",
        "date", "server",
        "access-control-allow-origin",
        "access-control-allow-headers",
        "access-control-allow-methods",
    })

    def log_message(self, fmt, *args):
        pass  # we do our own logging

    # ── Route dispatch ──

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/v1/models", "/models"):
            self._handle_models()
        elif path == "/health":
            self._handle_health()
        elif path == "/stats":
            self._handle_stats()
        elif path == "/usage":
            self._handle_usage()
        elif path == "/logs":
            self._handle_logs()
        elif path in ("/v1/responses", "/responses"):
            # Codex CLI sends GET with Upgrade: websocket — reject cleanly
            # with 405 + Allow: POST so the CLI falls back immediately
            # (no retries, no body to display in the UI).
            self.send_response(405)
            self.send_header("Allow", "POST, OPTIONS")
            self.send_header("Connection", "close")
            self.send_header("Content-Length", "0")
            self.close_connection = True
            self.end_headers()
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
            # Per-model picker/effort metadata passed through from upstream so
            # downstream clients (e.g. superX) can build a per-model reasoning
            # effort selector like VS Code / the Copilot CLI does.  These are
            # ADDITIVE — the flat fields above stay for back-compat.
            #   reasoning_efforts: capabilities.supports.reasoning_effort array
            #     (e.g. opus-4.8 → ["low","medium","high","xhigh","max"]).
            #     Emitted as [] when the model has no effort control (e.g.
            #     haiku is non-thinking) so the shape is uniform and clients
            #     read "empty ⇒ hide the effort control".
            "reasoning_efforts": supports.get("reasoning_effort", []),
            "model_picker_enabled": m.get("model_picker_enabled", False),
            "model_picker_category": m.get("model_picker_category"),
            # Live catalog pricing is passed through verbatim. Keep its
            # snake_case shape and per-token values separate from the legacy
            # request-level BILLING_MULTIPLIERS estimates used by /stats.
            "model_picker_price_category": m.get("model_picker_price_category"),
            "billing": m.get("billing"),
            "preview": m.get("preview", False),
        }

    # ── /health ──

    def _handle_health(self):
        # Liveness probe: read only local cached state — no _lock acquisition and
        # no upstream call — so /health stays fast (<100ms) even while a token or
        # models refresh is in flight. token_present/models_cached report
        # last-known cached state, which is the correct semantics for "am I up?".
        health = {
            "status": "ok",
            "version": GATEWAY_VERSION,
            "upstream": _get_upstream(),
            "mode": token_mgr.mode if token_mgr else "?",
            "models_cached": models_cache.count_cached(),
            "token_present": token_mgr.has_token() if token_mgr else False,
        }
        if request_stats:
            snap = request_stats.snapshot()
            health["requests"] = snap["requests_succeeded"]
            health["tokens"] = snap["total_tokens"]
        body = json.dumps(health).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── /stats ──

    def _handle_stats(self):
        if not request_stats:
            body = b'{"error":"stats not initialized"}'
            self.send_response(503)
        else:
            body = json.dumps(request_stats.snapshot(), indent=2).encode()
            self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── /usage ──

    def _handle_usage(self):
        import urllib.parse as urlparse
        qs = urlparse.parse_qs(urlparse.urlparse(self.path).query)
        try:
            days = int(qs.get("days", ["30"])[0])
        except (TypeError, ValueError, OverflowError):
            days = 30
        days = max(1, min(days, 3650))

        report = usage_store.aggregate(days) if usage_store else None
        if report is None:
            body = b'{"error":"usage ledger unavailable"}'
            self.send_response(503)
        else:
            body = json.dumps(report, indent=2).encode()
            self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── /logs ──

    def _handle_logs(self):
        """Return the last N lines of gateway.log. ?n=100 for line count (default 200)."""
        import urllib.parse as urlparse
        qs = urlparse.parse_qs(urlparse.urlparse(self.path).query)
        n = min(int(qs.get("n", ["200"])[0]), 2000)

        log_file = SESSION_LOG_DIR / "gateway.log" if SESSION_LOG_DIR else None
        if log_file and log_file.exists():
            try:
                lines = log_file.read_text(errors="replace").splitlines()
                tail = lines[-n:] if len(lines) > n else lines
                body = "\n".join(tail).encode()
            except Exception:
                body = b"(error reading log file)"
        else:
            body = b"(no log file found)"

        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── Path mapping ──
    # Copilot API paths differ from standard SDK paths in some cases.
    PATH_MAP = {
        "/v1/chat/completions": "/chat/completions",  # OpenAI SDK sends /v1/, Copilot wants no /v1/
        "/v1/responses": "/responses",
    }

    # ── Forward (the core proxy) ──

    def _forward(self):
        method = self.command
        client_path = self.path.split("?", 1)[0]
        path = self.PATH_MAP.get(client_path, self.path)
        endpoint = _usage_endpoint(client_path)
        request_id = secrets.token_hex(16)
        origin = _classify_origin(self.client_address[0],
                                  self.headers.get("X-Gateway-Origin"))

        # Read request body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else None

        # Decompress request body if compressed (e.g. Codex CLI sends zstd)
        if body:
            encoding = (self.headers.get("Content-Encoding") or "").lower()
            if encoding == "zstd" or (not encoding and len(body) >= 4 and body[:4] == b'\x28\xb5\x2f\xfd'):
                try:
                    body = _zstd_decompress(body)
                    log("  decompressed zstd request body")
                except Exception as e:
                    err = json.dumps({"error": f"Failed to decompress zstd body: {e}"}).encode()
                    self.send_response(400)
                    self._cors_headers()
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(err)))
                    self.end_headers()
                    self.wfile.write(err)
                    self._record_failure(request_id, "unknown", endpoint,
                                         origin, 400)
                    return
            elif encoding == "gzip":
                try:
                    body = gzip.decompress(body)
                    log("  decompressed gzip request body")
                except Exception as e:
                    err = json.dumps({"error": f"Failed to decompress gzip body: {e}"}).encode()
                    self.send_response(400)
                    self._cors_headers()
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(err)))
                    self.end_headers()
                    self.wfile.write(err)
                    self._record_failure(request_id, "unknown", endpoint,
                                         origin, 400)
                    return

        # Parse request body for stream flag and model name
        is_stream = False
        model = "unknown"
        if body:
            try:
                req_json = json.loads(body)
                is_stream = req_json.get("stream", False)
                model = req_json.get("model", "unknown")
                # Strip fields not accepted by the Copilot API
                stripped = []
                for field in ("context_management",):
                    if field in req_json:
                        del req_json[field]
                        stripped.append(field)
                # Strip unsupported cache_control sub-fields (e.g. "scope")
                def _strip_cc_scope(obj):
                    if not isinstance(obj, dict):
                        return
                    cc = obj.get("cache_control")
                    if isinstance(cc, dict) and "scope" in cc:
                        del cc["scope"]
                        if "cache_control.scope" not in stripped:
                            stripped.append("cache_control.scope")
                    # Recurse into content blocks
                    content = obj.get("content")
                    if isinstance(content, list):
                        for block in content:
                            _strip_cc_scope(block)
                for msg_list in (req_json.get("system", []),
                                 req_json.get("messages", [])):
                    if isinstance(msg_list, list):
                        for item in msg_list:
                            _strip_cc_scope(item)
                    elif isinstance(msg_list, dict):
                        _strip_cc_scope(msg_list)
                # Forward output_config.effort largely as-is: Copilot models
                # accept different effort levels and the supported set expands
                # over time, so rather than maintain a hardcoded allowlist we
                # forward what the client sent and surface upstream's 400 with
                # an actionable hint (see error handling below).  The per-model
                # effort arrays are exposed on /v1/models (reasoning_efforts) so
                # clients can pre-validate.  The only adjustments are the two
                # narrow, per-model cases below: the 4.6 xhigh clamp here and
                # the 4.8 absent-effort injection further down.
                #
                # Base claude-opus-4.6 / 4.7 forward as-is: both ship with 1M
                # context and accept their native effort sets upstream (4.7:
                # low/medium/high/xhigh/max; 4.6: low/medium/high/max), so the
                # old rewrite to "-1m" / "-1m-internal" variants (a stale
                # workaround from when base 4.7 only accepted "medium") was
                # removed — the rewritten ids are not in GitHub's available list
                # and 400 with model_not_available_for_integrator.
                #
                # Defensive clamp: 4.6 does not accept "xhigh" (its native set
                # is low/medium/high/max — no xhigh).  A hand-crafted request
                # that sends xhigh would 400, so clamp it down to "high" (a
                # supported level) rather than reject.
                if model in ("claude-opus-4.6", "claude-opus-4-6"):
                    oc = req_json.get("output_config")
                    if isinstance(oc, dict) and oc.get("effort") == "xhigh":
                        oc["effort"] = "high"
                        stripped.append("effort:xhigh→high(4.6)")
                # Rewrite Anthropic-style `thinking.type=enabled` → `adaptive`
                # for Claude Opus 4.7/4.8 and the Claude 5 generation (opus-5,
                # sonnet-5).  Copilot's upstream rejects "enabled" on these
                # models with:
                #   "thinking.type.enabled" is not supported for this model.
                #   Use "thinking.type.adaptive" and "output_config.effort"
                # `adaptive` lets the model decide when to think and uses
                # `output_config.effort` (forwarded / injected below) as the
                # control.  `budget_tokens` is irrelevant in adaptive mode, so
                # drop it.  4.6 family still accepts `enabled`, so leave it
                # alone.  Claude Code ALWAYS sends `thinking.type=enabled`, so
                # without this entry a model 400s on every single request —
                # this rewrite is what makes a model usable from Claude Code at
                # all.  Narrow by design — only broaden after empirically
                # confirming another model hits the same rejection (4.8
                # verified 2026-06; opus-5 + sonnet-5 verified 2026-07).
                if model.startswith(("claude-opus-4.7", "claude-opus-4-7",
                                     "claude-opus-4.8", "claude-opus-4-8",
                                     "claude-opus-5", "claude-sonnet-5")):
                    thinking = req_json.get("thinking")
                    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
                        thinking["type"] = "adaptive"
                        thinking.pop("budget_tokens", None)
                        stripped.append("thinking.type:enabled→adaptive")
                # Pin "xhigh" reasoning effort for bare Claude Opus 4.8 / 5.
                # Claude Code never sends `output_config.effort`, so without
                # this the model falls back to its adaptive default rather
                # than the "(xhigh)" tier the Copilot CLI exposes.  Both ship
                # as a single model id (no -xhigh/-1m variants) with native 1M
                # context and accept low/medium/high/xhigh/max — 4.8 verified
                # 2026-06, opus-5 verified 2026-07.  Only inject when the
                # client did not specify an effort, so explicit selections
                # still win.
                #
                # Opus only, deliberately: sonnet-5 also accepts xhigh but is
                # the cheap/fast tier (Claude Code drives it for sub-agents),
                # so silently pinning max-cost effort there would be a
                # surprising cost regression.  It keeps upstream's default.
                if model in ("claude-opus-4.8", "claude-opus-4-8",
                             "claude-opus-5"):
                    oc = req_json.get("output_config")
                    if not isinstance(oc, dict):
                        oc = {}
                        req_json["output_config"] = oc
                    if "effort" not in oc:
                        oc["effort"] = "xhigh"
                        stripped.append(f"effort→xhigh({model})")
                # Strip tools not supported by the Copilot API
                tools = req_json.get("tools")
                if isinstance(tools, list):
                    unsupported_tools = {"image_generation"}
                    original_len = len(tools)
                    req_json["tools"] = [
                        t for t in tools
                        if t.get("type") not in unsupported_tools
                    ]
                    removed = original_len - len(req_json["tools"])
                    if removed:
                        stripped.append(f"tools({removed} unsupported)")
                if stripped:
                    body = json.dumps(req_json).encode()
                    log(f"  stripped unsupported fields: {stripped}")
                # DEBUG: log the effort actually being sent upstream
                oc_final = req_json.get("output_config") or {}
                log(f"  → effort sent upstream: {oc_final.get('effort', '<none>')} (model={model})")
            except (json.JSONDecodeError, AttributeError):
                pass

        # Build upstream request
        url = _get_upstream() + path
        headers = self._upstream_headers(len(body) if body else 0)

        log(f"{method} {path} → {url} (model={model}, stream={is_stream}, origin={origin})")

        # Try the request, auto-refresh token on 401
        resp, error_body, error_code = self._do_upstream(method, url, headers, body)
        if error_code == 401:
            log("  ← 401, refreshing token...")
            token_mgr.force_refresh()
            headers["Authorization"] = f"Bearer {token_mgr.token}"
            resp, error_body, error_code = self._do_upstream(method, url, headers, body)

        if resp is None:
            # If upstream rejected the effort level, augment the error so
            # the user sees an actionable hint (e.g. run /effort medium).
            if error_body and b"invalid_reasoning_effort" in error_body:
                try:
                    err_json = json.loads(error_body)
                    msg = err_json.get("error", {}).get("message", "")
                    err_json["error"]["message"] = (
                        msg + "  →  Run /effort to pick a supported level for this model."
                    )
                    error_body = json.dumps(err_json).encode()
                except (json.JSONDecodeError, AttributeError, KeyError, TypeError):
                    pass
            # Error response
            body_out = error_body or b'{"error":"upstream unavailable"}'
            self.send_response(error_code or 502)
            self._cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_out)))
            self.end_headers()
            self.wfile.write(body_out)
            log(f"  ← {error_code or 502} error: {body_out[:500]} (origin={origin})")
            self._record_failure(request_id, model, endpoint, origin,
                                 error_code or 502)
            return

        # Forward success response.
        # NOTE: upstream typically returns the body chunk-encoded with no
        # Content-Length. We strip Transfer-Encoding (urllib already de-chunks
        # for us). For non-streaming responses we read the body first and
        # send a proper Content-Length so HTTP/1.1 clients don't have to
        # wait for the socket to close. For streaming responses we send
        # Connection: close so the close itself terminates the stream.
        # Shared header-forwarding lives in _emit_forwarded_response_headers;
        # the per-path post-amble (Connection: close vs Content-Length) stays
        # inline so the divergence is visible at the call site.

        with resp:
            if is_stream:
                self._emit_forwarded_response_headers(resp)
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()

                total = 0
                input_tokens = 0
                output_tokens = 0
                nano_aiu = None
                token_details = []
                line_buf = b""
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    total += len(chunk)

                    # Incremental SSE line scanning for usage data
                    line_buf += chunk
                    while b"\n" in line_buf:
                        line, line_buf = line_buf.split(b"\n", 1)
                        line_str = line.decode("utf-8", errors="replace").strip()
                        if line_str.startswith("data: "):
                            try:
                                event = json.loads(line_str[6:])
                                it, ot, na, details = _extract_usage_from_event(event, path)
                                input_tokens = max(input_tokens, _nonnegative_int(it, 0))
                                output_tokens = max(output_tokens, _nonnegative_int(ot, 0))
                                if na is not None:
                                    nano_aiu = na if nano_aiu is None else max(nano_aiu, na)
                                token_details = _merge_token_detail_snapshots(
                                    token_details, details
                                )
                            except (json.JSONDecodeError, ValueError):
                                pass

                log(f"  ← {resp.status} streamed {total} bytes"
                    f" (in={input_tokens}, out={output_tokens}, origin={origin})")
                self._record_usage(
                    request_id, model, endpoint, resp.status, input_tokens,
                    output_tokens, nano_aiu, token_details, origin,
                )
            else:
                # Buffer the body so we can send an honest Content-Length.
                data = resp.read()
                self._emit_forwarded_response_headers(resp)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                input_tokens, output_tokens, nano_aiu, token_details = 0, 0, None, []
                try:
                    resp_json = json.loads(data)
                    input_tokens, output_tokens, nano_aiu, token_details = (
                        _extract_usage_from_response(resp_json, path)
                    )
                except (json.JSONDecodeError, ValueError):
                    pass
                log(f"  ← {resp.status} ({len(data)} bytes)"
                    f" (in={input_tokens}, out={output_tokens}, origin={origin})")
                self._record_usage(
                    request_id, model, endpoint, resp.status, input_tokens,
                    output_tokens, nano_aiu, token_details, origin,
                )

    def _record_usage(self, request_id: str, model: str, endpoint: str,
                      http_status: int, input_tokens: int, output_tokens: int,
                      nano_aiu, token_details: list, origin: str = "other"):
        """Record successful usage in both backward-compatible and durable stores."""
        input_tokens = _nonnegative_int(input_tokens, 0)
        output_tokens = _nonnegative_int(output_tokens, 0)
        nano_aiu = _optional_nonnegative_int(nano_aiu)
        if request_stats:
            if input_tokens or output_tokens:
                request_stats.record_success(
                    model, input_tokens, output_tokens, nano_aiu or 0,
                    origin=origin,
                )
            else:
                request_stats.record_success(model, 0, 0, 0, origin=origin)
                request_stats.record_parse_failure()
        if usage_store:
            usage_store.record(
                request_id, model, endpoint, origin, True, http_status,
                input_tokens, output_tokens, nano_aiu, token_details,
            )

    def _record_failure(self, request_id: str, model: str, endpoint: str,
                        origin: str, http_status: int):
        if request_stats:
            request_stats.record_failure(origin=origin)
        if usage_store:
            usage_store.record(
                request_id, model, endpoint, origin, False, http_status,
            )

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
            # Drop client auth, hop-by-hop, encoding, unsupported beta headers,
            # and gateway-internal headers (x-gateway-origin is read once in
            # _forward for stats classification — never proxied upstream).
            if lk in ("host", "connection", "transfer-encoding",
                       "x-api-key", "authorization", "accept-encoding",
                       "anthropic-beta", "x-gateway-origin"):
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
                         "Content-Type, Authorization, x-api-key, anthropic-version, openai-intent, x-gateway-origin")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, PUT, DELETE, PATCH, OPTIONS")

    def _emit_forwarded_response_headers(self, resp):
        """Emit the status, CORS, and upstream headers (minus _SKIP_FORWARDED_HEADERS).
        Caller is responsible for any per-path post-amble (Connection/Content-Length)
        and end_headers().
        """
        self.send_response(resp.status)
        self._cors_headers()
        for k, v in resp.headers.items():
            if k.lower() not in self._SKIP_FORWARDED_HEADERS:
                self.send_header(k, v)

    def _send_cors_preflight(self):
        self.send_response(200)
        self._cors_headers()
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
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
    global token_mgr, request_stats, usage_store

    # Windows / redirected-pipe safety: when stdout is piped to a file or
    # DEVNULL (the tray spawns us with stdout=stderr=DEVNULL), Python falls
    # back to the locale codec (cp1252 on Windows), which can't encode the
    # Unicode box-drawing characters in the startup banner. That raised
    # UnicodeEncodeError and killed the gateway before it bound the port —
    # the tray then saw "gateway not reachable". Force UTF-8 with a lossy
    # fallback so banner prints can never crash startup.
    for _stream in (sys.stdout, sys.stderr):
        # In GUI/windowed contexts (pythonw.exe, some frozen exes) std streams
        # can be None — skip explicitly rather than lean on the except below.
        if _stream is None:
            continue
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

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

    # Initialize in-memory stats and the independent durable usage ledger.
    request_stats = RequestStats()
    usage_store = UsageStore(USAGE_DB)

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
    print("║    GET  /stats               — token usage stats         ║")
    print("║    GET  /usage?days=N        — durable exact usage       ║")
    print("║    GET  /logs                — recent gateway log lines  ║")
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

    # Launch menu bar indicator if binary exists (skip if one is already running)
    menubar_proc = None
    menubar_bin = HERE / "menubar"
    if menubar_bin.exists() and not os.environ.get("GATEWAY_NO_MENUBAR"):
        # Adopt-don't-replace: if a menu-bar process is already drawing the icon
        # — standalone `menubar` binary OR the bundled CopilotGateway.app, which
        # has its own menu-bar UI — leave it alone. Killing-and-respawning
        # flickers the icon for no benefit, and not detecting CopilotGateway.app
        # caused duplicate icons to stack on the macOS status bar.
        def _first_pid(args: list[str]) -> str | None:
            try:
                r = subprocess.run(args, capture_output=True, text=True, timeout=5)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return None
            for line in r.stdout.splitlines():
                pid = line.strip()
                if pid:
                    return pid
            return None

        standalone_pid = _first_pid(["pgrep", "-x", "menubar"])
        app_pid = _first_pid(
            ["pgrep", "-f", r"CopilotGateway\.app/Contents/MacOS/CopilotGateway"]
        )
        if standalone_pid:
            log(f"menu bar indicator already running (PID {standalone_pid}) — skip launch")
        elif app_pid:
            log(f"CopilotGateway.app running (PID {app_pid}) — skip menubar launch (app provides its own)")
        else:
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
    finally:
        server.server_close()
        if demo_proc:
            demo_proc.terminate()
        if demo_log_file:
            demo_log_file.close()
        if menubar_proc:
            menubar_proc.terminate()
        if usage_store:
            usage_store.close()


if __name__ == "__main__":
    main()
