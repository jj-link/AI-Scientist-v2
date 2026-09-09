"""OAuth lifecycle and vault boundaries, without contacting an account or OS vault."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import sys
import types
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx

from ai_scientist import codex_auth
from ai_scientist.codex_auth import CodexAuth, CodexAuthError


def access_token(account="fixture-account"):
    payload = json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": account}}).encode()
    return "fixture." + base64.urlsafe_b64encode(payload).rstrip(b"=").decode() + ".fixture"


class Vault:
    def __init__(self):
        self.values = {}
        self.failure = None

    def get_password(self, service, user):
        if self.failure == "get":
            raise RuntimeError("fixture-secret-from-vault")
        return self.values.get((service, user))

    def set_password(self, service, user, value):
        if self.failure == "set":
            raise RuntimeError("fixture-secret-from-vault")
        if len(value.encode("utf-16-le")) > 2560:
            raise RuntimeError("Credential blob exceeds Windows size limit")
        self.values[(service, user)] = value

    def delete_password(self, service, user):
        if self.failure == "delete":
            raise RuntimeError("fixture-secret-from-vault")
        del self.values[(service, user)]


class CodexAuthentication(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / "ui_data" / "checks" / str(uuid4())
        self.root.mkdir(parents=True)
        self.vault = Vault()
        self.requests = []
        self.respond = lambda request: httpx.Response(200, json={
            "access_token": access_token(), "refresh_token": "fixture-refresh-rotated", "expires_in": 3600,
        })
        self.transport = httpx.MockTransport(self.exchange)
        self.controllers = []
        self.auth = self.controller()
        self.servers = []
        server_class = codex_auth._CallbackServer

        def fixture_listener(address, handler):
            server = server_class(("127.0.0.1", 0), handler)
            self.servers.append(server)
            return server

        # The production redirect/Host stay fixed; fixtures alone use an ephemeral
        # loopback port, never competing with the user's real OAuth listener.
        self.listener_patch = patch.object(codex_auth, "_CallbackServer", side_effect=fixture_listener)
        self.listener_patch.start()

    def tearDown(self):
        for controller in self.controllers:
            controller.close()
        self.listener_patch.stop()
        shutil.rmtree(self.root)

    def controller(self, **kwargs):
        controller = CodexAuth(store=self.vault, transport=self.transport,
                               namespace="fixture-" + self.root.name,
                               lock_path=self.root / "oauth.lock", **kwargs)
        self.controllers.append(controller)
        return controller

    def exchange(self, request):
        self.assertEqual(str(request.url), "https://auth.openai.com/oauth/token")
        self.requests.append(parse_qs(request.content.decode()))
        return self.respond(request)

    def begin(self, controller=None):
        result = (controller or self.auth).begin_login()
        return parse_qs(urlsplit(result["authorization_url"]).query), self.servers[-1].server_port

    def callback(self, port, state, *, code="fixture-code", path="/auth/callback", host="localhost:1455", origin=None):
        headers = {"Host": host}
        if origin is not None:
            headers["Origin"] = origin
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request("GET", path + "?" + urlencode({"state": state, "code": code}), headers=headers)
            response = connection.getresponse()
            return response.status, response.read().decode()
        finally:
            connection.close()

    def seed(self, *, expires_at=0):
        # Fixture records only; production never exposes refresh tokens.
        self.auth._save({
            "access_token": access_token(), "refresh_token": "fixture-refresh-original",
            "account_id": "fixture-account", "expires_at": expires_at,
        })

    def assert_listener_closed(self, port):
        with self.assertRaises(OSError):
            self.callback(port, "irrelevant")

    def test_success_uses_pkce_and_publishes_only_safe_metadata(self):
        params, port = self.begin()
        self.assertEqual(params["redirect_uri"], ["http://localhost:1455/auth/callback"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        status, body = self.callback(port, params["state"][0], origin="https://auth.openai.com")
        self.assertEqual(status, 200)
        verifier = self.requests[0]["code_verifier"][0]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(params["code_challenge"], [challenge])
        self.assertGreaterEqual(len(verifier), 43)
        self.assertEqual(self.auth.credentials(), {"access_token": access_token(), "account_id": "fixture-account"})
        metadata = self.auth.status()
        self.assertTrue(metadata["connected"])
        self.assertFalse(metadata["pending"])
        self.assertIsNone(metadata["error"])
        for secret in (access_token(), "fixture-refresh-rotated", "fixture-code", verifier):
            self.assertNotIn(secret, json.dumps(metadata) + body)
        self.assert_listener_closed(port)

    def test_bad_state_host_origin_and_path_do_not_consume_valid_attempt(self):
        params, port = self.begin()
        state = params["state"][0]
        self.assertEqual(self.callback(port, "wrong-state")[0], 400)
        self.assertEqual(self.callback(port, state, host="attacker.example:1455")[0], 403)
        self.assertEqual(self.callback(port, state, origin="https://attacker.example")[0], 403)
        self.assertEqual(self.callback(port, state, path="/unexpected")[0], 404)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.vault.values)
        self.assertTrue(self.auth.status()["pending"])
        self.assertEqual(self.callback(port, state)[0], 200)

    def test_state_is_single_use_while_exchange_is_still_in_flight(self):
        entered, release = threading.Event(), threading.Event()

        def blocked(request):
            entered.set()
            self.assertTrue(release.wait(3))
            return httpx.Response(200, json={"access_token": access_token(), "refresh_token": "rotated", "expires_in": 3600})

        self.respond = blocked
        params, port = self.begin()
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(self.callback, port, params["state"][0])
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.callback(port, params["state"][0])[0], 409)
                self.assertEqual(len(self.requests), 1)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=3)[0], 200)

    def test_cancel_logout_and_close_discard_late_exchange_results(self):
        for action in ("cancel_login", "logout", "close"):
            with self.subTest(action=action):
                controller = self.controller()
                entered, release = threading.Event(), threading.Event()

                def blocked(request):
                    entered.set()
                    self.assertTrue(release.wait(3))
                    return httpx.Response(200, json={"access_token": access_token(), "refresh_token": "late-secret", "expires_in": 3600})

                self.respond = blocked
                params, port = self.begin(controller)
                with ThreadPoolExecutor(max_workers=1) as executor:
                    result = executor.submit(self.callback, port, params["state"][0])
                    try:
                        self.assertTrue(entered.wait(3))
                        getattr(controller, action)()
                        self.assert_listener_closed(port)
                    finally:
                        release.set()
                    self.assertEqual(result.result(timeout=3)[0], 409)
                self.assertFalse(self.vault.values)
                self.assertFalse(controller.status()["connected"])
                self.assertFalse(controller.status()["pending"])

    def test_cancelled_state_cannot_authorize_a_new_attempt(self):
        old, old_port = self.begin()
        self.auth.cancel_login()
        params, port = self.begin()
        self.assertNotEqual(old["state"], params["state"])
        self.assertEqual(self.callback(port, old["state"][0])[0], 400)
        self.assertEqual(self.callback(port, params["state"][0])[0], 200)

    def test_close_allows_later_app_context_to_sign_in(self):
        params, port = self.begin()
        self.auth.close()
        self.assert_listener_closed(port)
        params, port = self.begin()
        self.assertEqual(self.callback(port, params["state"][0], host="127.0.0.1:1455")[0], 200)
        self.auth.close()
        self.assertEqual(self.auth.credentials()["account_id"], "fixture-account")

    def test_timeout_discards_an_exchange_that_finishes_late(self):
        controller = self.controller(login_timeout=0.5)
        entered, release = threading.Event(), threading.Event()

        def blocked(request):
            entered.set()
            self.assertTrue(release.wait(3))
            return httpx.Response(200, json={"access_token": access_token(),
                                            "refresh_token": "late-secret", "expires_in": 3600})

        self.respond = blocked
        params, port = self.begin(controller)
        timer = controller._attempt.timer
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(self.callback, port, params["state"][0])
            try:
                self.assertTrue(entered.wait(3))
                timer.join(timeout=3)
                self.assert_listener_closed(port)
            finally:
                release.set()
            self.assertEqual(result.result(timeout=3)[0], 409)
        self.assertFalse(self.vault.values)
        self.assertFalse(controller.status()["pending"])

    def test_timeout_closes_listener_and_preserves_no_credentials(self):
        controller = self.controller(login_timeout=0.2)
        params, port = self.begin(controller)
        controller._attempt.timer.join(timeout=3)
        metadata = controller.status()
        self.assertFalse(metadata["pending"])
        self.assertFalse(metadata["connected"])
        self.assertIn("timed out", metadata["error"])
        self.assert_listener_closed(port)
        self.assertEqual(self.requests, [])

    def test_large_oauth_session_survives_new_controller_and_is_fully_removed_on_logout(self):
        token = access_token() + "a" * 8192
        self.respond = lambda request: httpx.Response(200, json={
            "access_token": token, "refresh_token": "r" * 4096, "expires_in": 3600,
        })
        params, port = self.begin()
        self.assertEqual(self.callback(port, params["state"][0])[0], 200)
        reader = self.controller()
        self.assertTrue(reader.status()["connected"])
        self.assertEqual(reader.credentials(), {"access_token": token, "account_id": "fixture-account"})
        self.assertFalse(reader.logout()["connected"])
        self.assertFalse(self.vault.values)

    def test_interrupted_vault_rotation_recovers_only_the_committed_session(self):
        class ProcessInterrupted(BaseException):
            pass

        for operation in ("set_password", "delete_password"):
            with self.subTest(operation=operation):
                self.seed(expires_at=time.time() + 3600)
                original = getattr(self.vault, operation)

                def interrupt(service, user, *args):
                    if (operation == "set_password" and service == self.auth._namespace
                            or operation == "delete_password" and not service.endswith(".pending")):
                        raise ProcessInterrupted()
                    return original(service, user, *args)

                new_token = access_token() + "b" * 8192
                with patch.object(self.vault, operation, side_effect=interrupt):
                    with self.assertRaises(ProcessInterrupted):
                        self.auth._save({
                            "access_token": new_token, "refresh_token": "new-refresh" * 400,
                            "account_id": "fixture-account", "expires_at": time.time() + 3600,
                        })
                reader = self.controller()
                expected = access_token() if operation == "set_password" else new_token
                self.assertEqual(reader.credentials(), {"access_token": expected, "account_id": "fixture-account"})
                reader.logout()
                self.assertFalse(self.vault.values)

    def test_vault_write_failure_never_announces_success_or_leaks_error(self):
        params, port = self.begin()
        self.vault.failure = "set"
        status, body = self.callback(port, params["state"][0])
        self.assertEqual(status, 503)
        self.assertFalse(self.vault.values)
        metadata = self.auth.status()
        self.assertFalse(metadata["connected"])
        self.assertFalse(metadata["pending"])
        self.assertIn("vault", metadata["error"])
        self.assertNotIn("fixture-secret-from-vault", body + json.dumps(metadata))
        self.assert_listener_closed(port)

    def test_vault_read_and_delete_errors_are_safe_and_do_not_fake_logout(self):
        self.vault.failure = "get"
        with self.assertRaises(CodexAuthError) as raised:
            self.auth.begin_login()
        self.assertNotIn("fixture-secret-from-vault", str(raised.exception))
        self.assertEqual(self.servers, [])
        self.assertFalse(self.auth.status()["connected"])
        self.vault.failure = None
        self.seed(expires_at=time.time() + 3600)
        self.vault.failure = "delete"
        with self.assertRaises(CodexAuthError) as raised:
            self.auth.logout()
        self.assertNotIn("fixture-secret-from-vault", str(raised.exception))
        self.assertTrue(self.auth.status()["connected"])

    def test_non_os_keyring_is_rejected_without_reading_it(self):
        class PlaintextBackend:
            def get_password(self, *args):
                self.fail = True
                raise AssertionError("Untrusted backend must not be read")

        backend = PlaintextBackend()
        controller = CodexAuth(namespace="fixture", lock_path=self.root / "other.lock")
        self.controllers.append(controller)
        fake_keyring = types.ModuleType("keyring")
        fake_keyring.get_keyring = lambda: backend
        with patch.dict(sys.modules, {"keyring": fake_keyring}), \
                patch("keyring.get_keyring", return_value=backend):
            with self.assertRaises(CodexAuthError):
                controller.begin_login()
            self.assertFalse(controller.status()["connected"])
        self.assertFalse(hasattr(backend, "fail"))
        self.assertEqual(self.servers, [])

    def test_refresh_rotation_is_shared_and_reread_under_lock(self):
        self.seed()
        second = self.controller()
        entered, release, second_acquiring = threading.Event(), threading.Event(), threading.Event()
        original_acquire = codex_auth.FileLock.acquire

        def acquire(lock, *args, **kwargs):
            if threading.current_thread().name.startswith("refresh-second"):
                second_acquiring.set()
            return original_acquire(lock, *args, **kwargs)

        def blocked(request):
            entered.set()
            self.assertTrue(release.wait(3))
            return httpx.Response(200, json={"access_token": access_token("new-account"),
                                            "refresh_token": "new-refresh", "expires_in": 3600})

        self.respond = blocked
        with patch.object(codex_auth.FileLock, "acquire", acquire):
            with ThreadPoolExecutor(max_workers=1) as first_pool, ThreadPoolExecutor(max_workers=1, thread_name_prefix="refresh-second") as second_pool:
                first = first_pool.submit(self.auth.credentials)
                try:
                    self.assertTrue(entered.wait(3))
                    other = second_pool.submit(second.credentials)
                    self.assertTrue(second_acquiring.wait(3))
                finally:
                    release.set()
                first_value = first.result(timeout=3)
                self.assertEqual(other.result(timeout=3), first_value)
        self.assertEqual(first_value["account_id"], "new-account")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["refresh_token"], ["fixture-refresh-original"])
        # A newly constructed worker sees the rotated record, not a cached token.
        self.assertEqual(self.controller().credentials(), first_value)

    def test_logout_invalidates_an_inflight_refresh_before_persistence(self):
        self.seed()
        entered, release, deleting = threading.Event(), threading.Event(), threading.Event()
        original_acquire = codex_auth.FileLock.acquire

        def acquire(lock, *args, **kwargs):
            if threading.current_thread().name.startswith("logout-worker"):
                deleting.set()
            return original_acquire(lock, *args, **kwargs)

        def blocked(request):
            entered.set()
            self.assertTrue(release.wait(3))
            return httpx.Response(200, json={"access_token": access_token(),
                                            "refresh_token": "late-rotation", "expires_in": 3600})

        self.respond = blocked
        with patch.object(codex_auth.FileLock, "acquire", acquire):
            with ThreadPoolExecutor(max_workers=1) as refresh_pool, ThreadPoolExecutor(max_workers=1, thread_name_prefix="logout-worker") as logout_pool:
                refreshing = refresh_pool.submit(self.auth.credentials)
                try:
                    self.assertTrue(entered.wait(3))
                    logging_out = logout_pool.submit(self.auth.logout)
                    self.assertTrue(deleting.wait(3))
                finally:
                    release.set()
                with self.assertRaises(CodexAuthError):
                    refreshing.result(timeout=3)
                self.assertFalse(logging_out.result(timeout=3)["connected"])
        self.assertFalse(self.vault.values)

    def test_revoked_refresh_disconnects_without_returning_upstream_details(self):
        self.seed()
        self.respond = lambda request: httpx.Response(400, json={"error": "invalid_grant", "error_description": "fixture-upstream-secret"})
        with self.assertRaises(CodexAuthError) as raised:
            self.auth.credentials()
        self.assertNotIn("fixture-upstream-secret", str(raised.exception))
        self.assertFalse(self.auth.status()["connected"])
        self.assertFalse(self.vault.values)

    def test_network_failure_does_not_erase_refresh_grant(self):
        self.seed()

        def unavailable(request):
            raise httpx.ReadTimeout("fixture-upstream-secret", request=request)

        self.respond = unavailable
        with self.assertRaises(CodexAuthError) as raised:
            self.auth.credentials()
        self.assertNotIn("fixture-upstream-secret", str(raised.exception))
        self.assertTrue(self.auth.status()["connected"])
        self.assertTrue(self.vault.values)

    def test_missing_account_metadata_cannot_persist_credentials(self):
        params, port = self.begin()
        self.respond = lambda request: httpx.Response(200, json={
            "access_token": "not-a-jwt", "refresh_token": "fixture-refresh", "expires_in": 3600,
        })
        self.assertEqual(self.callback(port, params["state"][0])[0], 503)
        self.assertFalse(self.vault.values)
        self.assertFalse(self.auth.status()["connected"])


if __name__ == "__main__":
    unittest.main()
