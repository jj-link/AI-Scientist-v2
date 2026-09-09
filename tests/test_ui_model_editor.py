"""Studio configuration editor: revision safety, field validation, isolation."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import unittest
from uuid import uuid4
from unittest.mock import patch

import yaml

from ai_scientist import model_routing
from ai_scientist.ui.app import create_app
from ai_scientist.ui.configs import Configs, EditorConflict, InvalidConfiguration

HEADERS = lambda token: {  # noqa: E731
    "Origin": "http://127.0.0.1:8765",
    "X-Studio-Token": token,
}


def minimal_config(base_url="http://127.0.0.1:9/v1", extra_roles=()):
    roles = {"ideation": {"endpoint": "alpha", "model": "m1", "requires": ["text"]}}
    for name in extra_roles:
        roles[name] = {"endpoint": "alpha", "model": "m2"}
    return {
        "endpoints": {"alpha": {"base_url": base_url, "provides": ["text"]}},
        "roles": roles,
    }


class EditorBase(unittest.TestCase):
    def setUp(self):
        self.root = (
            Path(__file__).resolve().parents[1] / "ui_data" / "checks" / str(uuid4())
        )
        self.root.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        os.environ.pop("AI_SCIENTIST_ROLE_CONFIG", None)
        model_routing._cache.clear()

    def write_preset(self, name="ais_roles.yaml", cfg=None):
        target = self.root / name
        target.write_text(yaml.safe_dump(cfg or minimal_config()), encoding="utf-8")
        return target

    def editor_headers(self, client):
        token = client.get("/api/bootstrap").json()["request_token"]
        return HEADERS(token)

    def preset_id(self, client, label_substring):
        bootstrap = client.get("/api/bootstrap").json()
        return next(
            o["id"] for o in bootstrap["role_configs"] if label_substring in o["label"]
        )


class EditorApiTests(EditorBase):
    """HTTP contract for the editor routes without touching saved jobs."""

    def test_read_envelope_reports_file_digest_and_fields(self):
        target = self.write_preset("ais_roles.eapi.yaml")
        os.environ["AI_SCIENTIST_ROLE_CONFIG"] = str(target)
        app = create_app(self.root)
        from fastapi.testclient import TestClient

        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            headers = self.editor_headers(client)
            config_id = self.preset_id(client, "eapi")
            data = client.get(
                f"/api/models/editor?config_id={config_id}", headers=headers
            )
            self.assertEqual(data.status_code, 200, data.text)
            view = data.json()
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            self.assertEqual(view["revision"], digest)
            self.assertEqual(view["roles"]["ideation"]["model"], "m1")
            self.assertIsNone(view["roles"]["ideation"]["temperature"])
            self.assertEqual(
                view["endpoints"]["alpha"]["base_url"], "http://127.0.0.1:9/v1"
            )

    def test_patch_applies_only_submitted_changes_and_keeps_unrelated_keys(self):
        target = self.write_preset("ais_roles.patch.yaml")
        os.environ["AI_SCIENTIST_ROLE_CONFIG"] = str(target)
        app = create_app(self.root)
        from fastapi.testclient import TestClient

        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            headers = self.editor_headers(client)
            config_id = self.preset_id(client, "patch")
            before = json.loads(
                client.get(
                    f"/api/models/editor?config_id={config_id}", headers=headers
                ).text
            )
            response = client.patch(
                "/api/models/editor",
                json={
                    "config_id": config_id,
                    "expected_revision": before["revision"],
                    "roles": {"ideation": {"temperature": 0.5}},
                    "endpoints": {"alpha": {"timeout": 120}},
                },
                headers=headers,
            )
            self.assertEqual(response.status_code, 200, response.text)
            saved = response.json()
            self.assertEqual(saved["roles"]["ideation"]["temperature"], 0.5)
            self.assertEqual(saved["endpoints"]["alpha"]["timeout"], 120)
            self.assertIsNone(saved["roles"]["ideation"]["max_tokens"])
            self.assertEqual(saved["roles"]["ideation"]["model"], "m1")
            on_disk = yaml.safe_load(target.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["endpoints"]["alpha"]["provides"], ["text"])
            self.assertEqual(on_disk["roles"]["ideation"]["requires"], ["text"])
            self.assertEqual(on_disk["roles"]["ideation"]["temperature"], 0.5)

    def test_patch_requires_exact_revision_and_rejects_unknown_entries(self):
        target = self.write_preset("ais_roles.stale.yaml")
        os.environ["AI_SCIENTIST_ROLE_CONFIG"] = str(target)
        app = create_app(self.root)
        from fastapi.testclient import TestClient

        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            headers = self.editor_headers(client)
            config_id = self.preset_id(client, "stale")
            revision = hashlib.sha256(target.read_bytes()).hexdigest()
            stale = client.patch(
                "/api/models/editor",
                json={
                    "config_id": config_id,
                    "expected_revision": "a" * 64,
                    "roles": {"ideation": {"temperature": 1}},
                    "endpoints": {},
                },
                headers=headers,
            )
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(
                stale.json()["detail"]["errors"][0]["code"], "configuration_changed"
            )
            unknown = client.patch(
                "/api/models/editor",
                json={
                    "config_id": config_id,
                    "expected_revision": revision,
                    "roles": {"nope": {"temperature": 1}},
                    "endpoints": {},
                },
                headers=headers,
            )
            self.assertEqual(unknown.status_code, 422)
            self.assertEqual(
                unknown.json()["detail"]["errors"][0]["code"], "unknown_entry"
            )
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), revision)

    def test_patch_validates_values_and_never_writes_invalid_state(self):
        target = self.write_preset("ais_roles.invalid.yaml")
        os.environ["AI_SCIENTIST_ROLE_CONFIG"] = str(target)
        app = create_app(self.root)
        from fastapi.testclient import TestClient

        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            headers = self.editor_headers(client)
            config_id = self.preset_id(client, "invalid")
            revision = hashlib.sha256(target.read_bytes()).hexdigest()
            for field, value, code in (
                ("temperature", 5, "invalid_number"),
                ("max_tokens", 0, "invalid_number"),
                ("timeout", -1, "invalid_number"),
            ):
                body = {
                    "config_id": config_id,
                    "expected_revision": revision,
                    "roles": {"ideation": {field: value}},
                    "endpoints": {},
                }
                response = client.patch(
                    "/api/models/editor", json=body, headers=headers
                )
                self.assertEqual(response.status_code, 422, response.text)
                codes = [e["code"] for e in response.json()["detail"]["errors"]]
                self.assertIn(code, codes)

    def test_credential_bearing_url_rejected_with_safe_message(self):
        target = self.write_preset("ais_roles.credurl.yaml")
        os.environ["AI_SCIENTIST_ROLE_CONFIG"] = str(target)
        app = create_app(self.root)
        from fastapi.testclient import TestClient

        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            headers = self.editor_headers(client)
            config_id = self.preset_id(client, "credurl")
            revision = hashlib.sha256(target.read_bytes()).hexdigest()
            bad_url = {
                "config_id": config_id,
                "expected_revision": revision,
                "roles": {},
                "endpoints": {"alpha": {"base_url": "https://u:p@example.com/v1"}},
            }
            response = client.patch("/api/models/editor", json=bad_url, headers=headers)
            self.assertEqual(response.status_code, 422)
            # Safe generic message; credential text must never leak in the body.
            body_text = response.text
            self.assertNotIn("u:p", body_text)
            self.assertNotIn("example.com", body_text)

    def test_empty_patch_rejected_without_file_change(self):
        """A save cannot silently normalize a preset without an edit."""
        target = self.write_preset("ais_roles.guard.yaml")
        os.environ["AI_SCIENTIST_ROLE_CONFIG"] = str(target)
        app = create_app(self.root)
        from fastapi.testclient import TestClient

        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            headers = self.editor_headers(client)
            config_id = self.preset_id(client, "guard")
            revision = hashlib.sha256(target.read_bytes()).hexdigest()
            empty = client.patch(
                "/api/models/editor",
                json={
                    "config_id": config_id,
                    "expected_revision": revision,
                    "roles": {},
                    "endpoints": {},
                },
                headers=headers,
            )
            self.assertEqual(empty.status_code, 422, empty.text)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), revision)
            mismatched = client.patch(
                "/api/models/editor",
                json={
                    "config_id": config_id,
                    "expected_revision": revision,
                    "roles": {"ideation": {"temperature": 1}},
                    "endpoints": {},
                },
            )
            self.assertEqual(mismatched.status_code, 403)

    def test_editor_guards_embedded_secret_files(self):
        """Configs reader refuses presets with inline credential keys."""
        cfg = minimal_config()
        cfg["endpoints"]["alpha"]["api_key"] = "sk-inline"
        target = self.write_preset("ais_roles.embedded.yaml", cfg)
        os.environ["AI_SCIENTIST_ROLE_CONFIG"] = str(target)
        configs = Configs(self.root)
        config_id = configs.presets()["selected_role_config_id"]
        with self.assertRaises(ValueError):
            configs.editor(config_id)
        self.assertTrue(target.exists())
        target.unlink(missing_ok=True)

    def test_lock_timeout_surfaces_editor_conflict(self):
        target = self.write_preset("ais_roles.lock.yaml")
        os.environ["AI_SCIENTIST_ROLE_CONFIG"] = str(target)
        configs = Configs(self.root)
        config_id = configs.presets()["selected_role_config_id"]
        view = configs.editor(config_id)
        from filelock import FileLock

        lock_path = target.with_name("." + target.name + ".studio.lock")
        with FileLock(str(lock_path), timeout=0.1):
            with self.assertRaises(EditorConflict):
                configs.save_editor(
                    config_id, view["revision"], {"ideation": {"temperature": 1}}, {}
                )
        self.assertEqual(
            hashlib.sha256(target.read_bytes()).hexdigest(), view["revision"]
        )

    def test_credential_values_reject_embedding_and_env_names(self):
        target = self.write_preset("ais_roles.secret.yaml")
        os.environ["AI_SCIENTIST_ROLE_CONFIG"] = str(target)
        app = create_app(self.root)
        from fastapi.testclient import TestClient

        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            headers = self.editor_headers(client)
            config_id = self.preset_id(client, "secret")
            revision = hashlib.sha256(target.read_bytes()).hexdigest()
            inline = {
                "config_id": config_id,
                "expected_revision": revision,
                "roles": {},
                "endpoints": {"alpha": {"api_key_env": "sk-plaintext-secret"}},
            }
            response = client.patch("/api/models/editor", json=inline, headers=headers)
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["detail"]["errors"][0]["field"], "endpoints.alpha.api_key_env")
            self.assertNotIn("sk-plaintext-secret", response.text)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), revision)


    def test_discovery_uses_selected_preset_not_environment_default(self):
        from test_ui_config_probe import endpoint
        with endpoint("default-model") as default_url, endpoint("selected-model") as selected_url:
            default = self.write_preset(cfg=minimal_config(default_url))
            self.write_preset("ais_roles.selected.yaml", minimal_config(selected_url))
            os.environ["AI_SCIENTIST_ROLE_CONFIG"] = str(default)
            configs = Configs(self.root)
            selected = next(row["id"] for row in configs.presets()["role_configs"] if "selected" in row["label"])
            result = configs.endpoint_models(selected, "alpha")
            self.assertTrue(result["ok"], result["error"])
            self.assertEqual(result["models"], ["selected-model"])

    def test_required_fields_cannot_be_removed(self):
        target = self.write_preset()
        os.environ["AI_SCIENTIST_ROLE_CONFIG"] = str(target)
        configs = Configs(self.root)
        selected = configs.presets()["selected_role_config_id"]
        view = configs.editor(selected)
        for roles, endpoints in (
            ({"ideation": {"model": None}}, {}),
            ({"ideation": {"endpoint": None}}, {}),
            ({}, {"alpha": {"base_url": None}}),
        ):
            with self.subTest(roles=roles, endpoints=endpoints), self.assertRaises(InvalidConfiguration):
                configs.save_editor(selected, view["revision"], roles, endpoints)
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), view["revision"])

    def test_shared_alias_and_embedded_secret_substrings_block_edits(self):
        target = self.write_preset()
        os.environ["AI_SCIENTIST_ROLE_CONFIG"] = str(target)
        configs = Configs(self.root)
        selected = configs.presets()["selected_role_config_id"]
        target.write_text("endpoints: {alpha: {base_url: 'http://localhost/v1'}}\nroles:\n  ideation: &shared {endpoint: alpha, model: m1}\n  review: *shared\n", encoding="utf-8")
        original = target.read_bytes()
        with self.assertRaises(InvalidConfiguration):
            configs.editor(selected)
        with self.assertRaises(InvalidConfiguration):
            configs.save_editor(selected, hashlib.sha256(original).hexdigest(), {"ideation": {"model": "m2"}}, {})
        self.assertEqual(target.read_bytes(), original)
        cfg = minimal_config()
        cfg["endpoints"]["alpha"]["api_key_env"] = "EDITOR_SECRET"
        cfg["roles"]["ideation"]["model"] = "prefix-private-value-suffix"
        target.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        with patch.dict(os.environ, {"EDITOR_SECRET": "private-value"}), self.assertRaises(InvalidConfiguration):
            configs.editor(selected)

    def test_roundtrip_noop_inheritance_and_cached_reload(self):
        target = self.write_preset()
        target.write_text("# retain this comment\n" + target.read_text(encoding="utf-8"), encoding="utf-8")
        other = self.write_preset("ais_roles.other.yaml")
        other_bytes = other.read_bytes()
        os.environ["AI_SCIENTIST_ROLE_CONFIG"] = str(target)
        configs = Configs(self.root)
        selected = configs.presets()["selected_role_config_id"]
        view = configs.editor(selected)
        original = target.read_bytes()
        same = configs.save_editor(selected, view["revision"], {"ideation": {"model": "m1"}}, {})
        self.assertEqual(same["revision"], view["revision"])
        self.assertEqual(target.read_bytes(), original)
        model_routing.load_role_config(target)
        saved = configs.save_editor(selected, view["revision"], {"ideation": {"model": "m2", "timeout": 12}}, {"alpha": {"timeout": 48}})
        self.assertEqual(model_routing.load_role_config(target)["roles"]["ideation"]["model"], "m2")
        configs.save_editor(selected, saved["revision"], {"ideation": {"timeout": None}}, {})
        self.assertNotIn("timeout", yaml.safe_load(target.read_bytes())["roles"]["ideation"])
        self.assertEqual(configs.models(selected)["roles"][0]["timeout"], 48)
        self.assertTrue(target.read_text(encoding="utf-8").startswith("# retain this comment"))
        self.assertEqual(other.read_bytes(), other_bytes)

    def test_failed_replace_returns_safe_error_and_preserves_bytes(self):
        from fastapi.testclient import TestClient
        target = self.write_preset()
        os.environ["AI_SCIENTIST_ROLE_CONFIG"] = str(target)
        original = target.read_bytes()
        with TestClient(create_app(self.root), base_url="http://127.0.0.1:8765") as client:
            headers = self.editor_headers(client)
            selected = self.preset_id(client, "ais_roles.yaml")
            with patch.object(Path, "replace", side_effect=PermissionError("private-path")):
                response = client.patch("/api/models/editor", headers=headers, json={
                    "config_id": selected, "expected_revision": hashlib.sha256(original).hexdigest(),
                    "roles": {"ideation": {"model": "m2"}}, "endpoints": {},
                })
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private-path", response.text)
            self.assertEqual(target.read_bytes(), original)

    def test_jobs_and_assistant_keep_snapshots_until_explicit_resave(self):
        from fastapi.testclient import TestClient
        target = self.write_preset()
        os.environ["AI_SCIENTIST_ROLE_CONFIG"] = str(target)
        app = create_app(self.root)
        with TestClient(app, base_url="http://127.0.0.1:8765") as client, patch.object(app.state.supervisor, "launch"):
            headers = self.editor_headers(client)
            selected = self.preset_id(client, "ais_roles.yaml")
            assistant = client.put("/api/crash-assistant/settings", headers=headers,
                json={"enabled": True, "config_id": selected, "role": "ideation"})
            self.assertEqual(assistant.status_code, 200, assistant.text)
            def launch():
                result = client.post("/api/idea-jobs", headers=headers, json={
                    "request_id": str(uuid4()), "research_question": "Fixture snapshot",
                    "role_config_id": selected,
                })
                self.assertEqual(result.status_code, 202, result.text)
                job_id = result.json()["job_id"]
                app.state.store.update_job(job_id, state="completed")
                return app.state.store.job_dir(job_id) / "role_config.yaml"
            first = launch()
            original = first.read_bytes()
            view = app.state.configs.editor(selected)
            app.state.configs.save_editor(selected, view["revision"], {"ideation": {"model": "m9"}}, {})
            second = launch()
            self.assertEqual(first.read_bytes(), original)
            self.assertEqual(yaml.safe_load(second.read_bytes())["roles"]["ideation"]["model"], "m9")
            current = client.get("/api/crash-assistant/settings").json()
            self.assertEqual(current["model"], assistant.json()["model"])
            resaved = client.put("/api/crash-assistant/settings", headers=headers,
                json={"enabled": True, "config_id": selected, "role": "ideation"})
            self.assertEqual(resaved.status_code, 200, resaved.text)
            self.assertEqual(resaved.json()["model"], "m9")

    def test_saving_again_preserves_roundtrip_numeric_overrides(self):
        target = self.write_preset()
        os.environ["AI_SCIENTIST_ROLE_CONFIG"] = str(target)
        configs = Configs(self.root)
        selected = configs.presets()["selected_role_config_id"]
        view = configs.editor(selected)
        saved = configs.save_editor(selected, view["revision"],
            {"ideation": {"temperature": 0.4, "timeout": 30.0, "max_tokens": 2048}}, {})
        again = configs.save_editor(selected, saved["revision"], {"ideation": {"model": "m2"}}, {})
        self.assertEqual(again["roles"]["ideation"]["model"], "m2")
        self.assertEqual(again["roles"]["ideation"]["temperature"], 0.4)
        self.assertEqual(again["roles"]["ideation"]["timeout"], 30)
        self.assertEqual(again["roles"]["ideation"]["max_tokens"], 2048)


class CborgEditorTests(EditorBase):
    def test_saving_named_provider_preserves_other_preset_and_enrolled_assignment(self):
        from fastapi.testclient import TestClient

        cfg = minimal_config(extra_roles=("review",))
        cfg["custom"] = {"retain": True}
        target = self.write_preset(cfg=cfg)
        target.write_text("# keep this comment\n" + target.read_text(encoding="utf-8"), encoding="utf-8")
        other = self.write_preset("ais_roles.other.yaml")
        other_bytes = other.read_bytes()
        os.environ[model_routing.ROLE_CONFIG_ENV] = str(target)
        app = create_app(self.root)
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            headers = self.editor_headers(client)
            selected = self.preset_id(client, "ais_roles.yaml")
            enrolled = client.put("/api/crash-assistant/settings", headers=headers,
                json={"enabled": True, "config_id": selected, "role": "ideation"})
            self.assertEqual(enrolled.status_code, 200, enrolled.text)
            before = client.get(f"/api/models/editor?config_id={selected}").json()
            cached = model_routing.load_role_config(target)
            response = client.patch("/api/models/editor", headers=headers, json={
                "config_id": selected, "expected_revision": before["revision"],
                "endpoints": {"alpha": {"provider": "cborg", "base_url": "https://api.cborg.lbl.gov/v1", "api_key_env": "CBORG_API_KEY"}},
                "roles": {"ideation": {"model": "lbl/cborg-mini"}},
            })
            self.assertEqual(response.status_code, 200, response.text)
            saved = response.json()
            self.assertEqual(saved["endpoints"]["alpha"]["provider"], "cborg")
            self.assertEqual(saved["endpoints"]["alpha"]["base_url"], "https://api.cborg.lbl.gov/v1")
            self.assertEqual(saved["endpoints"]["alpha"]["api_key_env"], "CBORG_API_KEY")
            self.assertEqual(client.get(f"/api/models/editor?config_id={selected}").json(), saved)
            current = client.get("/api/crash-assistant/settings").json()
            self.assertEqual(current["provider"], "openai")
            self.assertEqual(current["model"], enrolled.json()["model"])
            assignment = Configs(self.root).diagnostic_assignment(selected, "ideation")
            self.assertEqual(assignment["provider"], "cborg")
            self.assertEqual(assignment["api_key_env"], "CBORG_API_KEY")
            self.assertEqual(cached["roles"]["ideation"]["model"], "m1")
            self.assertEqual(model_routing.load_role_config(target)["roles"]["ideation"]["model"], "lbl/cborg-mini")
        self.assertEqual(other.read_bytes(), other_bytes)
        on_disk = yaml.safe_load(target.read_bytes())
        self.assertEqual(on_disk["custom"], cfg["custom"])
        self.assertEqual(on_disk["roles"]["review"], cfg["roles"]["review"])
        self.assertTrue(target.read_text(encoding="utf-8").startswith("# keep this comment"))

    def test_cborg_defaults_and_explicit_role_credentials_follow_inheritance(self):
        cfg = minimal_config()
        cfg["endpoints"]["alpha"] = {"provider": "cborg", "provides": ["text"]}
        target = self.write_preset(cfg=cfg)
        with patch.dict(os.environ, {model_routing.ROLE_CONFIG_ENV: str(target),
                "CBORG_API_KEY": "fixture-default", "ENDPOINT_KEY": "fixture-endpoint", "ROLE_KEY": "fixture-role"}):
            configs = Configs(self.root)
            selected = configs.presets()["selected_role_config_id"]
            editor = configs.editor(selected)
            self.assertIsNone(editor["endpoints"]["alpha"]["base_url"])
            self.assertIsNone(editor["endpoints"]["alpha"]["api_key_env"])
            view = configs.models(selected)
            self.assertEqual(view["endpoints"][0]["url"], "https://api.cborg.lbl.gov/v1")
            self.assertEqual(view["roles"][0]["credential"], {"env": "CBORG_API_KEY", "present": True})
            snapshot = configs.diagnostic_assignment(selected, "ideation")
            self.assertEqual(snapshot["api_key_env"], "CBORG_API_KEY")
            self.assertIn("CBORG_API_KEY", snapshot["credential_envs"])
            with model_routing.create_selfhosted_client("role/ideation") as client:
                self.assertEqual(str(client.base_url).rstrip("/"), "https://api.cborg.lbl.gov/v1")
                self.assertEqual(client.api_key, "fixture-default")
            saved = configs.save_editor(selected, editor["revision"],
                {"ideation": {"api_key_env": "ROLE_KEY"}},
                {"alpha": {"base_url": "http://127.0.0.1:9/v1", "api_key_env": "ENDPOINT_KEY"}})
            with model_routing.create_selfhosted_client("role/ideation") as client:
                self.assertEqual(str(client.base_url).rstrip("/"), "http://127.0.0.1:9/v1")
                self.assertEqual(client.api_key, "fixture-role")
            self.assertEqual(configs.diagnostic_assignment(selected, "ideation")["api_key_env"], "ROLE_KEY")
            saved = configs.save_editor(selected, saved["revision"], {"ideation": {"api_key_env": None}}, {})
            with model_routing.create_selfhosted_client("role/ideation") as client:
                self.assertEqual(client.api_key, "fixture-endpoint")
            cleared = configs.save_editor(selected, saved["revision"], {}, {"alpha": {"base_url": None, "api_key_env": None}})
            self.assertIsNone(cleared["endpoints"]["alpha"]["base_url"])
            self.assertIsNone(cleared["endpoints"]["alpha"]["api_key_env"])
            with model_routing.create_selfhosted_client("role/ideation") as client:
                self.assertEqual(client.api_key, "fixture-default")
            self.assertEqual(snapshot["base_url"], "https://api.cborg.lbl.gov/v1")
            self.assertNotIn("fixture-default", json.dumps(configs.models(selected)))

    def test_provider_switch_validates_final_state_and_rejects_unknown_or_null(self):
        from fastapi.testclient import TestClient

        cfg = minimal_config()
        cfg["roles"]["ideation"].update(max_tokens=4096, temperature=0.4, api_key_env="ROLE_KEY")
        target = self.write_preset(cfg=cfg)
        os.environ[model_routing.ROLE_CONFIG_ENV] = str(target)
        with TestClient(create_app(self.root), base_url="http://127.0.0.1:8765") as client:
            headers = self.editor_headers(client)
            selected = self.preset_id(client, "ais_roles.yaml")
            before = client.get(f"/api/models/editor?config_id={selected}").json()
            original = target.read_bytes()
            def save(endpoint_patch, roles=None, revision=before["revision"]):
                return client.patch("/api/models/editor", headers=headers, json={
                    "config_id": selected, "expected_revision": revision,
                    "endpoints": {"alpha": endpoint_patch}, "roles": roles or {}})
            for provider in ("unknown-fixture-provider", None):
                response = save({"provider": provider})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(target.read_bytes(), original)
                self.assertNotIn("unknown-fixture-provider", response.text)
            rejected = save({"provider": "openai-codex"})
            self.assertEqual(rejected.status_code, 422, rejected.text)
            self.assertEqual(target.read_bytes(), original)
            rejected = save({"provider": "openai-codex", "base_url": None, "api_key_env": None})
            self.assertEqual(rejected.status_code, 422, rejected.text)
            self.assertEqual(target.read_bytes(), original)
            accepted = save({"provider": "openai-codex", "base_url": None, "api_key_env": None},
                {"ideation": {"max_tokens": None, "temperature": None, "api_key_env": None}})
            self.assertEqual(accepted.status_code, 200, accepted.text)
            codex_bytes = target.read_bytes()
            revision = accepted.json()["revision"]
            for endpoint_patch in ({"provider": "openai", "base_url": None},
                    {"provider": "openai"}, {"base_url": "https://attacker.invalid/v1"}, {"api_key_env": "OTHER_KEY"}):
                rejected = save(endpoint_patch, revision=revision)
                self.assertEqual(rejected.status_code, 422, rejected.text)
                self.assertEqual(target.read_bytes(), codex_bytes)
            compatible = save({"provider": "cborg", "base_url": None, "api_key_env": None}, revision=revision)
            self.assertEqual(compatible.status_code, 200, compatible.text)
            self.assertEqual(compatible.json()["endpoints"]["alpha"]["provider"], "cborg")

    def test_implicit_cborg_credential_is_guarded_and_invalid_source_provider_is_rejected(self):
        cfg = minimal_config()
        cfg["endpoints"]["alpha"] = {"provider": "cborg", "provides": ["text"]}
        cfg["roles"]["ideation"]["model"] = "prefix-fixture-private-key-suffix"
        target = self.write_preset(cfg=cfg)
        with patch.dict(os.environ, {model_routing.ROLE_CONFIG_ENV: str(target), "CBORG_API_KEY": "fixture-private-key"}):
            configs = Configs(self.root)
            selected = configs.presets()["selected_role_config_id"]
            with self.assertRaises(InvalidConfiguration):
                configs.editor(selected)
            self.assertNotIn("fixture-private-key", json.dumps(configs.models(selected)))
            cfg["roles"]["ideation"]["model"] = "m1"
            for provider in ("unknown", None):
                cfg["endpoints"]["alpha"]["provider"] = provider
                target.write_text(yaml.safe_dump(cfg), encoding="utf-8")
                with self.assertRaises(InvalidConfiguration):
                    configs.editor(selected)
                with self.assertRaises(model_routing.RoleConfigError):
                    model_routing.create_selfhosted_client("role/ideation")
