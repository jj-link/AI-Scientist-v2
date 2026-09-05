"""Isolated worker state transitions use actual finalized and started attempts."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from uuid import uuid4

WORKER_SCENARIO = r'''
import json, sys
from pathlib import Path
from uuid import uuid4
from ai_scientist.ui.store import Store
from ai_scientist.ui.worker import run_job, write_json
from ai_scientist.progress import Cancelled
from ai_scientist import llm, perform_ideation_temp_free
root, scenario = Path(sys.argv[1]), sys.argv[2]
store = Store(root)
request = {"research_question":"Controlled worker state verification", "attempts":10 if scenario=="stopped" else 2, "rounds":5}
job = store.create_job("idea",str(uuid4()),request)
directory = store.job_dir(job["id"])
directory.mkdir(parents=True)
write_json(directory/"request.json", request)
(directory/"role_config.yaml").write_text("endpoints: {}\nroles: {}\n",encoding="utf-8")
llm.create_client = lambda *_: (None,"verification")
def generate(*args,on_event,**kwargs):
    results=[]
    for attempt in (1,2):
        on_event({"type":"attempt_started","phase":"generation","data":{"attempt":attempt}})
        if scenario=="stopped":
            raise Cancelled("Controlled cancellation")
        if scenario=="partial" and attempt==1:
            idea={"Name":"verification","Title":"Controlled proposal"}
            on_event({"type":"proposal_finalized","phase":"generation","data":{"attempt":attempt,"idea":idea}})
            results.append(idea)
        else:
            on_event({"type":"attempt_failed","phase":"generation","data":{"attempt":attempt,"code":"controlled_failure"}})
    return results
perform_ideation_temp_free.generate_temp_free_idea=generate
run_job(store,job["id"])
record=store.get_job(job["id"])
print(json.dumps({"state":record["state"],"result":record["result"],"ideas":len(store.ideas()),"events":store.events(job["id"])}))
'''


class WorkerOutcomes(unittest.TestCase):
    def setUp(self):
        self.repo = Path(__file__).resolve().parents[1]
        self.directory = self.repo/"ui_data"/"checks"/str(uuid4())
        self.directory.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.directory)

    def run_scenario(self, scenario):
        process = subprocess.run([sys.executable,"-c",WORKER_SCENARIO,str(self.directory),scenario],cwd=self.repo,capture_output=True,text=True,encoding="utf-8",timeout=30)
        self.assertEqual(process.returncode,0,process.stderr)
        return json.loads(process.stdout.splitlines()[-1])

    def test_zero_proposals_is_failed_not_completed(self):
        result=self.run_scenario("zero")
        self.assertEqual(result["state"],"failed")
        self.assertEqual(result["result"],{"finalized":0,"attempted":2,"failed_attempts":2})
        self.assertEqual(result["ideas"],0)

    def test_partial_generation_retains_usable_card_without_raw_event_content(self):
        result=self.run_scenario("partial")
        self.assertEqual(result["state"],"partial")
        self.assertEqual(result["result"],{"finalized":1,"attempted":2,"failed_attempts":1})
        self.assertEqual(result["ideas"],1)
        self.assertFalse(any("idea" in event["data"] for event in result["events"]))

    def test_stop_reports_started_not_requested_attempt_count(self):
        result=self.run_scenario("stopped")
        self.assertEqual(result["state"],"stopped")
        self.assertEqual(result["result"],{"finalized":0,"attempted":1})
