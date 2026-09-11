"""Failed-run lifecycle boundaries, using local snapshots and no experiment workers."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import yaml

from ai_scientist import model_routing
from ai_scientist.ui import model_settings
from ai_scientist.ui.app import create_app
from test_ui_config_probe import endpoint


class JobLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / "ui_data" / "checks" / str(uuid4())
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.addCleanup(model_routing._cache.clear)
        self.url = self.enterContext(endpoint("fixture-model"))
        roles = ("ideation", "experiment_code", "experiment_feedback", "visual_feedback",
                 "findings_synthesis", "tree_scoring", "plot_generation", "citation",
                 "writeup", "writeup_small", "review")
        self.tasks = {
            name: {"server": "local", "model": "fixture-model", "timeout": 73,
                   "requires": ["vision" if name == "visual_feedback" else "text"]}
            for name in roles
        }
        model_settings.save_settings_atomic(self.root, {
            "local": {"api_format": "openai", "address": self.url,
                      "capabilities": ["text", "vision"], "timeout": 91}}, self.tasks)
        model_settings.set_extra_setting("experiment_execution", {"timeout": 123}, self.root)
        self.baseline = {
            "exp_name": "run",
            "agent": {"num_workers": 1, "steps": 12,
                      "stages": {"stage1_max_iters": 2, "stage2_max_iters": 3,
                                 "stage3_max_iters": 4, "stage4_max_iters": 5,
                                 "review_threshold": 0.7},
                      "multi_seed_eval": {"num_seeds": 2, "aggregate": "median"},
                      "search": {"debug_prob": 0.25}},
            "exec": {"timeout": 30, "format_tb_ipython": False},
            "report": {"format": "paper"},
        }
        self.baseline_path = self.root / "bfts_config.yaml"
        self.baseline_path.write_text(yaml.safe_dump(self.baseline), encoding="utf-8")
        self.app = create_app(self.root)
        self.store = self.app.state.store
        self.launch_worker = self.enterContext(patch.object(self.app.state.supervisor, "launch"))
        self.tools = self.enterContext(patch.object(self.app.state.configs, "prerequisites",
                                                   return_value={"ok": True, "tools": []}))
        # Deliberately omit lifespan: no supervisor recovery or diagnostic/model tasks
        # run in the background while tests construct terminal worker outcomes.
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8765")
        self.addCleanup(self.client.close)
        bootstrap = self.client.get("/api/bootstrap").json()
        self.headers = {"Origin": "http://127.0.0.1:8765",
                        "X-Studio-Token": bootstrap["request_token"]}
        self.idea = self.store.add_idea("fixture-generation", {
            "Name": "lifecycle_fixture", "Title": "Original scientific question",
            "Short Hypothesis": "Independent repetitions preserve the original workload.",
            "Abstract": "Compare fresh runs without inheriting partially generated code.",
            "Experiments": [{"method": "compare", "custom": [1, 2]}],
            "Risk Factors and Limitations": [], "Unrecognized": {"keep": True},
        })
        editor = self.client.get("/api/models/editor").json()
        self.body = {
            "request_id": str(uuid4()), "idea_id": self.idea["id"],
            "idea_revision": self.idea["revision"],
            "bfts_config_id": bootstrap["selected_bfts_config_id"],
            "execution_acknowledged": True,
            "model_settings_revision": editor["revision"],
            "role_assignments": {name: {key: value for key, value in role.items() if key != "requires"}
                                 for name, role in editor["roles"].items()},
            "run_settings": {"num_workers": 3, "num_seeds": 7, "execution_timeout": 12.5,
                             "stage_iterations": {"stage1": 11, "stage2": 13,
                                                  "stage3": 17, "stage4": 19}},
        }

    def start(self, *, state="failed", body=None, **fields):
        response = self.client.post("/api/experiments", headers=self.headers,
                                    json=body or self.body)
        self.assertEqual(response.status_code, 202, response.text)
        result = response.json()
        if state != "starting" or fields:
            self.store.update_job(result["job_id"], state=state, **fields)
        return result

    def restart(self, source, body=None):
        return self.client.post(f"/api/jobs/{source['job_id']}/restart", headers=self.headers,
                                json=body or {"request_id": str(uuid4()),
                                              "execution_acknowledged": True})

    def delete(self, source):
        return self.client.request("DELETE", f"/api/jobs/{source['job_id']}",
                                   headers=self.headers, json={})

    def run_dir(self, job):
        return self.root / "experiments" / job["run_id"]

    def snapshots(self, job):
        directory = self.store.job_dir(job["job_id"])
        return {name: (directory / name).read_bytes()
                for name in ("request.json", "idea.json", "model_settings.json", "bfts_config.yaml")}

    def assert_deleted(self, job):
        for suffix in ("", "/events", "/log", "/diagnostic"):
            response = self.client.get(f"/api/jobs/{job['job_id']}{suffix}")
            self.assertEqual(response.status_code, 404, response.text)
        self.assertNotIn(job["job_id"], [item["id"] for item in self.client.get("/api/jobs").json()["jobs"]])
        self.assertFalse(self.run_dir(job).exists())
        self.assertFalse(self.store.job_dir(job["job_id"]).exists())
        self.assertEqual(self.store.events(job["job_id"]), [])
        self.assertIsNone(self.store.get_diagnostic(job["job_id"]))

    def directory_link(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            if sys.platform != "win32":
                raise
            result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_restart_uses_frozen_inputs_and_fresh_output_after_current_edits(self):
        source = self.start()
        original = self.snapshots(source)
        source_view = self.client.get(f"/api/jobs/{source['job_id']}").json()
        checkpoint = self.run_dir(source) / "checkpoint.pkl"
        checkpoint.write_bytes(b"original partial checkpoint")
        (self.run_dir(source) / "generated.py").write_text("raise RuntimeError('partial')", encoding="utf-8")
        edited = self.store.save_idea(self.idea["id"], self.idea["revision"],
                                      {**self.idea["idea"], "Title": "Changed proposal", "Experiments": []})
        self.baseline_path.write_text("exp_name: invalid-current-preset\nagent: {}\nexec: {}\n", encoding="utf-8")
        changed_tasks = {name: {**task, "model": "not-served-now"} for name, task in self.tasks.items()}
        model_settings.save_settings_atomic(self.root, {}, changed_tasks)
        current_settings = self.store.current_settings()
        response = self.restart(source)
        self.assertEqual(response.status_code, 202, response.text)
        restarted = response.json()
        self.assertNotEqual(restarted["job_id"], source["job_id"])
        self.assertNotEqual(restarted["run_id"], source["run_id"])
        copies = self.snapshots(restarted)
        for name in ("idea.json", "model_settings.json"):
            self.assertEqual(json.loads(copies[name]), json.loads(original[name]), name)
        self.assertEqual(yaml.safe_load(copies["bfts_config.yaml"]),
                         yaml.safe_load(original["bfts_config.yaml"]))
        self.assertEqual(json.loads(copies["request.json"])["idea"], self.idea["idea"])
        self.assertEqual(json.loads(copies["request.json"])["run_settings"], self.body["run_settings"])
        self.assertTrue(self.run_dir(restarted).is_dir())
        self.assertEqual(list(self.run_dir(restarted).iterdir()), [])
        self.assertEqual(checkpoint.read_bytes(), b"original partial checkpoint")
        self.assertEqual(self.snapshots(source), original)
        self.assertEqual(self.client.get(f"/api/jobs/{source['job_id']}").json(), source_view)
        self.assertEqual(self.store.get_idea(self.idea["id"]), edited)
        self.assertEqual(self.store.current_settings(), current_settings)
        self.assertEqual(self.launch_worker.call_count, 2)

    def test_restart_retry_survives_completion_and_source_deletion_but_not_target_deletion(self):
        source = self.start()
        retry = {"request_id": str(uuid4()), "execution_acknowledged": True}
        response = self.restart(source, retry)
        self.assertEqual(response.status_code, 202, response.text)
        restarted = response.json()
        self.assertEqual(self.restart(source, retry).json(), restarted)
        self.store.update_job(restarted["job_id"], state="failed")
        self.assertEqual(self.restart(source, retry).json(), restarted)
        self.assertEqual(self.delete(source).json(), {"deleted": True})
        recovered = self.restart(source, retry)
        self.assertEqual(recovered.status_code, 202, recovered.text)
        self.assertEqual(recovered.json(), restarted)
        self.assertEqual(self.delete(restarted).json(), {"deleted": True})
        self.assertEqual(self.delete(restarted).json(), {"deleted": True})
        self.assertEqual(self.restart(source, retry).status_code, 409)
        self.assertEqual(self.client.post("/api/experiments", headers=self.headers,
                                         json=self.body).status_code, 409)
        reused_start = {**self.body, "request_id": retry["request_id"]}
        self.assertEqual(self.client.post("/api/experiments", headers=self.headers,
                                         json=reused_start).status_code, 409)
        self.assert_deleted(source)
        self.assert_deleted(restarted)
        self.assertEqual(self.launch_worker.call_count, 2)

    def test_restart_request_cannot_be_reused_for_another_source_or_start(self):
        first = self.start()
        second = self.start(body={**self.body, "request_id": str(uuid4())})
        retry = {"request_id": str(uuid4()), "execution_acknowledged": True}
        response = self.restart(first, retry)
        self.assertEqual(response.status_code, 202, response.text)
        self.store.update_job(response.json()["job_id"], state="completed")
        self.assertEqual(self.restart(first, retry).json(), response.json())
        self.assertEqual(self.restart(second, retry).status_code, 409)
        self.assertEqual(self.restart(second, {**retry, "request_id": first["job_id"]}).status_code, 409)
        self.assertEqual(self.client.post("/api/experiments", headers=self.headers,
                                         json={**self.body, "request_id": retry["request_id"]}).status_code, 409)
        self.assertEqual(self.launch_worker.call_count, 3)

    def test_legacy_role_snapshot_is_recovered_without_reading_current_models(self):
        source = self.start()
        directory = self.store.job_dir(source["job_id"])
        legacy = {"endpoints": {"local": {"base_url": self.url, "provides": ["text", "vision"]}},
                  "roles": {name: {"endpoint": "local", "model": "fixture-model",
                                   "timeout": task["timeout"], "requires": task["requires"]}
                            for name, task in self.tasks.items()}}
        legacy_bytes = yaml.safe_dump(legacy).encode("utf-8")
        (directory / "role_config.yaml").write_bytes(legacy_bytes)
        (directory / "model_settings.json").unlink()
        model_settings.save_settings_atomic(self.root, {}, {
            name: {**task, "model": "not-served-now"} for name, task in self.tasks.items()})
        response = self.restart(source)
        self.assertEqual(response.status_code, 202, response.text)
        recovered = json.loads(self.snapshots(response.json())["model_settings.json"])
        self.assertEqual(recovered["roles"], legacy["roles"])
        self.assertEqual(recovered["endpoints"]["local"]["base_url"], self.url)
        self.assertEqual((directory / "role_config.yaml").read_bytes(), legacy_bytes)
        self.assertFalse((directory / "model_settings.json").exists())

    def test_missing_frozen_inputs_fail_instead_of_falling_back_to_current_inputs(self):
        # Each file supplies a different scientific input; losing any must fail closed.
        for name in ("request.json", "idea.json", "model_settings.json", "bfts_config.yaml"):
            with self.subTest(snapshot=name):
                source = self.start(body={**self.body, "request_id": str(uuid4())})
                (self.store.job_dir(source["job_id"]) / name).unlink()
                launched = self.launch_worker.call_count
                response = self.restart(source)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self.launch_worker.call_count, launched)
                self.assertEqual(self.client.get(f"/api/jobs/{source['job_id']}").json()["state"], "failed")

    def test_corrupt_current_snapshot_does_not_fall_back_to_legacy_or_current_settings(self):
        source = self.start()
        directory = self.store.job_dir(source["job_id"])
        settings = json.loads((directory / "model_settings.json").read_bytes())
        (directory / "role_config.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
        (directory / "model_settings.json").write_text("{truncated", encoding="utf-8")
        response = self.restart(source)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.launch_worker.call_count, 1)
        self.assertEqual([job["id"] for job in self.store.jobs()], [source["job_id"]])
        self.assertTrue(self.run_dir(source).is_dir())

    def test_restart_rechecks_external_readiness_before_reserving_compute(self):
        source = self.start()
        self.tools.return_value = {"ok": False, "tools": [
            {"name": "pdflatex", "available": False, "error": "Fixture compiler unavailable"}]}
        response = self.restart(source)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual([job["id"] for job in self.store.jobs()], [source["job_id"]])
        self.assertEqual(self.launch_worker.call_count, 1)
        self.assertTrue(self.run_dir(source).is_dir())

    def test_acknowledgement_and_failed_experiment_state_are_required(self):
        source = self.start(state="starting")
        self.assertEqual(self.restart(source).status_code, 409)
        self.assertEqual(self.delete(source).status_code, 409)
        self.store.update_job(source["job_id"], state="stopped")
        self.assertEqual(self.restart(source).status_code, 409)
        self.assertEqual(self.delete(source).status_code, 409)
        failed = self.start(body={**self.body, "request_id": str(uuid4())})
        self.assertEqual(self.restart(failed, {"request_id": str(uuid4()),
                                              "execution_acknowledged": False}).status_code, 422)
        idea_job = self.store.create_job("idea", str(uuid4()), {"research_question": "Fixture"})
        self.store.update_job(idea_job["id"], state="failed")
        self.assertEqual(self.restart({"job_id": idea_job["id"]}).status_code, 409)
        self.assertEqual(self.delete({"job_id": idea_job["id"]}).status_code, 409)
        self.assertEqual(self.restart({"job_id": str(uuid4())}).status_code, 404)
        self.assertEqual(self.launch_worker.call_count, 2)

    def test_another_active_job_keeps_compute_reserved(self):
        source = self.start()
        active = self.store.create_job("idea", str(uuid4()), {"research_question": "Reserved slot"})
        response = self.restart(source)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.active_job()["id"], active["id"])
        self.assertEqual(self.launch_worker.call_count, 1)
        self.assertTrue(self.run_dir(source).is_dir())

    def diagnostic_source(self):
        self.store.save_assistant_settings(True, {"endpoint": "local", "model": "fixture-model"})
        source = self.start()
        self.store.scan_diagnostics()
        owner = {"pid": 987654321, "created": 1.0, "token": str(uuid4())}
        claimed = self.store.claim_diagnostic(owner)
        self.assertEqual(claimed["job_id"], source["job_id"])
        return source, owner

    def test_delete_removes_only_owned_artifacts_and_preserves_idea_and_conversation(self):
        source, owner = self.diagnostic_source()
        self.store.finish_diagnostic(source["job_id"], owner["token"], result={
            "summary": "Fixture failure", "issue": {"title": "Fixture", "body": "Details"}})
        self.store.add_event(source["job_id"], "failed", data={"code": "fixture_failure"})
        directory = self.store.job_dir(source["job_id"])
        (directory / "technical.log").write_text("fixture traceback", encoding="utf-8")
        (directory / "diagnosis.json").write_text('{"private":"fixture"}', encoding="utf-8")
        (self.run_dir(source) / "checkpoint.pkl").write_bytes(b"failed checkpoint")
        conversation_request = str(uuid4())
        conversation, _ = self.store.claim_conversation(conversation_request, "Discuss the saved idea", {},
                                                         idea_id=self.idea["id"])
        self.store.finish_conversation(conversation["id"], conversation_request, message="Saved discussion")
        conversation_before = self.store.get_conversation(conversation["id"])
        other = self.start(body={**self.body, "request_id": str(uuid4())}, state="completed")
        other_snapshots = self.snapshots(other)
        other_file = self.run_dir(other) / "result.txt"
        other_file.write_text("unrelated result", encoding="utf-8")
        external = self.root / "unrelated-files"
        external.mkdir()
        preserved = external / "keep.txt"
        preserved.write_bytes(b"external target")
        self.directory_link(self.run_dir(source) / "external-link", external)
        self.directory_link(directory / "external-link", external)
        response = self.delete(source)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"deleted": True})
        self.assert_deleted(source)
        self.assertEqual(preserved.read_bytes(), b"external target")
        self.assertEqual(other_file.read_text(encoding="utf-8"), "unrelated result")
        self.assertEqual(self.snapshots(other), other_snapshots)
        self.assertEqual(self.client.get(f"/api/jobs/{other['job_id']}").json()["state"], "completed")
        self.assertEqual(self.store.get_idea(self.idea["id"]), self.idea)
        self.assertEqual(self.store.get_conversation(conversation["id"]), conversation_before)

    def test_delete_refuses_matching_worker_and_recorded_descendant_until_they_exit(self):
        source = self.start(pid=987654321, process_created=1.0)
        directory = self.store.job_dir(source["job_id"])
        child = {"pid": 987654322, "created": 2.0}
        (directory / "processes.json").write_text(json.dumps([child]), encoding="utf-8")
        with patch("ai_scientist.ui.worker.matching_process",
                   side_effect=lambda record: object() if record and record.get("pid") == 987654321 else None):
            response = self.delete(source)
            self.assertEqual(response.status_code, 409, response.text)
        with patch("ai_scientist.ui.worker.matching_process",
                   side_effect=lambda record: object() if record == child else None):
            response = self.delete(source)
            self.assertEqual(response.status_code, 409, response.text)
        self.assertTrue(directory.is_dir())
        self.assertTrue(self.run_dir(source).is_dir())
        with patch("ai_scientist.ui.worker.matching_process", return_value=None):
            response = self.delete(source)
            self.assertEqual(response.status_code, 200, response.text)
        self.assert_deleted(source)

    def test_delete_waits_for_analysis_and_publication(self):
        source, owner = self.diagnostic_source()
        self.assertEqual(self.delete(source).status_code, 409)
        self.store.finish_diagnostic(source["job_id"], owner["token"], result={
            "summary": "Fixture failure", "issue": {"title": "Fixture", "body": "Details"}})
        self.store.claim_issue(source["job_id"], 1, owner)
        self.assertEqual(self.delete(source).status_code, 409)
        self.assertTrue(self.store.job_dir(source["job_id"]).is_dir())
        self.store.finish_issue(source["job_id"], owner["token"], "not_published")
        response = self.delete(source)
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_deleted(source)

    def test_delete_refuses_an_unknown_publication_outcome(self):
        source, owner = self.diagnostic_source()
        self.store.finish_diagnostic(source["job_id"], owner["token"], result={
            "summary": "Fixture failure", "issue": {"title": "Fixture", "body": "Details"}})
        self.store.claim_issue(source["job_id"], 1, owner)
        self.store.finish_issue(source["job_id"], owner["token"], "unknown")
        response = self.delete(source)
        self.assertEqual(response.status_code, 409, response.text)
        diagnostic = self.client.get(f"/api/jobs/{source['job_id']}/diagnostic").json()
        self.assertEqual(diagnostic["diagnostic"]["issue"]["state"], "unknown")
        self.assertTrue(self.store.job_dir(source["job_id"]).is_dir())

    def test_redirected_owned_directories_fail_closed_without_deleting_targets(self):
        source = self.start()
        # Exercise both owned leaves and their top-level parents. All targets stay
        # inside this disposable fixture, never in the developer's own run tree.
        paths = (self.run_dir(source), self.store.job_dir(source["job_id"]),
                 self.root / "experiments", self.store.data_dir / "jobs")
        for index, path in enumerate(paths):
            with self.subTest(directory=path.name):
                target = self.root / f"redirect-target-{index}"
                path.rename(target)
                self.directory_link(path, target)
                try:
                    response = self.delete(source)
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertEqual(self.client.get(f"/api/jobs/{source['job_id']}").json()["state"], "failed")
                    self.assertTrue(target.is_dir())
                    self.assertEqual(self.launch_worker.call_count, 1)
                finally:
                    if path.is_symlink():
                        path.unlink()
                    else:
                        path.rmdir()  # Windows directory junction, not its target.
                    target.rename(path)
        self.assertEqual(self.delete(source).status_code, 200)
        self.assert_deleted(source)

    def test_foreign_run_id_cannot_authorize_deletion(self):
        foreign = self.root / "experiments" / "historical"
        foreign.mkdir(parents=True)
        output = foreign / "result.txt"
        output.write_bytes(b"unowned historical run")
        source = self.start(run_id="historical")
        response = self.delete(source)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(output.read_bytes(), b"unowned historical run")
        self.assertTrue(self.store.job_dir(source["job_id"]).is_dir())
        self.assertEqual(self.client.get(f"/api/jobs/{source['job_id']}").json()["state"], "failed")

    def test_cleanup_failure_remains_retryable_without_restarting_deleted_work(self):
        source = self.start()
        with patch("ai_scientist.ui.job_lifecycle.shutil.rmtree", side_effect=PermissionError("locked fixture")):
            response = self.delete(source)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.client.get(f"/api/jobs/{source['job_id']}").json()["state"], "failed")
        self.assertEqual(self.restart(source).status_code, 409)
        self.assertEqual(self.client.post("/api/experiments", headers=self.headers, json=self.body).status_code, 409)
        with self.assertRaises(KeyError):
            self.store.add_event(source["job_id"], "late_callback")
        self.assertEqual(self.delete(source).status_code, 200)
        self.assert_deleted(source)
        self.assertEqual(self.launch_worker.call_count, 1)

    def test_invalid_process_identity_cannot_authorize_deletion(self):
        source = self.start()
        ownership = self.store.job_dir(source["job_id"]) / "processes.json"
        ownership.write_text(json.dumps([{"pid": 987654321}]), encoding="utf-8")
        self.assertEqual(self.delete(source).status_code, 409)
        self.assertTrue(self.run_dir(source).is_dir())
        self.assertEqual(json.loads(ownership.read_bytes()), [{"pid": 987654321}])
        ownership.write_text("[]", encoding="utf-8")
        self.assertEqual(self.delete(source).status_code, 200)
        self.assert_deleted(source)
