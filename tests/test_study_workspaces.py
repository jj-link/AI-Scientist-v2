"""Workspace admission must precede scoring; runtime outputs are not source patches."""
from copy import deepcopy
import io
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_scientist import fixed_study
from ai_scientist import swebench_study as study
from ai_scientist.progress import PipelineFailure


@pytest.fixture
def case_root(monkeypatch):
    root = Path(__file__).parent / f"workspace_checks_{uuid4().hex}"
    root.mkdir()
    # Directory fsync is a Linux publication concern, not the contract exercised here.
    monkeypatch.setattr(study, "atomic", lambda path, data: Path(path).write_bytes(data))
    monkeypatch.setattr(study, "save", lambda path, value: Path(path).write_bytes(study.json_bytes(value)))
    try:
        yield root
    finally:
        def remove_readonly(action, path, error):
            if not isinstance(error[1], PermissionError):
                raise error[1]
            Path(path).chmod(0o700 if Path(path).is_dir() else 0o600)
            action(path)
        shutil.rmtree(root, onerror=remove_readonly)


def test_partial_cohort_qualification_is_never_published_as_passed(case_root, monkeypatch):
    rows = [{"instance_id": "fixture__project-1"}, {"instance_id": "fixture__project-2"}]
    protocol = {"cohort": rows, "resources": {"minimum_free_bytes": 0}}
    monkeypatch.setattr(study.shutil, "disk_usage", lambda _: SimpleNamespace(free=100 * study.MAX_ARCHIVE))
    monkeypatch.setattr(study, "snapshot", lambda *args: (case_root / "base", case_root / "pristine.tar"))

    class ProjectWorkspace:
        def __init__(self, protocol, row, upload, directory, mode, control):
            if row["instance_id"] == rows[1]["instance_id"]:
                raise study.InfrastructureError("Second project's native dependency is missing")
            self.qualification = {"instance_id": row["instance_id"], "mode": mode, "passed": True}

        def close(self):
            pass

    monkeypatch.setattr(study, "Workspace", ProjectWorkspace)
    with pytest.raises(study.InfrastructureError):
        study.prepare_workspaces(protocol, case_root, SimpleNamespace(check=lambda: None))
    receipt = study.load(case_root / "workspaces/qualification.json")
    assert receipt["passed"] is False
    assert {(item["instance_id"], item["mode"]) for item in receipt["checks"]} == {
        (rows[0]["instance_id"], "preparation"), (rows[0]["instance_id"], "repair")}


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "core.hooksPath=/dev/null", *args],
                          check=True, capture_output=True).stdout


class LocalCommands:
    def check(self):
        pass

    def command(self, argv, *, deadline, env=None, cwd=None):
        result = subprocess.run(argv, env=env, cwd=cwd, capture_output=True, check=False)
        return result.returncode, result.stdout, result.stderr, False


def patch_case(root):
    base = root / "base"
    (base / "package").mkdir(parents=True)
    (base / "package/__init__.py").write_text("def value():\n    return 1\n")
    (base / ".gitignore").write_text("*.so\n")
    git(base, "init", "--initial-branch=baseline")
    git(base, "add", "--all")
    git(base, "-c", "user.name=Workspace regression", "-c", "user.email=fixture@invalid",
        "commit", "--no-gpg-sign", "-m", "Baseline")
    library = base / "package/_runtime.so"
    library.write_bytes(b"\x7fELFimmutable-runtime-output")
    artifact = {"path": "package/_runtime.so", "sha256": study.file_digest(library),
                "bytes": library.stat().st_size}
    model = root / "model"
    shutil.copytree(base, model)
    (model / "package/__init__.py").write_text("def value():\n    return 2\n")
    # A model-editable ignore file cannot be the runtime exclusion boundary.
    (model / ".gitignore").unlink()
    return base, model, artifact


def freeze(root, base, model, artifact):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        archive.add(model, arcname="testbed", recursive=True)
    workspace = study.Workspace.__new__(study.Workspace)
    workspace.control = LocalCommands()
    workspace.source_identity = {"runtime_artifacts": [artifact]}
    workspace.container = SimpleNamespace(
        reload=lambda: None, attrs={"State": {"Running": False}},
        get_archive=lambda path: ([stream.getvalue()], {}))
    captured = root / "captured"
    captured.mkdir()
    return workspace.freeze(captured, base)


def test_source_repair_applies_without_overwriting_existing_runtime(case_root):
    base, model, artifact = patch_case(case_root)
    patch, changed = freeze(case_root, base, model, artifact)
    # This would fail if capture tried to create a runtime file already present
    # in the evaluator image. Apply the actual patch, not a mocked diff.
    git(base, "apply", str((case_root / "captured/patch.diff").resolve()))
    assert runpy.run_path(str(base / "package/__init__.py"))["value"]() == 2
    assert study.file_digest(base / artifact["path"]) == artifact["sha256"]
    assert changed == 2
    assert not (base / ".gitignore").exists()


@pytest.mark.parametrize("mutation", ["changed", "missing", "symlink"])
def test_runtime_tampering_is_not_silently_discarded(case_root, mutation):
    base, model, artifact = patch_case(case_root)
    library = model / artifact["path"]
    if mutation == "changed":
        library.write_bytes(b"different runtime output")
    elif mutation == "missing":
        library.unlink()
    else:
        target = model / "package/other.so"
        library.rename(target)
        try:
            library.symlink_to("other.so")
        except OSError:
            pytest.skip("Host does not permit file symlinks")
    with pytest.raises(study.InfrastructureError):
        freeze(case_root, base, model, artifact)
    assert not (case_root / "captured/patch.diff").exists()
    assert runpy.run_path(str(base / "package/__init__.py"))["value"]() == 1


def junit(cases):
    return ("WORKSPACE_JUNIT=" + json.dumps(cases) + "\n").encode()


def public_case():
    return {"name": "test_get_index", "skipped": False, "errors": [], "failures": []}


@pytest.mark.parametrize("failure", ["zero_tests", "skipped", "collection_error", "wrong_test", "truncated"])
def test_success_exit_does_not_admit_unexercised_tests(failure):
    case = public_case()
    cases = [case]
    if failure == "zero_tests":
        cases = []
    elif failure == "skipped":
        case["skipped"] = True
    elif failure == "collection_error":
        case["errors"] = ["ImportError: missing compiled dependency"]
    elif failure == "wrong_test":
        case["name"] = "unrelated_test"
    assert not study._workspace_test_passed("pydata/xarray", False, 0, junit(cases), b"", failure == "truncated")


def test_negative_control_requires_the_real_assertion_failure():
    control = {"name": "test_workspace_assertion_control", "skipped": False,
               "errors": [], "failures": ["AssertionError: " + study.ASSERTION_CONTROL]}
    cases = [public_case(), control]
    assert study._workspace_test_passed("pydata/xarray", True, 1, junit(cases), b"", False)
    control["errors"], control["failures"] = ["ImportError: dependency"], []
    assert not study._workspace_test_passed("pydata/xarray", True, 1, junit(cases), b"", False)
    control["errors"], control["failures"] = [], ["an unrelated assertion failed"]
    assert not study._workspace_test_passed("pydata/xarray", True, 1, junit(cases), b"", False)


def publication():
    row = {"instance_id": "fixture__project-1", "repo": "fixture/project",
           "image": "fixture@sha256:" + "a" * 64, "image_id": "sha256:" + "a" * 64}
    phase = {"input_tokens": 10000, "output_tokens": 1000, "tool_calls": 10, "seconds": 100}
    protocol = {"protocol_id": "workspace-regression", "purpose": "runtime_smoke", "cohort": [row],
                "target": {"base_url": "http://127.0.0.1:8080", "model": "fixture", "system_fingerprint": "fixture",
                           "context_tokens": 4096, "temperature": 0, "seed": 7, "thinking": False,
                           "mtp": False, "cache_k": "f16", "cache_v": "f16"},
                "budgets": {"direct": phase, "preparation": phase, "repair": phase,
                            "handoff_tokens": 100, "terminal_output_reserve": 100,
                            "tool_timeout_seconds": 30, "tool_output_tokens": 100,
                            "evaluation_timeout_seconds": 100},
                "resources": {"container_cpus": 1, "container_memory_bytes": 1024**3, "container_pids": 64,
                              "minimum_free_bytes": 1024**3, "study_image_budget_bytes": 1024**3,
                              "evaluator_writable_limits_bytes": {}, "evaluator_read_only_root": True,
                              "evaluator_cap_sys_admin": False, "evaluator_command_output_bytes": 1024},
                "dataset": {"name": "fixture", "revision": "b" * 40}}
    results = {"schema_version": 1, "protocol_id": protocol["protocol_id"], "protocol_sha256": "c" * 64,
               "execution_id": "fixture-execution", "completed": True,
               "cohort": [{"instance_id": row["instance_id"], "repo": row["repo"]}],
               "arms": study.ARMS, "repetitions": 1, "trials": [], "notes": [],
               "runtime": {key: deepcopy(protocol[key]) for key in ("purpose", "target", "budgets", "resources", "dataset")}}
    results["runtime"].update(harness_version="5.0.2", runner_sha256="d" * 64,
                             images=[{key: row[key] for key in ("instance_id", "image", "image_id")}],
                             workspace_qualification={"passed": True, "sha256": "e" * 64})
    for arm in study.ARMS:
        results["trials"].append({"instance_id": row["instance_id"], "repo": row["repo"], "arm": arm,
            "repetition": 0, "status": "empty_patch", "resolved": False, "termination_reason": "finish",
            "patch": "", "patch_sha256": study.digest(b""), "handoff": "",
            "metrics": {name: 0 for name in fixed_study.METRICS}})
    return protocol, results


def validate_publication(protocol, results):
    return fixed_study.validate_results(results, protocol, results["protocol_sha256"], results["execution_id"])


def test_qualified_publication_preserves_the_frozen_terminal_allowance():
    protocol, results = publication()
    safe = validate_publication(protocol, results)
    assert safe["runtime"]["budgets"]["terminal_output_reserve"] == 100
    assert safe["runtime"]["workspace_qualification"] == {"passed": True, "sha256": "e" * 64}
    results["runtime"]["budgets"]["terminal_output_reserve"] += 1
    with pytest.raises(PipelineFailure):
        validate_publication(protocol, results)


@pytest.mark.parametrize("qualification", [None, {"passed": False, "sha256": "e" * 64}])
def test_new_study_cannot_publish_scores_from_unqualified_workspaces(qualification):
    protocol, results = publication()
    if qualification is None:
        del results["runtime"]["workspace_qualification"]
    else:
        results["runtime"]["workspace_qualification"] = qualification
    with pytest.raises(PipelineFailure):
        validate_publication(protocol, results)


def test_archived_results_are_read_without_rewriting_the_old_budget():
    protocol, results = publication()
    for budgets in (protocol["budgets"], results["runtime"]["budgets"]):
        budgets["handoff_output_reserve"] = budgets.pop("terminal_output_reserve")
    del results["runtime"]["workspace_qualification"]
    safe = validate_publication(protocol, results)
    assert safe["runtime"]["budgets"]["handoff_output_reserve"] == 100
    assert "terminal_output_reserve" not in safe["runtime"]["budgets"]
