"""Named assignments persist independently of defaults and reject unsafe changes."""
from copy import deepcopy
import json
import sqlite3
from unittest.mock import patch
from uuid import UUID, uuid4

from ai_scientist import model_routing
from ai_scientist.ui import model_settings
from ai_scientist.ui.configs import Configs, InvalidConfiguration
from test_ui_model_editor import EditorBase, alpha_server, ideation_task


class RoleProfileTests(EditorBase):
    def setUp(self):
        super().setUp()
        self.seed({"alpha": alpha_server(),
                   "vision": alpha_server(capabilities=["text", "vision"]),
                   "codex": alpha_server(api_format="openai-codex", address=None, credential_env=None)},
                  {"ideation": ideation_task(),
                   "custom_task": ideation_task(),
                   "visual_feedback": ideation_task(server="vision", requires=["vision"])})
        self.client = self.make_client()
        self.addCleanup(self.client.close)
        self.headers = self.editor_headers(self.client)
        self.before = self.client.get("/api/models/editor").json()
        self.roles = {name: {key: value for key, value in role.items() if key != "requires"}
                      for name, role in self.before["roles"].items()}

    def create(self, *, name="Research draft", roles=None):
        return self.client.post("/api/models/profiles", headers=self.headers,
                                json={"name": name, "roles": self.roles if roles is None else roles})

    def test_roundtrip_and_overwrite_are_persistent_without_default_mutation_or_probes(self):
        self.roles["ideation"].update(model="draft-model", temperature=0.375, timeout=12.5,
                                       max_tokens=513, api_key_env="DRAFT_MODEL_KEY")
        defaults = model_settings.current_settings(self.root)
        with patch.object(model_routing, "list_endpoint_models", side_effect=AssertionError("must not probe")), \
                patch.object(model_routing, "create_selfhosted_client", side_effect=AssertionError("must not infer")):
            response = self.create(name="  Research draft  ")
            self.assertEqual(response.status_code, 201, response.text)
            created = response.json()
            UUID(created["id"])
            self.assertEqual(created["name"], "Research draft")
            self.assertEqual(created["revision"], 1)
            self.assertEqual(created["roles"], self.roles)
            self.assertNotIn("base_url", json.dumps(created))
            self.assertNotIn("requires", json.dumps(created))
            updated_roles = deepcopy(self.roles)
            updated_roles["custom_task"]["model"] = "custom-selected-model"
            updated_roles["ideation"]["timeout"] = None
            updated = self.client.put(f"/api/models/profiles/{created['id']}", headers=self.headers,
                                      json={"expected_revision": 1, "roles": updated_roles})
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertEqual(updated.json()["revision"], 2)
            self.assertEqual(updated.json()["created_at"], created["created_at"])
            with self.make_client() as fresh:
                self.assertEqual(fresh.get("/api/models/profiles").json()["profiles"], [updated.json()])
        self.assertEqual(model_settings.current_settings(self.root), defaults)
        self.assertEqual(self.client.get("/api/models/editor").json(), self.before)

    def test_duplicate_names_stale_overwrites_and_unknown_ids_preserve_saved_profile(self):
        created = self.create(name="Straße").json()
        duplicate = self.create(name=" STRASSE ")
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        changed = deepcopy(self.roles)
        changed["ideation"]["model"] = "new-model"
        url = f"/api/models/profiles/{created['id']}"
        updated = self.client.put(url, headers=self.headers,
                                  json={"expected_revision": 1, "roles": changed})
        self.assertEqual(updated.status_code, 200, updated.text)
        stale = self.client.put(url, headers=self.headers,
                                json={"expected_revision": 1, "roles": self.roles})
        self.assertEqual(stale.status_code, 409, stale.text)
        unknown = self.client.put(f"/api/models/profiles/{uuid4()}", headers=self.headers,
                                  json={"expected_revision": 1, "roles": self.roles})
        self.assertEqual(unknown.status_code, 404, unknown.text)
        self.assertEqual(self.client.get("/api/models/profiles").json()["profiles"], [updated.json()])

    def test_invalid_assignments_fail_without_saving_or_exposing_values(self):
        cases = [
            ("ideation", "endpoint", "removed-server"),
            ("ideation", "model", "  "),
            ("ideation", "max_tokens", True),
            ("ideation", "max_tokens", 1.5),
            ("ideation", "temperature", 2.01),
            ("ideation", "timeout", 0),
            ("ideation", "api_key_env", "sk-private-credential"),
            ("ideation", "requires", []),
            ("visual_feedback", "endpoint", "alpha"),
        ]
        for role, key, value in cases:
            with self.subTest(role=role, key=key):
                assignments = deepcopy(self.roles)
                assignments[role][key] = value
                response = self.create(roles=assignments)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn("sk-private-credential", response.text)
        unknown = deepcopy(self.roles)
        unknown["removed_task"] = unknown.pop("custom_task")
        self.assertEqual(self.create(roles=unknown).status_code, 422)
        incomplete = deepcopy(self.roles)
        del incomplete["ideation"]["timeout"]
        self.assertEqual(self.create(roles=incomplete).status_code, 422)
        for key, value in (("max_tokens", 100), ("temperature", 0.5), ("api_key_env", "CODEX_KEY")):
            assignments = deepcopy(self.roles)
            assignments["ideation"].update(endpoint="codex", **{key: value})
            self.assertEqual(self.create(roles=assignments).status_code, 422)
        self.assertEqual(self.client.get("/api/models/profiles").json(), {"profiles": []})
        self.assertEqual(self.client.get("/api/models/editor").json(), self.before)

    def test_profile_names_and_update_revisions_are_strict(self):
        for name in (" ", "x" * 101, "bad\nname", "bad\x7fname"):
            with self.subTest(name=name):
                self.assertEqual(self.create(name=name).status_code, 422)
        created = self.create().json()
        for revision in (True, "1", 1.5, 0):
            response = self.client.put(f"/api/models/profiles/{created['id']}", headers=self.headers,
                                       json={"expected_revision": revision, "roles": self.roles})
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.get("/api/models/profiles").json()["profiles"], [created])

    def test_removed_reference_is_rejected_on_reuse_and_requirements_remain_protected(self):
        created = self.create().json()
        model_settings.delete_task(self.root, "custom_task")
        response = self.client.put(f"/api/models/profiles/{created['id']}", headers=self.headers,
                                   json={"expected_revision": 1, "roles": self.roles})
        self.assertEqual(response.status_code, 422, response.text)
        configs = Configs(self.root)
        with self.assertRaises(InvalidConfiguration):
            configs.assignment_snapshot(configs.editor()["revision"], created["roles"])
        self.assertEqual(self.client.get("/api/models/profiles").json()["profiles"], [created])

    def test_shared_editor_validation_rejects_overrides_and_unknown_fields(self):
        configs = Configs(self.root)
        for changes in ({"timeout": -1}, {"temperature": 3}, {"max_tokens": True},
                        {"api_key_env": "private-key-value"}, {"requires": []}):
            with self.subTest(changes=changes), self.assertRaises(InvalidConfiguration):
                configs.save_editor(self.before["revision"], {"ideation": changes}, {})
        self.assertEqual(configs.editor(), self.before)

    def test_database_revision_guard_prevents_stale_direct_settings_write(self):
        model_settings.save_settings_atomic(self.root, {}, {"ideation": ideation_task(model="new-default")})
        with self.assertRaises(model_settings.SettingsConflict):
            model_settings.save_settings_atomic(self.root, {}, {"ideation": ideation_task(model="stale")},
                                                expected_revision=self.before["revision"])
        self.assertEqual(model_settings.current_settings(self.root)["roles"]["ideation"]["model"], "new-default")

    def test_clearing_registered_role_preserves_capabilities_and_profile_reuse(self):
        created = self.create().json()
        cleared = Configs(self.root).save_editor(self.before["revision"], {
            "custom_task": {"endpoint": None, "model": None},
            "visual_feedback": {"endpoint": None, "model": None}}, {})
        self.assertIn("custom_task", cleared["roles"])
        self.assertIsNone(cleared["roles"]["visual_feedback"]["endpoint"])
        self.assertEqual(cleared["roles"]["visual_feedback"]["requires"], ["vision"])
        applied = Configs(self.root).assignment_snapshot(cleared["revision"], created["roles"])
        self.assertEqual(applied["roles"]["custom_task"]["model"], "m1")
        self.assertEqual(applied["roles"]["visual_feedback"]["requires"], ["vision"])
        invalid = deepcopy(created["roles"])
        invalid["visual_feedback"]["endpoint"] = "alpha"
        with self.assertRaises(InvalidConfiguration):
            Configs(self.root).assignment_snapshot(cleared["revision"], invalid)

    def test_write_failure_is_safe_and_preserves_existing_profile(self):
        created = self.create().json()
        with patch.object(model_settings, "save_profile_atomic",
                          side_effect=sqlite3.OperationalError("private-database-location")):
            response = self.client.put(f"/api/models/profiles/{created['id']}", headers=self.headers,
                                       json={"expected_revision": 1, "roles": self.roles})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn("private-database-location", response.text)
        self.assertEqual(self.client.get("/api/models/profiles").json()["profiles"], [created])
