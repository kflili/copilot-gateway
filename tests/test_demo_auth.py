"""Unit/provider regressions for demo_auth.py (fake transports, clocks, gh, store).

Run: python -B -m unittest discover -s tests -p "test_demo_*.py"
Set DEMO_TEST_TMP to relocate temporary state (defaults to the system temp dir).
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import stat
import sys
import tempfile
import threading
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import demo_fixture as fx  # noqa: E402
import demo_auth  # noqa: E402
from demo_auth import AuthError  # noqa: E402


def tmpdir(case):
    path = tempfile.mkdtemp(prefix="demo-auth-test-", dir=os.environ.get("DEMO_TEST_TMP"))
    case.addCleanup(shutil.rmtree, path, True)
    return pathlib.Path(path)


class ProviderCase(unittest.TestCase):
    def setUp(self):
        self.provider = fx.FakeProvider().start()
        self.addCleanup(self.provider.stop)
        self.state = tmpdir(self)

    def manager(self, **kw):
        manager, gh = fx.build_manager(self.provider, self.state, **kw)
        self.gh = gh
        return manager


class ScriptedTransport:
    """In-memory transport: maps (method, url) to responses; records every URL."""

    def __init__(self, routes):
        self.routes = routes
        self.seen = []

    def request(self, method, url, headers, body=None, timeout=15):
        self.seen.append(url)
        status, payload = self.routes[(method, url)]
        raw = types.SimpleNamespace(read=lambda n: json.dumps(payload).encode(),
                                    close=lambda: None)
        return demo_auth.Response(status, {}, raw)


class DestinationTests(unittest.TestCase):
    """A16: credential destinations are validated before any request."""

    def test_rejects_unsafe_urls(self):
        allowed = {"https://api.github.com"}
        bad = ["http://api.github.com/user", "https://user:pw@api.github.com/user",
               "https://api.github.com@evil.com/user", "https://api.github.com.evil.com/user",
               "https://evilapi.github.com/user", "https://api.github.com:8443/user",
               "https://api.github.com/user?x=1", "https://api.github.com/user#f",
               "https://api.github.com\\@evil.com/", "https://api.github.com/us er", "",
               "https://[::1]/user", "ftp://api.github.com/user"]
        for url in bad:
            with self.subTest(url=url), self.assertRaises(AuthError) as cm:
                demo_auth.validate_https_url(url, allowed)
            self.assertEqual(cm.exception.category, "unsafe_destination")
        self.assertEqual(demo_auth.validate_https_url("https://API.github.com:443/user", allowed),
                         "https://api.github.com/user")

    def test_api_base_allowlist(self):
        self.assertEqual(demo_auth.validate_api_base("https://api.githubcopilot.com/"),
                         "https://api.githubcopilot.com")
        for url in ("https://api.githubcopilot.com.evil.com", "https://evil.githubcopilot.com",
                    "https://api.githubcopilot.com/v1", "http://api.githubcopilot.com"):
            with self.subTest(url=url), self.assertRaises(AuthError) as cm:
                demo_auth.validate_api_base(url)
            self.assertEqual(cm.exception.category, "unsupported_endpoint")

    def test_foreign_verification_uri_rejected_before_use(self):
        base = {"device_code": fx.DEVICE_CODE, "user_code": fx.USER_CODE,
                "expires_in": 900, "interval": 5}
        for uri in ("https://evil.com/login/device", "https://github.com.evil.com/login/device",
                    "http://github.com/login/device", "https://github.com/login/device/../x",
                    "https://github.com:444/login/device", "https://github.com/other"):
            transport = ScriptedTransport({("POST", demo_auth.DEVICE_CODE_URL):
                                           (200, dict(base, verification_uri=uri))})
            provider = demo_auth.GitHubProvider(transport)
            with self.subTest(uri=uri), self.assertRaises(AuthError) as cm:
                provider.device_start()
            self.assertEqual(cm.exception.category, "unsafe_destination")
            self.assertEqual(transport.seen, [demo_auth.DEVICE_CODE_URL])

    def test_redirect_is_not_followed(self):
        transport = ScriptedTransport({("GET", "https://api.github.com/user"): (302, {})})
        with self.assertRaises(AuthError) as cm:
            demo_auth.GitHubProvider(transport).get_user(demo_auth.Secret("x" * 30))
        self.assertEqual(cm.exception.category, "provider_unavailable")
        self.assertIn("redirect", cm.exception.message)

    def test_real_transport_disables_redirects(self):
        handler = demo_auth._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://evil.com"))


class SecretAndSanitizerTests(unittest.TestCase):
    def test_secret_repr_hides_value(self):
        secret = demo_auth.Secret(fx.TOKENS["alice"])
        for text in (repr(secret), str(secret), repr({"s": secret}), f"{secret}"):
            self.assertNotIn(fx.CANARY, text)
        state = demo_auth.ModeState("cli", "g", "ready", secret=secret)
        self.assertNotIn(fx.CANARY, repr(state))
        self.assertNotIn(fx.CANARY, json.dumps(state.public()))

    def test_sanitize_provider_message(self):
        raw = json.dumps({"error": {"message": f"bad token ghu_{'a' * 20} and Bearer abcdefghijkl "
                                               f"{fx.TOKENS['bob']}"}}).encode()
        msg = demo_auth.sanitize_provider_message(raw, [fx.TOKENS["bob"]])
        self.assertNotIn(fx.CANARY, msg)
        self.assertNotIn("ghu_aaaa", msg)
        self.assertNotIn("abcdefghijkl", msg)
        self.assertIsNone(demo_auth.sanitize_provider_message(b"<html>raw body</html>"))


class DefaultSourceTests(ProviderCase):
    """A01, A02: default precedence, truthful provenance, independent modes."""

    def test_no_credentials_signed_out(self):
        manager = self.manager()
        self.gh.mode = "missing"
        for mode in ("vscode", "cli"):
            state = manager.ensure(mode)
            self.assertEqual(state.status, "signed_out", mode)
            self.assertEqual(state.catalog, ())
            self.assertTrue(state.error["message"])
        self.assertIn("gh", manager.ensure("cli").error["message"])

    def test_precedence_and_provenance(self):
        manager = self.manager(environ={"GH_TOKEN": fx.TOKENS["env"]},
                               legacy={"mode": "vscode", "token": fx.TOKENS["legacy"]})
        cli, vscode = manager.ensure("cli"), manager.ensure("vscode")
        self.assertEqual((cli.source, cli.account["login"]), ("environment", "envuser"))
        self.assertEqual((vscode.source, vscode.account["login"]), ("legacy_vscode", "legacyuser"))
        self.assertEqual(vscode.public()["source_label"],
                         "legacy saved VS Code OAuth (.gateway-token.json)")
        self.assertNotEqual(cli.generation, vscode.generation)
        self.assertEqual(self.gh.calls, [])  # GH_TOKEN wins before gh is consulted

        manager2 = fx.build_manager(self.provider, tmpdir(self))[0]
        cli2 = manager2.ensure("cli")
        self.assertEqual((cli2.source, cli2.account["login"]), ("gh_active", "alice"))
        self.assertEqual(cli2.public()["source_label"], "gh active account")

    def test_legacy_file_errors_are_visible(self):
        manager = self.manager()
        pathlib.Path(manager.legacy_token_file).write_text("{broken")
        state = manager.ensure("vscode")
        self.assertEqual((state.status, state.error["category"]), ("error", "legacy_unreadable"))
        manager = fx.build_manager(self.provider, tmpdir(self),
                                   legacy={"mode": "cli", "token": fx.TOKENS["legacy"]})[0]
        self.assertEqual(manager.ensure("vscode").status, "signed_out")


class GhSelectionTests(ProviderCase):
    """A03, A04, A05, A13, A14, A15."""

    def test_select_named_account_without_switching(self):
        manager = self.manager()
        gen = manager.ensure("vscode").generation
        state = manager.select_gh("vscode", "github.com", "bob", gen)
        self.assertEqual((state["status"], state["account"]["login"], state["source"]),
                         ("ready", "bob", "gh_account"))
        self.assertEqual(self.gh.active, {"github.com": "alice", "contoso.ghe.com": "eve"})
        token_calls = [c for c in self.gh.calls if c["args"][1:3] == ["auth", "token"]]
        self.assertEqual(token_calls[-1]["args"],
                         ["gh", "auth", "token", "--hostname", "github.com", "--user", "bob"])
        self.assertEqual(token_calls[-1]["token_env"], [])
        models = self.provider.calls_for(path="/models")
        self.assertEqual((models[-1]["login"], models[-1]["integration"]), ("bob", "vscode-chat"))
        self.assertNotIn(fx.CANARY, (self.state / ".demo-auth.json").read_text())

    def test_list_accounts_allowlists_fields(self):
        data = self.manager().list_accounts()
        by_login = {a["login"]: a for a in data["accounts"]}
        self.assertEqual(set(by_login["alice"]), {"host", "login", "active", "state",
                                                  "supported", "reason"})
        self.assertTrue(by_login["alice"]["active"] and by_login["alice"]["supported"])
        self.assertFalse(by_login["stale"]["supported"])
        self.assertFalse(by_login["eve"]["supported"])
        self.assertIn("GHE.com", by_login["eve"]["reason"])
        self.assertNotIn(fx.CANARY, json.dumps(data))

    def test_gh_failures_are_distinct(self):
        manager = self.manager()
        gen = manager.ensure("cli").generation
        before = manager.ensure("cli")
        for mode, category in (("missing", "gh_missing"), ("timeout", "gh_timeout"),
                               ("fail", "gh_account_unavailable")):
            self.gh.mode = mode
            with self.subTest(mode=mode), self.assertRaises(AuthError) as cm:
                manager.select_gh("cli", "github.com", "bob", gen)
            self.assertEqual(cm.exception.category, category)
        self.gh.mode = "badjson"
        self.assertEqual(manager.list_accounts()["error"]["category"], "gh_invalid_output")
        self.gh.mode = "ok"
        with self.assertRaises(AuthError) as cm:
            manager.select_gh("cli", "github.com", "stale", gen)
        self.assertEqual(cm.exception.category, "gh_account_unavailable")
        self.assertIs(manager.ensure("cli"), before)

    def test_identity_mismatch_preserves_prior(self):
        manager = self.manager()
        before = manager.ensure("cli")
        self.gh.token_override["bob"] = fx.TOKENS["mallory"]
        with self.assertRaises(AuthError) as cm:
            manager.select_gh("cli", "github.com", "bob", before.generation)
        self.assertEqual(cm.exception.category, "identity_mismatch")
        self.assertIs(manager.ensure("cli"), before)
        self.assertFalse((self.state / ".demo-auth.json").exists())
        self.assertEqual([c for c in self.provider.calls_for(path="/models")
                          if c["login"] == "mallory"], [])

    def test_verified_but_models_denied(self):
        manager = self.manager()
        before = manager.ensure("cli")
        with self.assertRaises(AuthError) as cm:
            manager.select_gh("cli", "github.com", "carol_contoso", before.generation)
        err = cm.exception.public()
        self.assertEqual(err["category"], "access_denied")
        self.assertEqual(err["identity"]["login"], "carol_contoso")
        self.assertIn("verified, models unavailable", err["message"])
        self.assertIs(manager.ensure("cli"), before)

    def test_empty_catalog_is_ready_but_not_callable(self):
        manager = self.manager()
        gen = manager.ensure("cli").generation
        state = manager.select_gh("cli", "github.com", "dave", gen)
        self.assertEqual(state["status"], "ready")
        catalog = manager.ensure("cli").catalog
        self.assertEqual([m for m in catalog if m.get("supported_endpoints")], [])
        self.assertEqual(len(catalog), 1)

    def test_unsupported_host_makes_no_requests(self):
        manager = self.manager()
        gen = manager.ensure("cli").generation
        seen = len(manager.provider.transport.seen)
        gh_calls = len(self.gh.calls)
        for host in ("contoso.ghe.com", "ghes.example.com"):
            with self.subTest(host=host), self.assertRaises(AuthError) as cm:
                manager.select_gh("cli", host, "eve", gen)
            self.assertEqual(cm.exception.category, "unsupported_host")
            self.assertIn("guidance_url", cm.exception.public())
        self.assertEqual(len(manager.provider.transport.seen), seen)
        self.assertEqual(len(self.gh.calls), gh_calls)


class PlanOriginTests(ProviderCase):
    """Round-2 finding 1: exact GitHub.com plan origins work; anything else is refused."""

    def test_individual_and_business_origins_reach_catalog(self):
        manager = self.manager()
        gen = manager.ensure("cli").generation
        for login, origin in (("bob", "https://api.individual.githubcopilot.com"),
                              ("bizuser", "https://api.business.githubcopilot.com")):
            state = manager.select_gh("cli", "github.com", login, gen)
            gen = state["generation"]
            with self.subTest(login=login):
                self.assertEqual((state["status"], state["api_base"]), ("ready", origin))
                host = origin.split("//")[1]
                models = self.provider.calls_for(host=host, path="/models")
                self.assertEqual(models[-1]["login"], login)
                self.assertEqual(len(manager.ensure("cli").catalog), 4)

    def test_default_discovery_of_individual_origin(self):
        self.provider.configure(discovery={"alice": "https://api.individual.githubcopilot.com"})
        state = self.manager().ensure("cli")
        self.assertEqual((state.status, state.api_base),
                         ("ready", "https://api.individual.githubcopilot.com"))

    def test_other_origins_still_refused(self):
        for url in ("https://x.api.individual.githubcopilot.com",
                    "https://proxy.individual.githubcopilot.com",
                    "https://individual.githubcopilot.com",
                    "https://api.individual.githubcopilot.com.evil.com",
                    "https://api.business.githubcopilot.com:8443",
                    "https://api.contoso.ghe.com", "https://copilot-api.contoso.ghe.com",
                    "https://api.githubcopilot.com.ghe.com"):
            with self.subTest(url=url), self.assertRaises(AuthError) as cm:
                demo_auth.validate_api_base(url)
            self.assertEqual(cm.exception.category, "unsupported_endpoint")

    def test_ghe_discovery_gets_no_credential(self):
        self.provider.configure(discovery={"alice": "https://copilot-api.contoso.ghe.com"})
        manager = self.manager()
        state = manager.ensure("cli")
        self.assertEqual(state.error["category"], "unsupported_endpoint")
        self.assertEqual(self.provider.calls_for(path="/models"), [])
        self.assertFalse(any("ghe.com" in url for _, url in manager.provider.transport.seen))


class GhHostBoundaryTests(ProviderCase):
    """Round-2 finding 2: a non-github.com GH_HOST credential never reaches github.com."""

    def test_enterprise_gh_host_refused_before_any_request(self):
        for environ in ({"GH_HOST": "contoso.ghe.com"},
                        {"GH_HOST": "https://contoso.ghe.com/", "GH_TOKEN": fx.TOKENS["ghe"]},
                        {"GH_HOST": "ghes.example.com"}):
            manager, gh = fx.build_manager(self.provider, tmpdir(self), environ=environ)
            state = manager.ensure("cli")
            with self.subTest(environ=sorted(environ)):
                self.assertEqual((state.status, state.error["category"]), ("error", "unsupported_host"))
                self.assertIsNone(state.secret)
                self.assertIn("not sent to github.com", state.error["message"])
                self.assertEqual(manager.provider.transport.seen, [])
                self.assertEqual(gh.calls, [])
        self.assertEqual([c for c in self.provider.calls if c["login"] == "eve"], [])
        with self.assertRaises(AuthError) as cm:
            manager.use_default("cli", manager.ensure("cli").generation)
        self.assertEqual(cm.exception.category, "unsupported_host")

    def test_explicit_selection_still_works_under_enterprise_gh_host(self):
        manager, gh = fx.build_manager(self.provider, self.state, environ={"GH_HOST": "contoso.ghe.com"})
        state = manager.select_gh("cli", "github.com", "bob", manager.ensure("cli").generation)
        self.assertEqual(state["account"]["login"], "bob")
        token_call = [c for c in gh.calls if c["args"][1:3] == ["auth", "token"]][-1]
        self.assertIn("--hostname", token_call["args"])
        self.assertEqual(token_call["token_env"], [])

    def test_default_gh_lookup_pinned_to_github_com(self):
        manager, gh = fx.build_manager(self.provider, self.state, environ={"GH_HOST": "https://GitHub.com/"})
        self.assertEqual(manager.ensure("cli").account["login"], "alice")
        self.assertEqual(gh.calls[-1]["args"], ["gh", "auth", "token", "--hostname", "github.com"])

    def test_only_enterprise_login_is_not_used(self):
        gh = fx.FakeGh(active=None)  # github.com has no active account; GHE.com does
        manager = fx.build_manager(self.provider, self.state, gh=gh)[0]
        state = manager.ensure("cli")
        self.assertEqual(state.status, "signed_out")
        self.assertEqual([c for c in self.provider.calls if c["login"] == "eve"], [])
        self.assertEqual(manager.provider.transport.seen, [])


class ModelFallbackTests(ProviderCase):
    """A23: versioned 400/406 falls back; 401/403 are not masked."""

    def test_version_fallback(self):
        self.provider.configure(version_reject=True)
        manager = self.manager()
        self.assertEqual(manager.ensure("cli").status, "ready")
        calls = self.provider.calls_for(path="/models")
        self.assertEqual([c["versioned"] for c in calls], [True, False])

    def test_auth_errors_not_masked(self):
        for status, category in ((401, "expired_credentials"), (403, "access_denied")):
            self.provider.configure(models={"alice": status})
            manager = fx.build_manager(self.provider, tmpdir(self))[0]
            before = len(self.provider.calls_for(path="/models"))
            state = manager.ensure("cli")
            self.assertEqual(state.error["category"], category)
            self.assertEqual(len(self.provider.calls_for(path="/models")) - before, 1)


class RaceTests(ProviderCase):
    """A17, A19: generation compare-and-swap."""

    def test_slow_account_a_loses_to_b(self):
        manager = self.manager()
        gen = manager.ensure("cli").generation
        self.provider.configure(models_delay={"bob": 1.0})
        result = {}

        def slow():
            try:
                manager.select_gh("cli", "github.com", "bob", gen)
            except AuthError as e:
                result["err"] = e.category

        t = threading.Thread(target=slow)
        t.start()
        # Wait until bob's catalog request is in flight, then activate dave.
        for _ in range(200):
            if any(c["login"] == "bob" for c in self.provider.calls_for(path="/models")):
                break
            threading.Event().wait(0.01)
        manager.select_gh("cli", "github.com", "dave", gen)
        t.join(5)
        self.assertEqual(result["err"], "stale_generation")
        state = manager.ensure("cli")
        self.assertEqual(state.account["login"], "dave")
        self.assertEqual(json.loads((self.state / ".demo-auth.json").read_text())["modes"]["cli"]["login"], "dave")

    def test_concurrent_selections_single_commit(self):
        manager = self.manager()
        gen = manager.ensure("cli").generation
        barrier = threading.Barrier(2)
        outcomes = []

        def pick(login):
            barrier.wait()
            try:
                outcomes.append(("ok", manager.select_gh("cli", "github.com", login, gen)["account"]["login"]))
            except AuthError as e:
                outcomes.append(("err", e.category))

        threads = [threading.Thread(target=pick, args=(n,)) for n in ("bob", "dave")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(sorted(o[0] for o in outcomes), ["err", "ok"])
        self.assertIn(("err", "stale_generation"), outcomes)
        winner = next(o[1] for o in outcomes if o[0] == "ok")
        self.assertEqual(manager.ensure("cli").account["login"], winner)

    def test_initial_load_discarded_after_activation(self):
        manager = self.manager()
        state = manager.ensure("vscode")
        self.assertEqual(state.status, "signed_out")
        new = manager.select_gh("vscode", "github.com", "bob", state.generation)
        with self.assertRaises(AuthError):
            manager.chat_snapshot("vscode", state.generation)
        self.assertEqual(manager.chat_snapshot("vscode", new["generation"]).generation, new["generation"])


class DeviceFlowTests(ProviderCase):
    """A06-A12 with a fake clock and a real local HTTP provider."""

    def setUp(self):
        super().setUp()
        self.clock = fx.FakeClock()
        self.wall = fx.FakeClock(1_700_000_000.0)
        self.mgr = self.manager(clock=self.clock, wall=self.wall)
        self.owner = "session-a"

    def polls(self):
        return self.provider.calls_for(host="github.com", path="/login/oauth/access_token")

    def start(self, **kw):
        return self.mgr.start_device(self.owner, kw.pop("mode", "cli"), **kw)

    def test_start_returns_only_validated_public_fields(self):
        txn = self.start()
        self.assertEqual(txn["status"], "pending")
        self.assertEqual(txn["verification_uri"], "https://github.com/login/device")
        self.assertEqual(txn["user_code"], fx.USER_CODE)
        self.assertEqual((txn["expires_in"], txn["retry_after"]), (900, 5))
        self.assertNotIn(fx.CANARY, json.dumps(txn))
        start = self.provider.calls_for(path="/login/device/code")[0]
        self.assertEqual(start["host"], "github.com")

    def test_pending_respects_interval_including_parallel_polls(self):
        txn = self.start()
        for _ in range(5):
            self.assertEqual(self.mgr.poll_device(self.owner, txn["transaction_id"])["status"], "pending")
        self.assertEqual(len(self.polls()), 0)
        self.clock.advance(5)
        threads = [threading.Thread(target=self.mgr.poll_device, args=(self.owner, txn["transaction_id"]))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(self.polls()), 1)
        self.clock.advance(4.9)
        self.mgr.poll_device(self.owner, txn["transaction_id"])
        self.assertEqual(len(self.polls()), 1)
        self.clock.advance(0.1)
        self.assertEqual(self.mgr.poll_device(self.owner, txn["transaction_id"])["status"], "pending")
        self.assertEqual(len(self.polls()), 2)

    def test_slow_down_increases_interval(self):
        self.provider.configure(device_script=["slow_down", "slow_down:30", "slow_down",
                                               "authorization_pending"])
        tid = self.start()["transaction_id"]
        waited = 5
        for expected in (10, 30, 35):
            self.clock.advance(waited - 0.1)
            n = len(self.polls())
            self.mgr.poll_device(self.owner, tid)
            self.assertEqual(len(self.polls()), n, "browser retry must not bypass the interval")
            self.clock.advance(0.1)
            out = self.mgr.poll_device(self.owner, tid)
            self.assertEqual(len(self.polls()), n + 1)
            self.assertEqual((out["status"], out["retry_after"]), ("pending", expected))
            waited = expected
        self.clock.advance(35)
        self.assertEqual(self.mgr.poll_device(self.owner, tid)["status"], "pending")
        self.assertEqual(len(self.polls()), 4)

    def test_success_verify_confirm_once(self):
        self.provider.configure(device_script=["authorization_pending", "token:device"])
        gen = self.mgr.ensure("cli").generation
        txn = self.start(expected_login="DevUser")
        tid = txn["transaction_id"]
        self.clock.advance(5)
        self.assertEqual(self.mgr.poll_device(self.owner, tid)["status"], "pending")
        self.clock.advance(5)
        out = self.mgr.poll_device(self.owner, tid)
        self.assertEqual(out["status"], "awaiting_confirmation")
        self.assertEqual(out["candidate"]["login"], "devuser")
        self.assertEqual(out["candidate"]["callable_count"], 3)
        self.assertNotIn("user_code", out)
        self.assertEqual(self.mgr.ensure("cli").account["login"], "alice")  # not yet committed
        result = self.mgr.confirm_device(self.owner, tid, gen)
        self.assertEqual(result["state"]["source"], "device")
        self.assertEqual(result["state"]["account"]["login"], "devuser")
        with self.assertRaises(AuthError) as cm:
            self.mgr.confirm_device(self.owner, tid, gen)
        self.assertEqual(cm.exception.category, "conflict")
        stored = (self.state / ".demo-auth.json").read_text()
        self.assertNotIn(fx.CANARY, stored)
        self.assertNotIn("refresh", stored)
        self.assertEqual(self.provider.calls_for(path="/user")[-1]["login"], "devuser")

    def test_expected_login_mismatch_fails(self):
        self.provider.configure(device_script=["token:device"])
        txn = self.start(expected_login="someoneelse")
        self.clock.advance(5)
        out = self.mgr.poll_device(self.owner, txn["transaction_id"])
        self.assertEqual((out["status"], out["error"]["category"]), ("failed", "identity_mismatch"))
        self.assertEqual(self.mgr.ensure("cli").account["login"], "alice")

    def test_terminal_outcomes(self):
        for step, status, category in (("access_denied", "denied", "device_denied"),
                                       ("expired_token", "expired", "device_expired"),
                                       ("device_flow_disabled", "failed", "device_disabled"),
                                       ("incorrect_client_credentials", "failed", "device_disabled"),
                                       ("unsupported_grant_type", "failed", "device_failed")):
            self.provider.configure(device_script=[step])
            txn = self.start()
            self.clock.advance(5)
            out = self.mgr.poll_device(self.owner, txn["transaction_id"])
            with self.subTest(step=step):
                self.assertEqual((out["status"], out["error"]["category"]), (status, category))
                self.assertNotIn("user_code", out)
                n = len(self.polls())
                self.clock.advance(60)
                self.mgr.poll_device(self.owner, txn["transaction_id"])
                self.assertEqual(len(self.polls()), n, "terminal state must not poll again")
        self.assertEqual(self.mgr.ensure("cli").account["login"], "alice")

    def test_local_expiry(self):
        txn = self.start()
        self.clock.advance(901)
        out = self.mgr.poll_device(self.owner, txn["transaction_id"])
        self.assertEqual(out["status"], "expired")
        self.assertEqual(len(self.polls()), 0)

    def test_transient_provider_errors_are_bounded(self):
        self.provider.configure(device_script=["status:503"])
        txn = self.start()
        statuses = []
        for _ in range(6):
            self.clock.advance(100)
            statuses.append(self.mgr.poll_device(self.owner, txn["transaction_id"]))
        self.assertEqual(len(self.polls()), 3)
        self.assertEqual(statuses[-1]["status"], "failed")
        self.assertNotIn(fx.CANARY, json.dumps(statuses))

    def test_device_start_failures(self):
        self.provider.configure(device_start_status=200,
                                device_start={"error": "device_flow_disabled"})
        with self.assertRaises(AuthError) as cm:
            self.start()
        self.assertEqual(cm.exception.category, "device_disabled")
        self.provider.configure(device_start_status=503)
        with self.assertRaises(AuthError) as cm:
            self.start()
        self.assertEqual(cm.exception.category, "provider_unavailable")

    def test_cancel_idempotent_and_during_inflight_poll(self):
        txn = self.start()
        tid = txn["transaction_id"]
        entered, release = threading.Event(), threading.Event()
        real_poll = self.mgr.provider.device_poll

        def blocking_poll(code):
            entered.set()
            release.wait(5)
            self.provider.configure(device_script=["token:device"])
            return real_poll(code)

        self.mgr.provider.device_poll = blocking_poll
        self.clock.advance(5)
        t = threading.Thread(target=self.mgr.poll_device, args=(self.owner, tid))
        t.start()
        self.assertTrue(entered.wait(5))
        self.assertEqual(self.mgr.cancel_device(self.owner, tid)["status"], "cancelled")
        self.assertEqual(self.mgr.cancel_device(self.owner, tid)["status"], "cancelled")
        release.set()
        t.join(5)
        self.assertEqual(self.mgr.poll_device(self.owner, tid)["status"], "cancelled")
        with self.assertRaises(AuthError):
            self.mgr.confirm_device(self.owner, tid, self.mgr.ensure("cli").generation)
        self.assertEqual(self.mgr.ensure("cli").account["login"], "alice")
        self.assertEqual([c for c in self.provider.calls_for(path="/user") if c["login"] == "devuser"], [])

    def test_cancel_before_confirm_and_superseded_generation(self):
        self.provider.configure(device_script=["token:device"])
        gen = self.mgr.ensure("cli").generation
        txn = self.start()
        self.clock.advance(5)
        self.assertEqual(self.mgr.poll_device(self.owner, txn["transaction_id"])["status"],
                         "awaiting_confirmation")
        self.mgr.cancel_device(self.owner, txn["transaction_id"])
        with self.assertRaises(AuthError) as cm:
            self.mgr.confirm_device(self.owner, txn["transaction_id"], gen)
        self.assertEqual(cm.exception.category, "cancelled")

        txn = self.start()
        self.clock.advance(5)
        self.mgr.poll_device(self.owner, txn["transaction_id"])
        self.mgr.select_gh("cli", "github.com", "bob", gen)  # another tab switched
        with self.assertRaises(AuthError) as cm:
            self.mgr.confirm_device(self.owner, txn["transaction_id"], gen)
        self.assertEqual(cm.exception.category, "stale_generation")
        self.assertEqual(self.mgr.ensure("cli").account["login"], "bob")

    def test_foreign_owner_cannot_touch_transaction(self):
        txn = self.start()
        for fn in (lambda: self.mgr.poll_device("session-b", txn["transaction_id"]),
                   lambda: self.mgr.cancel_device("session-b", txn["transaction_id"]),
                   lambda: self.mgr.confirm_device("session-b", txn["transaction_id"], "x")):
            with self.assertRaises(AuthError) as cm:
                fn()
            self.assertEqual(cm.exception.category, "not_found")
        with self.assertRaises(AuthError) as cm:
            self.mgr.start_device("session-b", "cli")
        self.assertEqual(cm.exception.category, "conflict")
        again = self.start()
        self.assertEqual(self.mgr.poll_device(self.owner, txn["transaction_id"])["status"], "cancelled")
        self.assertEqual(again["status"], "pending")

    def reach_confirmation(self, owner=None):
        self.provider.configure(device_script=["token:device"])
        txn = self.mgr.start_device(owner or self.owner, "cli")
        self.clock.advance(5)
        out = self.mgr.poll_device(owner or self.owner, txn["transaction_id"])
        self.assertEqual(out["status"], "awaiting_confirmation")
        return txn["transaction_id"]

    def test_unconfirmed_candidate_expires_and_unblocks(self):  # round-2 finding 4
        tid = self.reach_confirmation()
        record = self.mgr._txns[tid]
        self.clock.advance(demo_auth.CONFIRM_WINDOW_SECONDS - 1)
        with self.assertRaises(AuthError) as cm:
            self.mgr.start_device("session-b", "cli")
        self.assertEqual(cm.exception.category, "conflict")
        self.clock.advance(1)
        other = self.mgr.start_device("session-b", "cli")
        self.assertEqual(other["status"], "pending")
        self.assertEqual(self.mgr.poll_device(self.owner, tid)["status"], "expired")
        self.assertIsNone(record.candidate_secret)
        self.assertEqual(record.candidate_catalog, ())
        with self.assertRaises(AuthError):
            self.mgr.confirm_device(self.owner, tid, self.mgr.ensure("cli").generation)
        self.assertEqual(self.mgr.ensure("cli").account["login"], "alice")

    def test_day_old_candidate_does_not_lock_out(self):
        tid = self.reach_confirmation()
        self.clock.advance(86400)
        self.assertEqual(self.mgr.start_device("session-b", "cli")["status"], "pending")
        self.assertEqual(self.mgr.cancel_device(self.owner, tid)["status"], "expired")

    def approve(self, mgr, clock, owner="s", expires_in=30):
        self.provider.configure(device_script=["token:device"], device_token_expires_in=expires_in)
        tid = mgr.start_device(owner, "cli")["transaction_id"]
        clock.advance(5)
        self.assertEqual(mgr.poll_device(owner, tid)["status"], "awaiting_confirmation")
        return tid

    def test_confirm_rejects_candidate_whose_token_expired(self):  # round-3 #1 boundary
        for elapsed, accepted in ((29, True), (30, False), (31, False)):
            with self.subTest(elapsed=elapsed):
                state_dir = tmpdir(self)
                clock, wall = fx.FakeClock(), fx.FakeClock(1_700_000_000.0)
                mgr = fx.build_manager(self.provider, state_dir, clock=clock, wall=wall)[0]
                before = mgr.ensure("cli")
                tid = self.approve(mgr, clock)
                clock.advance(elapsed)
                wall.advance(elapsed)
                store = state_dir / ".demo-auth.json"
                if accepted:
                    state = mgr.confirm_device("s", tid, before.generation)["state"]
                    self.assertEqual(state["account"]["login"], "devuser")
                    continue
                with self.assertRaises(AuthError) as cm:
                    mgr.confirm_device("s", tid, before.generation)
                self.assertEqual((cm.exception.category, cm.exception.status), ("device_expired", 410))
                self.assertIn("previous account is unchanged", cm.exception.message)
                self.assertIs(mgr.ensure("cli"), before)
                self.assertFalse(store.exists())
                txn = mgr._txns[tid]
                self.assertEqual(txn.status, "expired")
                self.assertIsNone(txn.candidate_secret)
                with self.assertRaises(AuthError) as cm:
                    mgr.confirm_device("s", tid, before.generation)
                self.assertEqual(cm.exception.category, "device_expired")
                self.assertEqual(mgr.chat_snapshot("cli", before.generation).generation, before.generation)

    def test_expired_candidate_token_releases_other_sessions(self):
        clock, wall = self.clock, self.wall
        tid = self.approve(self.mgr, clock)
        wall.advance(30)  # well inside the 600 s confirmation window
        self.assertEqual(self.mgr.start_device("session-b", "cli")["status"], "pending")
        self.assertEqual(self.mgr.poll_device("s", tid)["status"], "expired")

    def test_token_expired_during_verification_never_offered(self):
        clock, wall = self.clock, self.wall
        self.provider.configure(device_script=["token:device"], device_token_expires_in=30)
        tid = self.mgr.start_device("s", "cli")["transaction_id"]
        clock.advance(5)
        real_validate = self.mgr._validate

        def slow_validate(*a, **kw):
            result = real_validate(*a, **kw)
            wall.advance(31)  # verification outlived the token
            return result

        self.mgr._validate = slow_validate
        out = self.mgr.poll_device("s", tid)
        self.assertEqual((out["status"], out["error"]["category"]), ("expired", "device_expired"))
        self.assertIsNone(self.mgr._txns[tid].candidate_secret)

    def test_activation_never_commits_expired_state(self):
        before = self.mgr.ensure("cli")
        secret = demo_auth.Secret(fx.TOKENS["device"])
        expired = lambda gen: demo_auth.ModeState(
            "cli", gen, "ready", source="device",
            account={"host": "github.com", "login": "devuser", "user_id": 1005},
            expires_at=self.wall() - 1, secret=secret)
        record = {"kind": "device", "host": "github.com", "login": "devuser", "user_id": 1005,
                  "expires_at": self.wall() - 1, "secret": secret}
        with self.mgr._cv, self.assertRaises(AuthError) as cm:
            self.mgr._activate_locked("cli", before.generation, expired, record)
        self.assertEqual(cm.exception.category, "expired_credentials")
        self.assertIs(self.mgr.ensure("cli"), before)
        self.assertFalse((self.state / ".demo-auth.json").exists())

    def test_expired_active_state_demoted_on_read(self):
        before = self.mgr.ensure("cli")
        tid = self.approve(self.mgr, self.clock, expires_in=60)
        state = self.mgr.confirm_device("s", tid, before.generation)["state"]
        self.wall.advance(60)
        current = self.mgr.ensure("cli")
        self.assertEqual((current.status, current.error["category"]), ("error", "expired_credentials"))
        self.assertIsNone(current.secret)
        self.assertNotEqual(current.generation, state["generation"])
        with self.assertRaises(AuthError) as cm:
            self.mgr.chat_snapshot("cli", state["generation"])
        self.assertEqual(cm.exception.category, "stale_generation")

    def test_stuck_attempt_hits_hard_limit(self):
        txn = self.start()
        self.mgr._txns[txn["transaction_id"]].status = "verifying"  # e.g. a wedged verifier
        self.clock.advance(demo_auth.TXN_HARD_LIMIT_SECONDS)
        self.assertEqual(self.mgr.start_device("session-b", "cli")["status"], "pending")
        self.assertEqual(self.mgr._txns[txn["transaction_id"]].status, "failed")

    def test_token_expiry_honored(self):
        self.provider.configure(device_script=["token:device"], device_token_expires_in=3600)
        gen = self.mgr.ensure("cli").generation
        txn = self.start()
        self.clock.advance(5)
        self.mgr.poll_device(self.owner, txn["transaction_id"])
        state = self.mgr.confirm_device(self.owner, txn["transaction_id"], gen)["state"]
        self.assertEqual(state["expires_at"], 1_700_003_600.0)
        self.wall.advance(3601)
        with self.assertRaises(AuthError) as cm:
            self.mgr.chat_snapshot("cli", state["generation"])
        self.assertEqual(cm.exception.category, "expired_credentials")
        restarted = fx.build_manager(self.provider, self.state, wall=self.wall)[0]
        self.assertEqual(restarted.ensure("cli").error["category"], "expired_credentials")


class HookProtector(fx.MemoryProtector):
    def __init__(self):
        super().__init__()
        self.on_protect = None

    def protect(self, data):
        if self.on_protect:
            self.on_protect()
        return super().protect(data)


class ExpiryAdmissionTests(ProviderCase):
    """EXPIRY-CHECKPOINT.md: admission is sampled after staging, immediately before
    replace; pre-commit rejection preserves everything; after a successful replace the
    returned state is freshly sampled and there is no rollback."""

    ISSUED = 1_700_000_000.0
    LIFETIME = 30

    def setUp(self):
        super().setUp()
        self._patches = []
        self.addCleanup(self.unpatch_all)
        self.env()

    def unpatch_all(self):
        while self._patches:
            name, real = self._patches.pop()
            setattr(demo_auth.os, name, real)

    def env(self):
        self.unpatch_all()  # hooks never leak into another case's setup
        self.state = tmpdir(self)
        self.clock, self.wall = fx.FakeClock(), fx.FakeClock(self.ISSUED)
        self.protector = HookProtector()
        self.mgr = self.manager(clock=self.clock, wall=self.wall, protector=self.protector)
        self.mgr.select_gh("cli", "github.com", "bob", self.mgr.ensure("cli").generation)
        self.prior = self.mgr.ensure("cli")
        self.store = self.state / ".demo-auth.json"
        self.prior_bytes = self.store.read_bytes()
        self.provider.configure(device_script=["token:device"], device_token_expires_in=self.LIFETIME)
        self.tid = self.mgr.start_device("s", "cli")["transaction_id"]
        self.clock.advance(5)
        self.assertEqual(self.mgr.poll_device("s", self.tid)["status"], "awaiting_confirmation")
        self.expires_at = self.ISSUED + self.LIFETIME

    def set_wall(self, offset):
        self.wall.now = self.ISSUED + offset

    def patch_os(self, name, before):
        real = getattr(demo_auth.os, name)
        calls = []

        def hooked(*a, **kw):
            calls.append(a)
            before()
            return real(*a, **kw)

        self._patches.append((name, real))
        setattr(demo_auth.os, name, hooked)
        return calls

    def confirm(self):
        return self.mgr.confirm_device("s", self.tid, self.prior.generation)

    def restart(self):
        return fx.build_manager(self.provider, self.state, wall=self.wall,
                                protector=self.protector)[0].ensure("cli")

    def assert_precommit_rejected(self):
        with self.assertRaises(AuthError) as cm:
            self.confirm()
        self.assertEqual((cm.exception.category, cm.exception.status), ("device_expired", 410))
        self.assertIn("previous account is unchanged", cm.exception.message)
        self.assertIs(self.mgr.ensure("cli"), self.prior)  # same generation, identity, object
        self.assertEqual(self.store.read_bytes(), self.prior_bytes)
        self.assertEqual([p.name for p in self.state.iterdir() if p.suffix == ".tmp"], [])
        txn = self.mgr._txns[self.tid]
        self.assertEqual(txn.status, "expired")
        self.assertIsNone(txn.candidate_secret)
        restored = self.restart()
        self.assertEqual((restored.status, restored.account["login"]), ("ready", "bob"))

    def assert_committed_expired(self, result, replace_calls):
        self.assertEqual(result["transaction"]["status"], "committed")
        state = result["state"]
        self.assertEqual((state["status"], state["error"]["category"], state["account"]["login"]),
                         ("error", "expired_credentials", "devuser"))
        self.assertEqual(len(replace_calls), 1, "exactly one replacement, no compensating save")
        record = json.loads(self.store.read_text())["modes"]["cli"]
        self.assertEqual((record["kind"], record["login"]), ("device", "devuser"))
        active = self.mgr.ensure("cli")
        self.assertEqual((active.account["login"], active.status), ("devuser", "error"))
        self.assertIsNone(active.secret)
        for gen in (state["generation"], self.prior.generation):
            with self.assertRaises(AuthError):
                self.mgr.chat_snapshot("cli", gen)
        restored = self.restart()
        self.assertEqual((restored.status, restored.error["category"], restored.account["login"]),
                         ("error", "expired_credentials", "devuser"))
        self.assertEqual(len(replace_calls), 1)

    def test_admission_sample_before_at_after_expiry(self):
        for offset, admitted in ((self.LIFETIME - 0.1, True), (self.LIFETIME, False),
                                 (self.LIFETIME + 0.1, False)):
            with self.subTest(offset=offset):
                self.env()
                self.patch_os("fsync", lambda o=offset: self.set_wall(o))
                if admitted:
                    state = self.confirm()["state"]
                    self.assertEqual((state["status"], state["account"]["login"]), ("ready", "devuser"))
                else:
                    self.assert_precommit_rejected()

    def test_expiry_during_protection_rejected_before_replace(self):
        self.set_wall(self.LIFETIME - 1)
        self.protector.on_protect = lambda: self.set_wall(self.LIFETIME + 1)
        replaces = self.patch_os("replace", lambda: None)
        self.assert_precommit_rejected()
        self.assertEqual(replaces, [])

    def test_expiry_during_fsync_rejected_before_replace(self):
        self.set_wall(self.LIFETIME - 1)
        replaces = self.patch_os("replace", lambda: None)
        self.patch_os("fsync", lambda: self.set_wall(self.LIFETIME + 1))
        self.assert_precommit_rejected()
        self.assertEqual(replaces, [])

    def test_expiry_after_admission_during_replace_commits_expired(self):
        self.set_wall(self.LIFETIME - 1)
        replaces = self.patch_os("replace", lambda: self.set_wall(self.LIFETIME + 1))
        self.assert_committed_expired(self.confirm(), replaces)

    def test_expiry_after_save_returns_is_observed(self):
        self.set_wall(self.LIFETIME - 1)
        replaces = self.patch_os("replace", lambda: None)
        real_save = self.mgr.store.save

        def save_then_expire(*a, **kw):
            real_save(*a, **kw)
            self.set_wall(self.LIFETIME + 1)

        self.mgr.store.save = save_then_expire
        self.assert_committed_expired(self.confirm(), replaces)

    def test_replace_failure_preserves_prior_and_generation(self):
        self.set_wall(self.LIFETIME - 5)

        def fail():
            raise PermissionError("denied")

        self.patch_os("replace", fail)
        with self.assertRaises(AuthError) as cm:
            self.confirm()
        self.assertEqual(cm.exception.category, "persistence_failure")
        self.assertIs(self.mgr.ensure("cli"), self.prior)
        self.assertEqual(self.store.read_bytes(), self.prior_bytes)
        self.assertEqual([p.name for p in self.state.iterdir() if p.suffix == ".tmp"], [])
        self.assertEqual(self.mgr._txns[self.tid].status, "awaiting_confirmation")

    def test_protection_failure_preserves_prior_and_generation(self):
        def fail():
            raise demo_auth.StoreError("Windows DPAPI failed (error 5).")

        self.protector.on_protect = fail
        with self.assertRaises(AuthError) as cm:
            self.confirm()
        self.assertEqual(cm.exception.category, "persistence_failure")
        self.assertIs(self.mgr.ensure("cli"), self.prior)
        self.assertEqual(self.store.read_bytes(), self.prior_bytes)


class PersistenceTests(ProviderCase):
    """A27, A28, A29."""

    def test_restart_restores_gh_selection_without_copying_secret(self):
        manager = self.manager(legacy={"mode": "vscode", "token": fx.TOKENS["legacy"]})
        legacy_before = pathlib.Path(manager.legacy_token_file).read_bytes()
        gen = manager.ensure("vscode").generation
        manager.select_gh("vscode", "github.com", "bob", gen)
        data = json.loads((self.state / ".demo-auth.json").read_text())
        self.assertEqual(data["modes"]["vscode"], {"kind": "gh", "host": "github.com",
                                                   "login": "bob", "user_id": 1002})
        models_before = len(self.provider.calls_for(path="/models"))
        restarted, gh = fx.build_manager(self.provider, self.state)
        state = restarted.ensure("vscode")
        self.assertEqual((state.status, state.account["login"], state.source), ("ready", "bob", "gh_account"))
        self.assertEqual(len(self.provider.calls_for(path="/models")), models_before + 1)
        self.assertEqual(pathlib.Path(manager.legacy_token_file).read_bytes(), legacy_before)

    def test_missing_gh_account_after_restart_no_fallback(self):
        manager = self.manager()
        manager.select_gh("cli", "github.com", "bob", manager.ensure("cli").generation)
        gh = fx.FakeGh(accounts=[a for a in fx.FakeGh().accounts if a["login"] != "bob"])
        restarted = fx.build_manager(self.provider, self.state, gh=gh)[0]
        state = restarted.ensure("cli")
        self.assertEqual((state.status, state.error["category"]), ("error", "gh_account_unavailable"))
        self.assertEqual(state.account["login"], "bob")
        self.assertIsNone(state.secret)

    def test_identity_changed_on_restore(self):
        manager = self.manager()
        manager.select_gh("cli", "github.com", "bob", manager.ensure("cli").generation)
        gh = fx.FakeGh()
        gh.token_override["bob"] = fx.TOKENS["mallory"]
        state = fx.build_manager(self.provider, self.state, gh=gh)[0].ensure("cli")
        self.assertEqual(state.error["category"], "identity_mismatch")
        self.assertIsNone(state.secret)

    def test_device_record_round_trip(self):
        protector = fx.MemoryProtector()
        clock = fx.FakeClock()
        manager = self.manager(protector=protector, clock=clock)
        self.provider.configure(device_script=["token:device"])
        gen = manager.ensure("vscode").generation
        txn = manager.start_device("s", "vscode")
        clock.advance(5)
        manager.poll_device("s", txn["transaction_id"])
        manager.confirm_device("s", txn["transaction_id"], gen)
        restored = fx.build_manager(self.provider, self.state, protector=protector)[0].ensure("vscode")
        self.assertEqual((restored.status, restored.account["login"], restored.source),
                         ("ready", "devuser", "device"))

    def test_sign_out_survives_restart_without_resurrection(self):
        manager = self.manager(legacy={"mode": "vscode", "token": fx.TOKENS["legacy"]},
                               environ={"GH_TOKEN": fx.TOKENS["env"]})
        vs = manager.ensure("vscode")
        cli = manager.ensure("cli")
        self.assertEqual(vs.status, "ready")
        out = manager.sign_out("vscode", vs.generation)
        self.assertEqual(out["status"], "signed_out")
        restarted = fx.build_manager(self.provider, self.state, environ={"GH_TOKEN": fx.TOKENS["env"]})[0]
        self.assertEqual(restarted.ensure("vscode").status, "signed_out")
        self.assertEqual(restarted.ensure("cli").account["login"], cli.account["login"])
        self.assertTrue(pathlib.Path(manager.legacy_token_file).exists())
        again = restarted.use_default("vscode", restarted.ensure("vscode").generation)
        self.assertEqual((again["status"], again["source"]), ("ready", "legacy_vscode"))
        self.assertNotIn("vscode", json.loads((self.state / ".demo-auth.json").read_text())["modes"])

    def test_corrupt_store_blocks_without_default_or_overwrite(self):
        store = self.state / ".demo-auth.json"
        store.write_text("{corrupt")
        manager = self.manager(environ={"GH_TOKEN": fx.TOKENS["env"]})
        state = manager.ensure("cli")
        self.assertEqual((state.status, state.error["category"]), ("error", "persistence_failure"))
        self.assertIsNone(state.secret)
        with self.assertRaises(AuthError) as cm:
            manager.select_gh("cli", "github.com", "bob", state.generation)
        self.assertEqual(cm.exception.category, "persistence_failure")
        self.assertEqual(store.read_text(), "{corrupt")

    def test_protection_failure_keeps_prior(self):
        manager = self.manager(protector=fx.MemoryProtector(fail=True))
        manager.select_gh("cli", "github.com", "bob", manager.ensure("cli").generation)
        before = (self.state / ".demo-auth.json").read_bytes()
        self.provider.configure(device_script=["token:device"])
        clock = fx.FakeClock()
        manager.clock = clock
        gen = manager.ensure("cli").generation
        txn = manager.start_device("s", "cli")
        clock.advance(5)
        manager.poll_device("s", txn["transaction_id"])
        with self.assertRaises(AuthError) as cm:
            manager.confirm_device("s", txn["transaction_id"], gen)
        self.assertEqual(cm.exception.category, "persistence_failure")
        self.assertEqual(manager.ensure("cli").account["login"], "bob")
        self.assertEqual((self.state / ".demo-auth.json").read_bytes(), before)
        self.assertEqual([p.name for p in self.state.iterdir() if p.suffix == ".tmp"], [])

    def test_write_failure_keeps_prior(self):
        manager = self.manager()
        gen = manager.ensure("cli").generation
        original = demo_auth.os.replace

        def failing_replace(*a):
            raise PermissionError("denied")

        demo_auth.os.replace = failing_replace
        try:
            with self.assertRaises(AuthError) as cm:
                manager.select_gh("cli", "github.com", "bob", gen)
        finally:
            demo_auth.os.replace = original
        self.assertEqual(cm.exception.category, "persistence_failure")
        self.assertEqual(manager.ensure("cli").account["login"], "alice")
        self.assertEqual([p.name for p in self.state.iterdir() if p.suffix == ".tmp"], [])

    def test_symlink_store_refused(self):
        target = self.state / "elsewhere.json"
        target.write_text(json.dumps({"version": 1, "modes": {}}))
        link = self.state / ".demo-auth.json"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not permitted on this host")
        state = self.manager().ensure("cli")
        self.assertEqual(state.error["category"], "persistence_failure")
        self.assertIn("symbolic link", state.error["message"])

    def test_posix_permission_checks(self):
        def st(mode, uid=1000):
            return types.SimpleNamespace(st_mode=mode, st_uid=uid)
        self.assertIsNone(demo_auth.check_posix_file(st(stat.S_IFREG | 0o600), 1000))
        self.assertIn("other users", demo_auth.check_posix_file(st(stat.S_IFREG | 0o644), 1000))
        self.assertIn("another user", demo_auth.check_posix_file(st(stat.S_IFREG | 0o600, 0), 1000))
        self.assertIn("symbolic", demo_auth.check_posix_file(st(stat.S_IFLNK | 0o600), 1000))
        self.assertIn("regular", demo_auth.check_posix_file(st(stat.S_IFDIR | 0o700), 1000))

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI only")
    def test_real_dpapi_round_trip_with_fake_secret(self):
        protector = demo_auth.DpapiProtector()
        self.assertEqual(protector.scheme, "dpapi-user")
        blob = protector.protect(fx.TOKENS["device"].encode())
        self.assertNotIn(fx.CANARY, blob)
        self.assertEqual(protector.unprotect(blob).decode(), fx.TOKENS["device"])
        with self.assertRaises(AuthError):
            protector.unprotect("AAAA" + blob[4:8] + "garbage")
        store = demo_auth.AuthStore(self.state / "dpapi.json", protector)
        store.save({"cli": {"kind": "device", "host": "github.com", "login": "devuser",
                            "user_id": 1005, "expires_at": None,
                            "secret": demo_auth.Secret(fx.TOKENS["device"])}})
        self.assertNotIn(fx.CANARY, (self.state / "dpapi.json").read_text())
        self.assertEqual(store.load()["cli"]["secret"].reveal(), fx.TOKENS["device"])


if __name__ == "__main__":
    unittest.main()
