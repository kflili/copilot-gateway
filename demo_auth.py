"""Demo-only GitHub account management for demo.py.

Scope (see README "Demo App"): per-mode, process-wide account selection for the
demo UI only. It never changes the gateway's account, `.gateway-token.json`,
or the global gh login. Supported provider host: github.com (personal and
Enterprise Managed User accounts). GHE.com and GitHub Enterprise Server are
reported as unsupported and never receive github.com requests.

Every network, subprocess, clock and storage boundary is injectable so tests
run without real credentials or provider calls.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace

logger = logging.getLogger("demo.auth")

MODES = ("vscode", "cli")
INTEGRATION_IDS = {"vscode": "vscode-chat", "cli": "copilot-developer-cli"}
MODE_LABELS = {"vscode": "VS Code", "cli": "CLI"}

GITHUB_HOST = "github.com"
GITHUB_ORIGIN = "https://github.com"
GITHUB_API_ORIGIN = "https://api.github.com"
DEVICE_CODE_URL = GITHUB_ORIGIN + "/login/device/code"
DEVICE_TOKEN_URL = GITHUB_ORIGIN + "/login/oauth/access_token"
VERIFICATION_PATHS = frozenset({"/login/device"})
# Public VS Code GitHub OAuth client ID (microsoft/vscode github-authentication
# Config.gitHubClientId). Using it here is an experimental demo integration,
# not an officially supported Copilot client.
DEVICE_CLIENT_ID = "01ab8ac9400c4e429b23"
DEVICE_SCOPE = "read:user"
# Only origins with verified evidence; extend with primary-source evidence + tests.
COPILOT_API_ORIGINS = frozenset({
    # Exact GitHub.com plan routing origins (GitHub "Copilot allowlist reference":
    # *.individual / *.business / *.enterprise.githubcopilot.com families; the
    # repo's docs/research.md shows discovery returning the `api.` host).
    "https://api.githubcopilot.com",
    "https://api.individual.githubcopilot.com",
    "https://api.business.githubcopilot.com",
    "https://api.enterprise.githubcopilot.com",
})
ENTERPRISE_GUIDANCE_URL = (
    "https://docs.github.com/en/enterprise-cloud@latest/copilot/how-tos/"
    "configure-personal-settings/authenticate-to-ghecom")
COPILOT_MODELS_API_VERSION = os.environ.get("COPILOT_MODELS_API_VERSION", "2026-08-01")
USER_AGENT = "copilot-gateway-demo"

LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]{20,2048}$")
USER_CODE_RE = re.compile(r"^[A-Z0-9]{4,8}(?:-[A-Z0-9]{4,8})?$")

GH_TOKEN_ENV_KEYS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
                     "GITHUB_ENTERPRISE_TOKEN", "GH_HOST")

SOURCE_LABELS = {
    "environment": "environment variable GH_TOKEN",
    "gh_active": "gh active account",
    "gh_account": "gh account (selected in demo)",
    "legacy_vscode": "legacy saved VS Code OAuth (.gateway-token.json)",
    "device": "device OAuth (signed in via demo)",
}

MAX_JSON_BYTES = 1 << 20
MAX_MODELS_BYTES = 8 << 20
DEVICE_MIN_INTERVAL = 5
DEVICE_MAX_LIFETIME = 1800
DEVICE_TRANSIENT_LIMIT = 3
TXN_RETENTION_SECONDS = 600
# An approved-but-unconfirmed candidate holds a secret and blocks other sessions;
# it expires after this window.
CONFIRM_WINDOW_SECONDS = 600
# Absolute cap for any non-terminal attempt (covers stuck starting/verifying).
TXN_HARD_LIMIT_SECONDS = DEVICE_MAX_LIFETIME + CONFIRM_WINDOW_SECONDS + 300
TXN_MAX = 32

# ─── Secrets and errors ──────────────────────────────────────────────────────


class Secret:
    """Credential holder whose repr/str never reveal the value."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("Secret values cannot be serialized")


CATEGORY_STATUS = {
    "invalid_request": 400,
    "unauthenticated": 401,
    "expired_credentials": 401,
    "forbidden": 403,
    "csrf_failed": 403,
    "loopback_only": 403,
    "access_denied": 403,
    "device_denied": 403,
    "device_disabled": 403,
    "not_found": 404,
    "gh_account_unavailable": 404,
    "identity_mismatch": 409,
    "stale_generation": 409,
    "conflict": 409,
    "cancelled": 409,
    "device_expired": 410,
    "payload_too_large": 413,
    "unsupported_media_type": 415,
    "unsupported_host": 422,
    "persistence_failure": 500,
    "legacy_unreadable": 500,
    "unsafe_destination": 502,
    "unsupported_endpoint": 502,
    "provider_unavailable": 502,
    "gh_invalid_output": 502,
    "device_failed": 502,
    "provider_rate_limited": 503,
    "gh_missing": 503,
    "gh_timeout": 504,
    "upstream_error": 502,
}


class AuthError(Exception):
    """Sanitized, user-presentable failure. `message` must never hold secrets."""

    def __init__(self, category: str, message: str, status: int | None = None, **details):
        super().__init__(category)
        self.category = category
        self.message = message
        self.status = status or CATEGORY_STATUS.get(category, 400)
        self.details = details

    def public(self) -> dict:
        out = {"category": self.category, "message": self.message}
        out.update(self.details)
        return out


# ─── Destination validation ──────────────────────────────────────────────────


def validate_https_url(url: str, allowed_origins, *, allowed_paths=None) -> str:
    """Return a normalized URL or raise before any credential is attached."""
    def reject(reason: str):
        raise AuthError("unsafe_destination",
                        f"Refused an unexpected provider destination ({reason}).")

    if not isinstance(url, str) or not url or len(url) > 2048:
        reject("invalid URL")
    if any(ch in url for ch in "\\@?#") or any(ord(ch) <= 32 or ord(ch) == 127 for ch in url):
        reject("disallowed characters")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        reject("unparseable URL")
    if parts.scheme != "https":
        reject("HTTPS required")
    if parts.username is not None or parts.password is not None:
        reject("credentials in URL")
    if port not in (None, 443):
        reject("unexpected port")
    host = (parts.hostname or "").lower()
    netloc = parts.netloc.lower()
    if not host or netloc not in (host, f"{host}:443"):
        reject("unexpected host form")
    origin = f"https://{host}"
    if origin not in allowed_origins:
        reject("host not allowlisted")
    path = parts.path
    if allowed_paths is not None and path not in allowed_paths:
        reject("unexpected path")
    return origin + path


def validate_api_base(url) -> str:
    try:
        normalized = validate_https_url(url, COPILOT_API_ORIGINS, allowed_paths={"", "/"})
    except AuthError:
        raise AuthError("unsupported_endpoint",
                        "Copilot discovery returned an API endpoint this demo does not "
                        "allow; no credential was sent to it.") from None
    return normalized.rstrip("/")


# ─── HTTP transport (no redirects) ───────────────────────────────────────────


class TransportError(Exception):
    """Network failure; message is only the exception class name."""


class Response:
    def __init__(self, status: int, headers, raw):
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers.items() if headers else [])}
        self._raw = raw

    def read(self, limit: int = MAX_JSON_BYTES) -> bytes:
        if self._raw is None:
            return b""
        try:
            data = self._raw.read(limit + 1)
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise TransportError(type(e).__name__) from None
        if len(data) > limit:
            raise TransportError("ResponseTooLarge")
        return data

    def __iter__(self):
        if self._raw is None:
            return iter(())
        return iter(self._raw)

    def close(self):
        try:
            if self._raw is not None:
                self._raw.close()
        except Exception:
            pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # surfaces as a 3xx Response; credentials never follow a redirect


class UrllibTransport:
    def __init__(self):
        self._opener = urllib.request.build_opener(_NoRedirect())

    def request(self, method, url, headers, body=None, timeout=15) -> Response:
        req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            raw = self._opener.open(req, timeout=timeout)
            return Response(raw.status, raw.headers, raw)
        except urllib.error.HTTPError as e:
            return Response(e.code, e.headers, e)
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            raise TransportError(type(e).__name__) from None


# ─── Provider message sanitizing ─────────────────────────────────────────────

_TOKENISH = re.compile(
    r"(gh[opusr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}"
    r"|(?i:bearer|token)\s+[A-Za-z0-9._\-]{8,}"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-.]+|tid=[^;\s\"]+)")


def sanitize_provider_message(raw: bytes, secret_values=()) -> str | None:
    """Extract a short structured error message; never the raw body."""
    try:
        data = json.loads(raw[:65536].decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    msg = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            msg = err["message"]
        elif isinstance(data.get("message"), str):
            msg = data["message"]
        elif isinstance(err, str):
            msg = err
    if not msg:
        return None
    msg = msg[:300]
    for value in secret_values:
        if value:
            msg = msg.replace(value, "[redacted]")
    msg = _TOKENISH.sub("[redacted]", msg)
    return "".join(ch if ch.isprintable() else " " for ch in msg).strip() or None


# ─── GitHub provider calls ───────────────────────────────────────────────────


def _status_error(resp: Response, what: str, *, not_found_denied=False) -> AuthError:
    code = resp.status
    resp.close()
    if 300 <= code < 400:
        return AuthError("provider_unavailable", f"{what}: unexpected redirect refused.")
    if code == 401:
        return AuthError("expired_credentials",
                         f"{what}: the credential was rejected (expired, revoked or invalid).")
    if code == 403 or (code == 404 and not_found_denied):
        return AuthError("access_denied", f"{what}: access denied (HTTP {code}).")
    if code == 429:
        return AuthError("provider_rate_limited", f"{what}: rate limited by GitHub; try again later.")
    return AuthError("provider_unavailable", f"{what}: provider returned HTTP {code}.")


def _parse_json(resp: Response, what: str, limit=MAX_JSON_BYTES):
    try:
        data = resp.read(limit)
    except TransportError:
        raise AuthError("provider_unavailable", f"{what}: response could not be read.") from None
    finally:
        resp.close()
    try:
        return json.loads(data)
    except ValueError:
        raise AuthError("provider_unavailable", f"{what}: invalid JSON response.") from None


class GitHubProvider:
    def __init__(self, transport=None, timeout: float = 15):
        self.transport = transport or UrllibTransport()
        self.timeout = timeout

    def call(self, method, url, allowed_origins, headers, body=None, *,
             allowed_paths=None, timeout=None) -> Response:
        target = validate_https_url(url, allowed_origins, allowed_paths=allowed_paths)
        hdrs = {"User-Agent": USER_AGENT}
        hdrs.update(headers)
        try:
            return self.transport.request(method, target, hdrs, body, timeout or self.timeout)
        except TransportError as e:
            raise AuthError("provider_unavailable",
                            f"Could not reach {urllib.parse.urlsplit(target).hostname} "
                            f"({e.args[0] if e.args else 'network error'}).") from None

    def get_user(self, token: Secret) -> dict:
        what = "GitHub identity check"
        resp = self.call("GET", GITHUB_API_ORIGIN + "/user", {GITHUB_API_ORIGIN},
                         {"Authorization": "Bearer " + token.reveal(),
                          "Accept": "application/vnd.github+json"})
        if resp.status != 200:
            raise _status_error(resp, what)
        data = _parse_json(resp, what)
        uid = data.get("id") if isinstance(data, dict) else None
        login = data.get("login") if isinstance(data, dict) else None
        if (not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0
                or not isinstance(login, str) or not LOGIN_RE.match(login)):
            raise AuthError("provider_unavailable", f"{what}: unexpected identity response.")
        return {"host": GITHUB_HOST, "login": login, "user_id": uid}

    def discover_api_base(self, token: Secret) -> str:
        what = "Copilot access check"
        resp = self.call("GET", GITHUB_API_ORIGIN + "/copilot_internal/user", {GITHUB_API_ORIGIN},
                         {"Authorization": "token " + token.reveal(),
                          "Accept": "application/json"})
        if resp.status != 200:
            err = _status_error(resp, what, not_found_denied=True)
            if err.category == "access_denied":
                err.message = ("GitHub verified the account, but Copilot API access was not "
                               "granted (no Copilot entitlement, policy, or SSO authorization).")
            raise err
        data = _parse_json(resp, what)
        endpoints = data.get("endpoints") if isinstance(data, dict) else None
        api = endpoints.get("api") if isinstance(endpoints, dict) else None
        if not isinstance(api, str):
            raise AuthError("provider_unavailable", f"{what}: no Copilot API endpoint returned.")
        return validate_api_base(api)

    def fetch_models(self, token: Secret, api_base: str, integration_id: str) -> list:
        what = "Model catalog"
        url = validate_api_base(api_base) + "/models"
        headers = {"Authorization": "Bearer " + token.reveal(), "Accept": "application/json",
                   "Copilot-Integration-Id": integration_id,
                   "X-GitHub-Api-Version": COPILOT_MODELS_API_VERSION}
        resp = self.call("GET", url, COPILOT_API_ORIGINS, headers)
        if resp.status in (400, 406):
            resp.close()
            logger.warning("models API version %s rejected with HTTP %s; retrying unversioned",
                           COPILOT_MODELS_API_VERSION, resp.status)
            headers.pop("X-GitHub-Api-Version")
            resp = self.call("GET", url, COPILOT_API_ORIGINS, headers)
        if resp.status != 200:
            err = _status_error(resp, what)
            if err.category == "access_denied":
                err.message = ("GitHub verified the account, but the Copilot model catalog "
                               "was denied for this integration.")
            raise err
        data = _parse_json(resp, what, MAX_MODELS_BYTES)
        models = data.get("data", data) if isinstance(data, dict) else data
        if not isinstance(models, list) or not all(isinstance(m, dict) for m in models):
            raise AuthError("provider_unavailable", f"{what}: unexpected catalog format.")
        return models

    def _form_post(self, url: str, form: dict, what: str):
        body = urllib.parse.urlencode(form).encode()
        resp = self.call("POST", url, {GITHUB_ORIGIN}, {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded"}, body)
        if resp.status >= 500 or resp.status == 429 or 300 <= resp.status < 400:
            raise _status_error(resp, what)
        data = _parse_json(resp, what)
        if not isinstance(data, dict):
            raise AuthError("provider_unavailable", f"{what}: unexpected response.")
        return resp.status, data

    def device_start(self) -> dict:
        what = "Device sign-in start"
        status, data = self._form_post(DEVICE_CODE_URL,
                                       {"client_id": DEVICE_CLIENT_ID, "scope": DEVICE_SCOPE}, what)
        error = data.get("error")
        if error or status != 200:
            if error == "device_flow_disabled":
                raise AuthError("device_disabled", "Device sign-in is disabled for this OAuth client.")
            if error in ("unauthorized_client", "incorrect_client_credentials", "invalid_client"):
                raise AuthError("device_disabled", "GitHub rejected this demo's OAuth client.")
            raise AuthError("device_failed", f"{what}: GitHub refused the request.")
        device_code = data.get("device_code")
        user_code = data.get("user_code")
        if (not isinstance(device_code, str) or not 8 <= len(device_code) <= 512
                or not isinstance(user_code, str) or not USER_CODE_RE.match(user_code)):
            raise AuthError("device_failed", f"{what}: unexpected response.")
        verification_uri = validate_https_url(data.get("verification_uri"), {GITHUB_ORIGIN},
                                              allowed_paths=VERIFICATION_PATHS)
        expires_in = data.get("expires_in")
        interval = data.get("interval", DEVICE_MIN_INTERVAL)
        if not isinstance(expires_in, int) or isinstance(expires_in, bool) or expires_in <= 0:
            raise AuthError("device_failed", f"{what}: missing code lifetime.")
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < 0:
            interval = DEVICE_MIN_INTERVAL
        return {"device_code": Secret(device_code), "user_code": user_code,
                "verification_uri": verification_uri,
                "expires_in": min(expires_in, DEVICE_MAX_LIFETIME),
                "interval": min(max(interval, DEVICE_MIN_INTERVAL), 300)}

    def device_poll(self, device_code: Secret) -> dict:
        what = "Device sign-in"
        _, data = self._form_post(DEVICE_TOKEN_URL, {
            "client_id": DEVICE_CLIENT_ID, "device_code": device_code.reveal(),
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}, what)
        token = data.get("access_token")
        if isinstance(token, str):
            if not TOKEN_RE.match(token):
                raise AuthError("device_failed", f"{what}: unexpected token format.")
            expires_in = data.get("expires_in")
            if not isinstance(expires_in, int) or isinstance(expires_in, bool) or expires_in <= 0:
                expires_in = None
            # refresh_token is intentionally ignored: never exposed or persisted.
            return {"result": "token", "token": Secret(token), "expires_in": expires_in}
        error = data.get("error")
        if error == "authorization_pending":
            return {"result": "pending"}
        if error == "slow_down":
            interval = data.get("interval")
            if not isinstance(interval, int) or isinstance(interval, bool):
                interval = None
            return {"result": "slow_down", "interval": interval}
        if error == "access_denied":
            raise AuthError("device_denied", "Authorization was denied on GitHub.")
        if error == "expired_token":
            raise AuthError("device_expired", "The code expired before authorization. Start again.")
        if error == "device_flow_disabled":
            raise AuthError("device_disabled", "Device sign-in is disabled for this OAuth client.")
        if error in ("incorrect_client_credentials", "unauthorized_client", "invalid_client"):
            raise AuthError("device_disabled", "GitHub rejected this demo's OAuth client.")
        if error in ("incorrect_device_code", "unsupported_grant_type", "invalid_grant"):
            raise AuthError("device_failed", "GitHub rejected the device sign-in request.")
        raise AuthError("device_failed", f"{what}: unexpected response.")


# ─── gh CLI ──────────────────────────────────────────────────────────────────


def _default_runner(args, env, timeout):
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env,
                          stdin=subprocess.DEVNULL, **kwargs)


def _valid_token(value: str) -> Secret:
    token = value.strip()
    if not TOKEN_RE.match(token):
        raise AuthError("gh_invalid_output", "gh returned an unexpected token format.")
    return Secret(token)


class GhCli:
    def __init__(self, runner=None, executable: str = "gh", timeout: float = 10, environ=None):
        self.runner = runner or _default_runner
        self.executable = executable
        self.timeout = timeout
        self.environ = environ if environ is not None else os.environ

    def _run(self, args, *, isolate: bool):
        env = dict(self.environ)
        if isolate:
            for key in GH_TOKEN_ENV_KEYS:
                env.pop(key, None)
        try:
            result = self.runner([self.executable, *args], env, self.timeout)
        except FileNotFoundError:
            raise AuthError("gh_missing", "GitHub CLI (gh) was not found on PATH.") from None
        except subprocess.TimeoutExpired:
            raise AuthError("gh_timeout", "GitHub CLI (gh) did not respond in time.") from None
        except OSError:
            raise AuthError("gh_missing", "GitHub CLI (gh) could not be started.") from None
        return result.returncode, result.stdout or ""

    def list_accounts(self) -> list:
        rc, out = self._run(["auth", "status", "--json", "hosts"], isolate=True)
        try:
            data = json.loads(out)
        except ValueError:
            if rc != 0 and not out.strip():
                raise AuthError("gh_invalid_output", "gh auth status failed.") from None
            raise AuthError("gh_invalid_output", "gh auth status returned invalid JSON.") from None
        hosts = data.get("hosts") if isinstance(data, dict) else None
        if not isinstance(hosts, dict):
            raise AuthError("gh_invalid_output", "gh auth status returned an unexpected shape.")
        accounts = []
        for host, entries in hosts.items():
            if not isinstance(host, str) or not HOST_RE.match(host.lower()) or not isinstance(entries, list):
                continue
            host = host.lower()
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                login = entry.get("login")
                if not isinstance(login, str) or not LOGIN_RE.match(login):
                    continue
                state = entry.get("state") if isinstance(entry.get("state"), str) else "unknown"
                state = state if re.match(r"^[a-z_]{1,20}$", state) else "unknown"
                account = {"host": host, "login": login, "active": entry.get("active") is True,
                           "state": state}
                if host != GITHUB_HOST:
                    account.update(supported=False, reason=unsupported_host_message(host))
                elif state != "success":
                    account.update(supported=False,
                                   reason=f"gh reports this account's state as '{state}'; "
                                          "re-authenticate it with gh first.")
                else:
                    account.update(supported=True, reason=None)
                accounts.append(account)
        return accounts

    def token_for(self, host: str, login: str) -> Secret:
        rc, out = self._run(["auth", "token", "--hostname", host, "--user", login], isolate=True)
        if rc != 0 or not out.strip():
            raise AuthError("gh_account_unavailable",
                            f"gh has no usable token for {login} on {host}.")
        return _valid_token(out)

    def default_token(self) -> Secret | None:
        """Legacy CLI default, pinned to github.com: the active github.com account
        (gh would otherwise pick GH_HOST or another authenticated host)."""
        rc, out = self._run(["auth", "token", "--hostname", GITHUB_HOST], isolate=False)
        if rc != 0 or not out.strip():
            return None
        return _valid_token(out)


def normalize_gh_host(value) -> str | None:
    """GH_HOST as gh interprets it (bare hostname), or None when unset."""
    host = (value or "").strip().lower()
    if not host:
        return None
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host.rstrip("/")


def unsupported_host_message(host: str) -> str:
    kind = "GHE.com data residency" if host.endswith(".ghe.com") else "GitHub Enterprise Server"
    return (f"{host} looks like {kind}. This demo supports github.com only; enterprise hosts "
            "need their own sign-in and are never sent to github.com.")


# ─── Protected persistence ───────────────────────────────────────────────────


class StoreError(AuthError):
    def __init__(self, message: str):
        super().__init__("persistence_failure", message)


class DpapiProtector:
    """User-scoped Windows DPAPI (never CRYPTPROTECT_LOCAL_MACHINE)."""

    scheme = "dpapi-user"
    _ENTROPY = b"copilot-gateway-demo-auth-v1"
    _UI_FORBIDDEN = 0x1

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        self._ctypes = ctypes
        self._Blob = Blob
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        sig = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p,
               ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        self._protect = crypt32.CryptProtectData
        self._protect.argtypes = sig
        self._protect.restype = wintypes.BOOL
        self._unprotect = crypt32.CryptUnprotectData
        self._unprotect.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p] + sig[2:]
        self._unprotect.restype = wintypes.BOOL
        self._free = kernel32.LocalFree
        self._free.argtypes = [ctypes.c_void_p]
        self._free.restype = ctypes.c_void_p

    def _blob(self, data: bytes):
        buf = self._ctypes.create_string_buffer(data, len(data))
        return self._Blob(len(data), self._ctypes.cast(buf, self._ctypes.POINTER(self._ctypes.c_char))), buf

    def _call(self, fn, data: bytes, *description) -> bytes:
        ctypes = self._ctypes
        in_blob, in_buf = self._blob(data)
        ent_blob, _ent_buf = self._blob(self._ENTROPY)
        out = self._Blob()
        try:
            ok = fn(ctypes.byref(in_blob), *description, ctypes.byref(ent_blob), None, None,
                    self._UI_FORBIDDEN, ctypes.byref(out))
            if not ok:
                raise StoreError(f"Windows DPAPI failed (error {ctypes.get_last_error()}).")
            try:
                return ctypes.string_at(out.pbData, out.cbData)
            finally:
                ctypes.memset(out.pbData, 0, out.cbData)
                self._free(ctypes.cast(out.pbData, ctypes.c_void_p))
        finally:
            ctypes.memset(in_buf, 0, len(data))

    def protect(self, data: bytes) -> str:
        return base64.b64encode(self._call(self._protect, data, "copilot-gateway demo auth")).decode()

    def unprotect(self, value: str) -> bytes:
        try:
            raw = base64.b64decode(value, validate=True)
        except ValueError:
            raise StoreError("Saved demo credential is corrupt.") from None
        return self._call(self._unprotect, raw, None)


class PosixFileProtector:
    """Owner-only file permissions. This is access control, not encryption."""

    scheme = "posix-0600"

    def protect(self, data: bytes) -> str:
        return base64.b64encode(data).decode()

    def unprotect(self, value: str) -> bytes:
        try:
            return base64.b64decode(value, validate=True)
        except ValueError:
            raise StoreError("Saved demo credential is corrupt.") from None


def default_protector():
    return DpapiProtector() if sys.platform == "win32" else PosixFileProtector()


def check_posix_file(st, uid) -> str | None:
    """Return a problem description for an unsafe POSIX store file, else None."""
    if stat.S_ISLNK(st.st_mode):
        return "is a symbolic link"
    if not stat.S_ISREG(st.st_mode):
        return "is not a regular file"
    if uid is not None and st.st_uid != uid:
        return "is owned by another user"
    if st.st_mode & 0o077:
        return "is readable or writable by other users (expected mode 0600)"
    return None


class AuthStore:
    VERSION = 1

    def __init__(self, path, protector=None, *, posix=None):
        self.path = os.fspath(path)
        self._protector = protector
        self.posix = (os.name == "posix") if posix is None else posix

    @property
    def protector(self):
        if self._protector is None:
            try:
                self._protector = default_protector()
            except (OSError, AttributeError):
                raise StoreError("Credential protection is unavailable on this system.") from None
        return self._protector

    def _name(self) -> str:
        return os.path.basename(self.path)

    def load(self) -> dict:
        if os.path.islink(self.path):
            raise StoreError(f"Demo auth store {self._name()} is a symbolic link; refusing to use it.")
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            return {}
        except OSError:
            raise StoreError(f"Demo auth store {self._name()} could not be read.") from None
        problem = check_posix_file(st, os.getuid() if hasattr(os, "getuid") else None) \
            if self.posix else (None if stat.S_ISREG(st.st_mode) else "is not a regular file")
        if problem:
            raise StoreError(f"Demo auth store {self._name()} {problem}; refusing to use it.")
        if st.st_size > MAX_JSON_BYTES:
            raise StoreError(f"Demo auth store {self._name()} is too large.")
        try:
            with open(self.path, "rb") as fh:
                data = json.loads(fh.read())
        except (OSError, ValueError):
            raise StoreError(f"Demo auth store {self._name()} is unreadable or corrupt. "
                             "Move it aside to reset demo accounts.") from None
        if not isinstance(data, dict) or data.get("version") != self.VERSION \
                or not isinstance(data.get("modes"), dict):
            raise StoreError(f"Demo auth store {self._name()} has an unsupported format.")
        records = {}
        for mode, rec in data["modes"].items():
            if mode not in MODES:
                continue
            records[mode] = self._decode_record(rec)
        return records

    def _decode_record(self, rec) -> dict:
        if not isinstance(rec, dict):
            raise StoreError("Demo auth store has an invalid record.")
        kind = rec.get("kind")
        if kind == "disconnected":
            return {"kind": "disconnected"}
        login, uid, host = rec.get("login"), rec.get("user_id"), rec.get("host")
        if (host != GITHUB_HOST or not isinstance(login, str) or not LOGIN_RE.match(login)
                or not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0):
            raise StoreError("Demo auth store has an invalid account record.")
        if kind == "gh":
            return {"kind": "gh", "host": host, "login": login, "user_id": uid}
        if kind == "device":
            secret = rec.get("secret")
            expires_at = rec.get("expires_at")
            if expires_at is not None and not isinstance(expires_at, (int, float)):
                raise StoreError("Demo auth store has an invalid expiry.")
            if not isinstance(secret, dict) or secret.get("scheme") != self.protector.scheme \
                    or not isinstance(secret.get("data"), str):
                raise StoreError("Saved demo credential uses an unavailable protection scheme.")
            try:
                token = self.protector.unprotect(secret["data"]).decode("utf-8")
            except UnicodeDecodeError:
                raise StoreError("Saved demo credential is corrupt.") from None
            if not TOKEN_RE.match(token):
                raise StoreError("Saved demo credential is corrupt.")
            return {"kind": "device", "host": host, "login": login, "user_id": uid,
                    "expires_at": expires_at, "secret": Secret(token)}
        raise StoreError("Demo auth store has an unknown record type.")

    def _encode_record(self, rec: dict) -> dict:
        if rec["kind"] == "disconnected":
            return {"kind": "disconnected"}
        out = {"kind": rec["kind"], "host": rec["host"], "login": rec["login"],
               "user_id": rec["user_id"]}
        if rec["kind"] == "device":
            out["expires_at"] = rec.get("expires_at")
            out["secret"] = {"scheme": self.protector.scheme,
                             "data": self.protector.protect(rec["secret"].reveal().encode())}
        return out

    def save(self, records: dict, *, before_replace=None) -> None:
        """Atomic replace. `before_replace` runs after staging/fsync and immediately
        before os.replace; if it raises, nothing is replaced and the staged file is
        removed (the exception propagates unchanged)."""
        payload = json.dumps({"version": self.VERSION,
                              "modes": {m: self._encode_record(r) for m, r in records.items()}},
                             indent=2).encode()
        directory = os.path.dirname(os.path.abspath(self.path))
        if os.path.islink(self.path):
            raise StoreError(f"Demo auth store {self._name()} is a symbolic link; refusing to write.")
        tmp = None
        try:
            if not os.path.isdir(directory):
                os.makedirs(directory, mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".demo-auth-", suffix=".tmp", dir=directory)
            with os.fdopen(fd, "wb") as fh:
                if self.posix and hasattr(os, "fchmod"):
                    os.fchmod(fh.fileno(), 0o600)
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            if before_replace is not None:
                before_replace()
            os.replace(tmp, self.path)
            tmp = None
        except OSError:
            raise StoreError(f"Could not write demo auth store {self._name()}; "
                             "the previous selection was kept.") from None
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


# ─── Account state ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModeState:
    mode: str
    generation: str
    status: str  # unloaded | loading | ready | signed_out | error
    source: str | None = None
    account: dict | None = None
    api_base: str | None = None
    catalog: tuple = ()
    error: dict | None = None
    expires_at: float | None = None
    secret: Secret | None = field(default=None, repr=False)

    def public(self) -> dict:
        return {
            "mode": self.mode,
            "generation": self.generation,
            "status": self.status,
            "account": dict(self.account) if self.account else None,
            "source": self.source,
            "source_label": SOURCE_LABELS.get(self.source) if self.source else None,
            "integration_id": INTEGRATION_IDS[self.mode],
            "api_base": self.api_base,
            "expires_at": self.expires_at,
            "error": dict(self.error) if self.error else None,
        }


@dataclass(frozen=True)
class ChatSnapshot:
    mode: str
    generation: str
    secret: Secret = field(repr=False)
    api_base: str
    integration_id: str
    catalog: tuple


ACTIVE_TXN = ("starting", "pending", "verifying", "awaiting_confirmation")


@dataclass
class DeviceTxn:
    id: str
    owner: str
    mode: str
    base_generation: str
    expected_login: str | None
    status: str = "starting"
    device_code: Secret | None = field(default=None, repr=False)
    user_code: str | None = None
    verification_uri: str | None = None
    deadline: float = 0.0
    interval: int = DEVICE_MIN_INTERVAL
    next_poll_at: float = 0.0
    polling: bool = False
    transient_failures: int = 0
    candidate: dict | None = None
    candidate_secret: Secret | None = field(default=None, repr=False)
    candidate_api_base: str | None = None
    candidate_catalog: tuple = ()
    candidate_expires_at: float | None = None
    error: dict | None = None
    finished_at: float | None = None
    created_at: float = 0.0

    def public(self, now: float) -> dict:
        out = {"transaction_id": self.id, "mode": self.mode, "status": self.status,
               "error": dict(self.error) if self.error else None}
        if self.status == "pending":
            out.update(user_code=self.user_code, verification_uri=self.verification_uri,
                       expires_in=max(0, int(self.deadline - now)),
                       retry_after=max(1, math.ceil(self.next_poll_at - now)))
        if self.status in ("pending", "verifying"):
            out.setdefault("retry_after", 1)
        if self.candidate:
            out["candidate"] = dict(self.candidate)
        return out


def _catalog_counts(catalog) -> dict:
    callable_count = sum(1 for m in catalog if m.get("supported_endpoints"))
    return {"model_count": len(catalog), "callable_count": callable_count}


def _require_mode(mode) -> str:
    if mode not in MODES:
        raise AuthError("invalid_request", "Unknown mode.")
    return mode


def _optional_login(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not LOGIN_RE.match(value.strip()):
        raise AuthError("invalid_request", "Invalid GitHub username.")
    return value.strip()


class AuthManager:
    """Per-mode demo account state with generation compare-and-swap."""

    def __init__(self, *, transport=None, gh=None, store=None, legacy_token_file=None,
                 clock=time.monotonic, wall=time.time, environ=None):
        self.provider = GitHubProvider(transport)
        self.gh = gh or GhCli()
        self.store = store
        self.legacy_token_file = os.fspath(legacy_token_file) if legacy_token_file else None
        self.clock = clock
        self.wall = wall
        self.environ = environ if environ is not None else os.environ
        self._cv = threading.Condition(threading.Lock())
        self._epoch = secrets.token_hex(4)
        self._counter = 0
        self._records = None
        self._store_error = None
        self._txns: dict[str, DeviceTxn] = {}
        self._states = {m: ModeState(m, self._new_generation(), "unloaded") for m in MODES}

    # ── generations / snapshots ──

    def _new_generation(self) -> str:
        self._counter += 1
        return f"{self._epoch}-{self._counter}"

    def _load_records_locked(self):
        if self._records is None and self._store_error is None:
            if self.store is None:
                self._records = {}
            else:
                try:
                    self._records = self.store.load()
                except AuthError as e:
                    self._store_error = e.public()
        return self._records, self._store_error

    def _expired(self, expires_at) -> bool:
        return expires_at is not None and self.wall() >= expires_at

    def _demote_expired_locked(self, mode: str) -> ModeState:
        """A ready state whose credential expired is never reported as usable."""
        state = self._states[mode]
        if state.status == "ready" and self._expired(state.expires_at):
            who = f"@{state.account['login']}" if state.account else "this account"
            state = ModeState(mode, self._new_generation(), "error", source=state.source,
                              account=state.account, expires_at=state.expires_at, error={
                                  "category": "expired_credentials",
                                  "message": f"The demo sign-in for {who} expired. Sign in again."})
            self._states[mode] = state
            self._cv.notify_all()
        return state

    def ensure(self, mode: str, timeout: float = 90, *, demote: bool = True) -> ModeState:
        _require_mode(mode)
        with self._cv:
            state = self._states[mode]
            if state.status == "unloaded":
                self._states[mode] = state = replace(state, status="loading")
                records, store_error = self._load_records_locked()
            else:
                deadline = self.clock() + timeout
                while self._states[mode].status == "loading":
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        break
                    self._cv.wait(min(remaining, 1.0))
                return self._demote_expired_locked(mode) if demote else self._states[mode]
        result = self._resolve(mode, state.generation, records, store_error)
        with self._cv:
            current = self._states[mode]
            if current.generation == state.generation and current.status == "loading":
                self._states[mode] = current = result
            self._cv.notify_all()
            return self._demote_expired_locked(mode) if demote else self._states[mode]

    def status(self, mode: str) -> dict:
        return self.ensure(mode).public()

    def chat_snapshot(self, mode: str, generation) -> ChatSnapshot:
        _require_mode(mode)
        self.ensure(mode, demote=False)
        with self._cv:
            state = self._states[mode]
            if (generation == state.generation and state.status == "ready"
                    and self._expired(state.expires_at)):
                self._demote_expired_locked(mode)
                raise AuthError("expired_credentials", "The demo sign-in expired. Sign in again.")
            if not isinstance(generation, str) or generation != state.generation:
                raise AuthError("stale_generation",
                                "The account for this mode changed. Refresh before sending.",
                                generation=state.generation)
            if state.status != "ready" or state.secret is None:
                raise AuthError("unauthenticated", "No usable account for this mode.")
            if state.expires_at is not None and self.wall() >= state.expires_at:
                raise AuthError("expired_credentials", "The demo sign-in expired. Sign in again.")
            return ChatSnapshot(mode, state.generation, state.secret, state.api_base,
                                INTEGRATION_IDS[mode], state.catalog)

    # ── resolution (no lock held) ──

    def _validate(self, mode: str, token: Secret, *, expected_login=None, expected_id=None):
        identity = self.provider.get_user(token)
        if expected_id is not None and identity["user_id"] != expected_id:
            raise AuthError("identity_mismatch",
                            f"The credential now belongs to @{identity['login']}, not the saved "
                            "account. It was not used.", identity=identity)
        if expected_login and identity["login"].casefold() != expected_login.casefold():
            raise AuthError("identity_mismatch",
                            f"Signed in as @{identity['login']}, but @{expected_login} was "
                            "expected. The account was not selected.", identity=identity)
        try:
            api_base = self.provider.discover_api_base(token)
            catalog = tuple(self.provider.fetch_models(token, api_base, INTEGRATION_IDS[mode]))
        except AuthError as e:
            e.details["identity"] = identity
            if e.category == "access_denied":
                e.message = f"@{identity['login']} verified, models unavailable: {e.message}"
            raise
        return identity, api_base, catalog

    def _ready_state(self, mode, generation, source, token, identity, api_base, catalog,
                     expires_at=None) -> ModeState:
        return ModeState(mode, generation, "ready", source=source, account=identity,
                         api_base=api_base, catalog=catalog, expires_at=expires_at, secret=token)

    def _error_state(self, mode, generation, source, err: AuthError, status="error") -> ModeState:
        return ModeState(mode, generation, status, source=source,
                         account=err.details.get("identity"), error=err.public())

    def _resolve(self, mode, generation, records, store_error) -> ModeState:
        if store_error:
            return ModeState(mode, generation, "error", error=dict(store_error))
        record = (records or {}).get(mode)
        if record is None:
            return self._resolve_default(mode, generation)
        kind = record["kind"]
        if kind == "disconnected":
            return ModeState(mode, generation, "signed_out", error={
                "category": "signed_out",
                "message": f"Signed out of the demo's {MODE_LABELS[mode]} mode. "
                           "Choose an account or sign in."})
        source = "gh_account" if kind == "gh" else "device"
        saved = {"host": record["host"], "login": record["login"], "user_id": record["user_id"]}
        try:
            if kind == "gh":
                token = self.gh.token_for(record["host"], record["login"])
                expires_at = None
            else:
                expires_at = record.get("expires_at")
                if expires_at is not None and self.wall() >= expires_at:
                    raise AuthError("expired_credentials",
                                    "The saved demo sign-in expired. Sign in again.")
                token = record["secret"]
            identity, api_base, catalog = self._validate(mode, token, expected_id=record["user_id"])
        except AuthError as e:
            e.details.setdefault("identity", saved)
            return self._error_state(mode, generation, source, e)
        return self._ready_state(mode, generation, source, token, identity, api_base, catalog,
                                 expires_at)

    def _default_credential(self, mode):
        if mode == "cli":
            # gh applies GH_TOKEN (and its own default lookup) to GH_HOST. A
            # non-github.com GH_HOST means the environment's credential belongs to
            # an unsupported host: refuse before any github.com request, with no
            # fallback to another account.
            gh_host = normalize_gh_host(self.environ.get("GH_HOST"))
            if gh_host is not None and gh_host != GITHUB_HOST:
                raise AuthError("unsupported_host",
                                f"GH_HOST={gh_host} selects a host this demo does not support; "
                                "its credential was not sent to github.com. Choose a "
                                "github.com gh account or sign in.",
                                guidance_url=ENTERPRISE_GUIDANCE_URL)
            value = (self.environ.get("GH_TOKEN") or "").strip()
            if value:
                if not TOKEN_RE.match(value):
                    raise AuthError("unauthenticated", "GH_TOKEN has an unexpected format.")
                return "environment", Secret(value)
            try:
                token = self.gh.default_token()
            except AuthError as e:
                raise AuthError("unauthenticated",
                                f"No CLI credential: GH_TOKEN is unset and {e.message}") from None
            if token is None:
                raise AuthError("unauthenticated",
                                "No CLI credential: GH_TOKEN is unset and gh has no active login.")
            return "gh_active", token
        path = self.legacy_token_file
        if not path or not os.path.exists(path):
            raise AuthError("unauthenticated",
                            "No saved VS Code OAuth credential. Choose a gh account or sign in.")
        try:
            with open(path, "rb") as fh:
                data = json.loads(fh.read(MAX_JSON_BYTES))
        except (OSError, ValueError):
            raise AuthError("legacy_unreadable",
                            "The legacy .gateway-token.json could not be read.") from None
        if not isinstance(data, dict):
            raise AuthError("legacy_unreadable", "The legacy .gateway-token.json is invalid.")
        if data.get("mode") != "vscode":
            raise AuthError("unauthenticated",
                            "The legacy token file is not a VS Code OAuth token. "
                            "Choose a gh account or sign in.")
        token = data.get("token")
        if not isinstance(token, str) or not TOKEN_RE.match(token.strip()):
            raise AuthError("legacy_unreadable", "The legacy .gateway-token.json is invalid.")
        return "legacy_vscode", Secret(token.strip())

    def _resolve_default(self, mode, generation) -> ModeState:
        source = None
        try:
            source, token = self._default_credential(mode)
            identity, api_base, catalog = self._validate(mode, token)
        except AuthError as e:
            status = "signed_out" if e.category == "unauthenticated" else "error"
            return self._error_state(mode, generation, source, e, status)
        return self._ready_state(mode, generation, source, token, identity, api_base, catalog)

    # ── activation ──

    def _check_generation_locked(self, mode, expected_generation):
        state = self._states[mode]
        if not isinstance(expected_generation, str) or expected_generation != state.generation:
            raise AuthError("stale_generation",
                            "The account for this mode changed in another tab. Refresh and retry.",
                            generation=state.generation)
        if state.status == "loading":
            raise AuthError("conflict", "The account for this mode is still loading.")
        return state

    def _activate_locked(self, mode, expected_generation, build, record, *, remove=False):
        self._check_generation_locked(mode, expected_generation)
        records, store_error = self._load_records_locked()
        if store_error:
            raise AuthError(store_error["category"], store_error["message"])
        new_records = dict(records)
        if remove:
            new_records.pop(mode, None)
        elif record is not None:
            new_records[mode] = record
        candidate = build("pending")

        # Expiry admission contract (PLAN.md "Expiry admission contract"): an
        # expiring ready candidate must be unexpired at the last sample taken after
        # staging and immediately before replacement. Rejection there changes
        # nothing. Expiry after that sample is ordinary expiry of the committed
        # selection: no rollback; the returned state is freshly sampled instead.
        def admit():
            if candidate.status == "ready" and self._expired(candidate.expires_at):
                raise AuthError("expired_credentials",
                                "The credential expired before it could be applied. The "
                                "previous account is unchanged.", phase="admission")

        admit()  # also covers activations that write nothing
        if self.store is not None and new_records != records:
            self.store.save(new_records, before_replace=admit)
        self._records = new_records
        new_state = replace(candidate, generation=self._new_generation())
        self._states[mode] = new_state
        self._cv.notify_all()
        logger.info("demo account [%s] -> %s (%s)", mode,
                    new_state.account["login"] if new_state.account else new_state.status,
                    new_state.source)
        return self._demote_expired_locked(mode).public()

    def _precheck(self, mode, expected_generation):
        self.ensure(mode)
        with self._cv:
            self._check_generation_locked(mode, expected_generation)

    def select_gh(self, mode, host, login, expected_generation) -> dict:
        _require_mode(mode)
        host = host.lower() if isinstance(host, str) else host
        if not isinstance(host, str) or not HOST_RE.match(host):
            raise AuthError("invalid_request", "Invalid host.")
        if not isinstance(login, str) or not LOGIN_RE.match(login):
            raise AuthError("invalid_request", "Invalid GitHub username.")
        if host != GITHUB_HOST:
            raise AuthError("unsupported_host", unsupported_host_message(host),
                            guidance_url=ENTERPRISE_GUIDANCE_URL)
        self._precheck(mode, expected_generation)
        token = self.gh.token_for(host, login)
        identity, api_base, catalog = self._validate(mode, token, expected_login=login)
        record = {"kind": "gh", "host": host, "login": identity["login"],
                  "user_id": identity["user_id"]}
        with self._cv:
            return self._activate_locked(
                mode, expected_generation,
                lambda gen: self._ready_state(mode, gen, "gh_account", token, identity,
                                              api_base, catalog), record)

    def sign_out(self, mode, expected_generation) -> dict:
        _require_mode(mode)
        self._precheck(mode, expected_generation)
        with self._cv:
            return self._activate_locked(
                mode, expected_generation,
                lambda gen: ModeState(mode, gen, "signed_out", error={
                    "category": "signed_out",
                    "message": f"Signed out of the demo's {MODE_LABELS[mode]} mode. "
                               "Choose an account or sign in."}),
                {"kind": "disconnected"})

    def use_default(self, mode, expected_generation) -> dict:
        _require_mode(mode)
        self._precheck(mode, expected_generation)
        source, token = self._default_credential(mode)
        identity, api_base, catalog = self._validate(mode, token)
        with self._cv:
            return self._activate_locked(
                mode, expected_generation,
                lambda gen: self._ready_state(mode, gen, source, token, identity, api_base,
                                              catalog), None, remove=True)

    def reload(self, mode, expected_generation) -> dict:
        _require_mode(mode)
        self._precheck(mode, expected_generation)
        with self._cv:
            records, store_error = self._load_records_locked()
            if store_error and self.store is not None:
                self._records, self._store_error = None, None
                records, store_error = self._load_records_locked()
        result = self._resolve(mode, "pending", records, store_error)
        with self._cv:
            current = self._check_generation_locked(mode, expected_generation)
            if result.status != "ready" and current.status == "ready":
                raise AuthError(result.error["category"], result.error["message"],
                                **{k: v for k, v in result.error.items()
                                   if k not in ("category", "message")})
            new_state = replace(result, generation=self._new_generation())
            self._states[mode] = new_state
            self._cv.notify_all()
            return new_state.public()

    # ── gh accounts ──

    def list_accounts(self) -> dict:
        try:
            return {"accounts": self.gh.list_accounts(), "error": None,
                    "guidance_url": ENTERPRISE_GUIDANCE_URL}
        except AuthError as e:
            return {"accounts": [], "error": e.public(), "guidance_url": ENTERPRISE_GUIDANCE_URL}

    # ── device sign-in ──

    def _prune_locked(self):
        now = self.clock()
        for txn in self._txns.values():
            if txn.status == "pending" and now >= txn.deadline:
                self._finish_locked(txn, "expired", AuthError(
                    "device_expired", "The code expired before authorization. Start again."))
            elif txn.status == "awaiting_confirmation" and (
                    now >= txn.deadline or self._expired(txn.candidate_expires_at)):
                self._finish_locked(txn, "expired", AuthError(
                    "device_expired",
                    "The approved sign-in's credential expired before confirmation. Start again."
                    if self._expired(txn.candidate_expires_at) else
                    "The verified account was not confirmed in time. Start again."))
            elif txn.status in ACTIVE_TXN and now - txn.created_at >= TXN_HARD_LIMIT_SECONDS:
                self._finish_locked(txn, "failed", AuthError(
                    "device_failed", "The sign-in attempt timed out. Start again."))
        finished = sorted((t for t in self._txns.values() if t.finished_at is not None),
                          key=lambda t: t.finished_at)
        for txn in finished:
            if now - txn.finished_at > TXN_RETENTION_SECONDS or len(self._txns) > TXN_MAX:
                del self._txns[txn.id]

    def _finish_locked(self, txn: DeviceTxn, status: str, err: AuthError | None = None):
        txn.status = status
        txn.device_code = None
        txn.candidate_secret = None
        txn.candidate_catalog = ()
        txn.polling = False
        txn.finished_at = self.clock()
        if err is not None:
            txn.error = err.public()

    def _get_txn_locked(self, owner, txn_id) -> DeviceTxn:
        txn = self._txns.get(txn_id) if isinstance(txn_id, str) else None
        if txn is None or not secrets.compare_digest(txn.owner, owner or ""):
            raise AuthError("not_found", "No such sign-in for this browser session.")
        return txn

    def start_device(self, owner, mode, expected_login=None) -> dict:
        _require_mode(mode)
        expected_login = _optional_login(expected_login)
        self.ensure(mode)
        with self._cv:
            self._prune_locked()
            active = [t for t in self._txns.values() if t.mode == mode and t.status in ACTIVE_TXN]
            for other in active:
                if other.owner != owner:
                    raise AuthError("conflict", "Another browser session is signing in for this "
                                                "mode. Wait for it to finish or expire.")
            for other in active:
                self._finish_locked(other, "cancelled", AuthError(
                    "cancelled", "Replaced by a newer sign-in attempt."))
            txn = DeviceTxn(secrets.token_urlsafe(18), owner, mode, self._states[mode].generation,
                            expected_login, created_at=self.clock())
            self._txns[txn.id] = txn
        try:
            started = self.provider.device_start()
        except AuthError as e:
            with self._cv:
                if txn.status == "starting":
                    self._finish_locked(txn, "failed", e)
            raise
        with self._cv:
            if txn.status != "starting":
                return txn.public(self.clock())
            now = self.clock()
            txn.status = "pending"
            txn.device_code = started["device_code"]
            txn.user_code = started["user_code"]
            txn.verification_uri = started["verification_uri"]
            txn.deadline = now + started["expires_in"]
            txn.interval = started["interval"]
            txn.next_poll_at = now + txn.interval
            return txn.public(now)

    def poll_device(self, owner, txn_id) -> dict:
        with self._cv:
            self._prune_locked()
            txn = self._get_txn_locked(owner, txn_id)
            now = self.clock()
            if txn.status != "pending" or txn.polling or now < txn.next_poll_at:
                return txn.public(now)
            txn.polling = True
            txn.next_poll_at = now + txn.interval
            device_code = txn.device_code
            call_started = now
        try:
            result = self.provider.device_poll(device_code)
        except AuthError as e:
            with self._cv:
                txn.polling = False
                if txn.status == "pending":
                    if e.category in ("provider_unavailable", "provider_rate_limited"):
                        txn.transient_failures += 1
                        if txn.transient_failures >= DEVICE_TRANSIENT_LIMIT:
                            self._finish_locked(txn, "failed", e)
                        else:
                            txn.next_poll_at = max(txn.next_poll_at,
                                                   call_started + txn.interval * 2)
                    else:
                        status = {"device_denied": "denied",
                                  "device_expired": "expired"}.get(e.category, "failed")
                        self._finish_locked(txn, status, e)
                return txn.public(self.clock())
        with self._cv:
            txn.polling = False
            if txn.status != "pending":
                return txn.public(self.clock())
            txn.transient_failures = 0
            if result["result"] == "pending":
                return txn.public(self.clock())
            if result["result"] == "slow_down":
                txn.interval = max(txn.interval + 5, result.get("interval") or 0)
                txn.next_poll_at = max(txn.next_poll_at, call_started + txn.interval)
                return txn.public(self.clock())
            token = result["token"]
            expires_in = result.get("expires_in")
            issued_at = self.wall()  # expiry is anchored to issuance, not to verification end
            txn.status = "verifying"
            txn.device_code = None
            mode, expected_login = txn.mode, txn.expected_login
        try:
            identity, api_base, catalog = self._validate(mode, token, expected_login=expected_login)
        except AuthError as e:
            with self._cv:
                if txn.status == "verifying":
                    if "identity" in e.details:
                        txn.candidate = dict(e.details["identity"])
                    self._finish_locked(txn, "failed", e)
                return txn.public(self.clock())
        with self._cv:
            if txn.status != "verifying":
                return txn.public(self.clock())
            candidate_expires_at = issued_at + expires_in if expires_in else None
            if self._expired(candidate_expires_at):
                self._finish_locked(txn, "expired", AuthError(
                    "device_expired", "The approved sign-in expired during verification. "
                                      "Start again."))
                return txn.public(self.clock())
            txn.status = "awaiting_confirmation"
            txn.deadline = self.clock() + CONFIRM_WINDOW_SECONDS
            txn.candidate = dict(identity, **_catalog_counts(catalog))
            txn.candidate_secret = token
            txn.candidate_api_base = api_base
            txn.candidate_catalog = catalog
            txn.candidate_expires_at = candidate_expires_at
            return txn.public(self.clock())

    def confirm_device(self, owner, txn_id, expected_generation) -> dict:
        with self._cv:
            self._prune_locked()
            txn = self._get_txn_locked(owner, txn_id)
            if txn.status == "committed":
                raise AuthError("conflict", "This sign-in was already applied.")
            if txn.status != "awaiting_confirmation":
                if txn.status == "expired":
                    raise AuthError("device_expired", (txn.error or {}).get(
                        "message", "This sign-in expired.") + " Your previous account is unchanged.")
                raise AuthError("cancelled" if txn.status == "cancelled" else "conflict",
                                "This sign-in can no longer be confirmed.")
            mode = txn.mode
            if self._expired(txn.candidate_expires_at):
                err = AuthError("device_expired", "The approved sign-in expired before it was "
                                                  "confirmed. Your previous account is unchanged.")
                self._finish_locked(txn, "expired", err)
                raise err
            try:
                self._check_generation_locked(mode, txn.base_generation)
            except AuthError as e:
                self._finish_locked(txn, "failed", e)
                raise
            if expected_generation != txn.base_generation:
                raise AuthError("stale_generation", "Refresh this tab before confirming.",
                                generation=txn.base_generation)
            identity = {k: txn.candidate[k] for k in ("host", "login", "user_id")}
            token, api_base, catalog = txn.candidate_secret, txn.candidate_api_base, txn.candidate_catalog
            expires_at = txn.candidate_expires_at
            record = {"kind": "device", **identity, "expires_at": expires_at, "secret": token}
            try:
                public = self._activate_locked(
                    mode, txn.base_generation,
                    lambda gen: self._ready_state(mode, gen, "device", token, identity, api_base,
                                                  catalog, expires_at), record)
            except AuthError as e:
                if e.details.get("phase") != "admission":
                    raise  # e.g. persistence failure: attempt stays confirmable
                err = AuthError("device_expired", "The approved sign-in expired before it was "
                                                  "applied. Your previous account is unchanged.")
                self._finish_locked(txn, "expired", err)
                raise err from None
            # Committed. `public` is freshly sampled and may already be
            # error/expired_credentials; callers must not equate commit with usable.
            self._finish_locked(txn, "committed")
            return {"transaction": txn.public(self.clock()), "state": public}

    def cancel_device(self, owner, txn_id) -> dict:
        with self._cv:
            self._prune_locked()
            txn = self._get_txn_locked(owner, txn_id)
            if txn.status in ACTIVE_TXN:
                self._finish_locked(txn, "cancelled",
                                    AuthError("cancelled", "Sign-in cancelled."))
            return txn.public(self.clock())
