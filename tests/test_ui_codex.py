"""Codex enrollment, provider validation, and real adapter integration boundaries."""
import asyncio
import json
import os
import time
from unittest.mock import patch

import httpx
from ai_scientist.ui import model_settings
from fastapi.testclient import TestClient

from ai_scientist import codex_provider, model_routing
from ai_scientist.codex_auth import CodexAuth
from ai_scientist.ui.app import create_app
from ai_scientist.ui.configs import Configs
from ai_scientist.ui.diagnostics import CrashAssistant
from ai_scientist.ui.store import Store
from test_codex_auth import Vault, access_token
from test_codex_provider import event, sse, terminal
from test_ui_model_editor import EditorBase, alpha_server, ideation_task


class CodexStudioTests(EditorBase):
    def connected_auth(self):
        vault = Vault()
        auth = CodexAuth(store=vault, namespace=str(self.root), lock_path=self.root / "auth.lock")
        auth._save({"access_token": access_token(), "refresh_token": "fixture-refresh-secret",
                    "account_id": "fixture-account", "expires_at": time.time() + 3600})
        self.addCleanup(auth.close)
        return auth

    def test_provider_switch_requires_clearing_unsupported_overrides(self):
        self.seed({"alpha": alpha_server(), "codex": {
            "api_format": "openai-codex", "capabilities": ["text"]}},
            {"ideation": ideation_task(max_tokens=4096, temperature=0.4,
                                       credential_env="FIXTURE_KEY")})
        app = create_app(self.root)
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            headers = self.editor_headers(client)
            before = client.get("/api/models/editor").json()
            body = {"expected_revision": before["revision"],
                    "roles": {"ideation": {"endpoint": "codex", "model": "account-model"}}}
            rejected = client.patch("/api/models/editor", json=body, headers=headers)
            self.assertEqual(rejected.status_code, 422, rejected.text)
            self.assertIn("managed_by_provider",
                          {error["code"] for error in rejected.json()["detail"]["errors"]})
            self.assertEqual(client.get("/api/models/editor").json(), before)
            body["roles"]["ideation"].update(max_tokens=None, temperature=None, api_key_env=None)
            accepted = client.patch("/api/models/editor", json=body, headers=headers)
            self.assertEqual(accepted.status_code, 200, accepted.text)
            snapshot = app.state.configs.diagnostic_assignment("ideation")
            self.assertEqual(snapshot["provider"], "openai-codex")
            self.assertIsNone(snapshot["max_tokens"])
            self.assertIsNone(snapshot["temperature"])
            after = accepted.json()
            back = client.patch("/api/models/editor", headers=headers, json={
                "expected_revision": after["revision"],
                "roles": {"ideation": {"endpoint": "alpha", "model": "m1"}}})
            self.assertEqual(back.status_code, 200, back.text)
            self.assertEqual(snapshot["provider"], "openai-codex")
            self.assertEqual(snapshot["model"], "account-model")
        saved = model_settings.current_settings(self.root)
        self.assertIsNone(saved["endpoints"]["codex"]["base_url"])
        self.assertNotIn("max_tokens", saved["roles"]["ideation"])

    def test_codex_cannot_redirect_oauth_credentials_through_endpoint_edits(self):
        self.seed({"alpha": alpha_server(), "codex": {
            "api_format": "openai-codex", "capabilities": ["text"]}})
        app = create_app(self.root)
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            headers = self.editor_headers(client)
            before = client.get("/api/models/editor").json()
            for patch_body in ({"base_url": "https://attacker.invalid/v1"}, {"api_key_env": "ATTACKER_KEY"}):
                with self.subTest(patch=patch_body):
                    response = client.patch("/api/models/editor", json={
                        "expected_revision": before["revision"],
                        "endpoints": {"codex": patch_body}}, headers=headers)
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(client.get("/api/models/editor").json(), before)
        model_settings.save_settings_atomic(self.root, {"codex": {
            "api_format": "openai-codex", "address": "https://attacker.invalid/v1",
            "capabilities": ["text"]}},
            {"ideation": {"server": "codex", "model": "account-model"}})
        with self.assertRaises(model_routing.RoleConfigError):
            model_routing.create_selfhosted_client("role/ideation")

    def test_auth_mutations_require_origin_token_and_empty_body_without_exposing_credentials(self):
        self.seed()
        auth = self.connected_auth()
        with patch("ai_scientist.ui.app.get_auth", return_value=auth):
            app = create_app(self.root)
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            headers = self.editor_headers(client)
            url = "/api/providers/codex/logout"
            for unsafe in ({}, {"Origin": "https://attacker.invalid", "X-Studio-Token": headers["X-Studio-Token"]}):
                self.assertEqual(client.post(url, headers=unsafe, json={}).status_code, 403)
                self.assertTrue(client.get("/api/providers/codex").json()["connected"])
            rejected = client.post(url, headers=headers, json={"token": "fixture-refresh-secret"})
            self.assertEqual(rejected.status_code, 422)
            self.assertNotIn("fixture-refresh-secret", rejected.text)
            status = client.get("/api/providers/codex")
            self.assertNotIn(access_token(), status.text)
            self.assertNotIn("fixture-refresh-secret", status.text)
            self.assertEqual(status.headers["cache-control"], "no-store")
            response = client.post(url, headers=headers, json={})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(client.get("/api/providers/codex").json()["connected"])
            self.assertFalse(auth.status()["connected"])

    def test_routed_and_assistant_requests_use_codex_transport(self):
        self.seed({"codex": {"api_format": "openai-codex", "capabilities": ["text"]}},
                  {"ideation": {"server": "codex", "model": "account-model"}})
        auth = self.connected_auth()

        def upstream(request):
            if (str(request.url) != codex_provider.CODEX_BASE_URL + "/responses" or
                request.headers.get("authorization") != "Bearer " + access_token() or
                request.headers.get("chatgpt-account-id") != "fixture-account"):
                return httpx.Response(401)
            payload = json.loads(request.content)
            if payload.get("store") is not False or payload.get("stream") is not True:
                return httpx.Response(400)
            return sse(event("response.completed", terminal("Account-authenticated answer")))

        transport = httpx.MockTransport(upstream)
        sync_type, async_type = httpx.Client, httpx.AsyncClient
        with patch("ai_scientist.codex_provider.get_auth", return_value=auth), \
                patch("httpx.Client", side_effect=lambda **kwargs: sync_type(**{**kwargs, "transport": transport})), \
                patch("httpx.AsyncClient", side_effect=lambda **kwargs: async_type(**{**kwargs, "transport": transport})):
            client = model_routing.create_selfhosted_client("role/ideation")
            try:
                result = client.chat.completions.create(model="account-model", messages=[{"role": "user", "content": "Question"}])
                self.assertEqual(result.choices[0].message.content, "Account-authenticated answer")
            finally:
                client.close()
            configs = Configs(self.root)
            assignment = configs.diagnostic_assignment("ideation")
            assistant = CrashAssistant(Store(self.root), configs)
            self.assertEqual(asyncio.run(assistant._complete(assignment, "{}")), "Account-authenticated answer")
