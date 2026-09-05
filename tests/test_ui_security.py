"""Local HTTP, artifact and process ownership boundaries, without production models."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from uuid import uuid4
import psutil
from fastapi.testclient import TestClient

from ai_scientist.ui.app import create_app
from ai_scientist.ui.artifacts import artifact_file, list_runs, run_detail
from ai_scientist.ui.store import Store
from ai_scientist.ui.worker import Supervisor, capture_descendants, identity, matching_process, terminate_owned


class SecurityBoundaries(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / "ui_data" / "checks" / str(uuid4())
        self.store = Store(self.root)

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_mutations_need_both_origin_and_token_and_preserve_request_body(self):
        app = create_app(self.root)
        idea = app.state.store.add_idea("generation", {"Title":"Draft"})
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            token = client.get("/api/bootstrap").json()["request_token"]
            self.assertEqual(client.get("/api/bootstrap",headers={"Host":"attacker.example"}).status_code,400)
            url = "/api/ideas/"+idea["id"]
            body = {"expected_revision":1,"idea":{"Name":"safe_export","Title":"<script>not executed</script>"}}
            for headers in ({}, {"Origin":"http://127.0.0.1:8765"}, {"Origin":"https://attacker.example","X-Studio-Token":token}):
                self.assertEqual(client.patch(url,json=body,headers=headers).status_code,403)
            headers = {"Origin":"http://127.0.0.1:8765","X-Studio-Token":token}
            response = client.patch(url,json=body,headers=headers)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()["idea"],body["idea"])
            self.assertEqual(client.patch(url,json=body,headers=headers).status_code,409)
            exported = client.get(url+"/export")
            self.assertEqual(exported.json(),[body["idea"]])
            self.assertTrue(exported.headers["content-disposition"].startswith("attachment"))
            self.assertIn("default-src 'self'", exported.headers["content-security-policy"])
            unsafe = client.patch(url, json={"expected_revision":2,"idea":{**body["idea"],"Name":"../escape"}}, headers=headers)
            self.assertEqual(unsafe.status_code,200)
            self.assertEqual(client.get(url+"/export").status_code,422)

    def test_generated_html_download_and_unknown_path_rejection(self):
        run = self.root/"experiments"/"historical"
        run.mkdir(parents=True)
        (run/"tree_plot.html").write_text("<script>alert(1)</script>",encoding="utf-8")
        (run/"idea.json").write_text('{"Title":"History"}',encoding="utf-8")
        id = list_runs(self.root,self.store)[0]["id"]
        detail = run_detail(self.root,self.store,id)
        self.assertEqual(detail["state"],"unavailable")
        html = next(item for item in detail["artifacts"] if item["name"]=="tree_plot.html")
        path,mime,attachment = artifact_file(self.root,self.store,id,html["id"])
        self.assertTrue(attachment)
        for invalid in ("../ui_data/ui.sqlite3", str(self.store.db_path), "unknown"):
            with self.assertRaises(FileNotFoundError):
                artifact_file(self.root,self.store,id,invalid)
        outside = self.root/"outside.png"
        outside.write_bytes(b"not a real image")
        link = run/"escape.png"
        try:
            link.symlink_to(outside)
        except OSError:
            if sys.platform == "win32":
                # Windows junctions exercise the same resolve containment without symlink privilege.
                external = self.root/"outside_directory"
                external.mkdir()
                (external/"escape.png").write_bytes(b"outside")
                junction = run/"escape_directory"
                result = subprocess.run(["cmd.exe","/c","mklink","/J",str(junction),str(external)],capture_output=True)
                self.assertEqual(result.returncode,0,result.stderr.decode(errors="replace"))
            else:
                raise
        detail = run_detail(self.root,self.store,id)
        self.assertFalse(any(item["name"]=="escape.png" for item in detail["artifacts"]))

    def test_stop_targets_recorded_children_not_unrelated_process_or_reused_identity(self):
        directory = self.root/"ownership"
        directory.mkdir()
        marker = directory/"child.json"
        code = "import subprocess,sys,time,json; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); Path(sys.argv[1]).write_text(json.dumps({'pid':p.pid})); time.sleep(60)"
        owner = subprocess.Popen([sys.executable,"-c",code,str(marker)])
        unrelated = subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])
        records = []
        try:
            deadline = time.monotonic()+10
            while not marker.exists() and time.monotonic()<deadline:
                time.sleep(.05)
            self.assertTrue(marker.exists(),"Owned child did not start")
            owner_record = identity(psutil.Process(owner.pid))
            records = capture_descendants(directory,psutil.Process(owner.pid))
            self.assertIn(json.loads(marker.read_text())["pid"], [item["pid"] for item in records])
            unrelated_record = identity(psutil.Process(unrelated.pid))
            stale = {**unrelated_record,"created":unrelated_record["created"]-1}
            self.assertIsNone(matching_process(stale))
            terminate_owned([*records,stale],include=owner_record)
            owner.wait(timeout=5)
            self.assertIsNone(unrelated.poll())
            self.assertTrue(all(matching_process(item) is None for item in records))
            job = self.store.create_job("idea",str(uuid4()),{})
            self.store.job_dir(job["id"]).mkdir(parents=True)
            self.store.update_job(job["id"],state="running",pid=unrelated_record["pid"],process_created=unrelated_record["created"])
            Supervisor(self.store).close()
            Supervisor(self.store).reconcile()
            self.assertEqual(self.store.get_job(job["id"])["state"],"running")
            self.assertIsNone(unrelated.poll())
            self.store.update_job(job["id"],state="running",pid=stale["pid"],process_created=stale["created"])
            Supervisor(self.store).reconcile()
            self.assertEqual(self.store.get_job(job["id"])["state"],"interrupted")
            self.assertIsNone(unrelated.poll())
        finally:
            terminate_owned(records)
            for process in (owner,unrelated):
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)
