"""Continuation may inherit evidence, never repeat or select model generations."""

from copy import deepcopy
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_scientist import swebench_study as study


def save_json(path, value):
    path.write_bytes(study.json_bytes(value))


def trial_key(result):
    return result["instance_id"] + "--" + result["arm"]


def file_snapshot(root):
    return {path.relative_to(root): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def predecessor(monkeypatch):
    # Exercise continuation, not Linux directory-fsync or the host process table.
    monkeypatch.setattr(study, "save", save_json)
    monkeypatch.setattr(study, "atomic", lambda path, data: Path(path).write_bytes(data))
    monkeypatch.setattr(study, "process_identity", lambda pid: None)
    root = Path(__file__).parent / f"study_continuation_{uuid4().hex}"
    root.mkdir()
    try:
        run_root = root / "runs"
        source = run_root / "predecessor"
        destination = run_root / "continuation"
        (source / "trials").mkdir(parents=True)
        (destination / "trials").mkdir(parents=True)
        cohort = [{"instance_id": f"example__project-{index}", "repo": "example/project",
                   "base_commit": str(index) * 40,
                   "image": "example@sha256:" + str(index) * 64,
                   "image_id": "sha256:" + str(index) * 64} for index in (1, 2)]
        protocol = {
            "schema_version": 1, "protocol_id": "predecessor-protocol",
            "kind": "fixed_swebench_repair", "purpose": "runtime_smoke",
            "dataset": {"name": "SWE-bench/SWE-bench_Verified", "revision": "a" * 40,
                        "records_path": str(root / "records.json"), "records_sha256": "b" * 64},
            "target": {"model": "fixture-gemma", "temperature": 0, "seed": 7},
            "cohort": cohort, "arms": list(study.ARMS), "repetitions": 1,
            "budgets": {"direct": {"output_tokens": 30}, "preparation": {"output_tokens": 10},
                        "repair": {"output_tokens": 20}},
            "resources": {"docker_host": "unix:///run/docker.sock", "cpus": 2},
            "prompts": {arm: "Frozen " + arm for arm in study.ARMS + ["repair"]},
            "interpretation": {"scope": "paired fixed cohort"},
            "conversation_policy": {"handoff": "visible-only"},
            "native": {"root": str(root.resolve()), "run_root": str(run_root.resolve())},
        }
        save_json(source / "frozen-protocol.json", protocol)
        execution = {"execution_id": source.name,
                     "protocol_sha256": study.file_digest(source / "frozen-protocol.json"),
                     "runner_sha256": study.digest(b"original native runner")}
        save_json(source / "execution.json", execution)
        save_json(source / "owner.json", {"pid": 12345, "pgid": 12345, "start": "old-start"})
        schedule = [(row, arm) for index, row in enumerate(cohort)
                    for arm in study.ARMS[index % 4:] + study.ARMS[:index % 4]]
        results = []
        for index, (row, arm) in enumerate(schedule[:6]):
            completed = index < 5
            patch = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n" if completed else ""
            values = study.metrics()
            values["elapsed_seconds"] = 2.5
            values["peak_gpu_memory_mib"] = 1024
            if completed:
                values.update(input_tokens=20, output_tokens=10, tool_calls=1, model_seconds=0.5,
                              changed_files=1)
            result = {"instance_id": row["instance_id"], "repo": row["repo"], "arm": arm,
                      "repetition": 0, "status": "unresolved" if completed else "infrastructure_failure",
                      "resolved": False if completed else None,
                      "termination_reason": "patch" if completed else "native_or_evaluator_failure",
                      "patch": patch, "patch_sha256": study.digest(patch.encode()),
                      "handoff": "", "metrics": values}
            trial = source / "trials" / trial_key(result)
            trial.mkdir()
            identity = {"schema_version": 1, **execution,
                        **{key: row[key] for key in ("instance_id", "repo", "base_commit", "image", "image_id")},
                        "arm": arm, "repetition": 0,
                        "evaluation_id": study.digest((source.name + "/" + row["instance_id"] + "/" + arm).encode())[:32]}
            save_json(trial / "identity.json", identity)
            save_json(trial / "result.json", result)
            (trial / "base").mkdir()
            (trial / "base" / "a.py").write_bytes(b"old\n")
            (trial / "base-source.tar").write_bytes(b"fixture source archive")
            (trial / "pristine.tar").write_bytes(b"fixture pristine archive")
            save_json(trial / "source-identity.json", {"base_commit": row["base_commit"]})
            (trial / "gpu-samples.jsonl").write_bytes(b'{"memory_mib": 1024}\n')
            if completed:
                (trial / "patch.diff").write_bytes(patch.encode())
                save_json(trial / "completed.json", {"identity": identity,
                          "result_sha256": study.file_digest(trial / "result.json"),
                          "patch_sha256": study.file_digest(trial / "patch.diff")})
            else:
                (trial / "failure-private.txt").write_bytes(b"Docker containers.create transport timeout\n")
            results.append(result)
        safe = {"schema_version": 1, "protocol_id": protocol["protocol_id"],
                "protocol_sha256": execution["protocol_sha256"], "execution_id": source.name,
                "completed": False, "cohort": [{"instance_id": row["instance_id"], "repo": row["repo"]}
                                               for row in cohort],
                "arms": list(study.ARMS), "repetitions": 1, "trials": results,
                "runtime": {"harness_version": "5.0.2", "runner_sha256": execution["runner_sha256"],
                            "target": protocol["target"], "budgets": protocol["budgets"],
                            "resources": protocol["resources"], "purpose": protocol["purpose"],
                            "dataset": {key: protocol["dataset"][key] for key in ("name", "revision")},
                            "images": [{key: row[key] for key in ("instance_id", "image", "image_id")}
                                       for row in cohort]}, "notes": []}
        save_json(source / "safe_results.json", safe)
        protocol = deepcopy(protocol)
        protocol["protocol_id"] = "continuation-protocol"
        protocol["continuation"] = {**execution, "safe_results_sha256": study.file_digest(source / "safe_results.json")}
        yield SimpleNamespace(root=root, source=source, destination=destination, protocol=protocol,
                              safe=safe, failed=source / "trials" / trial_key(results[-1]))
    finally:
        shutil.rmtree(root)


def republish_safe(case):
    save_json(case.source / "safe_results.json", case.safe)
    case.protocol["continuation"]["safe_results_sha256"] = study.file_digest(case.source / "safe_results.json")


def reject_without_copying(case):
    with pytest.raises(study.InfrastructureError):
        study.inherit_continuation(case.protocol, case.destination)
    assert list((case.destination / "trials").iterdir()) == []


def test_inherits_rotated_prefix_without_rewriting_lineage_or_copying_workspaces(predecessor):
    case = predecessor
    before = file_snapshot(case.source)
    results, provenance = study.inherit_continuation(case.protocol, case.destination)
    assert results == case.safe["trials"][:-1]
    assert provenance == {**case.protocol["continuation"], "inherited_trials": 5,
                          "failed_pre_model_trial": {key: case.safe["trials"][-1][key]
                                                     for key in ("instance_id", "arm", "repetition")},
                          "model_generations_repeated": 0}
    assert {path.name for path in (case.destination / "trials").iterdir()} == {trial_key(r) for r in results}
    for result in results:
        source = case.source / "trials" / trial_key(result)
        copied = case.destination / "trials" / trial_key(result)
        assert {path.name for path in copied.iterdir()} == {
            "identity.json", "result.json", "patch.diff", "completed.json", "source-provenance.json"}
        for filename in ("identity.json", "result.json", "patch.diff", "completed.json"):
            assert (copied / filename).read_bytes() == (source / filename).read_bytes()
        assert study.load(copied / "identity.json")["execution_id"] == case.source.name
        pointer = study.load(copied / "source-provenance.json")
        assert {key: pointer[key] for key in case.protocol["continuation"]} == case.protocol["continuation"]
        original = case.source.parent / pointer["execution_id"] / "trials" / pointer["trial"]
        assert original == source
    assert file_snapshot(case.source) == before


def test_changed_phase_budget_cannot_inherit_previous_results(predecessor):
    predecessor.protocol["budgets"]["preparation"]["output_tokens"] += 1
    reject_without_copying(predecessor)


def test_late_patch_tampering_rejects_entire_prefix(predecessor):
    case = predecessor
    path = case.source / "trials" / trial_key(case.safe["trials"][-2]) / "patch.diff"
    path.write_bytes(path.read_bytes() + b"tampered\n")
    reject_without_copying(case)


def test_repinned_result_cannot_override_completed_ledger(predecessor):
    case = predecessor
    result = case.safe["trials"][-2]
    result["metrics"]["output_tokens"] += 1
    save_json(case.source / "trials" / trial_key(result) / "result.json", result)
    republish_safe(case)
    reject_without_copying(case)


def test_forged_ledger_cannot_reassign_original_execution(predecessor):
    case = predecessor
    trial = case.source / "trials" / trial_key(case.safe["trials"][-2])
    identity = study.load(trial / "identity.json")
    identity["execution_id"] = case.destination.name
    save_json(trial / "identity.json", identity)
    ledger = study.load(trial / "completed.json")
    ledger["identity"] = identity
    save_json(trial / "completed.json", ledger)
    reject_without_copying(case)


def test_changed_publication_requires_original_pin(predecessor):
    case = predecessor
    case.safe["trials"][0]["resolved"] = True
    save_json(case.source / "safe_results.json", case.safe)
    reject_without_copying(case)


def test_cherry_picked_results_are_not_an_ordered_schedule_prefix(predecessor):
    case = predecessor
    omitted = case.safe["trials"].pop(1)
    # Remove omitted evidence too: remaining files and result list agree with each
    # other, but cannot stand in for the frozen native schedule.
    shutil.rmtree(case.source / "trials" / trial_key(omitted))
    republish_safe(case)
    reject_without_copying(case)


def test_reserved_request_with_zero_reported_usage_cannot_be_repeated(predecessor):
    # Even an empty durable reservation defeats a setup-only failure claim.
    (predecessor.failed / "preparation-request-001-reserved.json").write_bytes(b"")
    reject_without_copying(predecessor)


def test_accounted_generation_without_phase_files_cannot_be_repeated(predecessor):
    case = predecessor
    case.safe["trials"][-1]["metrics"]["output_tokens"] = 1
    save_json(case.failed / "result.json", case.safe["trials"][-1])
    republish_safe(case)
    reject_without_copying(case)


def test_live_predecessor_cannot_be_inherited(predecessor, monkeypatch):
    monkeypatch.setattr(study, "process_identity", lambda pid: "old-start" if pid == 12345 else None)
    reject_without_copying(predecessor)
