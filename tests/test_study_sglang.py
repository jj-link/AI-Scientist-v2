"""Native stream completion and development evidence must fail closed."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_scientist import fixed_study
from ai_scientist import swebench_study as study
from ai_scientist.progress import PipelineFailure


@pytest.fixture
def case_root():
    root = Path(__file__).parent / f"study_sglang_{uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root)


@pytest.fixture
def endpoint():
    # Exercise the real parser without entering native runtime qualification or HTTP.
    endpoint = study.Endpoint(
        {"target": {"backend": "sglang", "base_url": "http://127.0.0.1:8080",
                    "model": "fixture-qwen"}, "native": {}},
        SimpleNamespace(check=lambda deadline: None),
    )
    try:
        yield endpoint
    finally:
        endpoint.session.close()


class ByteStream:
    def __init__(self, events):
        self.wire = b"".join(
            b"data: " + (event.encode() if isinstance(event, str)
                         else json.dumps(event, ensure_ascii=False).encode()) + b"\r\n\r\n"
            for event in events
        )

    def iter_content(self, chunk_size):
        # Split UTF-8 codepoints, SSE delimiters, and JSON across transport reads.
        for index in range(len(self.wire)):
            yield self.wire[index:index + 1]


def tool_events():
    return [
        {"model": "fixture-qwen", "choices": [{"index": 0, "delta": {
            "reasoning_content": "Inspect café. "}}],
         "usage": {"prompt_tokens": 41, "completion_tokens": 2}},
        {"choices": [{"index": 0, "delta": {"role": None, "content": "\n\n",
            "reasoning_content": None, "tool_calls": None}}], "usage": None},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call-1", "type": "function", "function": None}]}}]},
        {"choices": [{"index": 0, "delta": {"reasoning_content": "Use a bounded command.",
            "tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                            "function": {"name": "exe", "arguments": '{"com'}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0,
            "function": {"name": "cute", "arguments": 'mand":"printf \'ca'}}]}}],
         "usage": {"prompt_tokens": 41, "completion_tokens": 7}},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0,
            "function": {"arguments": 'fé\'"}'}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


def final_usage():
    return {"choices": [], "usage": {"prompt_tokens": 41, "completion_tokens": 19,
                                    "total_tokens": 60}}


def test_fragmented_tool_waits_for_authoritative_usage_after_finish(endpoint, case_root):
    response = ByteStream(tool_events() + [final_usage(), "[DONE]"])
    result = endpoint.chat_events(response, case_root / "events.sse", time.monotonic() + 30)
    choice = result["choices"][0]
    message = choice["message"]
    assert choice["finish_reason"] == "tool_calls"
    assert message["content"] == "\n\n"
    assert message["reasoning_content"] == "Inspect café. Use a bounded command."
    assert len(message["tool_calls"]) == 1
    call = message["tool_calls"][0]
    assert (call["id"], call["type"], call["function"]["name"]) == ("call-1", "function", "execute")
    assert json.loads(call["function"]["arguments"]) == {"command": "printf 'café'"}
    assert result["usage"] == {"prompt_tokens": 41, "completion_tokens": 19, "total_tokens": 60}


def test_provider_error_after_finish_invalidates_parseable_tool(endpoint, case_root):
    response = ByteStream(tool_events() + [final_usage(),
        {"error": {"message": "generation worker failed", "type": "server_error"}}, "[DONE]"])
    with pytest.raises(study.InfrastructureError):
        endpoint.chat_events(response, case_root / "events.sse", time.monotonic() + 30)


def test_eof_with_tool_and_usage_is_not_a_completed_stream(endpoint, case_root):
    response = ByteStream(tool_events() + [final_usage()])
    with pytest.raises(study.InfrastructureError):
        endpoint.chat_events(response, case_root / "events.sse", time.monotonic() + 30)


@pytest.fixture
def pilot():
    return {"purpose": "development_feasibility", "arms": ["direct"], "repetitions": 1,
            "cohort": [{"instance_id": f"fixture__project-{index}", "repo": "fixture/project"}
                       for index in range(8)],
            "feasibility_gate": {"minimum_distinct_resolved_issues": 2,
                                 "requires_complete_execution": True, "held_out": False}}


def attempts(protocol, resolved=2):
    return [{"instance_id": row["instance_id"], "arm": arm, "repetition": 0,
             "status": "resolved" if index < resolved else "unresolved",
             "resolved": index < resolved}
            for index, (row, arm) in enumerate(study._trial_schedule(protocol))]


def test_schedule_preserves_direct_cohort_order_and_rotates_configured_arms(pilot):
    keys = lambda rows: [(row["instance_id"], row["arm"], row["repetition"]) for row in rows]
    issues = [f"fixture__project-{index}" for index in range(8)]
    assert keys(attempts(pilot)) == [(issue, "direct", 0) for issue in issues]

    pilot["purpose"] = "runtime_smoke"
    pilot["arms"] = ["locations", "direct", "notes", "diagnosis"]
    rotations = [
        ["locations", "direct", "notes", "diagnosis"],
        ["direct", "notes", "diagnosis", "locations"],
        ["notes", "diagnosis", "locations", "direct"],
        ["diagnosis", "locations", "direct", "notes"],
    ]
    assert keys(attempts(pilot)) == [
        (issue, arm, 0) for issue, order in zip(issues, rotations * 2) for arm in order
    ]


def test_feasibility_threshold_counts_distinct_issues_only_after_completion(pilot):
    assessment = study._feasibility_assessment(pilot, attempts(pilot, resolved=1), completed=True)
    assert assessment["decision"] == "no_go"
    assert assessment["feasibility_gate_passed"] is False

    assessment = study._feasibility_assessment(pilot, attempts(pilot), completed=True)
    assert assessment["decision"] == "go"
    assert assessment["feasibility_gate_passed"] is True
    assert assessment["distinct_resolved_issues"] == ["fixture__project-0", "fixture__project-1"]
    assert assessment["completed"] is True
    assert assessment["held_out"] is False

    prefix = attempts(pilot)[:2]
    assessment = study._feasibility_assessment(pilot, prefix, completed=False)
    assert assessment["decision"] == "incomplete"
    assert assessment["feasibility_gate_passed"] is False
    assert assessment["completed"] is False
    with pytest.raises(study.InfrastructureError):
        study._feasibility_assessment(pilot, prefix, completed=True)


def test_completed_feasibility_rejects_duplicate_attempts_even_with_full_coverage(pilot):
    trials = attempts(pilot)
    with pytest.raises(study.InfrastructureError):
        study._feasibility_assessment(pilot, trials + [deepcopy(trials[0])], completed=True)


def test_completed_feasibility_rejects_an_unscored_attempt(pilot):
    trials = attempts(pilot)
    trials[-1].update(status="infrastructure_failure", resolved=None)
    with pytest.raises(study.InfrastructureError):
        study._feasibility_assessment(pilot, trials, completed=True)


@pytest.fixture
def comparative_evidence(pilot):
    protocol = deepcopy(pilot)
    protocol.update(schema_version=1, kind=fixed_study.KIND, protocol_id="sglang-regression",
                    purpose="runtime_smoke", arms=list(study.ARMS))
    for row in protocol["cohort"]:
        row.update(image="fixture@sha256:" + "a" * 64, image_id="sha256:" + "a" * 64)
    phase = {"input_tokens": 10000, "output_tokens": 1000, "tool_calls": 10, "seconds": 100}
    protocol.update(
        target={"backend": "sglang", "base_url": "http://127.0.0.1:8080", "model": "fixture-qwen",
                "context_tokens": 65536, "temperature": 0, "seed": 7, "thinking": True,
                "reasoning_effort": "xhigh", "preserve_thinking": True,
                "runtime_manifest_sha256": "a" * 64},
        budgets={"direct": phase, "preparation": phase, "repair": phase,
                 "handoff_tokens": 100, "terminal_output_reserve": 100,
                 "tool_timeout_seconds": 30, "tool_output_tokens": 100, "evaluation_timeout_seconds": 100},
        resources={"container_cpus": 1, "container_memory_bytes": 1024**3, "container_pids": 64,
                   "minimum_free_bytes": 1024**3, "study_image_budget_bytes": 1024**3,
                   "evaluator_writable_limits_bytes": {}, "evaluator_read_only_root": True,
                   "evaluator_cap_sys_admin": False, "evaluator_command_output_bytes": 1024},
        dataset={"name": "fixture", "revision": "b" * 40},
    )
    results = {"schema_version": 1, "protocol_id": protocol["protocol_id"],
               "protocol_sha256": study.digest(study.json_bytes(protocol)),
               "execution_id": "fixture-execution", "completed": True,
               "cohort": [{key: row[key] for key in ("instance_id", "repo")} for row in protocol["cohort"]],
               "arms": list(study.ARMS), "repetitions": 1, "trials": [], "notes": [],
               "runtime": {key: deepcopy(protocol[key]) for key in
                           ("purpose", "target", "budgets", "resources", "dataset")}}
    results["runtime"].update(
        harness_version="5.0.2", runner_sha256="d" * 64,
        images=[{key: row[key] for key in ("instance_id", "image", "image_id")} for row in protocol["cohort"]],
        workspace_qualification={"passed": True, "sha256": "e" * 64},
        runtime_qualification={"passed": True, "sha256": "f" * 64, "runtime_manifest_sha256": "a" * 64},
    )
    for row, arm in study._trial_schedule(protocol):
        results["trials"].append({"instance_id": row["instance_id"], "repo": row["repo"], "arm": arm,
            "repetition": 0, "status": "empty_patch", "resolved": False, "termination_reason": "finish",
            "patch": "", "patch_sha256": study.digest(b""), "handoff": "",
            "metrics": {name: None if name in fixed_study.NULLABLE else 0 for name in fixed_study.METRICS}})
    return protocol, results


def test_development_protocol_cannot_enter_fixed_study(case_root, comparative_evidence, monkeypatch):
    protocol, _ = comparative_evidence
    path = case_root / "protocol.json"
    path.write_bytes(study.json_bytes(protocol))
    idea = {"Execution": {"kind": fixed_study.KIND, "protocol_sha256": study.file_digest(path)}}
    monkeypatch.setenv("AI_SCIENTIST_REPAIR_PROTOCOL", str(path))
    admitted, _ = fixed_study._protocol(idea)
    assert admitted["purpose"] == "runtime_smoke"

    # Keep the four-arm shape valid, so purpose itself must block admission.
    protocol["purpose"] = "development_feasibility"
    path.write_bytes(study.json_bytes(protocol))
    idea["Execution"]["protocol_sha256"] = study.file_digest(path)
    with pytest.raises(PipelineFailure):
        fixed_study._protocol(idea)
    with pytest.raises(PipelineFailure):
        fixed_study.run_fixed_study(idea, case_root / "analysis")
    assert not (case_root / "analysis").exists()


def test_development_evidence_cannot_be_published_for_paper_generation(case_root, comparative_evidence):
    protocol, results = comparative_evidence
    safe = fixed_study.validate_results(results, protocol, results["protocol_sha256"], results["execution_id"])
    assert len(safe["trials"]) == 32

    # Even fully shaped, consistently pinned four-arm data cannot launder a pilot.
    protocol["purpose"] = results["runtime"]["purpose"] = "development_feasibility"
    digest = study.digest(study.json_bytes(protocol))
    results["protocol_sha256"] = digest
    with pytest.raises(PipelineFailure):
        fixed_study.validate_results(results, protocol, digest, results["execution_id"])

    def save(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(study.json_bytes(value))

    root = case_root.resolve() / "analysis"
    save(root / "idea.json", {"Execution": {"kind": fixed_study.KIND, "protocol_sha256": digest}})
    save(root / fixed_study.STATE, {"protocol": protocol, "protocol_sha256": digest,
         "source_evidence": None, "evidence_execution_id": results["execution_id"],
         "runner_sha256": results["runtime"]["runner_sha256"]})
    save(root / fixed_study.RESULT, results)
    result_hash = study.file_digest(root / fixed_study.RESULT)
    save(root / ".fixed-study-completed.json", {"safe_results_sha256": result_hash,
                                               "native_safe_results_sha256": result_hash})
    summary = fixed_study.summarize_results(results, result_hash, root / fixed_study.RESULT,
                                           native_result_sha256=result_hash)
    save(root / fixed_study.SUMMARY, summary)
    log_dir = case_root / "paper"
    with pytest.raises(PipelineFailure):
        fixed_study.publish_fixed_study_summary(root, log_dir)
    assert not log_dir.exists()
