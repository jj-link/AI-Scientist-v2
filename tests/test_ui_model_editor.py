"""Studio model settings editor: revision safety, field validation, isolation.

Model settings live in ``<root>/ui_data/ui.sqlite3`` (tables ``model_servers``,
``task_models``, ``model_settings_extra``). The editor reads and patches those
rows over HTTP with an optimistic-concurrency ``revision`` digest. These tests
exercise the HTTP routes and the ``Configs`` methods behind them, including
credential safety, provider (cborg/codex) rules, and snapshot semantics.
"""

import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from ai_scientist import model_routing
from ai_scientist.ui import model_settings
from ai_scientist.ui.app import create_app
from ai_scientist.ui.configs import Configs, EditorConflict, InvalidConfiguration

HEADERS = lambda token: {  # noqa: E731
    "Origin": "http://127.0.0.1:8765",
    "X-Studio-Token": token,
}

ALPHA_URL = "http://127.0.0.1:9/v1"
BETA_URL = "http://127.0.0.1:10/v1"


def alpha_server(**overrides):
    server = {"api_format": "openai", "address": ALPHA_URL,
              "credential_env": "ALPHA_KEY", "timeout": 600,
              "capabilities": ["text"], "requires_user_message": False}
    server.update(overrides)
    return server


def ideation_task(**overrides):
    task = {"server": "alpha", "model": "m1", "requires": ["text"]}
    task.update(overrides)
    return task


class EditorBase(unittest.TestCase):
    def setUp(self):
        self.root = (
            Path(__file__).resolve().parents[1] / "ui_data" / "checks" / str(uuid4())
        )
        self.root.mkdir(parents=True)
        os.environ["AI_SCIENTIST_ROOT"] = str(self.root)
        model_routing._cache.clear()

    def tearDown(self):
        os.environ.pop("AI_SCIENTIST_ROOT", None)
        model_routing._cache.clear()
        shutil.rmtree(self.root, ignore_errors=True)

    def seed(self, servers=None, tasks=None):
        """Write already-shaped rows straight into this test's database."""
        model_settings.ensure_schema(self.root)
        if servers or tasks:
            model_settings.save_settings_atomic(self.root, servers or {}, tasks or {})

    def standard_seed(self):
        self.seed({"alpha": alpha_server()}, {"ideation": ideation_task()})
        return Configs(self.root).editor()

    def make_client(self):
        return TestClient(create_app(self.root), base_url="http://127.0.0.1:8765")

    def editor_headers(self, client):
        token = client.get("/api/bootstrap").json()["request_token"]
        return HEADERS(token)

    def patch_editor(self, client, headers, *, revision, roles=None, endpoints=None,
                     **extra):
        body = {"expected_revision": revision,
                "roles": roles or {}, "endpoints": endpoints or {}}
        body.update(extra)
        return client.patch("/api/models/editor", json=body, headers=headers)


class EditorApiTests(EditorBase):
    """HTTP contract for the editor routes without touching saved jobs."""

    def test_bootstrap_envelope_reports_settings_not_role_presets(self):
        self.standard_seed()
        with self.make_client() as client:
            bootstrap = client.get("/api/bootstrap").json()
        for key in ("bfts_configs", "selected_bfts_config_id", "prerequisites",
                    "active_job", "request_token", "experiments_directory"):
            self.assertIn(key, bootstrap)
        self.assertNotIn("role_configs", bootstrap)
        self.assertNotIn("selected_role_config_id", bootstrap)
        self.assertEqual(bootstrap["bfts_configs"][0]["id"],
                         bootstrap["selected_bfts_config_id"])

    def test_editor_view_reports_revision_and_configured_fields_only(self):
        before = self.standard_seed()
        with self.make_client() as client:
            data = client.get("/api/models/editor")
            self.assertEqual(data.status_code, 200, data.text)
            view = data.json()
            again = client.get("/api/models/editor").json()
        self.assertRegex(view["revision"], r"\A[0-9a-f]{64}\Z")
        self.assertEqual(view["revision"], before["revision"])
        self.assertEqual(view["revision"], again["revision"])
        # Lossless projection: configured values verbatim, unset fields None.
        self.assertEqual(list(view["endpoints"]), ["alpha"])
        self.assertEqual(view["endpoints"]["alpha"],
                         {"provider": "openai", "base_url": ALPHA_URL,
                          "api_key_env": "ALPHA_KEY", "timeout": 600, "provides": ["text"]})
        self.assertEqual(view["roles"]["ideation"],
                         {"endpoint": "alpha", "model": "m1", "max_tokens": None,
                          "temperature": None, "timeout": None, "api_key_env": None, "requires": ["text"]})
        # Required research tasks are listed even when unassigned.
        self.assertIn("review", view["roles"])
        self.assertIsNone(view["roles"]["review"]["endpoint"])
        self.assertIsNone(view["roles"]["review"]["model"])

    def test_models_view_resolves_provider_defaults_and_credential_presence(self):
        self.seed({"alpha": alpha_server(timeout=None)},
                  {"ideation": ideation_task()})
        configs = Configs(self.root)
        with patch.dict(os.environ):
            os.environ.pop("ALPHA_KEY", None)
            view = configs.models()
            by_endpoint = {row["id"]: row for row in view["endpoints"]}
            self.assertEqual(by_endpoint["alpha"]["url"], ALPHA_URL)
            self.assertEqual(by_endpoint["alpha"]["timeout"],
                             model_routing.DEFAULT_ENDPOINT_TIMEOUT)
            self.assertEqual(by_endpoint["alpha"]["credential"],
                             {"env": "ALPHA_KEY", "present": False})
        with patch.dict(os.environ, {"ALPHA_KEY": "viewer-secret-value"}):
            view = configs.models()
        by_endpoint = {row["id"]: row for row in view["endpoints"]}
        by_role = {row["name"]: row for row in view["roles"]}
        self.assertEqual(by_endpoint["alpha"]["credential"],
                         {"env": "ALPHA_KEY", "present": True})
        # Task credentials inherit the endpoint's environment variable name.
        self.assertEqual(by_role["ideation"]["credential"],
                         {"env": "ALPHA_KEY", "present": True})
        self.assertEqual(by_role["ideation"]["effective_max_tokens"], 4096)
        self.assertNotIn("viewer-secret-value", json.dumps(view))

    def test_patch_applies_only_submitted_changes_and_keeps_unrelated_fields(self):
        before = self.standard_seed()
        with self.make_client() as client:
            headers = self.editor_headers(client)
            response = self.patch_editor(
                client, headers, revision=before["revision"],
                roles={"ideation": {"temperature": 0.5}},
                endpoints={"alpha": {"timeout": 120}})
            self.assertEqual(response.status_code, 200, response.text)
            saved = response.json()
        # The response is a fresh editor view, not an echo of the patch.
        self.assertEqual(saved["roles"]["ideation"]["temperature"], 0.5)
        self.assertEqual(saved["roles"]["ideation"]["model"], "m1")
        self.assertIsNone(saved["roles"]["ideation"]["max_tokens"])
        self.assertEqual(saved["endpoints"]["alpha"]["timeout"], 120)
        self.assertEqual(saved["endpoints"]["alpha"]["base_url"], ALPHA_URL)
        self.assertNotEqual(saved["revision"], before["revision"])
        tasks = {row["task"]: row for row in model_settings.list_tasks(self.root)}
        self.assertEqual(tasks["ideation"]["model"], "m1")
        self.assertEqual(tasks["ideation"]["temperature"], 0.5)
        self.assertEqual(tasks["ideation"]["requires"], ["text"])
        servers = {row["name"]: row for row in model_settings.list_servers(self.root)}
        self.assertEqual(servers["alpha"]["address"], ALPHA_URL)
        self.assertEqual(servers["alpha"]["timeout"], 120)
        # Only credential NAMES are persisted, never values.
        self.assertEqual(servers["alpha"]["credential_env"], "ALPHA_KEY")

    def test_patch_requires_current_revision_and_authenticated_origin(self):
        before = self.standard_seed()
        with self.make_client() as client:
            headers = self.editor_headers(client)
            stale = self.patch_editor(client, headers, revision="a" * 64,
                                      roles={"ideation": {"temperature": 1}})
            self.assertEqual(stale.status_code, 409, stale.text)
            detail = stale.json()["detail"]
            self.assertIn("configuration changed", detail["message"])
            self.assertEqual(detail["errors"][0]["code"], "configuration_changed")
            # A write without the trusted origin and request token is refused.
            missing = client.patch("/api/models/editor", json={
                "expected_revision": before["revision"],
                "roles": {"ideation": {"temperature": 1}}, "endpoints": {}})
            self.assertEqual(missing.status_code, 403, missing.text)
        self.assertEqual(model_settings.revision(self.root), before["revision"])

    def test_patch_rejects_unknown_tasks_and_malformed_server_names(self):
        before = self.standard_seed()
        with self.make_client() as client:
            headers = self.editor_headers(client)
            unknown_task = self.patch_editor(
                client, headers, revision=before["revision"],
                roles={"nope": {"model": "m2"}})
            self.assertEqual(unknown_task.status_code, 422, unknown_task.text)
            first = unknown_task.json()["detail"]["errors"][0]
            self.assertEqual(first["code"], "unknown_entry")
            self.assertEqual(first["field"], "roles.nope")
            unpadded = self.patch_editor(
                client, headers, revision=before["revision"],
                endpoints={" beta ": {"base_url": BETA_URL}})
            self.assertEqual(unpadded.status_code, 422, unpadded.text)
            self.assertEqual(unpadded.json()["detail"]["errors"][0]["code"],
                             "unknown_entry")
        self.assertEqual(model_settings.revision(self.root), before["revision"])

    def test_save_editor_rejects_unknown_server_and_task_fields(self):
        before = self.standard_seed()
        configs = Configs(self.root)
        with self.assertRaises(InvalidConfiguration) as caught:
            configs.save_editor(before["revision"], {},
                                {"alpha": {"bogus": 1}})
        self.assertEqual(caught.exception.errors[0]["code"], "unknown_field")
        self.assertEqual(caught.exception.errors[0]["field"],
                         "endpoints.alpha.bogus")
        self.assertEqual(model_settings.revision(self.root), before["revision"])

    def test_patch_validates_endpoint_values_and_never_echoes_secrets(self):
        before = self.standard_seed()
        with self.make_client() as client:
            headers = self.editor_headers(client)
            cases = (
                ({"timeout": -1}, "invalid_number"),
                ({"base_url": "not a url"}, "invalid_url"),
                ({"base_url": ALPHA_URL + "?api_key=hunter2"}, "credential_bearing"),
                ({"base_url": "https://user:hunter2@example.com/v1"}, "credential_bearing"),
                ({"api_key_env": "sk-plaintext-secret"}, "invalid_env_name"),
            )
            for endpoint_patch, code in cases:
                with self.subTest(patch=endpoint_patch):
                    response = self.patch_editor(
                        client, headers, revision=before["revision"],
                        endpoints={"alpha": endpoint_patch})
                    self.assertEqual(response.status_code, 422, response.text)
                    errors = response.json()["detail"]["errors"]
                    self.assertIn(code, [error["code"] for error in errors])
            credential_url = self.patch_editor(
                client, headers, revision=before["revision"],
                endpoints={"alpha": {"base_url": "https://user:hunter2@example.com/v1"}})
            self.assertNotIn("hunter2", credential_url.text)
            self.assertNotIn("example.com", credential_url.text)
            inline = self.patch_editor(
                client, headers, revision=before["revision"],
                endpoints={"alpha": {"api_key_env": "sk-plaintext-secret"}})
            self.assertEqual(inline.json()["detail"]["errors"][0]["field"],
                             "endpoints.alpha.api_key_env")
            self.assertNotIn("sk-plaintext-secret", inline.text)
        self.assertEqual(model_settings.revision(self.root), before["revision"])

    def test_empty_patch_rejected_without_database_change(self):
        """A save must change something; blank entries are not an edit."""
        before = self.standard_seed()
        with self.make_client() as client:
            headers = self.editor_headers(client)
            empty = self.patch_editor(client, headers, revision=before["revision"])
            self.assertEqual(empty.status_code, 422, empty.text)
            self.assertIn("at least one changed field", empty.json()["detail"]["message"])
            noop_entries = self.patch_editor(
                client, headers, revision=before["revision"],
                roles={"ideation": {}})
            self.assertEqual(noop_entries.status_code, 422, noop_entries.text)
        self.assertEqual(model_settings.revision(self.root), before["revision"])

    def test_patch_validates_final_task_state(self):
        self.seed({"alpha": alpha_server(),
                   "beta": alpha_server(address=BETA_URL, credential_env="BETA_KEY",
                                        capabilities=[])},
                  {"ideation": ideation_task(),
                   "review": {"server": "alpha", "model": "m2", "requires": ["text"]}})
        before = Configs(self.root).editor()
        with self.make_client() as client:
            headers = self.editor_headers(client)
            ghost = self.patch_editor(client, headers, revision=before["revision"],
                                      roles={"review": {"endpoint": "ghost"}})
            self.assertEqual(ghost.status_code, 422, ghost.text)
            self.assertEqual(ghost.json()["detail"]["errors"][0]["code"],
                             "invalid_endpoint_reference")
            mismatched = self.patch_editor(
                client, headers, revision=before["revision"],
                roles={"review": {"endpoint": "beta", "model": "m2"}})
            self.assertEqual(mismatched.status_code, 422, mismatched.text)
            self.assertEqual(mismatched.json()["detail"]["errors"][0]["code"],
                             "capability_mismatch")
            # A server created in the same patch can accept an assignment.
            created = self.patch_editor(
                client, headers, revision=before["revision"],
                endpoints={"gamma": {"base_url": "http://127.0.0.1:11/v1",
                                     "api_key_env": "GAMMA_KEY"}},
                roles={"review": {"endpoint": "gamma", "model": "m3"}})
            self.assertEqual(created.status_code, 200, created.text)
            self.assertEqual(created.json()["endpoints"]["gamma"]["base_url"],
                             "http://127.0.0.1:11/v1")
        tasks = {row["task"]: row for row in model_settings.list_tasks(self.root)}
        self.assertEqual(tasks["review"]["server"], "gamma")
        self.assertEqual(tasks["review"]["model"], "m3")

    def test_delete_tasks_and_servers(self):
        self.seed({"alpha": alpha_server(),
                   "beta": alpha_server(address=BETA_URL, credential_env="BETA_KEY",
                                        capabilities=[])},
                  {"ideation": ideation_task(),
                   "review": {"server": "beta", "model": "m2"}})
        view = Configs(self.root).editor()
        with self.make_client() as client:
            headers = self.editor_headers(client)
            drop_task = self.patch_editor(client, headers, revision=view["revision"],
                                          delete_tasks=["review"])
            self.assertEqual(drop_task.status_code, 200, drop_task.text)
            tasks = {row["task"] for row in model_settings.list_tasks(self.root)}
            self.assertEqual(tasks, {"ideation"})
            busy = self.patch_editor(client, headers, revision=drop_task.json()["revision"],
                                     delete_servers=["alpha"])
            self.assertEqual(busy.status_code, 422, busy.text)
            self.assertEqual({row["name"] for row in model_settings.list_servers(self.root)},
                             {"alpha", "beta"})
            ghost = self.patch_editor(client, headers,
                                      revision=drop_task.json()["revision"],
                                      delete_servers=["ghost"], delete_tasks=["ghost"])
            self.assertEqual(ghost.status_code, 422, ghost.text)
            self.assertEqual(ghost.json()["detail"]["errors"][0]["code"], "unknown_entry")
            dropped = self.patch_editor(client, headers,
                                        revision=drop_task.json()["revision"],
                                        delete_servers=["beta"])
            self.assertEqual(dropped.status_code, 200, dropped.text)
            self.assertEqual(list(dropped.json()["endpoints"]), ["alpha"])

    def test_lock_contention_surfaces_editor_conflict(self):
        before = self.standard_seed()
        from filelock import FileLock

        lock_path = self.root / "ui_data" / ".model_settings.lock"
        with FileLock(str(lock_path), timeout=0.1):
            with self.assertRaises(EditorConflict) as caught:
                Configs(self.root).save_editor(
                    before["revision"], {"ideation": {"model": "m2"}}, {})
        self.assertEqual(caught.exception.errors[0]["code"], "locked")
        self.assertEqual(model_settings.revision(self.root), before["revision"])

    def test_failed_database_write_returns_safe_error_and_preserves_state(self):
        before = self.standard_seed()
        with self.make_client() as client:
            headers = self.editor_headers(client)
            with patch.object(model_settings, "save_settings_atomic",
                              side_effect=PermissionError("private-path")):
                response = self.patch_editor(
                    client, headers, revision=before["revision"],
                    roles={"ideation": {"model": "m2"}})
            self.assertEqual(response.status_code, 503, response.text)
            self.assertNotIn("private-path", response.text)
            self.assertIn("retained", response.text)
        self.assertEqual(model_settings.revision(self.root), before["revision"])

    def test_display_views_redact_configured_credential_values(self):
        self.seed({"alpha": alpha_server()},
                  {"ideation": ideation_task(model="prefix-leaked-value-suffix")})
        with patch.dict(os.environ, {"ALPHA_KEY": "leaked-value"}):
            view = Configs(self.root).models()
        self.assertNotIn("leaked-value", json.dumps(view))
        roles = {row["name"]: row for row in view["roles"]}
        self.assertEqual(roles["ideation"]["model"], "prefix-[redacted]-suffix")

    def test_check_probes_endpoints_and_redacts_credentials(self):
        model_settings.ensure_schema(self.root)
        model_settings.save_settings_atomic(self.root, {"alpha": alpha_server()}, {})
        required = sorted(Configs(self.root).editor()["roles"])
        model_settings.save_settings_atomic(
            self.root, {},
            {name: {"server": "alpha", "model": "m1"} for name in required})
        secret = "probe-credential-value"

        def fake_listing(name, settings, timeout=None):
            return ["m1", "leak-%s-tail" % os.environ["ALPHA_KEY"]]

        with self.make_client() as client, \
                patch.dict(os.environ, {"ALPHA_KEY": secret}), \
                patch.object(model_routing, "list_endpoint_models", fake_listing):
            headers = self.editor_headers(client)
            response = client.post(
                "/api/models/check",
                headers={**headers, "Content-Type": "application/json"})
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["unassigned_tasks"], [])
        self.assertEqual(len(result["endpoints"]), 1)
        probe = result["endpoints"][0]
        self.assertTrue(probe["ok"])
        self.assertEqual(probe["url"], ALPHA_URL)
        self.assertEqual(probe["models"], ["m1", "leak-[redacted]-tail"])
        listed = {row["name"]: row for row in probe["roles"]}
        self.assertEqual(sorted(listed), sorted(required))
        self.assertTrue(all(row["listed"] and row["capabilities_declared"]
                            for row in probe["roles"]))
        self.assertNotIn(secret, response.text)

    def test_endpoint_models_probes_only_the_named_server(self):
        from test_ui_config_probe import endpoint
        with endpoint("alpha-model", expected_api_key="alpha-credential-value") as alpha_url, \
                endpoint("beta-model", expected_api_key="beta-credential-value") as beta_url, \
                endpoint("error-model", status=500) as error_url:
            self.seed({"alpha": alpha_server(address=alpha_url),
                       "beta": alpha_server(address=beta_url, credential_env="BETA_KEY"),
                       "broken": alpha_server(address=error_url, credential_env=None)},
                      {"ideation": ideation_task()})
            with patch.dict(os.environ, {"ALPHA_KEY": "alpha-credential-value",
                                         "BETA_KEY": "beta-credential-value"}):
                with self.make_client() as client:
                    headers = self.editor_headers(client)
                    response = client.post("/api/models/endpoint-models",
                                           headers=headers, json={"endpoint": "alpha"})
                    self.assertEqual(response.status_code, 200, response.text)
                    result = response.json()
                self.assertEqual(result["endpoint"], "alpha")
                self.assertTrue(result["ok"], result["error"])
                self.assertEqual(result["models"], ["alpha-model"])
                configs = Configs(self.root)
                direct = configs.endpoint_models("beta")
                self.assertTrue(direct["ok"], direct["error"])
                self.assertEqual(direct["models"], ["beta-model"])
                failed = configs.endpoint_models("broken")
                self.assertFalse(failed["ok"])
                self.assertEqual(failed["models"], [])
                self.assertEqual(failed["error"],
                                 "Model listing failed. Check the server, credentials, and availability.")
                with self.assertRaises(KeyError):
                    configs.endpoint_models("ghost")
                with self.make_client() as client:
                    missing = client.post("/api/models/endpoint-models",
                                          headers=self.editor_headers(client),
                                          json={"endpoint": "ghost"})
                    self.assertEqual(missing.status_code, 404, missing.text)

    def test_jobs_and_assistant_keep_snapshots_until_explicit_resave(self):
        self.seed({"alpha": alpha_server()}, {"ideation": ideation_task()})
        app = create_app(self.root)
        with TestClient(app, base_url="http://127.0.0.1:8765") as client, \
                patch.object(app.state.supervisor, "launch"):
            headers = self.editor_headers(client)

            def launch_job():
                result = client.post("/api/idea-jobs", headers=headers, json={
                    "request_id": str(uuid4()), "research_question": "Fixture snapshot"})
                self.assertEqual(result.status_code, 202, result.text)
                job_id = result.json()["job_id"]
                app.state.store.update_job(job_id, state="completed")
                return app.state.store.job_dir(job_id) / "model_settings.json"

            first = launch_job()
            snapshot = json.loads(first.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["roles"]["ideation"]["model"], "m1")
            self.assertEqual(snapshot["endpoints"]["alpha"]["base_url"], ALPHA_URL)
            enrolled = client.put("/api/crash-assistant/settings", headers=headers,
                                  json={"enabled": True, "role": "ideation"})
            self.assertEqual(enrolled.status_code, 200, enrolled.text)
            self.assertEqual(enrolled.json()["enabled"], True)
            self.assertEqual(enrolled.json()["role"], "ideation")
            self.assertEqual(enrolled.json()["model"], "m1")
            self.assertEqual(enrolled.json()["endpoint"], "alpha")
            before = client.get("/api/models/editor").json()
            edited = self.patch_editor(client, headers, revision=before["revision"],
                                       roles={"ideation": {"model": "m9"}})
            self.assertEqual(edited.status_code, 200, edited.text)
            second = launch_job()
            self.assertEqual(
                json.loads(second.read_text(encoding="utf-8"))["roles"]["ideation"]["model"],
                "m9")
            self.assertEqual(
                json.loads(first.read_text(encoding="utf-8"))["roles"]["ideation"]["model"],
                "m1")
            self.assertEqual(client.get("/api/crash-assistant/settings").json()["model"], "m1")
            resaved = client.put("/api/crash-assistant/settings", headers=headers,
                                 json={"enabled": True, "role": "ideation"})
            self.assertEqual(resaved.status_code, 200, resaved.text)
            self.assertEqual(resaved.json()["model"], "m9")
            disabled = client.put("/api/crash-assistant/settings", headers=headers,
                                  json={"enabled": False})
            self.assertEqual(disabled.status_code, 200, disabled.text)
            self.assertIs(disabled.json()["enabled"], False)
            unassigned = client.put("/api/crash-assistant/settings", headers=headers,
                                    json={"enabled": True, "role": "writeup"})
            self.assertEqual(unassigned.status_code, 422, unassigned.text)

    def test_roundtrip_preserves_unrelated_numeric_overrides(self):
        self.standard_seed()
        configs = Configs(self.root)
        view = configs.editor()
        saved = configs.save_editor(
            view["revision"],
            {"ideation": {"temperature": 0.4, "timeout": 30.0, "max_tokens": 2048}}, {})
        self.assertEqual(saved["roles"]["ideation"]["temperature"], 0.4)
        self.assertEqual(saved["roles"]["ideation"]["timeout"], 30)
        self.assertEqual(saved["roles"]["ideation"]["max_tokens"], 2048)
        again = configs.save_editor(saved["revision"], {"ideation": {"model": "m2"}}, {})
        self.assertEqual(again["roles"]["ideation"]["model"], "m2")
        self.assertEqual(again["roles"]["ideation"]["temperature"], 0.4)
        self.assertEqual(again["roles"]["ideation"]["timeout"], 30)
        self.assertEqual(again["roles"]["ideation"]["max_tokens"], 2048)
        cleared = configs.save_editor(
            again["revision"],
            {"ideation": {"max_tokens": None, "temperature": None, "timeout": None}}, {})
        self.assertIsNone(cleared["roles"]["ideation"]["max_tokens"])
        self.assertIsNone(cleared["roles"]["ideation"]["temperature"])
        self.assertIsNone(cleared["roles"]["ideation"]["timeout"])


class CborgEditorTests(EditorBase):
    def test_cborg_defaults_follow_provider_without_stored_overrides(self):
        self.seed({"alpha": alpha_server(api_format="cborg", address=None,
                                         credential_env=None, timeout=None)},
                  {"ideation": ideation_task()})
        configs = Configs(self.root)
        editor = configs.editor()
        self.assertEqual(editor["endpoints"]["alpha"],
                         {"provider": "cborg", "base_url": None,
                          "api_key_env": None, "timeout": None, "provides": ["text"]})
        with patch.dict(os.environ, {"CBORG_API_KEY": "fixture-cborg-value"}):
            view = configs.models()
            endpoint_row = next(row for row in view["endpoints"] if row["id"] == "alpha")
            self.assertEqual(endpoint_row["url"], model_routing.CBORG_BASE_URL)
            self.assertEqual(endpoint_row["credential"],
                             {"env": "CBORG_API_KEY", "present": True})
            role_row = next(row for row in view["roles"] if row["name"] == "ideation")
            self.assertEqual(role_row["credential"],
                             {"env": "CBORG_API_KEY", "present": True})
            snapshot = configs.diagnostic_assignment("ideation")
            self.assertEqual(snapshot["provider"], "cborg")
            self.assertEqual(snapshot["base_url"], model_routing.CBORG_BASE_URL)
            self.assertEqual(snapshot["api_key_env"], "CBORG_API_KEY")
            self.assertIn("CBORG_API_KEY", snapshot["credential_envs"])
            self.assertEqual(snapshot["max_tokens"], 4096)
            self.assertEqual(snapshot["temperature"], 0.2)
            with model_routing.create_selfhosted_client("role/ideation") as client:
                self.assertEqual(str(client.base_url).rstrip("/"),
                                 model_routing.CBORG_BASE_URL)
                self.assertEqual(client.api_key, "fixture-cborg-value")
            self.assertNotIn("fixture-cborg-value", json.dumps(view))

    def test_explicit_overrides_win_and_clearing_restores_cborg_defaults(self):
        self.seed({"alpha": alpha_server(api_format="cborg", address=None,
                                         credential_env=None, timeout=None)},
                  {"ideation": ideation_task()})
        configs = Configs(self.root)
        editor = configs.editor()
        with patch.dict(os.environ, {"CBORG_API_KEY": "fixture-default",
                                     "ENDPOINT_KEY": "fixture-endpoint",
                                     "ROLE_KEY": "fixture-role"}):
            saved = configs.save_editor(
                editor["revision"], {"ideation": {"api_key_env": "ROLE_KEY"}},
                {"alpha": {"base_url": ALPHA_URL, "api_key_env": "ENDPOINT_KEY"}})
            self.assertEqual(saved["roles"]["ideation"]["api_key_env"], "ROLE_KEY")
            self.assertEqual(saved["endpoints"]["alpha"]["base_url"], ALPHA_URL)
            with model_routing.create_selfhosted_client("role/ideation") as client:
                self.assertEqual(str(client.base_url).rstrip("/"), ALPHA_URL)
                self.assertEqual(client.api_key, "fixture-role")
            self.assertEqual(configs.diagnostic_assignment("ideation")["api_key_env"],
                             "ROLE_KEY")
            saved = configs.save_editor(saved["revision"],
                                        {"ideation": {"api_key_env": None}}, {})
            with model_routing.create_selfhosted_client("role/ideation") as client:
                self.assertEqual(client.api_key, "fixture-endpoint")
            cleared = configs.save_editor(
                saved["revision"], {},
                {"alpha": {"base_url": None, "api_key_env": None}})
            self.assertIsNone(cleared["endpoints"]["alpha"]["base_url"])
            self.assertIsNone(cleared["endpoints"]["alpha"]["api_key_env"])
            with model_routing.create_selfhosted_client("role/ideation") as client:
                self.assertEqual(str(client.base_url).rstrip("/"),
                                 model_routing.CBORG_BASE_URL)
                self.assertEqual(client.api_key, "fixture-default")
            self.assertNotIn("fixture-default", json.dumps(configs.models()))

    def test_provider_switch_validates_final_state_and_rejects_unknown_or_null(self):
        self.seed({"alpha": alpha_server()},
                  {"ideation": ideation_task(max_tokens=4096, temperature=0.4,
                                             credential_env="ROLE_KEY")})
        before_revision = model_settings.revision(self.root)
        with self.make_client() as client:
            headers = self.editor_headers(client)

            def save(endpoint_patch, roles=None, revision=None):
                return self.patch_editor(
                    client, headers, revision=revision or before_revision,
                    endpoints={"alpha": endpoint_patch}, roles=roles or {})

            for provider in ("unknown-fixture-provider", None):
                with self.subTest(provider=provider):
                    response = save({"provider": provider})
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertNotIn("unknown-fixture-provider", response.text)
                    self.assertEqual(model_settings.revision(self.root),
                                     before_revision)
            rejected = save({"provider": "openai-codex"})
            self.assertEqual(rejected.status_code, 422, rejected.text)
            self.assertEqual(rejected.json()["detail"]["errors"][0]["code"],
                             "managed_by_provider")
            still = save({"provider": "openai-codex", "base_url": None,
                          "api_key_env": None})
            self.assertEqual(still.status_code, 422, still.text)
            self.assertEqual(still.json()["detail"]["errors"][0]["code"],
                             "managed_by_provider")
            accepted = save({"provider": "openai-codex", "base_url": None,
                             "api_key_env": None},
                            {"ideation": {"max_tokens": None, "temperature": None,
                                          "api_key_env": None}})
            self.assertEqual(accepted.status_code, 200, accepted.text)
            codex = accepted.json()
            self.assertEqual(codex["endpoints"]["alpha"]["provider"], "openai-codex")
            self.assertIsNone(codex["endpoints"]["alpha"]["base_url"])
            self.assertIsNone(codex["roles"]["ideation"]["max_tokens"])
            codex_revision = codex["revision"]
            self.assertEqual(model_settings.revision(self.root), codex_revision)
            view = client.get("/api/models").json()
            self.assertEqual(view["endpoints"][0]["credential"],
                             {"env": None, "present": False, "method": "codex"})
            for endpoint_patch in ({"provider": "openai", "base_url": None},
                                   {"provider": "openai"},
                                   {"base_url": "https://attacker.invalid/v1"},
                                   {"api_key_env": "OTHER_KEY"}):
                with self.subTest(endpoint_patch=endpoint_patch):
                    response = save(endpoint_patch, revision=codex_revision)
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(model_settings.revision(self.root), codex_revision)
            compatible = save({"provider": "cborg", "base_url": None,
                               "api_key_env": None}, revision=codex_revision)
            self.assertEqual(compatible.status_code, 200, compatible.text)
            self.assertEqual(compatible.json()["endpoints"]["alpha"]["provider"], "cborg")

    def test_invalid_provider_rows_are_reported_not_silently_normalized(self):
        self.seed({"alpha": alpha_server(api_format="unknown-fixture")},
                  {"ideation": ideation_task()})
        configs = Configs(self.root)
        # The editor stays lossless so the broken row can be inspected and fixed.
        editor = configs.editor()
        self.assertEqual(editor["endpoints"]["alpha"]["provider"], "unknown-fixture")
        with self.assertRaises(ValueError):
            configs.models()
        with self.assertRaises(model_routing.RoleConfigError):
            model_routing.create_selfhosted_client("role/ideation")
