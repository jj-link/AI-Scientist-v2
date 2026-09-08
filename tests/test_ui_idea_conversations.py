"""Conversation approval saves reviewed artifacts, never model-invented consent."""
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import threading
import time
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
import yaml

from ai_scientist.ui.app import create_app


@contextmanager
def conversation_model():
    responses, requests = deque(), []
    release = threading.Event()
    release.set()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            release.wait(10)
            payload = responses.popleft() if responses else {"action": "discuss", "message": "What would you like to investigate?"}
            encoded = json.dumps({"id": "conversation-fixture", "object": "chat.completion", "created": 0,
                "model": "fixture", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(payload)}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", responses, requests, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


class IdeaConversationApproval(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / "ui_data" / "checks" / str(uuid4())
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.endpoint = self.enterContext(conversation_model())
        url, self.responses, self.requests, self.release = self.endpoint
        (self.root / "ais_roles.yaml").write_text(yaml.safe_dump({
            "endpoints": {"local": {"base_url": url, "provides": ["text"], "timeout": 10}},
            "roles": {"ideation": {"endpoint": "local", "model": "fixture", "max_tokens": 4096}},
        }), encoding="utf-8")
        self.app = create_app(self.root)
        self.client = self.enterContext(TestClient(self.app, base_url="http://127.0.0.1:8765"))
        bootstrap = self.client.get("/api/bootstrap").json()
        self.headers = {"Origin": "http://127.0.0.1:8765", "X-Studio-Token": bootstrap["request_token"]}
        self.config = next(item["id"] for item in bootstrap["role_configs"] if item["label"] == "ais_roles.yaml")
        self.design = {"Name": "verifier_study", "Title": "Verifier-guided repository fixes",
            "Short Hypothesis": "Verification may improve repository issue resolution.",
            "Abstract": "Compare unassisted coding, self-verification, and stronger verification at matched cost.",
            "Sources": ["https://arxiv.org/abs/2607.05391"],
            "Constraints": {"hardware": "RTX 6000 Pro", "model": "Qwen3.8-27B"}}

    def post(self, path, body):
        return self.client.post(path, json=body, headers=self.headers)

    def settled(self, id):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/idea-conversations/{id}")
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            if result["state"] != "running":
                return result
            time.sleep(0.02)
        self.fail("Conversation did not settle")

    def start(self, output, message="Investigate verifier-guided repository coding.", idea_id=None):
        self.responses.append(output)
        body = {"request_id": str(uuid4()), "role_config_id": self.config, "message": message}
        if idea_id:
            body["idea_id"] = idea_id
        response = self.post("/api/idea-conversations", body)
        self.assertEqual(response.status_code, 202, response.text)
        return self.settled(response.json()["id"])

    def turn(self, conversation, message, output):
        self.responses.append(output)
        body = {"request_id": str(uuid4()), "expected_revision": conversation["revision"], "message": message}
        response = self.post(f"/api/idea-conversations/{conversation['id']}/messages", body)
        self.assertEqual(response.status_code, 202, response.text)
        return self.settled(conversation["id"])

    def approval(self, conversation):
        return {"action": "approve", "message": "Approved.",
                "candidate_revision": conversation.get("candidate_revision") or conversation["revision"]}

    def backlog(self):
        return self.client.get("/api/ideas").json()["ideas"]

    def test_refinement_saves_only_exact_latest_reviewed_design_and_reopens(self):
        first = self.start({"action": "present", "message": "Here is the complete design for your approval.", "idea": self.design})
        self.assertEqual(first["state"], "idle", first)
        self.assertEqual(self.backlog(), [])
        self.assertEqual(first["pending_idea"], self.design)
        refined = {**self.design, "Constraints": {"hardware": "RTX 6000 Pro", "model": "Qwen3.8-27B", "budget": "equal total inference cost"}}
        second = self.turn(first, "Specify equal total inference cost, not just equal coding tokens.",
            {"action": "present", "message": "Here is the revised complete design.", "idea": refined})
        self.assertEqual(self.backlog(), [])
        approved = self.turn(second, "Yes, I approve this design.", self.approval(second))
        self.assertEqual(approved["state"], "idle", approved)
        saved = self.backlog()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["idea"], refined)
        self.assertEqual(approved["idea_id"], saved[0]["id"])
        reopened = self.client.get(f"/api/idea-conversations/{approved['id']}").json()
        self.assertEqual(reopened["messages"], approved["messages"])
        self.assertEqual(reopened["idea_id"], saved[0]["id"])
        # An incomplete experimental design is a valid idea artifact, not permission to run.
        self.assertIn("Experiments", saved[0]["errors"])

    def test_conditional_approval_cannot_be_promoted_by_model_into_save(self):
        first = self.start({"action": "present", "message": "Please review this design.", "idea": self.design})
        result = self.turn(first, "Yes, but change the model to Gemma before saving.", self.approval(first))
        self.assertEqual(self.backlog(), [])
        self.assertIsNone(result["idea_id"])

    def test_negated_and_questioned_approval_never_save(self):
        for message in ("I do not approve this design.", "Should I approve this design?"):
            with self.subTest(message=message):
                first = self.start({"action": "present", "message": "Please review this design.", "idea": self.design})
                result = self.turn(first, message, self.approval(first))
                self.assertEqual(self.backlog(), [])
                self.assertIsNone(result["idea_id"])

    def test_model_cannot_save_unpresented_or_changed_candidate(self):
        first = self.start({"action": "approve", "message": "Saved.", "candidate_revision": 0}, message="Yes")
        self.assertIsNone(first["idea_id"])
        presented = self.start({"action": "present", "message": "Review this design.", "idea": self.design})
        altered = {**self.approval(presented), "idea": {**self.design, "Title": "Unreviewed replacement"}}
        result = self.turn(presented, "I approve this design.", altered)
        self.assertIsNone(result["idea_id"])
        self.assertEqual(self.backlog(), [])

    def test_duplicate_approval_request_does_not_create_second_artifact(self):
        first = self.start({"action": "present", "message": "Review this design.", "idea": self.design})
        self.responses.append(self.approval(first))
        body = {"request_id": str(uuid4()), "expected_revision": first["revision"], "message": "Approved."}
        path = f"/api/idea-conversations/{first['id']}/messages"
        response = self.post(path, body)
        self.assertEqual(response.status_code, 202, response.text)
        done = self.settled(first["id"])
        replay = self.post(path, body)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["idea_id"], done["idea_id"])
        self.assertEqual(len(self.backlog()), 1)
        self.assertEqual(len(self.requests), 2)

    def test_stale_turn_and_untrusted_origin_cannot_modify_conversation(self):
        first = self.start({"action": "discuss", "message": "Let us clarify the research question."})
        path = f"/api/idea-conversations/{first['id']}/messages"
        body = {"request_id": str(uuid4()), "expected_revision": first["revision"], "message": "A refinement"}
        denied = self.client.post(path, json=body, headers={**self.headers, "Origin": "https://untrusted.example"})
        self.assertEqual(denied.status_code, 403)
        stale = self.post(path, {**body, "expected_revision": first["revision"] - 1})
        self.assertEqual(stale.status_code, 409, stale.text)
        after = self.client.get(f"/api/idea-conversations/{first['id']}").json()
        self.assertEqual(after["messages"], first["messages"])
        self.assertEqual(self.backlog(), [])

    def test_refinement_does_not_overwrite_external_artifact_revision(self):
        existing = self.app.state.store.add_idea("fixture", self.design)
        candidate = {**self.design, "Title": "Discussed revision"}
        first = self.start({"action": "present", "message": "Review the revised idea.", "idea": candidate}, idea_id=existing["id"])
        external = {**self.design, "Title": "External edit"}
        self.app.state.store.save_idea(existing["id"], existing["revision"], external)
        result = self.turn(first, "I approve this design.", self.approval(first))
        self.assertIsNotNone(result["error"])
        self.assertEqual(self.app.state.store.get_idea(existing["id"])["idea"], external)

    def test_discussion_continues_past_five_turns_without_forced_artifact(self):
        conversation = self.start({"action": "discuss", "message": "We can explore your question."})
        questions = [f"Consider research constraint {index} before settling the design." for index in range(7)]
        for question in questions:
            conversation = self.turn(conversation, question,
                {"action": "discuss", "message": "We can incorporate that into the discussion."})
            self.assertEqual(conversation["state"], "idle", conversation)
            self.assertEqual(self.backlog(), [])
        recorded = [message["content"] for message in conversation["messages"] if message["role"] == "user"]
        self.assertEqual(recorded[1:], questions)
        self.assertIsNone(conversation["pending_idea"])

    def test_stop_prevents_late_model_response_from_publishing_design(self):
        self.release.clear()
        self.responses.append({"action": "present", "message": "Review this design.", "idea": self.design})
        response = self.post("/api/idea-conversations", {
            "request_id": str(uuid4()), "role_config_id": self.config, "message": "Investigate this idea."})
        self.assertEqual(response.status_code, 202, response.text)
        id = response.json()["id"]
        stopped = self.post(f"/api/idea-conversations/{id}/stop", {})
        self.assertEqual(stopped.status_code, 200, stopped.text)
        self.release.set()
        conversation = self.settled(id)
        self.assertNotEqual(conversation["state"], "running")
        self.assertIsNone(conversation["pending_idea"])
        self.assertEqual(self.backlog(), [])
