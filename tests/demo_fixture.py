"""Test-only fixtures for demo.py / demo_auth.py.

Nothing here ships with the demo's CLI: the demo has no flag that changes its
provider hosts. Instead a test injects `RoutingTransport`, which accepts only
the allowlisted GitHub/Copilot hosts (after demo_auth validation) and sends
them to a real local HTTP fake provider. All credentials are fake canaries.

Run a browser-driveable instance (tester harness):

    python -B tests/demo_fixture.py --state-dir <dir> [--port 0]

It prints one JSON line with demo_url/provider_url, then serves until killed.
Control the provider with POST <provider_url>/__fixture/config (JSON), e.g.
{"device_script": ["authorization_pending", "slow_down", "token:device"]}.
"""

from __future__ import annotations

import argparse
import copy
import http.client
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import types
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import demo_auth  # noqa: E402

FIXTURE_HOSTS = {"github.com", "api.github.com", "api.githubcopilot.com",
                 "api.individual.githubcopilot.com", "api.business.githubcopilot.com",
                 "api.enterprise.githubcopilot.com"}

TOKENS = {
    "alice": "fake_alice_token_CANARYSECRET_0001",
    "bob": "fake_bob_token_CANARYSECRET_0002",
    "carol": "fake_carol_token_CANARYSECRET_0003",
    "dave": "fake_dave_token_CANARYSECRET_0004",
    "device": "fake_device_token_CANARYSECRET_0005",
    "legacy": "fake_legacy_token_CANARYSECRET_0006",
    "env": "fake_env_token_CANARYSECRET_0007",
    "mallory": "fake_mallory_token_CANARYSECRET_0008",
    "biz": "fake_biz_token_CANARYSECRET_0009",
    "ghe": "fake_ghe_token_CANARYSECRET_0010",
}
DEVICE_CODE = "fake-device-code-CANARYSECRET-9999"
USER_CODE = "WDJB-MJHT"
CANARY = "CANARYSECRET"

PRICES = {"token_prices": {"batch_size": 1000000,
                           "default": {"input_price": 300, "output_price": 1500,
                                       "cache_read_price": 30, "max_prompt_tokens": 200000},
                           "long_context": {"input_price": 600, "output_price": 2250}}}


def catalog(prefix=""):
    def model(mid, vendor, endpoints, tier="medium"):
        return {"id": prefix + mid, "name": prefix + mid, "vendor": vendor,
                "supported_endpoints": endpoints, "policy": {"state": "enabled"},
                "capabilities": {"limits": {"max_context_window_tokens": 200000,
                                            "max_output_tokens": 32000},
                                 "supports": {"streaming": True, "tool_calls": True,
                                              "vision": False}},
                "model_picker_price_category": tier, "billing": copy.deepcopy(PRICES)}
    return [
        model("claude-sonnet-4.6", "Anthropic", ["/v1/messages", "/chat/completions"]),
        model("gpt-4.1", "OpenAI", ["/chat/completions"], "low"),
        model("gpt-5.4", "OpenAI", ["/v1/responses"], "high"),
        {"id": prefix + "listed-only", "name": prefix + "listed-only", "vendor": "Other",
         "supported_endpoints": [], "model_picker_enabled": True,
         "model_picker_price_category": "very_high", "billing": copy.deepcopy(PRICES)},
    ]


def default_config():
    return {
        "users": {  # token -> identity
            TOKENS["alice"]: {"login": "alice", "id": 1001},
            TOKENS["bob"]: {"login": "bob", "id": 1002},
            TOKENS["carol"]: {"login": "carol_contoso", "id": 1003},
            TOKENS["dave"]: {"login": "dave", "id": 1004},
            TOKENS["device"]: {"login": "devuser", "id": 1005},
            TOKENS["legacy"]: {"login": "legacyuser", "id": 1006},
            TOKENS["env"]: {"login": "envuser", "id": 1007},
            TOKENS["mallory"]: {"login": "mallory", "id": 1666},
            TOKENS["biz"]: {"login": "bizuser", "id": 1009},
            # Recognized only to prove it is never sent: a GHE.com host token.
            TOKENS["ghe"]: {"login": "eve", "id": 1010},
        },
        # login -> endpoints.api value, or an int HTTP status for the discovery call
        "discovery": {"alice": "https://api.enterprise.githubcopilot.com",
                      "bob": "https://api.individual.githubcopilot.com",
                      "bizuser": "https://api.business.githubcopilot.com",
                      "carol_contoso": 403},
        "default_api": "https://api.githubcopilot.com",
        # login -> int status for /models, or "empty"
        "models": {"dave": "empty"},
        "models_delay": {},        # login -> seconds before answering /models
        "version_reject": False,   # 400 when X-GitHub-Api-Version is present
        "user_status": {},         # login -> forced /user status
        "device_start": {"device_code": DEVICE_CODE, "user_code": USER_CODE,
                         "verification_uri": "https://github.com/login/device",
                         "expires_in": 900, "interval": 5},
        "device_start_status": 200,
        "device_script": ["authorization_pending"],  # consumed per poll; last repeats
        "device_token_expires_in": None,
        "chat_status": 200,
        "chat_error_body": {"error": {"message": "model overloaded"}},
        "chat_hold": False,        # block streaming chats until released
    }


class FakeProvider:
    """Real local HTTP server impersonating GitHub + Copilot endpoints."""

    def __init__(self):
        self.lock = threading.Lock()
        self.config = default_config()
        self.calls = []
        self.release = threading.Event()
        self.release.set()
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_GET(self):
                provider._dispatch(self, "GET")

            def do_POST(self):
                provider._dispatch(self, "POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()

    def configure(self, **updates):
        with self.lock:
            self.config.update(updates)
            if "chat_hold" in updates:
                (self.release.clear if updates["chat_hold"] else self.release.set)()

    def calls_for(self, host=None, path=None):
        with self.lock:
            return [c for c in self.calls if (host is None or c["host"] == host)
                    and (path is None or c["path"] == path)]

    # ── request handling ──

    def _identity(self, handler):
        auth = handler.headers.get("Authorization") or ""
        token = auth.split(" ", 1)[1] if " " in auth else ""
        return self.config["users"].get(token)

    def _send(self, handler, status, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            handler.send_header(k, v)
        handler.end_headers()
        handler.wfile.write(data)

    def _dispatch(self, handler, method):
        host = handler.headers.get("X-Fixture-Host", "")
        path = urllib.parse.urlsplit(handler.path).path
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        if path.startswith("/__fixture/"):
            return self._control(handler, method, path, body)
        with self.lock:
            ident = self._identity(handler)
            self.calls.append({
                "host": host, "method": method, "path": path,
                "login": ident["login"] if ident else None,
                "integration": handler.headers.get("Copilot-Integration-Id"),
                "versioned": "X-GitHub-Api-Version" in handler.headers,
                "time": time.monotonic(),
                "body": json.loads(body) if body and path.startswith(("/v1", "/chat")) else None,
            })
            cfg = copy.deepcopy({k: v for k, v in self.config.items() if k != "users"})
        if host == "github.com":
            return self._github_oauth(handler, path, body, cfg)
        if host == "api.github.com":
            return self._github_api(handler, path, ident, cfg)
        if host.startswith("api.") and host.endswith("githubcopilot.com"):
            return self._copilot(handler, path, ident, cfg, body)
        self._send(handler, 404, {"message": "unknown fixture host"})

    def _control(self, handler, method, path, body):
        if path == "/__fixture/config" and method == "POST":
            self.configure(**json.loads(body or b"{}"))
            return self._send(handler, 200, {"ok": True})
        if path == "/__fixture/calls":
            with self.lock:
                calls = [{k: v for k, v in c.items() if k != "body"} for c in self.calls]
            return self._send(handler, 200, {"calls": calls})
        if path == "/__fixture/release" and method == "POST":
            self.release.set()
            return self._send(handler, 200, {"ok": True})
        self._send(handler, 404, {})

    def _github_oauth(self, handler, path, body, cfg):
        form = urllib.parse.parse_qs(body.decode())
        if path == "/login/device/code":
            return self._send(handler, cfg["device_start_status"], cfg["device_start"])
        if path == "/login/oauth/access_token":
            if form.get("device_code", [""])[0] != cfg["device_start"]["device_code"]:
                return self._send(handler, 200, {"error": "incorrect_device_code"})
            with self.lock:
                script = self.config["device_script"]
                step = script[0]
                if len(script) > 1:
                    self.config["device_script"] = script[1:]
            if step.startswith("token:"):
                out = {"access_token": TOKENS[step[6:]], "token_type": "bearer",
                       "scope": "read:user", "refresh_token": "fake_refresh_CANARYSECRET"}
                if cfg["device_token_expires_in"]:
                    out["expires_in"] = cfg["device_token_expires_in"]
                return self._send(handler, 200, out)
            if step.startswith("status:"):
                return self._send(handler, int(step[7:]), {"message": "boom CANARYSECRET"})
            if step.startswith("slow_down:"):
                return self._send(handler, 200, {"error": "slow_down", "interval": int(step[10:])})
            return self._send(handler, 200, {"error": step})
        self._send(handler, 404, {})

    def _github_api(self, handler, path, ident, cfg):
        if ident is None:
            return self._send(handler, 401, {"message": "Bad credentials"})
        if path == "/user":
            status = cfg["user_status"].get(ident["login"], 200)
            return self._send(handler, status, {"login": ident["login"], "id": ident["id"]}
                              if status == 200 else {"message": "nope"})
        if path == "/copilot_internal/user":
            value = cfg["discovery"].get(ident["login"], cfg["default_api"])
            if isinstance(value, int):
                return self._send(handler, value, {"message": "no copilot"})
            return self._send(handler, 200, {"endpoints": {"api": value}})
        self._send(handler, 404, {})

    def _copilot(self, handler, path, ident, cfg, body):
        if ident is None:
            return self._send(handler, 401, {"error": {"message": "unauthorized"}})
        if path == "/models":
            delay = cfg["models_delay"].get(ident["login"])
            if delay:
                time.sleep(delay)
            if cfg["version_reject"] and "X-GitHub-Api-Version" in handler.headers:
                return self._send(handler, 400, {"error": "bad version"})
            value = cfg["models"].get(ident["login"])
            if isinstance(value, int):
                return self._send(handler, value, {"error": {"message": "denied"}})
            if value == "empty":
                return self._send(handler, 200, {"data": [catalog()[3]]})
            return self._send(handler, 200, {"data": catalog()})
        if cfg["chat_status"] != 200:
            return self._send(handler, cfg["chat_status"], cfg["chat_error_body"])
        req = json.loads(body or b"{}")
        text = f"hello {ident['login']} via {handler.headers.get('Copilot-Integration-Id')}"
        if path == "/v1/responses":
            return self._send(handler, 200, {"model": req.get("model"), "output_text": text})
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        if path == "/v1/messages":
            events = [{"type": "message_start", "message": {"model": req.get("model")}},
                      {"type": "content_block_delta", "delta": {"text": text[:6]}},
                      {"type": "content_block_delta", "delta": {"text": text[6:]}},
                      {"type": "message_stop"}]
        else:
            events = [{"model": req.get("model"), "choices": [{"delta": {"content": text[:6]}}]},
                      {"model": req.get("model"), "choices": [{"delta": {"content": text[6:]}}]}]
        for i, ev in enumerate(events):
            handler.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
            handler.wfile.flush()
            if i == 1:
                self.release.wait(30)
        handler.wfile.write(b"data: [DONE]\n\n")


def connect_with_retry(conn, attempts=5):
    """Connect-phase retry for shared-host socket pressure (e.g. WinError 10055).
    Only the TCP connect is retried, before any request bytes are sent, so a
    request can never reach the fixture twice."""
    for attempt in range(attempts):
        try:
            conn.connect()
            return attempt
        except OSError:
            conn.close()
            if attempt == attempts - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


class _ClosingBody:
    def __init__(self, resp, conn):
        self._resp, self._conn = resp, conn

    def read(self, n=-1):
        return self._resp.read(n)

    def __iter__(self):
        return iter(self._resp)

    def close(self):
        self._resp.close()
        self._conn.close()


class RoutingTransport:
    """Sends already-validated GitHub/Copilot URLs to the local fake provider.
    Never follows redirects (http.client does not)."""

    def __init__(self, provider_port: int):
        self.port = provider_port
        self.seen = []
        self.connect_retries = 0

    def request(self, method, url, headers, body=None, timeout=15):
        parts = urllib.parse.urlsplit(url)
        self.seen.append((method, url))
        if parts.scheme != "https" or parts.hostname not in FIXTURE_HOSTS:
            raise demo_auth.TransportError("UnknownFixtureHost")
        hdrs = dict(headers)
        hdrs["X-Fixture-Host"] = parts.hostname
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            self.connect_retries += connect_with_retry(conn)
            conn.request(method, parts.path, body=body, headers=hdrs)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            raise demo_auth.TransportError(type(e).__name__) from None
        return demo_auth.Response(resp.status, resp.headers, _ClosingBody(resp, conn))


class FakeGh:
    """Fake `gh` runner. Records calls; never changes the active account."""

    def __init__(self, accounts=None, active="alice"):
        self.accounts = accounts if accounts is not None else [
            {"host": "github.com", "login": "alice", "token": TOKENS["alice"], "state": "success"},
            {"host": "github.com", "login": "bob", "token": TOKENS["bob"], "state": "success"},
            {"host": "github.com", "login": "carol_contoso", "token": TOKENS["carol"], "state": "success"},
            {"host": "github.com", "login": "dave", "token": TOKENS["dave"], "state": "success"},
            {"host": "github.com", "login": "bizuser", "token": TOKENS["biz"], "state": "success"},
            {"host": "github.com", "login": "stale", "token": "", "state": "error"},
            {"host": "contoso.ghe.com", "login": "eve", "token": TOKENS["ghe"], "state": "success"},
        ]
        self.active = {"github.com": active, "contoso.ghe.com": "eve"}
        self.mode = "ok"  # ok | missing | timeout | badjson | fail
        self.token_override = {}  # login -> token actually returned (identity swap tests)
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, args, env, timeout):
        with self.lock:
            self.calls.append({"args": list(args),
                               "token_env": sorted(k for k in demo_auth.GH_TOKEN_ENV_KEYS if k in env)})
            mode = self.mode
        if mode == "missing":
            raise FileNotFoundError(args[0])
        if mode == "timeout":
            raise subprocess.TimeoutExpired(args, timeout)
        rest = args[1:]
        if rest[:4] == ["auth", "status", "--json", "hosts"]:
            if mode == "badjson":
                return types.SimpleNamespace(returncode=0, stdout="{not json")
            hosts = {}
            for a in self.accounts:
                hosts.setdefault(a["host"], []).append({
                    "login": a["login"], "state": a["state"], "host": a["host"],
                    "active": self.active.get(a["host"]) == a["login"],
                    "tokenSource": "keyring", "scopes": "secret-ish-field"})
            return types.SimpleNamespace(returncode=1 if mode == "fail" else 0,
                                         stdout=json.dumps({"hosts": hosts}))
        if rest[:2] == ["auth", "token"]:
            if mode == "fail":
                return types.SimpleNamespace(returncode=1, stdout="")
            # Like gh's DefaultHost: --hostname, else GH_HOST, else github.com when it
            # is logged in, else the first authenticated host; --user, else active.
            opts = dict(zip(rest[2::2], rest[3::2]))
            host = opts.get("--hostname") or env.get("GH_HOST")
            if not host:
                logged_in = [h for h, a in self.active.items() if a]
                host = "github.com" if "github.com" in logged_in or not logged_in else logged_in[0]
            login = opts.get("--user") or self.active.get(host)
            for a in self.accounts:
                if a["login"] == login and a["host"] == host and a["token"]:
                    token = self.token_override.get(login, a["token"])
                    return types.SimpleNamespace(returncode=0, stdout=token + "\n")
            return types.SimpleNamespace(returncode=1, stdout="")
        return types.SimpleNamespace(returncode=2, stdout="")


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def advance(self, seconds):
        with self.lock:
            self.now += seconds


class MemoryProtector:
    """Deterministic protector for store tests on any OS (not encryption)."""
    scheme = "memory-test"

    def __init__(self, fail=False):
        self.fail = fail

    def protect(self, data):
        if self.fail:
            raise demo_auth.StoreError("Windows DPAPI failed (error 5).")
        return "mem:" + data.hex()

    def unprotect(self, value):
        if self.fail or not value.startswith("mem:"):
            raise demo_auth.StoreError("Saved demo credential could not be decrypted.")
        return bytes.fromhex(value[4:])


def build_manager(provider: FakeProvider, state_dir, *, gh=None, environ=None, clock=None,
                  wall=None, protector=None, legacy=None):
    state_dir = pathlib.Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    legacy_path = state_dir / "legacy-gateway-token.json"
    if legacy is not None:
        legacy_path.write_text(json.dumps(legacy))
    gh = gh or FakeGh()
    kwargs = {}
    if clock:
        kwargs["clock"] = clock
    if wall:
        kwargs["wall"] = wall
    store = demo_auth.AuthStore(state_dir / ".demo-auth.json", protector)
    manager = demo_auth.AuthManager(
        transport=RoutingTransport(provider.port),
        gh=demo_auth.GhCli(runner=gh, environ=dict(environ or {})),
        store=store, legacy_token_file=legacy_path,
        environ=environ if environ is not None else {}, **kwargs)
    return manager, gh


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--legacy", action="store_true",
                        help="Seed a legacy VS Code token file for the fake 'legacyuser'")
    args = parser.parse_args()

    import demo
    state = pathlib.Path(args.state_dir).resolve()
    demo.configure_logging(state / "logs")
    provider = FakeProvider().start()
    legacy = {"mode": "vscode", "token": TOKENS["legacy"]} if args.legacy else None
    manager, _gh = build_manager(provider, state, legacy=legacy)
    server = demo.create_server("127.0.0.1", args.port, manager,
                                gateway_url="http://127.0.0.1:9")  # unreachable on purpose
    print(json.dumps({"demo_url": f"http://127.0.0.1:{server.server_address[1]}/",
                      "provider_url": provider.url, "pid": os.getpid(),
                      "state_dir": str(state)}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        provider.stop()


if __name__ == "__main__":
    main()
