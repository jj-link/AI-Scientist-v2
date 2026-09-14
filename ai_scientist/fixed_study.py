"""Trusted, once-only native repair execution and frozen evidence for BFTS.

Set AI_SCIENTIST_REPAIR_PROTOCOL to the pinned protocol JSON. A saved idea's
Execution declares kind=fixed_swebench_repair and protocol_sha256. For another
analysis, copy the published summary's source_evidence mapping: it identifies
the native receipt, not the reserialized local data hash. Completed evidence
can be imported without native/model calls; partial attempts cannot be retried.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import threading
import time

from ai_scientist.progress import PipelineFailure, check_stop, emit

KIND = "fixed_swebench_repair"
ARMS = ["direct", "diagnosis", "notes", "locations"]
METRICS = (
    "input_tokens", "output_tokens", "tool_calls", "elapsed_seconds", "model_seconds",
    "prompt_seconds", "generation_seconds", "tool_seconds", "evaluation_seconds",
    "max_context_tokens", "peak_gpu_memory_mib", "draft_tokens", "accepted_draft_tokens",
    "changed_files", "handoff_tokens",
)
NULLABLE = {"model_seconds", "prompt_seconds", "generation_seconds", "peak_gpu_memory_mib",
            "draft_tokens", "accepted_draft_tokens"}
STATE = ".fixed-study.json"
RESULT = "data/safe_results.json"
SUMMARY = "data/fixed_study_summary.json"


def is_fixed_study(idea):
    execution = idea.get("Execution", {})
    return isinstance(execution, dict) and execution.get("kind") == KIND


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _exclusive_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())


def _require(condition, message):
    if not condition:
        raise PipelineFailure("Fixed study: " + message)


def _protocol(idea):
    path = os.environ.get("AI_SCIENTIST_REPAIR_PROTOCOL")
    _require(bool(path), "AI_SCIENTIST_REPAIR_PROTOCOL is required.")
    raw = Path(path).read_bytes()
    digest = _sha(raw)
    _require(idea["Execution"].get("protocol_sha256") == digest, "frozen protocol SHA256 mismatch.")
    protocol = json.loads(raw)
    _require(protocol.get("kind") == KIND and protocol.get("schema_version") == 1,
             "unsupported protocol.")
    _require(protocol.get("purpose") in {"comparative_study", "runtime_smoke"}, "missing study purpose.")
    _require(protocol.get("arms") == ARMS and protocol.get("repetitions") == 1,
             "expected four frozen arms and one repetition.")
    cohort = protocol["cohort"]
    _require(bool(cohort) and len({row["instance_id"] for row in cohort}) == len(cohort),
             "cohort is empty or duplicated.")
    _require(protocol["purpose"] != "comparative_study" or len(cohort) == 20,
             "comparative study requires exactly 20 issues (80 trials).")
    return protocol, digest


def _native_share(native, path):
    _require(re.fullmatch(r"[A-Za-z0-9_.-]+", native["distro"]) is not None, "invalid distro.")
    posix = PurePosixPath(path)
    _require(posix.is_absolute() and ".." not in posix.parts, "invalid native absolute path.")
    return Path(f"//wsl.localhost/{native['distro']}") / str(posix).lstrip("/")


def _command(state, *, stop=False):
    native = state["native"]
    command = ["wsl.exe", "--distribution", native["distro"], "--user", native["user"],
               "--exec", native["python"], state["runner"], "--protocol", native["protocol_path"],
               "--execution-id", state["execution_id"]]
    if stop:
        command.append("--stop")
    return command


def stop_fixed_study(run_dir):
    """Native-aware cancellation, also callable by Studio's crash-recovery supervisor."""
    path = Path(run_dir) / STATE
    if not path.is_file():
        return
    state = _json(path)
    _require(state["execution_id"] == Path(run_dir).resolve().name and
             re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", state["execution_id"]) is not None,
             "native cleanup execution ownership mismatch.")
    if state["source_evidence"] is not None:
        # Analysis owns its local copies, never the shared completed execution.
        return
    native = state["native"]
    run = _native_share(native, str(PurePosixPath(native["run_root"]) / state["execution_id"]))
    if run.is_dir():
        (run / "STOP").touch(exist_ok=True)
    # --stop validates the native PID/start identity and removes only owned containers.
    # Killing the Windows transport is not a substitute for this acknowledgement.
    result = subprocess.run(_command(state, stop=True), timeout=75, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    _require(result.returncode == 0, "native cancellation/cleanup was not acknowledged.")


def validate_results(results, protocol, digest, execution_id):
    """Reject partial, duplicated, mismatched, or unaccounted trials before any analysis."""
    _require(results.get("schema_version") == 1 and results.get("completed") is True,
             "native study did not complete.")
    for key, expected in (("protocol_id", protocol["protocol_id"]), ("protocol_sha256", digest),
                          ("execution_id", execution_id), ("arms", ARMS), ("repetitions", 1)):
        _require(results.get(key) == expected, f"result {key} mismatch.")
    cohort = [{"instance_id": row["instance_id"], "repo": row["repo"]} for row in protocol["cohort"]]
    _require(results.get("cohort") == cohort, "result cohort/order mismatch.")
    expected = {(row["instance_id"], arm, 0): row["repo"] for row in cohort for arm in ARMS}
    trials = results.get("trials")
    _require(isinstance(trials, list) and len(trials) == len(expected), "missing or extra trials.")
    safe_trials = []
    seen = set()
    for trial in trials:
        key = (trial.get("instance_id"), trial.get("arm"), trial.get("repetition"))
        _require(key in expected and key not in seen and trial.get("repo") == expected[key],
                 "duplicate or foreign trial identity.")
        seen.add(key)
        status, resolved = trial.get("status"), trial.get("resolved")
        _require(status in {"resolved", "unresolved", "empty_patch", "budget_exhausted"}
                 and isinstance(resolved, bool), "interrupted/infra/unscored trial cannot produce a paper.")
        _require((status != "resolved" or resolved) and (status not in {"unresolved", "empty_patch"} or not resolved),
                 "inconsistent resolution status.")
        patch = trial.get("patch")
        _require(isinstance(patch, str) and trial.get("patch_sha256") == _sha(patch.encode("utf-8")),
                 "patch identity mismatch.")
        _require(isinstance(trial.get("handoff"), str) and isinstance(trial.get("termination_reason"), str),
                 "missing visible handoff or termination reason.")
        metrics = trial.get("metrics", {})
        for name in METRICS:
            _require(name in metrics, f"missing measured {name}.")
            value = metrics[name]
            _require((value is None and name in NULLABLE) or
                     (type(value) in {int, float} and math.isfinite(value) and value >= 0),
                     f"invalid measured {name}.")
            if name.endswith("_tokens") or name in {"tool_calls", "changed_files"}:
                _require(value is None or type(value) is int, f"noninteger measured {name}.")
        _require(type(trial["repetition"]) is int, "invalid repetition identity.")
        limits = protocol["budgets"]
        phases = [limits["direct"]] if trial["arm"] == "direct" else [limits["preparation"], limits["repair"]]
        for metric in ("input_tokens", "output_tokens", "tool_calls"):
            _require(metrics[metric] <= sum(phase[metric] for phase in phases), f"{metric} exceeds frozen allowance.")
        _require(metrics["handoff_tokens"] <= limits["handoff_tokens"], "handoff exceeds frozen allowance.")
        _require(metrics["max_context_tokens"] <= protocol["target"]["context_tokens"], "context exceeds frozen allowance.")
        if metrics["draft_tokens"] is not None and metrics["accepted_draft_tokens"] is not None:
            _require(metrics["accepted_draft_tokens"] <= metrics["draft_tokens"], "invalid draft accounting.")
        safe_trials.append({name: trial[name] for name in (
            "instance_id", "repo", "arm", "repetition", "status", "resolved", "termination_reason",
            "patch_sha256", "patch", "handoff")})
        safe_trials[-1]["metrics"] = {name: metrics[name] for name in METRICS}
    # Export only deliberately safe fields, never arbitrary native telemetry or evaluator paths.
    runtime = results.get("runtime", {})
    target_keys = ("base_url", "model", "system_fingerprint", "context_tokens", "temperature",
                   "seed", "thinking", "mtp", "cache_k", "cache_v")
    _require(all(runtime.get("target", {}).get(key) == protocol["target"][key] for key in target_keys),
             "runtime target provenance differs from frozen protocol.")
    _require(runtime.get("harness_version") == "5.0.2", "incompatible evaluator harness provenance.")
    _require(isinstance(runtime.get("runner_sha256"), str) and
             re.fullmatch(r"[0-9a-f]{64}", runtime["runner_sha256"]) is not None,
             "missing native source identity.")
    budget_keys = ("direct", "preparation", "repair", "handoff_tokens", "handoff_output_reserve",
                   "tool_timeout_seconds", "tool_output_tokens", "evaluation_timeout_seconds")
    resource_keys = ("container_cpus", "container_memory_bytes", "container_pids",
                     "minimum_free_bytes", "study_image_budget_bytes", "evaluator_writable_limits_bytes",
                     "evaluator_read_only_root", "evaluator_cap_sys_admin", "evaluator_command_output_bytes")
    safe_runtime = {"purpose": protocol["purpose"],
                    "target": {key: protocol["target"][key] for key in target_keys},
                    "budgets": {key: protocol["budgets"][key] for key in budget_keys},
                    "resources": {key: protocol["resources"][key] for key in resource_keys},
                    "dataset": {key: protocol["dataset"][key] for key in ("name", "revision")},
                    "images": [{key: row[key] for key in ("instance_id", "image", "image_id")}
                               for row in protocol["cohort"]]}
    for key in ("purpose", "dataset", "images"):
        _require(runtime.get(key) == safe_runtime[key], f"runtime {key} provenance mismatch.")
    for section, keys in (("budgets", budget_keys), ("resources", resource_keys)):
        _require(all(runtime.get(section, {}).get(key) == safe_runtime[section][key] for key in keys),
                 f"runtime {section} provenance mismatch.")
    for key in ("harness_version", "runner_sha256"):
        if key in runtime:
            _require(isinstance(runtime[key], str), "invalid runtime provenance.")
            safe_runtime[key] = runtime[key]
    notes = results.get("notes", [])
    _require(isinstance(notes, list) and all(isinstance(note, str) for note in notes), "invalid caveats.")
    return {"schema_version": 1, "protocol_id": protocol["protocol_id"], "protocol_sha256": digest,
            "execution_id": execution_id, "completed": True, "cohort": cohort, "arms": ARMS,
            "repetitions": 1, "trials": safe_trials, "runtime": safe_runtime, "notes": notes}


def _wilson(successes, n):
    z = 1.959963984540054
    p = successes / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return [max(0.0, center - radius), min(1.0, center + radius)]


def summarize_results(results, result_sha256, data_path, *, native_result_sha256):
    """Compute denominators, measured costs, and paired uncertainty without an LLM."""
    trials = results["trials"]
    n = len(results["cohort"])
    indexed = {(row["instance_id"], row["arm"]): row for row in trials}
    arms = {}
    for arm in ARMS:
        rows = [row for row in trials if row["arm"] == arm]
        count = sum(row["resolved"] for row in rows)
        costs = {}
        for metric in METRICS:
            values = [row["metrics"][metric] for row in rows if row["metrics"][metric] is not None]
            costs[metric] = {"observed_trials": len(values), "total": sum(values) if values else None,
                             "mean": sum(values) / len(values) if values else None,
                             "max": max(values) if values else None}
        arms[arm] = {"resolved": count, "denominator": n, "resolution_rate": count / n,
                     "resolution_wilson_95": _wilson(count, n), "costs": costs,
                     "statuses": {status: sum(row["status"] == status for row in rows)
                                  for status in sorted({row["status"] for row in rows})}}
    paired = {}
    for left, right in itertools.combinations(ARMS, 2):
        differences = [int(indexed[(row["instance_id"], left)]["resolved"]) -
                       int(indexed[(row["instance_id"], right)]["resolved"]) for row in results["cohort"]]
        wins, losses = differences.count(1), differences.count(-1)
        discordant = wins + losses
        exact_p = min(1.0, 2 * sum(math.comb(discordant, k) for k in range(min(wins, losses) + 1)) /
                      (2 ** discordant)) if discordant else 1.0
        delta = (wins - losses) / n
        # Finite-sample distribution-free bound for paired differences in [-1, 1].
        radius = math.sqrt(2 * math.log(40) / n)
        paired[f"{left}_vs_{right}"] = {
            "left": left, "right": right, "wins": wins, "losses": losses,
            "ties": n - discordant, "denominator": n, "resolution_rate_difference": delta,
            "paired_hoeffding_95": [max(-1.0, delta - radius), min(1.0, delta + radius)],
            "mcnemar_exact_two_sided_p": exact_p,
        }
    return {"schema_version": 1, "kind": KIND, "execution_id": results["execution_id"],
            "protocol_id": results["protocol_id"], "protocol_sha256": results["protocol_sha256"],
            "safe_results_sha256": result_sha256, "safe_data_path": str(data_path),
            "source_evidence": {"execution_id": results["execution_id"],
                                "safe_results_sha256": native_result_sha256},
            "completed": True, "trial_count": len(trials), "repetitions": 1,
            "arms": arms, "paired_comparisons": paired,
            "individual_outcomes": [{key: row[key] for key in
                ("instance_id", "repo", "arm", "repetition", "status", "resolved", "termination_reason", "metrics")}
                for row in trials], "runtime": results["runtime"],
            "interpretation": [
                "All frozen issues remain in every denominator; unsuccessful attempts are not dropped.",
                "Wilson intervals are marginal resolution intervals; paired Hoeffding intervals are conservative finite-sample 95% bounds for mean issue-level differences.",
                "Exact McNemar p-values are two-sided, unadjusted for six comparisons; descriptive/exploratory, not proof of superiority.",
                "Uncertainty treats issues as sampling units; this fixed cohort and one deterministic repetition do not establish model-seed variability or population representativeness.",
                "Null measurements remain unavailable. Cost means/totals show observed counts; do not treat partially observed totals as complete cost.",
                "BFTS code seeds rerun analysis of the same evidence, never independent repair trials.",
            ] + results["notes"]}


def load_fixed_study_summary(run_dir):
    """Fail closed for fixed ideas; recompute authoritative evidence outside node filters."""
    root = Path(run_dir).resolve()
    idea_path = root / "idea.json"
    if not idea_path.is_file() or not is_fixed_study(_json(idea_path)):
        _require(not (root / STATE).exists(), "saved fixed-study idea identity disappeared.")
        return None
    state = _json(root / STATE)
    raw = (root / RESULT).read_bytes()
    saved = _json(root / SUMMARY)
    receipt = _json(root / ".fixed-study-completed.json")
    _require(_sha(raw) == receipt["safe_results_sha256"] == saved.get("safe_results_sha256"),
             "safe evidence changed after execution.")
    execution = _json(idea_path)["Execution"]
    _require(execution["protocol_sha256"] == state["protocol_sha256"]
             and execution.get("source_evidence") == state["source_evidence"],
             "saved idea execution identity changed.")
    if state["source_evidence"] is not None:
        _require(receipt["native_safe_results_sha256"] == state["source_evidence"]["safe_results_sha256"],
                 "shared evidence receipt mismatch.")
    results = validate_results(json.loads(raw), state["protocol"], state["protocol_sha256"],
                               state["evidence_execution_id"])
    _require(results["runtime"]["runner_sha256"] == state["runner_sha256"], "saved runner identity mismatch.")
    summary = summarize_results(results, _sha(raw), root / RESULT,
                                native_result_sha256=receipt["native_safe_results_sha256"])
    _require(saved == summary, "trusted summary changed after execution.")
    return summary


def analysis_guidance(summary_path):
    summary = load_fixed_study_summary(Path(summary_path).parent.parent)
    _require(summary is not None, "analysis requires a completed fixed study.")
    return [
        "The trusted native Gemma repair study is ALREADY COMPLETE. You are analyzing immutable recorded evidence only.",
        f"Read authoritative JSON summary at {str(summary_path)!r} and raw safe trials at {summary['safe_data_path']!r}.",
        "Never invoke repair/model/evaluator APIs, Docker, WSL, the native runner, downloads, training, or new experiments. Never alter evidence or change arms, budgets, cohort, patches, outcomes, or denominators.",
        "Baseline tuning means checking analysis correctness and visual clarity, not changing Gemma settings or selecting by hidden-test scores. Ablations mean the four already-recorded arms only.",
        "Analyze all direct/diagnosis/notes/locations trials, paired issue wins/losses/ties, uncertainty and actual measured costs. Null costs are unavailable, not zero. No synthetic data, loss curves, train/validation split or invented metrics.",
        "BFTS seeds repeat deterministic analysis, not scientific replication. Never use their variance as repair-model uncertainty or count them as additional trials.",
        "Write a self-contained Python script executing at global scope. Create working_dir = os.path.join(os.getcwd(), 'working'). Save derived plottable data to working/experiment_data.npy using np.save with a dictionary keyed by arm, retaining recorded metric names and values; no train/val/loss placeholders. Save figures in working_dir.",
        "Print measured per-arm resolution_rate and paired comparisons. Analysis improvements cannot change resolution_rate. Compare derived data against the authoritative summary; report disagreements as bugs rather than rewriting evidence.",
    ]


def run_fixed_study(idea, run_dir, *, on_event=None, should_stop=None):
    """Publish one native execution, or pinned completed evidence shared by analyses."""
    if not is_fixed_study(idea):
        return None
    check_stop(should_stop)
    root = Path(run_dir).resolve()
    protocol, digest = _protocol(idea)
    execution_id = root.name
    valid_id = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}"
    _require(re.fullmatch(valid_id, execution_id) is not None, "invalid Studio execution identity.")
    reference = idea["Execution"].get("source_evidence")
    if reference is not None:
        _require(isinstance(reference, dict)
                 and set(reference) == {"execution_id", "safe_results_sha256"}
                 and isinstance(reference["execution_id"], str)
                 and re.fullmatch(valid_id, reference["execution_id"]) is not None
                 and isinstance(reference["safe_results_sha256"], str)
                 and re.fullmatch(r"[0-9a-f]{64}", reference["safe_results_sha256"]) is not None,
                 "invalid shared evidence reference.")
    evidence_id = reference["execution_id"] if reference is not None else execution_id
    state_path = root / STATE
    if state_path.exists():
        state = _json(state_path)
        _require(state["execution_id"] == execution_id and state["protocol_sha256"] == digest
                 and state["source_evidence"] == reference,
                 "execution identity cannot be changed or restarted.")
        # Completed local evidence may be read again; an incomplete reservation is terminal.
        summary = load_fixed_study_summary(root)
        _require(summary is not None, "incomplete execution cannot be restarted.")
        return summary
    source = Path(__file__).with_name("swebench_study.py").resolve().as_posix()
    _require(len(source) > 2 and source[1:3] == ":/", "bridge requires a Windows checkout.")
    runner = "/mnt/" + source[0].lower() + source[2:]
    native = protocol["native"]
    native_protocol = _native_share(native, native["protocol_path"])
    _require(_sha(native_protocol.read_bytes()) == digest, "native protocol differs from pinned protocol.")
    native_run = _native_share(native, str(PurePosixPath(native["run_root"]) / evidence_id))
    if reference is None:
        _require(not native_run.exists(), "native execution already exists; no attempt regeneration is allowed.")
        runner_hash = _sha(Path(source).read_bytes())
    else:
        _require((native_run / "completed.json").is_file(), "shared native evidence is incomplete.")
        runner_hash = _json(native_run / "execution.json")["runner_sha256"]
    state = {"execution_id": execution_id, "evidence_execution_id": evidence_id,
             "source_evidence": reference, "protocol_sha256": digest, "native": native,
             "runner": runner, "runner_sha256": runner_hash, "protocol": protocol}
    _exclusive_json(state_path, state)
    child = None
    try:
        if reference is None:
            child = subprocess.Popen(_command(state), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1)
            def relay():
                for line in child.stdout:
                    print("[native study] " + line.rstrip(), flush=True)
            reader = threading.Thread(target=relay, daemon=True)
            reader.start()
            emit(on_event, "fixed_study_started", "experiments", execution_id=execution_id)
            while child.poll() is None:
                check_stop(should_stop)
                time.sleep(0.2)
            reader.join(timeout=1)
            check_stop(should_stop)
            _require(child.returncode == 0, "native runner failed; retained attempts cannot be regenerated.")
        else:
            check_stop(should_stop)
            emit(on_event, "fixed_study_evidence_reused", "experiments",
                 execution_id=execution_id, evidence_execution_id=evidence_id)
        _require(_sha(native_protocol.read_bytes()) == digest
                 and _sha((native_run / "frozen-protocol.json").read_bytes()) == digest,
                 "protocol changed during execution.")
        raw = (native_run / "safe_results.json").read_bytes()
        native_hash = _sha(raw)
        identity = {"execution_id": evidence_id, "protocol_sha256": digest, "runner_sha256": runner_hash}
        completion = _json(native_run / "completed.json")
        _require(_json(native_run / "execution.json") == completion["identity"] == identity
                 and completion["safe_results_sha256"] == native_hash,
                 "native completion receipt mismatch.")
        _require(reference is None or reference["safe_results_sha256"] == native_hash,
                 "shared native evidence SHA256 mismatch.")
        safe = validate_results(json.loads(raw), protocol, digest, evidence_id)
        _require(safe["runtime"]["runner_sha256"] == runner_hash, "native source identity mismatch.")
        _exclusive_json(root / RESULT, safe)
        result_hash = _sha((root / RESULT).read_bytes())
        summary = summarize_results(safe, result_hash, root / RESULT, native_result_sha256=native_hash)
        _exclusive_json(root / SUMMARY, summary)
        _exclusive_json(root / ".fixed-study-completed.json",
                        {"safe_results_sha256": result_hash, "native_safe_results_sha256": native_hash})
        for path in (root / RESULT, root / SUMMARY, state_path, root / ".fixed-study-completed.json"):
            path.chmod(0o444)
        emit(on_event, "fixed_study_completed", "experiments", execution_id=execution_id,
             evidence_execution_id=evidence_id, trials=len(safe["trials"]))
        return summary
    except BaseException:
        stop_fixed_study(root)
        raise
    finally:
        if child is not None and child.poll() is not None and child.stdout is not None:
            child.stdout.close()


def configure_fixed_analysis(config_path, run_dir):
    """Keep all real BFTS machinery but replace its inputs with completed safe data."""
    import yaml
    summary = load_fixed_study_summary(run_dir)
    if summary is None:
        return
    _require(summary["runtime"]["purpose"] == "comparative_study",
             "runtime smoke evidence is not a publishable comparative study.")
    path = Path(config_path)
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["fixed_study_summary"] = str((Path(run_dir) / SUMMARY).resolve())
    config["data_dir"] = str((Path(run_dir) / "data").resolve())
    config["copy_data"] = True
    config["preprocess_data"] = False
    config["exp_name"] = "run"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")


def publish_fixed_study_summary(run_dir, log_dir):
    """Publish trusted evidence after BFTS allocates its ordinary log directory."""
    summary = load_fixed_study_summary(run_dir)
    _require(summary is not None, "no completed evidence to publish.")
    destination = Path(log_dir) / "fixed_study_summary.json"
    _exclusive_json(destination, summary)
    destination.chmod(0o444)
