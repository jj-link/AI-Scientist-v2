"""Per-run experiment settings are strict and isolated from selected presets."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import yaml

from ai_scientist import model_routing
from ai_scientist.ui.app import create_app
from test_ui_config_probe import endpoint


class ExperimentSettingsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / "ui_data" / "checks" / str(uuid4())
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.addCleanup(model_routing._cache.clear)
        url = self.enterContext(endpoint("fixture-model"))
        roles = ("ideation", "experiment_code", "experiment_feedback", "visual_feedback",
                 "findings_synthesis", "tree_scoring", "plot_generation", "citation",
                 "writeup", "writeup_small", "review")
        self.role_config = {
            "endpoints": {"local": {"base_url": url, "provides": ["text", "vision"]}},
            "roles": {name: {"endpoint": "local", "model": "fixture-model",
                              "requires": ["vision" if name == "visual_feedback" else "text"]}
                      for name in roles},
        }
        self.role_path = self.root / "ais_roles.yaml"
        self.role_path.write_text(yaml.safe_dump(self.role_config), encoding="utf-8")
        self.enterContext(patch.dict("os.environ", {
            model_routing.ROLE_CONFIG_ENV: str(self.role_path),
        }))
        stages = {"stage1_max_iters": 2, "stage2_max_iters": 3,
                  "stage3_max_iters": 4, "stage4_max_iters": 5, "review_threshold": 0.7}
        seeds = {"num_seeds": 2, "aggregate": "median"}
        agent = {"num_workers": 1, "steps": 12, "stages": stages,
                 "multi_seed_eval": seeds, "search": {"debug_prob": 0.25}}
        self.baseline = {
            "exp_name": "run", "agent": agent,
            "exec": {"timeout": 30, "format_tb_ipython": False},
            "report": {"format": "paper"},
            # YAML aliases must not make edits change unrelated baseline metadata.
            "archived_agent": agent, "archived_stages": stages, "archived_seeds": seeds,
        }
        self.baseline_path = self.root / "bfts_config.yaml"
        self.write_baseline(self.baseline)
        self.other_path = self.root / "bfts_config.other.yaml"
        self.other_path.write_text("exp_name: other\nagent: {}\nexec: {}\n", encoding="utf-8")
        self.app = create_app(self.root)
        self.launch_worker = self.enterContext(patch.object(self.app.state.supervisor, "launch"))
        # Only external tool discovery is stubbed; candidate validation and HTTP model
        # availability checks run normally against the local endpoint fixture.
        self.tools = self.enterContext(patch.object(self.app.state.configs, "prerequisites",
                                                   return_value={"ok": True, "tools": []}))
        self.client = self.enterContext(TestClient(self.app, base_url="http://127.0.0.1:8765"))
        bootstrap = self.client.get("/api/bootstrap").json()
        self.headers = {"Origin": "http://127.0.0.1:8765",
                        "X-Studio-Token": bootstrap["request_token"]}
        self.idea = self.app.state.store.add_idea("fixture-generation", {
            "Name": "settings_fixture", "Title": "Isolated settings",
            "Short Hypothesis": "Changing the worker count changes concurrency.",
            "Abstract": "Compare independent per-run settings without changing the preset.",
            "Experiments": ["Measure each setting in the reserved run snapshot."],
            "Risk Factors and Limitations": [],
        })
        self.body = {
            "request_id": str(uuid4()), "idea_id": self.idea["id"],
            "idea_revision": self.idea["revision"],
            "role_config_id": bootstrap["selected_role_config_id"],
            "bfts_config_id": bootstrap["selected_bfts_config_id"],
            "execution_acknowledged": True,
            "run_settings": {"num_workers": 7, "num_seeds": 9, "execution_timeout": 12.5,
                             "stage_iterations": {"stage1": 11, "stage2": 13,
                                                  "stage3": 17, "stage4": 19}},
        }

    def write_baseline(self, config):
        self.baseline_path.write_text("# Preset remains user-owned.\n" + yaml.safe_dump(config),
                                      encoding="utf-8")

    def launch(self, body):
        response = self.client.post("/api/experiments", headers=self.headers, json=body)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["job_id"]

    def snapshot(self, job_id):
        return self.app.state.store.job_dir(job_id) / "bfts_config.yaml"

    def assert_settings(self, snapshot, settings):
        self.assertEqual(snapshot["agent"]["num_workers"], settings["num_workers"])
        self.assertEqual(snapshot["agent"]["multi_seed_eval"]["num_seeds"], settings["num_seeds"])
        self.assertEqual(snapshot["exec"]["timeout"], settings["execution_timeout"])
        self.assertEqual({f"stage{i}": snapshot["agent"]["stages"][f"stage{i}_max_iters"]
                          for i in range(1, 5)}, settings["stage_iterations"])

    def assert_unreserved(self):
        self.assertEqual(self.app.state.store.jobs(), [])
        self.assertFalse((self.app.state.store.data_dir / "jobs").exists())
        self.assertFalse((self.root / "experiments").exists())
        self.launch_worker.assert_not_called()

    def test_two_runs_keep_settings_and_unrelated_baseline_data_isolated(self):
        original = self.baseline_path.read_bytes()
        other = self.other_path.read_bytes()
        role_bytes = self.role_path.read_bytes()
        first = self.launch(self.body)
        first_bytes = self.snapshot(first).read_bytes()
        self.app.state.store.update_job(first, state="completed")
        second_body = deepcopy(self.body)
        second_body["request_id"] = str(uuid4())
        second_body["run_settings"] = {
            "num_workers": 100001, "num_seeds": 100003, "execution_timeout": 1000000,
            "stage_iterations": {"stage1": 100019, "stage2": 100043,
                                 "stage3": 100049, "stage4": 100057},
        }
        second = self.launch(second_body)
        self.assertNotEqual(first, second)
        for job_id, body in ((first, self.body), (second, second_body)):
            with self.subTest(job_id=job_id):
                snapshot = yaml.safe_load(self.snapshot(job_id).read_bytes())
                self.assert_settings(snapshot, body["run_settings"])
                self.assertEqual(snapshot["report"], self.baseline["report"])
                self.assertEqual(snapshot["exp_name"], "run")
                self.assertEqual(snapshot["agent"]["search"], self.baseline["agent"]["search"])
                self.assertEqual(snapshot["agent"]["steps"], self.baseline["agent"]["steps"])
                self.assertEqual(snapshot["agent"]["stages"]["review_threshold"], 0.7)
                self.assertEqual(snapshot["agent"]["multi_seed_eval"]["aggregate"], "median")
                self.assertIs(snapshot["exec"]["format_tb_ipython"], False)
                for key in ("archived_agent", "archived_stages", "archived_seeds"):
                    self.assertEqual(snapshot[key], self.baseline[key])
                request = self.app.state.store.get_job(job_id)["request"]
                self.assertEqual(request["run_settings"], body["run_settings"])
                recorded = json.loads((self.snapshot(job_id).parent / "request.json").read_bytes())
                self.assertEqual(recorded, request)
        self.assertEqual(self.snapshot(first).read_bytes(), first_bytes)
        self.assertEqual(self.baseline_path.read_bytes(), original)
        self.assertEqual(self.other_path.read_bytes(), other)
        self.assertEqual(self.role_path.read_bytes(), role_bytes)

    def test_retry_retains_original_request_and_snapshot_after_source_change(self):
        first = self.launch(self.body)
        original = self.snapshot(first).read_bytes()
        request_path = self.snapshot(first).parent / "request.json"
        request_bytes = request_path.read_bytes()
        recorded = self.app.state.store.get_job(first)["request"]
        self.write_baseline({"exp_name": "changed", "agent": {}, "exec": {}})
        changed_source = self.baseline_path.read_bytes()
        retry = deepcopy(self.body)
        retry["run_settings"]["num_workers"] = 99
        retry["run_settings"]["stage_iterations"]["stage4"] = 999
        self.assertEqual(self.launch(retry), first)
        self.assertEqual(self.snapshot(first).read_bytes(), original)
        self.assertEqual(request_path.read_bytes(), request_bytes)
        self.assertEqual(self.app.state.store.get_job(first)["request"], recorded)
        self.assertEqual(self.baseline_path.read_bytes(), changed_source)
        self.assertEqual(len(self.app.state.store.jobs()), 1)
        self.assertEqual(self.launch_worker.call_count, 1)

    def test_source_change_during_readiness_does_not_replace_validated_candidate(self):
        def tools_after_source_change():
            changed = deepcopy(self.baseline)
            changed["exp_name"] = "not-the-validated-workflow"
            changed["report"]["format"] = "changed-after-load"
            self.write_baseline(changed)
            return {"ok": True, "tools": []}

        self.tools.side_effect = tools_after_source_change
        job_id = self.launch(self.body)
        snapshot = yaml.safe_load(self.snapshot(job_id).read_bytes())
        self.assertEqual(snapshot["exp_name"], "run")
        self.assertEqual(snapshot["report"], self.baseline["report"])
        self.assert_settings(snapshot, self.body["run_settings"])
        self.assertEqual(yaml.safe_load(self.baseline_path.read_bytes())["exp_name"],
                         "not-the-validated-workflow")

    def test_validation_uses_overrides_instead_of_invalid_baseline_counts(self):
        baseline = deepcopy(self.baseline)
        baseline["agent"]["num_workers"] = 0
        baseline["agent"]["multi_seed_eval"]["num_seeds"] = 0
        baseline["exec"]["timeout"] = 0
        for i in range(1, 5):
            baseline["agent"]["stages"][f"stage{i}_max_iters"] = 0
        self.write_baseline(baseline)
        original = self.baseline_path.read_bytes()
        job_id = self.launch(self.body)
        self.assert_settings(yaml.safe_load(self.snapshot(job_id).read_bytes()), self.body["run_settings"])
        self.assertEqual(self.baseline_path.read_bytes(), original)

    def test_invalid_numbers_are_rejected_before_reservation(self):
        count_paths = [("num_workers",), ("num_seeds",)] + [
            ("stage_iterations", f"stage{i}") for i in range(1, 5)]
        numeric_paths = count_paths + [("execution_timeout",)]
        original = self.baseline_path.read_bytes()
        for path in numeric_paths:
            values = [0, -1, True, False, "12", "private-input-value", None,
                      float("nan"), float("inf"), -float("inf")]
            if path != ("execution_timeout",):
                values.append(1.5)
            for value in values:
                with self.subTest(path=path, value=value):
                    body = deepcopy(self.body)
                    target = body["run_settings"]
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = value
                    response = self.client.post("/api/experiments", headers={
                        **self.headers, "Content-Type": "application/json",
                    }, content=json.dumps(body))
                    self.assertEqual(response.status_code, 422, response.text)
                    errors = response.json()["detail"]["errors"]
                    self.assertIn("run_settings." + ".".join(path),
                                  [error["field"] for error in errors])
                    self.assertNotIn("private-input-value", response.text)
                    self.assertNotIn(str(self.root), response.text)
        self.assert_unreserved()
        self.assertEqual(self.baseline_path.read_bytes(), original)

    def test_missing_and_extra_settings_are_rejected_before_reservation(self):
        required = [("run_settings",)] + [
            ("run_settings", name) for name in
            ("num_workers", "num_seeds", "execution_timeout", "stage_iterations")
        ] + [("run_settings", "stage_iterations", f"stage{i}") for i in range(1, 5)]
        for path in required:
            with self.subTest(missing=path):
                body = deepcopy(self.body)
                target = body
                for key in path[:-1]:
                    target = target[key]
                del target[path[-1]]
                response = self.client.post("/api/experiments", headers=self.headers, json=body)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn(".".join(path), [error["field"] for error in
                                             response.json()["detail"]["errors"]])
        for path in ((), ("run_settings",), ("run_settings", "stage_iterations")):
            with self.subTest(extra=path):
                body = deepcopy(self.body)
                target = body
                for key in path:
                    target = target[key]
                target["unexpected"] = "private-input-value"
                response = self.client.post("/api/experiments", headers=self.headers, json=body)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn("private-input-value", response.text)
        self.assert_unreserved()

    def test_unedited_workflow_requirements_still_block_launch(self):
        baseline = deepcopy(self.baseline)
        baseline["exp_name"] = "unsupported"
        self.write_baseline(baseline)
        original = self.baseline_path.read_bytes()
        response = self.client.post("/api/experiments", headers=self.headers, json=self.body)
        self.assertEqual(response.status_code, 422, response.text)
        self.assert_unreserved()
        self.assertEqual(self.baseline_path.read_bytes(), original)

    def test_readiness_failures_still_block_launch(self):
        self.tools.return_value = {"ok": False, "tools": [
            {"name": "pdflatex", "available": False, "error": "Fixture TeX is unavailable."},
        ]}
        response = self.client.post("/api/experiments", headers=self.headers, json=self.body)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("Fixture TeX is unavailable.", response.json()["detail"]["blockers"])
        self.assert_unreserved()
        self.tools.return_value = {"ok": True, "tools": []}
        self.role_config["roles"]["experiment_code"]["model"] = "not-served"
        self.role_path.write_text(yaml.safe_dump(self.role_config), encoding="utf-8")
        response = self.client.post("/api/experiments", headers=self.headers, json=self.body)
        self.assertEqual(response.status_code, 422, response.text)
        self.assert_unreserved()
