"""Codex enrollment, provider validation, and real adapter integration boundaries."""
import asyncio
import json
import os
import time
from unittest.mock import patch

import httpx
import yaml
from fastapi.testclient import TestClient

from ai_scientist import codex_provider, model_routing
from ai_scientist.codex_auth import CodexAuth
from ai_scientist.ui.app import create_app
from ai_scientist.ui.configs import Configs
from ai_scientist.ui.diagnostics import CrashAssistant
from ai_scientist.ui.store import Store
from test_codex_auth import Vault, access_token
from test_codex_provider import event, sse, terminal
from test_ui_model_editor import EditorBase, minimal_config


def provider_config():
    config = minimal_config()
    config["endpoints"]["codex"] = {
        "provider": "openai-codex", "label": "Codex", "provides": ["text", "vision", "function_calling"]
    }
    config["roles"]["ideation"].update(max_tokens=4096, temperature=0.4, api_key_env="FIXTURE_KEY")
    return config


class CodexStudioTests(EditorBase):
    def connected_auth(self):
        vault = Vault()
        auth = CodexAuth(store=vault, namespace=str(self.root), lock_path=self.root / "auth.lock")
        auth._save({"access_token": access_token(), "refresh_token": "fixture-refresh-secret",
                    "account_id": "fixture-account", "expires_at": time.time() + 3600})
        self.addCleanup(auth.close)
        return auth

    def test_provider_switch_requires_clearing_unsupported_overrides_and_preserves_other_preset(self):
        config = provider_config()
        target = self.write_preset(cfg=config)
        other = self.write_preset("ais_roles.other.yaml", config)
        original, other_bytes = target.read_bytes(), other.read_bytes()
        app = create_app(self.root)
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            headers = self.editor_headers(client)
            selected = self.preset_id(client, "ais_roles.yaml")
            before = client.get(f"/api/models/editor?config_id={selected}").json()
            body = {"config_id": selected, "expected_revision": before["revision"],
                    "roles": {"ideation": {"endpoint": "codex", "model": "account-model"}}}
            rejected = client.patch("/api/models/editor", json=body, headers=headers)
            self.assertEqual(rejected.status_code, 422, rejected.text)
            self.assertEqual({error["field"] for error in rejected.json()["detail"]["errors"]},
                             {"roles.ideation.max_tokens", "roles.ideation.temperature", "roles.ideation.api_key_env"})
            self.assertEqual(target.read_bytes(), original)
            body["roles"]["ideation"].update(max_tokens=None, temperature=None, api_key_env=None)
            accepted = client.patch("/api/models/editor", json=body, headers=headers)
            self.assertEqual(accepted.status_code, 200, accepted.text)
            snapshot = app.state.configs.diagnostic_assignment(selected, "ideation")
            self.assertEqual(snapshot["provider"], "openai-codex")
            self.assertIsNone(snapshot["max_tokens"])
            self.assertIsNone(snapshot["temperature"])
            after = accepted.json()
            back = client.patch("/api/models/editor", headers=headers, json={
                "config_id": selected, "expected_revision": after["revision"],
                "roles": {"ideation": {"endpoint": "alpha", "model": "m1"}}})
            self.assertEqual(back.status_code, 200, back.text)
            self.assertEqual(snapshot["provider"], "openai-codex")
            self.assertEqual(snapshot["model"], "account-model")
        self.assertEqual(other.read_bytes(), other_bytes)
        on_disk = yaml.safe_load(target.read_bytes())
        self.assertNotIn("base_url", on_disk["endpoints"]["codex"])
        self.assertNotIn("max_tokens", on_disk["roles"]["ideation"])

    def test_codex_cannot_redirect_oauth_credentials_through_endpoint_edits(self):
        target = self.write_preset(cfg=provider_config())
        original = target.read_bytes()
        app = create_app(self.root)
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            headers = self.editor_headers(client)
            selected = self.preset_id(client, "ais_roles.yaml")
            before = client.get(f"/api/models/editor?config_id={selected}").json()
            for patch_body in ({"base_url": "https://attacker.invalid/v1"}, {"api_key_env": "ATTACKER_KEY"}):
                with self.subTest(patch=patch_body):
                    response = client.patch("/api/models/editor", json={
                        "config_id": selected, "expected_revision": before["revision"],
                        "endpoints": {"codex": patch_body}}, headers=headers)
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(target.read_bytes(), original)
        unsafe = provider_config()
        unsafe["endpoints"]["codex"]["base_url"] = "https://attacker.invalid/v1"
        unsafe["roles"]["ideation"] = {"endpoint": "codex", "model": "account-model"}
        self.write_preset(cfg=unsafe)
        with patch.dict(os.environ, {model_routing.ROLE_CONFIG_ENV: str(target)}):
            with self.assertRaises(model_routing.RoleConfigError):
                model_routing.create_selfhosted_client("role/ideation")

    def test_auth_mutations_require_origin_token_and_empty_body_without_exposing_credentials(self):
        self.write_preset()
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
        config = provider_config()
        config["roles"]["ideation"] = {"endpoint": "codex", "model": "account-model"}
        target = self.write_preset(cfg=config)
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
        with patch.dict(os.environ, {model_routing.ROLE_CONFIG_ENV: str(target)}), \
                patch("ai_scientist.codex_provider.get_auth", return_value=auth), \
                patch("httpx.Client", side_effect=lambda **kwargs: sync_type(**{**kwargs, "transport": transport})), \
                patch("httpx.AsyncClient", side_effect=lambda **kwargs: async_type(**{**kwargs, "transport": transport})):
            client = model_routing.create_selfhosted_client("role/ideation")
            try:
                result = client.chat.completions.create(model="account-model", messages=[{"role": "user", "content": "Question"}])
                self.assertEqual(result.choices[0].message.content, "Account-authenticated answer")
            finally:
                client.close()
            configs = Configs(self.root)
            assignment = configs.diagnostic_assignment(configs.presets()["selected_role_config_id"], "ideation")
            assistant = CrashAssistant(Store(self.root), configs)
            self.assertEqual(asyncio.run(assistant._complete(assignment, "{}")), "Account-authenticated answer")
