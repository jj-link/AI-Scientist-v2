"""Finalized proposals survive failed attempts and cooperative cancellation."""
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from ai_scientist import perform_ideation_temp_free as ideation
from ai_scientist.progress import Cancelled


class GenerationBoundaries(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parents[1] / "ui_data" / "checks" / str(uuid4())
        self.directory.mkdir(parents=True)
        self.output = self.directory / "ideas.json"
        self.events = []

    def tearDown(self):
        shutil.rmtree(self.directory)

    def generate(self, replies, **kwargs):
        with patch.object(ideation, "get_response_from_llm", side_effect=[(text, []) for text in replies]):
            return ideation.generate_temp_free_idea(str(self.output), None, "test", "Question", reload_ideas=False,
                num_reflections=2, on_event=kwargs.pop("on_event", self.events.append), **kwargs)

    def test_usable_proposal_persisted_before_next_attempt_fails(self):
        idea = {"Title": "Usable draft", "unknown": {"preserved": True}}
        def observe(event):
            self.events.append(event)
            if event["type"] == "proposal_finalized":
                self.assertEqual(json.loads(self.output.read_text(encoding="utf-8")), [idea])
        result = self.generate(['ACTION: FinalizeIdea\nARGUMENTS: '+json.dumps({"idea":idea}), 'invalid response'],
                               max_num_generations=2, on_event=observe)
        self.assertEqual(result, [idea])
        self.assertEqual([e["data"]["attempt"] for e in self.events if e["type"]=="attempt_failed"], [2])

    def test_malformed_finalization_is_not_a_usable_proposal(self):
        result = self.generate(['ACTION: FinalizeIdea\nARGUMENTS: {"idea": ["invalid"]}'], max_num_generations=1)
        self.assertEqual(result, [])
        self.assertEqual(json.loads(self.output.read_text(encoding="utf-8")), [])
        self.assertFalse(any(e["type"]=="proposal_finalized" for e in self.events))
        diagnosis = Path(str(self.output)+".malformed.jsonl").read_text(encoding="utf-8")
        self.assertEqual(json.loads(diagnosis)["idea"], ["invalid"])

    def test_cancellation_does_not_erase_a_finalized_proposal(self):
        idea = {"Title": "Retained"}
        def stop_after_save(event):
            if event["type"] == "proposal_finalized":
                raise Cancelled("Stopped")
        with self.assertRaises(Cancelled):
            self.generate(['ACTION: FinalizeIdea\nARGUMENTS: '+json.dumps({"idea":idea})],
                          max_num_generations=2, on_event=stop_after_save)
        self.assertEqual(json.loads(self.output.read_text(encoding="utf-8")), [idea])
