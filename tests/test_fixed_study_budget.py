"""A preparation limit must leave room for the required visible handoff."""

import json
from pathlib import Path
import shutil
import sys
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_scientist import swebench_study as study


@pytest.fixture
def workspace(monkeypatch):
    # These tests cover the controller, not the native-only directory-fsync ledger.
    monkeypatch.setattr(study, "save", lambda path, value: path.write_text(json.dumps(value), encoding="utf-8"))
    root = Path(__file__).parent / f"fixed_study_budget_{uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root)


class CappedInvestigation:
    """The first response uses its entire allowance without completing a tool."""

    def __init__(self):
        self.requests = 0
        self.control = SimpleNamespace(check=lambda deadline=None: None, stopped=threading.Event())

    def prompt_count(self, body, deadline):
        return 20

    def tokens(self, text, deadline):
        return text.split()

    def request(self, path, body, deadline, events_path):
        self.requests += 1
        if self.requests == 1:
            generated = body["max_tokens"]
            choice = {
                "finish_reason": "length",
                "message": {"role": "assistant", "content": None,
                            "reasoning_content": "The available evidence does not establish a cause."},
            }
        else:
            generated = 24
            choice = {
                "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "report", "type": "function", "function": {
                        "name": "handoff",
                        "arguments": json.dumps({"notes": "The investigation is incomplete; no cause is established."}),
                    },
                }]},
            }
        return {"choices": [choice], "system_fingerprint": "budget-fixture",
                "usage": {"prompt_tokens": 20, "completion_tokens": generated}}


@pytest.fixture
def protocol():
    return {
        "budgets": {
            "preparation": {"output_tokens": 4096, "input_tokens": 262144,
                            "tool_calls": 12, "seconds": 300},
            "handoff_output_reserve": 1024,
            "handoff_tokens": 1024,
            "tool_output_tokens": 2048,
            "tool_timeout_seconds": 30,
        },
        "target": {"model": "budget-fixture", "system_fingerprint": "budget-fixture",
                   "temperature": 0, "seed": 7, "context_tokens": 262144},
        "prompts": {"notes": "Investigate, then provide an honest visible handoff."},
    }


def test_capped_investigation_still_delivers_handoff_within_total_allowance(workspace, protocol):
    totals = study.metrics()
    termination, _ = study.phase(
        protocol, "notes", "preparation", {"problem_statement": "An unresolved defect"},
        "", workspace, None, workspace, CappedInvestigation(), totals,
    )
    assert termination == "handoff"
    assert totals["output_tokens"] <= protocol["budgets"]["preparation"]["output_tokens"]


class RepeatedInspection(CappedInvestigation):
    def prompt_count(self, body, deadline):
        return 4000 + 2048 * sum(message["role"] == "tool" for message in body["messages"])

    def bounded(self, text, limit, deadline, truncated):
        return " ".join(text.split()[:limit])

    def request(self, path, body, deadline, events_path):
        reporting = all(tool["function"]["name"] == "handoff" for tool in body["tools"])
        name = "handoff" if reporting else "execute"
        arguments = {"notes": "The inspected source does not establish the defect's cause."} if reporting else {"command": "inspect source"}
        return {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{
                "id": "inspection", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }],
        }}], "system_fingerprint": "budget-fixture",
            "usage": {"prompt_tokens": self.prompt_count(body, deadline),
                      "completion_tokens": 24 if reporting else 100}}


def test_growing_tool_history_leaves_input_capacity_for_handoff(workspace, protocol):
    protocol["budgets"]["preparation"]["input_tokens"] = 14500
    tools = SimpleNamespace(execute=lambda command, deadline: (0, b"source " * 2048, b"", False))
    totals = study.metrics()
    termination, _ = study.phase(
        protocol, "notes", "preparation", {"problem_statement": "An unresolved defect"},
        "", workspace, tools, workspace, RepeatedInspection(), totals,
    )
    assert termination == "handoff"
    assert totals["input_tokens"] <= protocol["budgets"]["preparation"]["input_tokens"]
