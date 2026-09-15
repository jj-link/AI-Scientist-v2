"""Every phase must leave bounded capacity for its required terminal tool."""

import json
from pathlib import Path
import shutil
import sys
import threading
from types import SimpleNamespace
from uuid import uuid4

import jsonschema
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

    def __init__(self, terminal="handoff", arguments=None, terminal_output=24):
        self.requests = 0
        self.terminal = terminal
        self.arguments = arguments if arguments is not None else (
            {"notes": "The investigation is incomplete; no cause is established."}
            if terminal == "handoff" else {})
        self.terminal_output = terminal_output
        self.control = SimpleNamespace(check=lambda deadline=None: None, stopped=threading.Event())

    def prompt_count(self, body, deadline):
        return 20

    def tokens(self, text, deadline):
        return text.split()

    def response(self, body, deadline, name, arguments, generated):
        if name is None or generated > body["max_tokens"]:
            generated = body["max_tokens"]
            choice = {
                "finish_reason": "length",
                "message": {"role": "assistant", "content": None,
                            "reasoning_content": "The available evidence does not establish a cause."},
            }
        else:
            choice = {
                "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": [{
                    "id": f"call-{self.requests}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(arguments),
                    },
                }]},
            }
        return {"choices": [choice], "system_fingerprint": "budget-fixture",
                "usage": {"prompt_tokens": self.prompt_count(body, deadline),
                          "completion_tokens": generated}}

    def request(self, path, body, deadline, events_path):
        self.requests += 1
        name = None if self.requests == 1 else self.terminal
        return self.response(body, deadline, name, self.arguments, self.terminal_output)


class RepeatedInspection(CappedInvestigation):
    terminal_prompt_overhead = 0

    def prompt_count(self, body, deadline):
        terminal_only = len(body["tools"]) == 1
        return (4000 + 2048 * sum(message["role"] == "tool" for message in body["messages"])
                + (self.terminal_prompt_overhead if terminal_only else 0))

    def bounded(self, text, limit, deadline, truncated):
        return " ".join(text.split()[:limit])

    def request(self, path, body, deadline, events_path):
        self.requests += 1
        reporting = len(body["tools"]) == 1
        name = self.terminal if reporting else "execute"
        arguments = self.arguments if reporting else {"command": f"inspect source {self.requests}"}
        return self.response(body, deadline, name, arguments, self.terminal_output if reporting else 100)


@pytest.fixture
def protocol():
    return {
        "budgets": {
            "preparation": {"output_tokens": 4096, "input_tokens": 262144,
                            "tool_calls": 12, "seconds": 300},
            "repair": {"output_tokens": 4096, "input_tokens": 262144,
                       "tool_calls": 12, "seconds": 300},
            "direct": {"output_tokens": 8192, "input_tokens": 524288,
                       "tool_calls": 24, "seconds": 600},
            "terminal_output_reserve": 1024,
            "handoff_tokens": 1024,
            "tool_output_tokens": 2048,
            "tool_timeout_seconds": 30,
        },
        "target": {"model": "budget-fixture", "system_fingerprint": "budget-fixture",
                   "temperature": 0, "seed": 7, "context_tokens": 262144, "thinking": False},
        "prompts": {"notes": "Investigate, then provide an honest visible handoff.",
                    "locations": "Report established locations, or an empty list if none are established.",
                    "direct": "Repair the defect, then finish.",
                    "repair": "Repair using the visible handoff, then finish."},
    }


PHASES = [("notes", "preparation", "handoff"), ("direct", "direct", "finish"),
          ("notes", "repair", "finish")]


@pytest.mark.parametrize("arm,name,terminal", PHASES)
def test_capped_action_still_delivers_terminal_within_total_allowance(workspace, protocol, arm, name, terminal):
    totals = study.metrics()
    endpoint = CappedInvestigation(terminal)
    termination, visible = study.phase(
        protocol, arm, name, {"problem_statement": "An unresolved defect"},
        "", workspace, None, workspace, endpoint, totals,
    )
    assert termination == terminal
    assert json.loads(visible) == endpoint.arguments if terminal == "handoff" else visible == ""
    assert endpoint.requests == 2
    assert totals["tool_calls"] == 0
    assert totals["output_tokens"] <= protocol["budgets"][name]["output_tokens"]
    assert totals["input_tokens"] <= protocol["budgets"][name]["input_tokens"]


@pytest.mark.parametrize("arm,name,terminal", PHASES)
@pytest.mark.parametrize("resource", ["input_tokens", "context_tokens"])
def test_growing_tool_history_leaves_capacity_for_terminal(workspace, protocol, arm, name, terminal, resource):
    # Equal per-phase bounds isolate the controller from study-level allocation.
    protocol["budgets"][name]["output_tokens"] = 4096
    commands = []
    if resource == "input_tokens":
        protocol["budgets"][name]["input_tokens"] = 14500
    else:
        protocol["target"]["context_tokens"] = 12000

    def execute(command, deadline):
        commands.append(command)
        return 0, b"source " * 2048, b"", False

    totals = study.metrics()
    termination, _ = study.phase(
        protocol, arm, name, {"problem_statement": "An unresolved defect"},
        "", workspace, SimpleNamespace(execute=execute), workspace, RepeatedInspection(terminal), totals,
    )
    assert termination == terminal
    assert commands == ["inspect source 1"]
    assert totals["tool_calls"] == len(commands)
    assert totals["input_tokens"] <= protocol["budgets"][name]["input_tokens"]
    assert totals["output_tokens"] <= protocol["budgets"][name]["output_tokens"]
    assert totals["max_context_tokens"] <= protocol["target"]["context_tokens"]


@pytest.mark.parametrize("arm,name,terminal", PHASES)
def test_tool_ceiling_terminal_can_spend_more_than_reserve(workspace, protocol, arm, name, terminal):
    protocol["budgets"][name]["tool_calls"] = 1
    endpoint = RepeatedInspection(terminal, terminal_output=2000)
    totals = study.metrics()
    termination, visible = study.phase(
        protocol, arm, name, {}, "", workspace,
        SimpleNamespace(execute=lambda command, deadline: (0, b"observed", b"", False)),
        workspace, endpoint, totals,
    )
    assert termination == terminal
    assert json.loads(visible) == endpoint.arguments if terminal == "handoff" else visible == ""
    assert totals["output_tokens"] == 2100
    assert totals["output_tokens"] <= protocol["budgets"][name]["output_tokens"]
    assert totals["handoff_tokens"] <= protocol["budgets"]["handoff_tokens"]


def test_complete_terminal_prompt_is_reserved_before_growing_history(workspace, protocol):
    protocol["budgets"]["preparation"]["input_tokens"] = 14500
    endpoint = RepeatedInspection()
    endpoint.terminal_prompt_overhead = 5000
    commands = []

    def execute(command, deadline):
        commands.append(command)
        return 0, b"source " * 2048, b"", False

    totals = study.metrics()
    termination, visible = study.phase(
        protocol, "notes", "preparation", {}, "", workspace,
        SimpleNamespace(execute=execute), workspace, endpoint, totals,
    )
    assert termination == "handoff"
    assert json.loads(visible) == endpoint.arguments
    assert commands == []
    assert totals["tool_calls"] == 0
    assert totals["input_tokens"] == 9000
    assert totals["input_tokens"] <= protocol["budgets"]["preparation"]["input_tokens"]


@pytest.mark.parametrize("resource", ["input_tokens", "context_tokens"])
def test_unaffordable_terminal_never_starts_generation(workspace, protocol, resource):
    if resource == "input_tokens":
        protocol["budgets"]["direct"][resource] = 19
    else:
        protocol["target"][resource] = 19
    endpoint = CappedInvestigation("finish")
    totals = study.metrics()
    termination, _ = study.phase(
        protocol, "direct", "direct", {}, "", workspace, None, workspace, endpoint, totals,
    )
    assert termination == "budget_exhausted"
    assert endpoint.requests == 0
    assert totals["input_tokens"] == totals["output_tokens"] == totals["tool_calls"] == 0


def test_visible_handoff_limit_remains_independent_of_terminal_output(workspace, protocol):
    protocol["budgets"]["preparation"]["tool_calls"] = 0
    protocol["budgets"]["handoff_tokens"] = 3
    totals = study.metrics()
    termination, visible = study.phase(
        protocol, "notes", "preparation", {}, "", workspace, None, workspace,
        RepeatedInspection(terminal_output=2000), totals,
    )
    assert termination == "invalid_handoff"
    assert visible == ""
    assert totals["handoff_tokens"] == 0
    assert totals["output_tokens"] == 2000


@pytest.mark.parametrize("arm,name", [("direct", "direct"), ("notes", "repair")])
def test_finish_rejects_extra_arguments_without_reexecuting_or_discarding_edits(workspace, protocol, arm, name):
    protocol["budgets"][name]["tool_calls"] = 1
    commands = []
    source = workspace / "source.py"
    source.write_text("broken = True\n", encoding="utf-8")

    def execute(command, deadline):
        commands.append(command)
        source.write_text("broken = False\n", encoding="utf-8")
        return 0, b"updated", b"", False

    totals = study.metrics()
    termination, _ = study.phase(
        protocol, arm, name, {}, "", workspace, SimpleNamespace(execute=execute), workspace,
        RepeatedInspection("finish", {"description": "Fixed the defect"}), totals,
    )
    assert termination == "invalid_finalization"
    assert commands == ["inspect source 1"]
    assert source.read_text(encoding="utf-8") == "broken = False\n"
    schema = study.tools_for(arm, False)[-1]["function"]["parameters"]
    jsonschema.validate({}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"description": "Fixed the defect"}, schema)


def test_empty_locations_are_schema_valid_and_delivered_unchanged(workspace, protocol):
    protocol["budgets"]["preparation"]["tool_calls"] = 0
    value = {"locations": []}
    schema = study.tools_for("locations", True)[-1]["function"]["parameters"]
    jsonschema.validate(value, schema)
    termination, visible = study.phase(
        protocol, "locations", "preparation", {}, "", workspace, None, workspace,
        RepeatedInspection(arguments=value), study.metrics(),
    )
    assert termination == "handoff"
    assert json.loads(visible) == value


@pytest.mark.parametrize("symbol", ["Alpha.method", "Beta.method", "Alpha", "helper", ""])
def test_exact_symbols_and_file_only_locations_are_preserved(workspace, symbol):
    (workspace / "source.py").write_text(
        "class Alpha:\n    def method(self): pass\n"
        "class Beta:\n    async def method(self): pass\n"
        "def helper(): pass\n", encoding="utf-8")
    value = {"locations": [{"path": "source.py", "symbol": symbol}]}
    assert json.loads(study.valid_handoff("locations", value, workspace)) == value


@pytest.mark.parametrize("path,symbol", [
    ("source.py", "method"),  # Ambiguous bare method; no automatic qualification.
    ("source.py", "Invented.method"),
    ("source.py", "Alpha.missing"),
    ("missing.py", ""),
    ("../source.py", ""),
    ("/source.py", ""),
    ("./source.py", ""),
    (".git/config", ""),
    ("source.txt", "Alpha.method"),
])
def test_invalid_locations_are_rejected_not_rewritten(workspace, path, symbol):
    source = "class Alpha:\n    def method(self): pass\nclass Beta:\n    def method(self): pass\n"
    (workspace / "source.py").write_text(source, encoding="utf-8")
    (workspace / "source.txt").write_text(source, encoding="utf-8")
    assert study.valid_handoff("locations", {"locations": [{"path": path, "symbol": symbol}]}, workspace) is None


@pytest.mark.parametrize("arm", ["notes", "diagnosis"])
@pytest.mark.parametrize("code", [
    "def copied_method(): pass",
    "Observed the failure.\nimport source",
    "Reproduction:\n```python\nassert source.method()\n```",
    "diff --git a/source.py b/source.py\n@@ -1 +1 @@\n-broken\n+fixed",
])
def test_code_bearing_handoffs_are_rejected_not_sanitized(workspace, arm, code):
    value = ({"notes": code} if arm == "notes" else
             {"root_cause": "Not established", "evidence": code,
              "cross_file_coordination": "None established", "intended_behavior": "The issue should not recur",
              "uncertainties": "Further investigation is needed"})
    assert study.valid_handoff(arm, value, workspace) is None
