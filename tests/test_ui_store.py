"""Durable compute-slot and saved scientific-input boundaries."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from ai_scientist.ui.store import Conflict, Store


class StoreBoundaries(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / "ui_data" / "checks" / str(uuid4())
        self.store = Store(self.root)

    def tearDown(self):
        shutil.rmtree(self.root)

    def proposal(self):
        return self.store.add_idea("generation", {
            "Name": "research", "Title": "Question", "Abstract": "Context",
            "Short Hypothesis": "Prediction", "Experiments": [{"method": "compare", "custom": [1, 2]}],
            "Risk Factors and Limitations": "Limited sample", "Unrecognized": {"keep": True},
        })

    def test_double_start_and_concurrent_slot_reservation(self):
        request_id = str(uuid4())
        first = self.store.create_job("idea", request_id, {})
        self.assertEqual(first["id"], self.store.create_job("idea", request_id, {})["id"])
        self.store.update_job(first["id"], state="completed")
        def start(_):
            try:
                return self.store.create_job("idea", str(uuid4()), {})["id"]
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(start, range(2)))
        self.assertEqual(sum(item is not None for item in result), 1)

    def test_snapshot_revision_and_unknown_fields_survive_edits(self):
        original = self.proposal()
        self.assertEqual(original["idea"]["Risk Factors and Limitations"], ["Limited sample"])
        job = self.store.create_job("experiment", str(uuid4()), {}, original["id"], 1)
        edit = {**original["idea"], "Title": "Edited title"}
        saved = self.store.save_idea(original["id"], 1, edit)
        self.assertEqual(saved["revision"], 2)
        self.assertEqual(saved["idea"]["Unrecognized"], {"keep": True})
        self.assertEqual(saved["idea"]["Experiments"][0]["custom"], [1, 2])
        self.assertEqual(self.store.get_job(job["id"])["request"]["idea"]["Title"], "Question")
        self.assertEqual(saved["original"], original["original"])
        with self.assertRaises(Conflict):
            self.store.save_idea(original["id"], 1, edit)
        self.store.update_job(job["id"], state="completed")
        with self.assertRaises(Conflict):
            self.store.create_job("experiment", str(uuid4()), {}, original["id"], 1)

    def test_incomplete_drafts_cannot_run_and_late_events_cannot_resurrect(self):
        proposal = self.proposal()
        saved = self.store.save_idea(proposal["id"], 1, {**proposal["idea"], "Name": "../escape", "Experiments": []})
        self.assertIn("Name", saved["errors"])
        self.assertIn("Experiments", saved["errors"])
        with self.assertRaises(Conflict):
            self.store.create_job("experiment", str(uuid4()), {}, proposal["id"], 2)
        job = self.store.create_job("idea", str(uuid4()), {})
        self.store.update_job(job["id"], state="stopping")
        self.store.update_job(job["id"], state="completed")
        self.assertEqual(self.store.get_job(job["id"])["state"], "stopping")
        self.store.update_job(job["id"], state="stopped")
        self.store.update_job(job["id"], state="running")
        self.assertEqual(self.store.get_job(job["id"])["state"], "stopped")
