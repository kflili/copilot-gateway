"""HTTP-level regressions: the real demo handler/server with fake credentials.

Run: python -B -m unittest discover -s tests -p "test_demo_*.py"
"""

from __future__ import annotations

import email.message
import http.client
import io
import json
import logging
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import demo_fixture as fx  # noqa: E402
import demo  # noqa: E402
import demo_auth  # noqa: E402


def tmpdir(case):
    path = tempfile.mkdtemp(prefix="demo-http-test-", dir=os.environ.get("DEMO_TEST_TMP"))
    case.addCleanup(shutil.rmtree, path, True)
    return pathlib.Path(path)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, port):
        self.port = port
        self.cookie = None
        self.csrf = None
        self.transcript = []

    def request(self, method, path, body=None, headers=None, *, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if self.cookie:
            h["Cookie"] = self.cookie
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        fx.connect_with_retry(conn)  # connect-phase only; see demo_fixture
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        self.transcript.append(data)
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data

    def get(self, path, **kw):
        status, headers, data = self.request("GET", path, **kw)
        return status, headers, json.loads(data) if data else None

    def session(self):
        status, headers, data = self.get("/api/session")
        assert status == 200, data
        if "set-cookie" in headers:
            self.cookie = headers["set-cookie"].split(";")[0]
        self.csrf = data["csrf"]
        return headers

    def post(self, path, obj, *, origin="default", csrf="default", ctype="application/json",
             headers=None, host=None):
        if self.csrf is None and csrf == "default":
            self.session()
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        h = {"Content-Type": ctype,
             "Origin": f"http://127.0.0.1:{self.port}" if origin == "default" else origin,
             "X-Demo-CSRF": self.csrf if csrf == "default" else csrf}
        h.update(headers or {})
        status, rh, data = self.request("POST", path, body, h, host=host)
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = data
        return status, rh, parsed


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.provider = fx.FakeProvider().start()
        self.addCleanup(self.provider.stop)
        self.state = tmpdir(self)
        self.clock = fx.FakeClock()
        self.start_server()

    def start_server(self, host="127.0.0.1", **kw):
        kw.setdefault("clock", self.clock)
        self.manager, self.gh = fx.build_manager(self.provider, self.state, **kw)
        self.server = demo.create_server(host, 0, self.manager, gateway_url="http://127.0.0.1:9")
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.stop_server, self.server)
        self.client = Client(self.port)

    @staticmethod
    def stop_server(server):
        server.shutdown()
        server.server_close()

    def models(self, mode, client=None):
        status, _, data = (client or self.client).get(f"/api/models?mode={mode}")
        self.assertEqual(status, 200, data)
        return data

    def select(self, mode, login, client=None):
        client = client or self.client
        gen = self.models(mode, client)["generation"]
        status, _, data = client.post("/api/auth/select-gh",
                                      {"mode": mode, "host": "github.com", "login": login,
                                       "generation": gen})
        self.assertEqual(status, 200, data)
        return data

    def chat(self, mode, model, generation, text="hi", client=None):
        return (client or self.client).post("/api/chat", {
            "mode": mode, "model": model, "generation": generation,
            "messages": [{"role": "user", "content": text}]})

    def chat_calls(self):
        return [c for c in self.provider.calls if c["path"] in
                ("/v1/messages", "/chat/completions", "/v1/responses")]


class ModelsApiTests(ServerCase):
    def test_signed_out_state_is_explicit(self):  # A01
        self.gh.mode = "missing"
        for mode in ("vscode", "cli"):
            data = self.models(mode)
            self.assertEqual(data["auth"]["status"], "signed_out")
            self.assertEqual(data["catalog_status"], "unavailable")
            self.assertEqual((data["models"], data["listed_only"]), ([], []))
            self.assertTrue(data["auth"]["error"]["message"])
            self.assertNotIn("token_prefix", data)
            self.assertNotIn("token_type", data)

    def test_ready_catalog_keeps_existing_fields(self):  # A02, A22 metadata
        data = self.models("cli")
        self.assertEqual(data["auth"]["account"], {"host": "github.com", "login": "alice",
                                                   "user_id": 1001})
        self.assertEqual(data["credential_source"], "gh active account")
        self.assertEqual(data["integration_id"], "copilot-developer-cli")
        self.assertEqual(data["api_base"], "https://api.enterprise.githubcopilot.com")
        self.assertEqual([m["id"] for m in data["models"]], ["claude-sonnet-4.6", "gpt-4.1", "gpt-5.4"])
        self.assertEqual(data["models"][0]["billing"], fx.PRICES)
        self.assertEqual(data["models"][0]["model_picker_price_category"], "medium")
        self.assertEqual(data["listed_only"][0]["billing"], fx.PRICES)
        self.assertEqual(data["catalog_status"], "ready")

    def test_empty_catalog_distinguished(self):  # A14
        self.select("cli", "dave")
        data = self.models("cli")
        self.assertEqual((data["auth"]["status"], data["catalog_status"]), ("ready", "empty"))
        self.assertEqual(len(data["listed_only"]), 1)
        self.assertEqual(data["models"], [])

    def test_errors_have_non_200_statuses(self):  # A04, A05, A13, A15
        gen = self.models("cli")["generation"]
        cases = [({"login": "carol_contoso"}, 403, "access_denied"),
                 ({"host": "contoso.ghe.com", "login": "eve"}, 422, "unsupported_host"),
                 ({"login": "stale"}, 404, "gh_account_unavailable"),
                 ({"login": "bad login!"}, 400, "invalid_request"),
                 ({"generation": "old"}, 409, "stale_generation")]
        for patch, status, category in cases:
            body = {"mode": "cli", "host": "github.com", "login": "bob", "generation": gen}
            body.update(patch)
            code, _, data = self.client.post("/api/auth/select-gh", body)
            with self.subTest(patch=patch):
                self.assertEqual((code, data["error"]["category"]), (status, category))
        self.assertEqual(self.models("cli")["generation"], gen)
        _, _, err = self.client.post("/api/auth/select-gh", {"mode": "cli", "host": "github.com",
                                                             "login": "carol_contoso", "generation": gen})
        self.assertEqual(err["error"]["identity"]["login"], "carol_contoso")

    def test_request_validation(self):
        code, _, data = self.client.get("/api/models?mode=bogus")
        self.assertEqual((code, data["error"]["category"]), (400, "invalid_request"))
        code, _, data = self.client.post("/api/auth/sign-out", b"{not json")
        self.assertEqual(code, 400)
        code, _, data = self.client.post("/api/auth/sign-out", b"x" * (demo.AUTH_BODY_LIMIT + 1))
        self.assertEqual(code, 413)


class BoundaryTests(ServerCase):  # A24, A25, A26
    def assert_no_cors(self, headers):
        self.assertNotIn("access-control-allow-origin", headers)
        self.assertNotIn("access-control-allow-credentials", headers)

    def test_mutation_origin_host_and_type(self):
        gen = self.models("vscode")["generation"]
        body = {"mode": "vscode", "generation": gen}
        port = self.port
        cases = [
            ({"origin": None}, 403, "forbidden"),
            ({"origin": "null"}, 403, "forbidden"),
            ({"origin": "http://evil.example"}, 403, "forbidden"),
            ({"origin": f"http://127.0.0.1:{port + 1}"}, 403, "forbidden"),
            ({"origin": f"https://127.0.0.1:{port}"}, 403, "forbidden"),
            ({"host": f"evil.example:{port}", "origin": f"http://evil.example:{port}"}, 403, "loopback_only"),
            ({"host": f"127.0.0.1:{port + 1}"}, 403, "loopback_only"),
            ({"ctype": "text/plain"}, 415, "unsupported_media_type"),
            ({"ctype": "application/x-www-form-urlencoded"}, 415, "unsupported_media_type"),
            ({"origin": "http://evil.example",
              "headers": {"X-Forwarded-Host": f"127.0.0.1:{port}", "X-Forwarded-For": "127.0.0.1",
                          "Forwarded": f"host=127.0.0.1:{port}"}}, 403, "forbidden"),
            ({"csrf": None}, 403, "csrf_failed"),
            ({"csrf": "wrong"}, 403, "csrf_failed"),
        ]
        self.client.session()
        for kw, status, category in cases:
            code, headers, data = self.client.post("/api/auth/sign-out", body, **kw)
            with self.subTest(kw=kw):
                self.assertEqual((code, data["error"]["category"]), (status, category))
                self.assert_no_cors(headers)
        for path in ("/api/chat", "/api/auth/device/start", "/api/auth/select-gh"):
            code, _, data = self.client.post(path, {"mode": "cli"}, origin="http://evil.example")
            self.assertEqual(code, 403)
        self.assertEqual(self.models("vscode")["generation"], gen)  # nothing mutated
        self.assertEqual(self.chat_calls(), [])

    def test_protected_reads(self):
        for path in ("/api/models?mode=cli", "/api/auth/status?mode=cli", "/api/auth/accounts",
                     "/api/session", "/api/events", "/api/gateway/logs"):
            code, headers, _ = self.client.request("GET", path, host=f"attacker.example:{self.port}")
            self.assertEqual(code, 403, path)
            self.assert_no_cors(headers)
            for site in ("cross-site", "same-site"):
                code, _, _ = self.client.request("GET", path, headers={"Sec-Fetch-Site": site})
                self.assertEqual(code, 403, (path, site))
        code, headers, _ = self.client.request("GET", "/api/auth/status?mode=cli",
                                               headers={"Sec-Fetch-Site": "same-origin",
                                                        "Origin": "http://evil.example"})
        self.assertEqual(code, 200)
        self.assert_no_cors(headers)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["x-frame-options"], "DENY")
        code, headers, _ = self.client.request("OPTIONS", "/api/chat",
                                               headers={"Origin": "http://evil.example",
                                                        "Access-Control-Request-Method": "POST"})
        self.assertEqual(code, 405)
        self.assert_no_cors(headers)
        code, headers, _ = self.client.request("GET", "/")
        self.assertEqual((code, headers["x-frame-options"]), (200, "DENY"))

    def test_session_cookie_flags(self):
        headers = self.client.session()
        cookie = headers["set-cookie"]
        self.assertTrue(cookie.startswith(f"demo_session_{self.port}="))
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        again = self.client.session()
        self.assertNotIn("set-cookie", again)

    def test_device_transaction_owned_by_session(self):
        a, b = self.client, Client(self.port)
        code, _, txn = a.post("/api/auth/device/start", {"mode": "cli"})
        self.assertEqual(code, 200, txn)
        tid = txn["transaction_id"]
        b.session()
        for path, extra in (("/api/auth/device/poll", {}), ("/api/auth/device/cancel", {}),
                            ("/api/auth/device/confirm", {"generation": "x"})):
            code, _, data = b.post(path, {"transaction_id": tid, **extra})
            self.assertEqual((code, data["error"]["category"]), (404, "not_found"), path)
            self.assertNotIn(fx.USER_CODE, json.dumps(data))
        # B presenting A's cookie without A's CSRF token is still refused.
        forged = Client(self.port)
        forged.cookie, forged.csrf = a.cookie, b.csrf
        code, _, data = forged.post("/api/auth/device/poll", {"transaction_id": tid})
        self.assertEqual((code, data["error"]["category"]), (403, "csrf_failed"))
        code, _, data = b.post("/api/auth/device/start", {"mode": "cli"})
        self.assertEqual((code, data["error"]["category"]), (409, "conflict"))
        code, _, data = a.post("/api/auth/device/poll", {"transaction_id": tid})
        self.assertEqual((code, data["status"]), (200, "pending"))

    def test_remote_peer_is_refused(self):
        for address, allowed in (("192.168.1.20", False), ("10.0.0.5", False),
                                 ("::ffff:192.168.1.2", False), ("127.0.0.1", True),
                                 ("::1", True), ("::ffff:127.0.0.1", True)):
            handler = demo.DemoHandler.__new__(demo.DemoHandler)
            handler.server = self.server
            handler.client_address = (address, 5555)
            handler.headers = email.message.Message()
            handler.headers["Host"] = f"127.0.0.1:{self.port}"
            handler.wfile = io.BytesIO()
            handler.request_version = "HTTP/1.0"
            handler.requestline = "GET /api/models HTTP/1.0"
            handler.command = "GET"
            handler._responded = False
            with self.subTest(address=address):
                self.assertEqual(handler._guard(mutation=False), allowed)
                if not allowed:
                    self.assertIn(b"loopback_only", handler.wfile.getvalue())

    def test_lan_bind_keeps_apis_loopback_only(self):
        lan = demo.create_server("0.0.0.0", 0, self.manager, gateway_url="http://127.0.0.1:9")
        threading.Thread(target=lan.serve_forever, daemon=True).start()
        self.addCleanup(self.stop_server, lan)
        client = Client(lan.server_address[1])
        self.assertEqual(client.get("/api/auth/status?mode=cli")[0], 200)
        code, _, data = client.get("/api/auth/status?mode=cli",
                                   host=f"192.168.1.50:{lan.server_address[1]}")
        self.assertEqual((code, data["error"]["category"]), (403, "loopback_only"))
        self.assertIn("127.0.0.1", data["error"]["message"])
        code, _, _ = client.request("GET", "/", host=f"192.168.1.50:{lan.server_address[1]}")
        self.assertEqual(code, 200)  # UI loads and explains the boundary


class ChatTests(ServerCase):  # A20, A21, A22
    def test_all_formats_in_both_modes(self):
        self.select("vscode", "alice")
        for mode, integration in (("vscode", "vscode-chat"), ("cli", "copilot-developer-cli")):
            gen = self.models(mode)["generation"]
            for model, path, streaming in (("claude-sonnet-4.6", "/v1/messages", True),
                                           ("gpt-4.1", "/chat/completions", True),
                                           ("gpt-5.4", "/v1/responses", False)):
                code, headers, data = self.chat(mode, model, gen)
                with self.subTest(mode=mode, model=model):
                    self.assertEqual(code, 200)
                    call = self.chat_calls()[-1]
                    self.assertEqual((call["path"], call["integration"], call["login"]),
                                     (path, integration, "alice"))
                    self.assertEqual(call["body"]["model"], model)
                    expected = f"hello alice via {integration}"
                    if streaming:
                        self.assertIn("text/event-stream", headers["content-type"])
                        text = "".join(
                            (json.loads(line[6:]).get("delta", {}).get("text", "")
                             or (json.loads(line[6:]).get("choices") or [{}])[0].get("delta", {}).get("content", ""))
                            for line in data.decode().splitlines()
                            if line.startswith("data: ") and line != "data: [DONE]")
                        self.assertEqual(text, expected)
                    else:
                        self.assertEqual(data["output_text"], expected)

    def test_upstream_errors_visible_and_sanitized(self):
        gen = self.models("cli")["generation"]
        self.provider.configure(chat_status=500, chat_error_body={
            "error": {"message": f"overloaded for {fx.TOKENS['alice']} Bearer abcdefghijklmnop"}})
        code, _, data = self.chat("cli", "claude-sonnet-4.6", gen)
        self.assertEqual((code, data["error"]["category"], data["error"]["upstream_status"]),
                         (502, "upstream_error", 500))
        self.assertIn("overloaded", data["error"]["message"])
        self.assertNotIn(fx.CANARY, json.dumps(data))
        self.provider.configure(chat_status=400, chat_error_body={"error": {"message": "bad model"}})
        code, _, data = self.chat("cli", "gpt-5.4", gen)
        self.assertEqual((code, data["error"]["message"]), (400, "bad model"))
        self.provider.configure(chat_status=502, chat_error_body={"raw": "<html>"})
        code, _, data = self.chat("cli", "gpt-4.1", gen)
        self.assertEqual(data["error"]["message"], "Copilot API returned HTTP 502.")

    def test_stale_or_missing_generation_rejected_before_upstream(self):
        old = self.models("cli")["generation"]
        self.select("cli", "bob")
        for gen in (old, None, "", 5):
            code, _, data = self.chat("cli", "claude-sonnet-4.6", gen)
            self.assertEqual((code, data["error"]["category"]), (409, "stale_generation"))
        code, _, data = self.chat("cli", "not-in-catalog", self.models("cli")["generation"])
        self.assertEqual(code, 400)
        self.gh.mode = "missing"
        vs = self.models("vscode")
        code, _, data = self.chat("vscode", "claude-sonnet-4.6", vs["generation"])
        self.assertEqual((code, data["error"]["category"]), (401, "unauthenticated"))
        self.assertEqual(self.chat_calls(), [])

    def test_inflight_stream_keeps_old_snapshot(self):
        gen_a = self.models("cli")["generation"]
        self.provider.configure(chat_hold=True)
        result = {}

        def stream():
            result["resp"] = self.chat("cli", "claude-sonnet-4.6", gen_a, client=Client(self.port))

        t = threading.Thread(target=stream)
        t.start()
        for _ in range(300):
            if self.chat_calls():
                break
            time.sleep(0.01)
        state = self.select("cli", "bob")
        self.provider.configure(chat_hold=False)
        t.join(10)
        self.assertEqual(result["resp"][0], 200)
        self.assertIn("alice", result["resp"][2].decode())
        self.assertEqual(self.chat_calls()[0]["login"], "alice")
        code, _, _ = self.chat("cli", "claude-sonnet-4.6", gen_a)
        self.assertEqual(code, 409)  # never replayed under bob implicitly
        code, _, data = self.chat("cli", "claude-sonnet-4.6", state["generation"])
        self.assertEqual(code, 200)
        self.assertEqual([c["login"] for c in self.chat_calls()], ["alice", "bob"])


class RoundTwoHttpTests(ServerCase):
    """Round-2 findings 1 and 2 through the real handler."""

    def test_individual_origin_catalog_and_chat(self):
        self.select("cli", "bob")
        data = self.models("cli")
        self.assertEqual((data["api_base"], data["catalog_status"]),
                         ("https://api.individual.githubcopilot.com", "ready"))
        code, _, _ = self.chat("cli", "gpt-5.4", data["generation"])
        self.assertEqual(code, 200)
        call = self.chat_calls()[-1]
        self.assertEqual((call["host"], call["login"]), ("api.individual.githubcopilot.com", "bob"))

    def test_enterprise_gh_host_shown_as_unsupported(self):
        self.start_server(environ={"GH_HOST": "contoso.ghe.com", "GH_TOKEN": fx.TOKENS["ghe"]})
        data = self.models("cli")
        self.assertEqual((data["auth"]["status"], data["auth"]["error"]["category"]),
                         ("error", "unsupported_host"))
        self.assertEqual(data["models"], [])
        self.assertEqual([c for c in self.provider.calls if c["login"] == "eve"], [])
        self.assertNotIn(fx.CANARY, json.dumps(data))


class RoundThreeHttpTests(ServerCase):
    """Round-3 finding 1 through the real handler: expiry never replaces a usable account."""

    def setUp(self):
        super().setUp()
        self.wall = fx.FakeClock(1_700_000_000.0)
        self.start_server(wall=self.wall)

    def approve(self, expires_in):
        self.provider.configure(device_script=["token:device"], device_token_expires_in=expires_in)
        code, _, txn = self.client.post("/api/auth/device/start", {"mode": "cli"})
        self.assertEqual(code, 200, txn)
        self.clock.advance(5)
        code, _, out = self.client.post("/api/auth/device/poll", {"transaction_id": txn["transaction_id"]})
        self.assertEqual(out["status"], "awaiting_confirmation")
        return txn["transaction_id"]

    def test_expired_candidate_confirm_preserves_account(self):
        before = self.models("cli")
        tid = self.approve(30)
        self.clock.advance(30)
        self.wall.advance(30)
        code, _, data = self.client.post("/api/auth/device/confirm",
                                         {"transaction_id": tid, "generation": before["generation"]})
        self.assertEqual((code, data["error"]["category"]), (410, "device_expired"))
        after = self.models("cli")
        self.assertEqual((after["generation"], after["auth"]["status"], after["auth"]["account"]["login"]),
                         (before["generation"], "ready", "alice"))
        self.assertFalse((self.state / ".demo-auth.json").exists())
        code, _, _ = self.chat("cli", "gpt-5.4", before["generation"])
        self.assertEqual((code, self.chat_calls()[-1]["login"]), (200, "alice"))

    def test_expired_active_account_is_not_reported_ready(self):
        before = self.models("cli")
        tid = self.approve(60)
        code, _, done = self.client.post("/api/auth/device/confirm",
                                         {"transaction_id": tid, "generation": before["generation"]})
        self.assertEqual(code, 200, done)
        self.wall.advance(60)
        data = self.models("cli")
        self.assertEqual((data["auth"]["status"], data["auth"]["error"]["category"], data["catalog_status"]),
                         ("error", "expired_credentials", "unavailable"))
        self.assertEqual(data["models"], [])
        calls = len(self.chat_calls())
        code, _, _ = self.chat("cli", "gpt-5.4", done["state"]["generation"])
        self.assertEqual(code, 409)
        self.assertEqual(len(self.chat_calls()), calls)


class ExpiryAdmissionHttpTests(ServerCase):
    """EXPIRY-CHECKPOINT.md HTTP outcomes: pre-commit rejected vs committed-but-expired."""

    ISSUED = 1_700_000_000.0

    def setUp(self):
        super().setUp()
        self.wall = fx.FakeClock(self.ISSUED)
        self.start_server(wall=self.wall, protector=fx.MemoryProtector())
        self.prior = self.select("cli", "bob")
        self.store = self.state / ".demo-auth.json"
        self.prior_bytes = self.store.read_bytes()
        self.provider.configure(device_script=["token:device"], device_token_expires_in=30)
        code, _, txn = self.client.post("/api/auth/device/start", {"mode": "cli"})
        self.clock.advance(5)
        code, _, out = self.client.post("/api/auth/device/poll", {"transaction_id": txn["transaction_id"]})
        self.assertEqual(out["status"], "awaiting_confirmation")
        self.tid = txn["transaction_id"]
        self.wall.now = self.ISSUED + 29

    def hook_os(self, name, before):
        real = getattr(demo_auth.os, name)

        def hooked(*a, **kw):
            before()
            return real(*a, **kw)

        setattr(demo_auth.os, name, hooked)
        self.addCleanup(setattr, demo_auth.os, name, real)

    def expire(self):
        self.wall.now = self.ISSUED + 31

    def confirm(self):
        return self.client.post("/api/auth/device/confirm",
                                {"transaction_id": self.tid, "generation": self.prior["generation"]})

    def test_precommit_expiry_returns_410_and_old_account_stays_usable(self):
        self.hook_os("fsync", self.expire)
        code, _, data = self.confirm()
        self.assertEqual((code, data["error"]["category"]), (410, "device_expired"))
        after = self.models("cli")
        self.assertEqual((after["generation"], after["auth"]["status"], after["auth"]["account"]["login"]),
                         (self.prior["generation"], "ready", "bob"))
        self.assertEqual(self.store.read_bytes(), self.prior_bytes)
        code, _, _ = self.chat("cli", "gpt-5.4", after["generation"])
        self.assertEqual((code, self.chat_calls()[-1]["login"]), (200, "bob"))

    def test_commit_then_expiry_reports_committed_but_expired(self):
        self.hook_os("replace", self.expire)
        code, _, data = self.confirm()
        self.assertEqual(code, 200, data)
        self.assertEqual(data["transaction"]["status"], "committed")
        self.assertEqual((data["state"]["status"], data["state"]["error"]["category"],
                          data["state"]["account"]["login"]), ("error", "expired_credentials", "devuser"))
        models = self.models("cli")
        self.assertEqual((models["auth"]["status"], models["auth"]["account"]["login"], models["models"]),
                         ("error", "devuser", []))
        calls = len(self.chat_calls())
        for gen in (data["state"]["generation"], self.prior["generation"]):
            code, _, _ = self.chat("cli", "gpt-5.4", gen)
            self.assertIn(code, (401, 409))
        self.assertEqual(len(self.chat_calls()), calls)
        self.assertEqual(json.loads(self.store.read_text())["modes"]["cli"]["login"], "devuser")


class DeviceHttpAndSecrecyTests(ServerCase):  # A06, A09, A11, A27, A29, A30
    def setUp(self):
        self.log = io.StringIO()
        handler = logging.StreamHandler(self.log)
        root = logging.getLogger()
        old_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, old_level)
        super().setUp()

    def read_events(self, stop, sink):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/events", headers={"Host": f"127.0.0.1:{self.port}"})
        resp = conn.getresponse()
        while not stop.is_set():
            try:
                line = resp.fp.readline()
            except OSError:
                break
            if not line:
                break
            sink.append(line)
        conn.close()

    def test_full_device_flow_without_secret_leaks(self):
        events, stop = [], threading.Event()
        reader = threading.Thread(target=self.read_events, args=(stop, events), daemon=True)
        reader.start()
        self.provider.configure(device_script=["authorization_pending", "slow_down", "token:device"])
        gen = self.models("cli")["generation"]
        code, _, txn = self.client.post("/api/auth/device/start", {"mode": "cli",
                                                                   "expected_login": "devuser"})
        self.assertEqual((code, txn["user_code"], txn["verification_uri"]),
                         (200, fx.USER_CODE, "https://github.com/login/device"))
        tid = txn["transaction_id"]
        seen = []
        for _ in range(12):
            self.clock.advance(20)
            code, _, out = self.client.post("/api/auth/device/poll", {"transaction_id": tid})
            seen.append(out["status"])
            if out["status"] == "awaiting_confirmation":
                break
        self.assertEqual(out["candidate"]["login"], "devuser")
        code, _, done = self.client.post("/api/auth/device/confirm",
                                         {"transaction_id": tid, "generation": gen})
        self.assertEqual((code, done["state"]["source"], done["state"]["account"]["login"]),
                         (200, "device", "devuser"))
        code, _, again = self.client.post("/api/auth/device/confirm",
                                          {"transaction_id": tid, "generation": gen})
        self.assertEqual(code, 409)
        new_gen = done["state"]["generation"]
        code, _, _ = self.chat("cli", "claude-sonnet-4.6", new_gen)
        self.assertEqual(code, 200)
        self.assertEqual(self.chat_calls()[-1]["login"], "devuser")
        self.assertEqual(self.models("cli")["auth"]["account"]["login"], "devuser")
        time.sleep(0.3)
        stop.set()

        store = (self.state / ".demo-auth.json").read_text()
        responses = b"".join(self.client.transcript).decode("utf-8", "replace")
        evidence = {"responses": responses, "events": b"".join(events).decode(),
                    "logs": self.log.getvalue(), "store": store}
        for name, text in evidence.items():
            with self.subTest(surface=name):
                self.assertNotIn(fx.CANARY, text)
                self.assertNotIn(fx.DEVICE_CODE, text)
                self.assertNotIn("refresh", text.lower()) if name == "store" else None
        self.assertNotIn(fx.USER_CODE, evidence["events"] + evidence["logs"] + store)
        self.assertIn(fx.USER_CODE, responses)  # intentional: owning browser only

        # Restart: verified identity restored, catalog refetched, persisted marker honored.
        self.stop_server(self.server)
        self.start_server()
        data = self.models("cli")
        self.assertEqual((data["auth"]["status"], data["auth"]["source"],
                          data["auth"]["account"]["login"]), ("ready", "device", "devuser"))
        code, _, out = self.client.post("/api/auth/sign-out", {"mode": "cli",
                                                               "generation": data["generation"]})
        self.assertEqual((code, out["status"]), (200, "signed_out"))
        self.stop_server(self.server)
        self.start_server()
        self.assertEqual(self.models("cli")["auth"]["status"], "signed_out")
        self.assertEqual(self.models("vscode")["auth"]["status"], "signed_out")  # untouched default

    def test_cancel_via_http_is_idempotent(self):
        code, _, txn = self.client.post("/api/auth/device/start", {"mode": "vscode"})
        for _ in range(2):
            code, _, out = self.client.post("/api/auth/device/cancel",
                                            {"transaction_id": txn["transaction_id"]})
            self.assertEqual((code, out["status"]), (200, "cancelled"))
            self.assertNotIn("user_code", out)
        self.clock.advance(60)
        self.client.post("/api/auth/device/poll", {"transaction_id": txn["transaction_id"]})
        self.assertEqual(self.provider.calls_for(path="/login/oauth/access_token"), [])


class ProcessIsolationTests(unittest.TestCase):  # A33
    def test_no_start_gateway_with_isolated_paths(self):
        state = tmpdir(self)
        (state / "ghconfig").mkdir()
        port, dead_gateway = free_port(), free_port()
        env = {k: v for k, v in os.environ.items()
               if k not in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")}
        env.update(GH_CONFIG_DIR=str(state / "ghconfig"), PYTHONDONTWRITEBYTECODE="1")
        repo_log = ROOT / "logs" / "demo.log"
        log_mtime = repo_log.stat().st_mtime if repo_log.exists() else None
        proc = subprocess.Popen(
            [sys.executable, "-B", str(ROOT / "demo.py"), "--no-start-gateway",
             "--gateway", f"http://127.0.0.1:{dead_gateway}", "--port", str(port),
             "--auth-store", str(state / "auth.json"),
             "--legacy-token-file", str(state / "missing-legacy.json"),
             "--log-dir", str(state / "logs")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=str(state))
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait(10)))
        client = Client(port)
        for _ in range(100):
            try:
                if client.request("GET", "/health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.1)
        else:
            self.fail("demo did not start")
        code, _, data = client.get("/api/auth/status?mode=vscode")
        self.assertEqual((code, data["status"]), (200, "signed_out"))
        with socket.socket() as s:
            self.assertNotEqual(s.connect_ex(("127.0.0.1", dead_gateway)), 0,
                                "no gateway may be started")
        proc.terminate()
        output = proc.communicate(timeout=10)[0].decode("utf-8", "replace")
        self.assertIn("--no-start-gateway set, not starting it", output)
        self.assertNotIn("Starting gateway", output)
        self.assertTrue((state / "logs" / "demo.log").exists())
        self.assertFalse((state / "auth.json").exists())
        self.assertFalse((ROOT / ".demo-auth.json").exists())
        if log_mtime is not None:
            self.assertEqual(repo_log.stat().st_mtime, log_mtime)

    def test_import_has_no_side_effects(self):
        self.assertEqual(logging.getLogger("demo").handlers, [])
        out = subprocess.run([sys.executable, "-B", "-c",
                              "import sys; sys.path.insert(0, r'%s'); import demo, logging; "
                              "print(len(logging.getLogger().handlers))" % ROOT],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.strip(), "0")


if __name__ == "__main__":
    unittest.main()
