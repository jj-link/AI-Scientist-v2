"""Durable, independently supervised research jobs; no research imports at module load."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

import psutil
import yaml

from .store import Store

ACTIVE = frozenset({"starting", "running", "stopping"})
TERMINAL = frozenset({"stopped", "completed", "partial", "failed", "interrupted"})
LOG_PREVIEW_BYTES = 64 * 1024


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: object, *, exclusive: bool = False) -> None:
    # Same-directory replacement makes readers see either complete version.
    if exclusive:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        return
    staging = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.new")
    try:
        with staging.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)


def identity(process: psutil.Process) -> dict:
    return {"pid": process.pid, "created": process.create_time()}


def matching_process(record: dict | None) -> psutil.Process | None:
    if not record or not record.get("pid") or record.get("created") is None:
        return None
    try:
        process = psutil.Process(int(record["pid"]))
        if process.create_time() != float(record["created"]):
            return None
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        return process
    except (psutil.Error, ValueError, TypeError):
        return None


def job_process(job: dict) -> psutil.Process | None:
    return matching_process({"pid": job.get("pid"), "created": job.get("process_created")})


def owned_records(directory: Path) -> list[dict]:
    try:
        value = json.loads((directory / "processes.json").read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, ValueError):
        return []


def capture_descendants(directory: Path, parent: psutil.Process) -> list[dict]:
    records = {(item.get("pid"), item.get("created")): item for item in owned_records(directory)}
    try:
        children = parent.children(recursive=True)
    except psutil.Error:
        children = []
    for child in children:
        try:
            item = identity(child)
            records[(item["pid"], item["created"])] = item
        except psutil.Error:
            continue
    result = list(records.values())
    write_json(directory / "processes.json", result)
    return result


def stop_time(directory: Path) -> float:
    path = directory / "stop.json"
    try:
        return float(json.loads(path.read_text(encoding="utf-8"))["requested_at"])
    except (OSError, ValueError, KeyError, TypeError):
        requested = time.time()
        try:
            write_json(path, {"requested_at": requested}, exclusive=True)
        except FileExistsError:
            # Another observer is writing the durable request. Never shorten grace.
            return requested
        return requested


def terminate_owned(records: list[dict], *, include: dict | None = None) -> None:
    """Every signal revalidates identity; never target process groups/model servers."""
    candidates = list(records)
    if include:
        candidates.append(include)
    unique = {(item.get("pid"), item.get("created")): item for item in candidates}
    for item in unique.values():
        process = matching_process(item)
        if process is not None and process.pid != os.getpid():
            try:
                process.terminate()
            except psutil.Error:
                pass
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not any(matching_process(item) for item in unique.values() if item.get("pid") != os.getpid()):
            return
        time.sleep(0.1)
    for item in unique.values():
        process = matching_process(item)
        if process is not None and process.pid != os.getpid():
            try:
                process.kill()
            except psutil.Error:
                pass


class Supervisor:
    """Web-side recovery also works when a worker cannot service cancellation."""

    def __init__(self, store: Store):
        self.store = store
        self.closed = threading.Event()
        self.thread: threading.Thread | None = None
        self.children: dict[str, subprocess.Popen] = {}
        self.lock = threading.RLock()

    def start(self) -> None:
        self.reconcile()
        self.thread = threading.Thread(target=self._watch, name="studio-supervisor", daemon=True)
        self.thread.start()

    def close(self) -> None:
        # Closing the HTTP server deliberately does not stop independent workers.
        self.closed.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _watch(self) -> None:
        while not self.closed.wait(0.5):
            try:
                self.reconcile()
            except Exception:
                logging.exception("Job reconciliation failed")

    def launch(self, job: dict) -> None:
        directory = self.store.job_dir(job["id"])
        with self.lock:
            environment = os.environ.copy()
            environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
            kwargs = {"cwd": str(self.store.root), "stdin": subprocess.DEVNULL, "env": environment}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            else:
                kwargs["start_new_session"] = True
            with (directory / "technical.log").open("ab", buffering=0) as log:
                process = subprocess.Popen(
                    [sys.executable, "-u", "-m", "ai_scientist.ui.worker", "--job-id", job["id"]],
                    stdout=log, stderr=log, **kwargs,
                )
            self.children[job["id"]] = process
            try:
                created = psutil.Process(process.pid).create_time()
                self.store.update_job(job["id"], pid=process.pid, process_created=created)
            except psutil.NoSuchProcess:
                # Worker writes its own identity before research imports, or reconciliation fails it.
                pass

    def request_stop(self, job_id: str) -> dict:
        with self.lock:
            job = self.store.get_job(job_id)
            if job["state"] not in ACTIVE:
                return job
            stop_time(self.store.job_dir(job_id))
            self.store.update_job(job_id, state="stopping")
            self.store.add_event(job_id, "stopping", phase=job.get("phase"), data={"message": "Stop requested"})
            return self.store.get_job(job_id)

    def reconcile(self) -> None:
        with self.lock:
            for job_id, child in list(self.children.items()):
                if child.poll() is not None:
                    self.children.pop(job_id, None)
            for job in self.store.jobs():
                if job["state"] not in ACTIVE:
                    continue
                directory = self.store.job_dir(job["id"])
                parent = job_process(job)
                if job["state"] == "stopping":
                    requested = stop_time(directory)
                    if time.time() - requested < 10:
                        continue
                    # The worker normally records its own descendants. At forced stop, refresh once
                    # while the root identity still matches, then keep the captured identities.
                    records = capture_descendants(directory, parent) if parent else owned_records(directory)
                    terminate_owned(records, include={"pid": job.get("pid"), "created": job.get("process_created")})
                    current = self.store.get_job(job["id"])
                    if current["state"] in ACTIVE:
                        self.store.update_job(job["id"], state="stopped", finished_at=now())
                        self.store.add_event(job["id"], "stopped", data={"message": "Job stopped; saved outputs retained"})
                elif parent is None:
                    current = self.store.get_job(job["id"])
                    if current["state"] not in {"starting", "running"} or job_process(current) is not None:
                        continue
                    # Allow the child to publish its identity if the server died during spawn.
                    age = time.time() - datetime.fromisoformat(job["created_at"]).timestamp()
                    if job["state"] == "starting" and age < 15:
                        continue
                    terminate_owned(owned_records(directory))
                    terminal = self.store.update_job(job["id"], state="interrupted", finished_at=now(), error={"code": "worker_missing", "message": "Worker process is no longer running"})
                    if terminal["state"] == "interrupted":
                        self.store.add_event(job["id"], "interrupted", data={"code": "worker_missing", "message": "Worker process is no longer running"})


def credential_values(directory: Path) -> set[str]:
    values = {value for key, value in os.environ.items() if value and re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", key, re.I)}
    try:
        config = yaml.safe_load((directory / "role_config.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        config = None

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).endswith("_env") and isinstance(item, str) and os.environ.get(item):
                    values.add(os.environ[item])
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    collect(config)
    return values


def redact(text: str, secrets: set[str]) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(https?://)[^/\s@]+@", r"\1[REDACTED]@", text, flags=re.I)
    text = re.sub(r"([?&](?:[^=&\s]*(?:key|token|secret|password|credential|signature)[^=&\s]*)=)[^&\s]+", r"\1[REDACTED]", text, flags=re.I)
    text = re.sub(r"(Authorization\s*[:=]\s*(?:Bearer|Basic)\s+)\S+", r"\1[REDACTED]", text, flags=re.I)
    text = re.sub(r"""((?:api[_-]?key|access[_-]?token|password|secret|credential)["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;]+)""", r"\1[REDACTED]", text, flags=re.I)
    return text


def log_preview(directory: Path, *, extra_secrets: set[str] | None = None) -> str:
    path = directory / "technical.log"
    if not path.is_file():
        return "No technical log is available yet."
    secrets = credential_values(directory) | {value for value in (extra_secrets or ()) if value}
    # Read overlap to avoid displaying the suffix of a credential at the tail boundary.
    overlap = max((len(value.encode("utf-8")) for value in secrets), default=0)
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        handle.seek(max(0, size - LOG_PREVIEW_BYTES - overlap))
        content = handle.read(LOG_PREVIEW_BYTES + overlap).decode("utf-8", errors="replace")
    content = redact(content, secrets)
    if size > LOG_PREVIEW_BYTES:
        content = content.split("\n", 1)[-1] if "\n" in content else "[Long log line omitted; download the redacted log.]"
        return "[Showing the most recent log output]\n" + content[-LOG_PREVIEW_BYTES:]
    return content


def log_download(directory: Path):
    """Stream bounded chunks, retaining enough overlap for credentials across boundaries."""
    secrets = credential_values(directory)
    overlap = max(4096, max((len(value) for value in secrets), default=0))
    with (directory / "technical.log").open("r", encoding="utf-8", errors="replace") as handle:
        pending = ""
        while chunk := handle.read(LOG_PREVIEW_BYTES):
            pending += chunk
            # Replace complete configured secrets while preserving the unfinished final line.
            # URL redaction runs only on released complete lines, never on a partial URL.
            if len(pending) > overlap * 2:
                for secret in sorted(secrets, key=len, reverse=True):
                    pending = pending.replace(secret, "[REDACTED]")
                boundary = pending.rfind("\n", 0, len(pending) - overlap)
                if boundary >= 0:
                    yield redact(pending[:boundary + 1], secrets)
                    pending = pending[boundary + 1:]
                elif len(pending) > LOG_PREVIEW_BYTES * 4 + overlap:
                    yield "[Oversized technical log line omitted]\n"
                    while pending and "\n" not in pending:
                        pending = handle.read(LOG_PREVIEW_BYTES)
                    pending = pending.split("\n", 1)[-1] if pending else ""
        if pending:
            yield redact(pending, secrets)


_EVENT_INTS = {"attempt", "round", "attempts", "rounds", "finalized", "failed", "saved_nodes", "total_nodes", "good_nodes", "buggy_nodes", "working_nodes", "error_nodes", "writeup_attempt", "step", "stage", "stage_index", "node_count", "good_count", "buggy_count"}
_EVENT_TEXT = {"stage", "stage_name", "substage", "metric_name", "metric_label", "best_metric", "artifact_id", "code"}
_PHASES = {"preparing", "generation", "experiments", "figures", "citations", "paper", "reviews"}


def safe_event(event: dict) -> tuple[str, str | None, dict]:
    kind = event.get("type", "progress")
    kind = kind if isinstance(kind, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", kind) else "progress"
    phase = event.get("phase")
    phase = phase if isinstance(phase, str) and phase in _PHASES else None
    source = event.get("data") if isinstance(event.get("data"), dict) else {}
    data = {}
    for key, value in source.items():
        if key in _EVENT_INTS and isinstance(value, int) and not isinstance(value, bool):
            data[key] = value
        elif key in _EVENT_TEXT and isinstance(value, str):
            limit = 2048 if key == "best_metric" else 256
            data[key] = value if len(value) <= limit else value[:limit] + "…"
        elif key in {"metric", "best_metric"} and isinstance(value, (int, float)) and not isinstance(value, bool):
            import math
            if math.isfinite(value):
                data[key] = value
    # Exception messages are not safe status text. Raw events remain in the private OS log.
    if kind.endswith(("error", "failed", "failure")):
        data["message"] = "This operation failed. Open technical details for the recorded error."
    return kind, phase, data


def run_job(store: Store, job_id: str) -> int:
    from ai_scientist.progress import Cancelled

    job = store.get_job(job_id)
    if job["state"] not in ACTIVE:
        return 0
    directory = store.job_dir(job_id)
    request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    process = psutil.Process(os.getpid())
    store.update_job(job_id, pid=process.pid, process_created=process.create_time(), started_at=now())
    os.environ["AI_SCIENTIST_ROOT"] = str(store.root)
    os.environ["AI_SCIENTIST_ROLE_CONFIG"] = str(directory / "role_config.yaml")
    os.environ["AI_SCIENTIST_REQUEST_LOG"] = str(directory / "model_requests.jsonl")
    finished = threading.Event()

    def should_stop() -> bool:
        return (directory / "stop.json").exists() or store.get_job(job_id)["state"] == "stopping"

    def watch() -> None:
        while not finished.wait(0.5):
            try:
                records = capture_descendants(directory, process)
                if should_stop() and time.time() - stop_time(directory) >= 10:
                    terminate_owned(records)
                    store.update_job(job_id, state="stopped", finished_at=now())
                    store.add_event(job_id, "stopped", data={"message": "Job stopped; saved outputs retained"})
                    # Exit the uncooperative worker, not its independent model servers.
                    os._exit(0)
            except Exception:
                traceback.print_exc()

    watcher = threading.Thread(target=watch, name="studio-worker-ownership", daemon=True)
    watcher.start()
    finalized = 0
    failed_attempts: set[int] = set()
    started_attempts: set[int] = set()
    secrets = credential_values(directory)

    def on_event(event: dict) -> None:
        nonlocal finalized
        print("Research event:", repr(event), flush=True)
        kind, phase, data = safe_event(event)
        data = {key: redact(value, secrets) if isinstance(value, str) else value for key, value in data.items()}
        source = event.get("data") if isinstance(event.get("data"), dict) else {}
        original = source.get("idea")
        if kind == "attempt_started" and isinstance(data.get("attempt"), int):
            started_attempts.add(data["attempt"])
        if kind == "proposal_finalized":
            if isinstance(original, dict):
                record = store.add_idea(job_id, original)
                finalized += 1
                data.update(idea_id=record["id"], finalized=finalized)
            else:
                with (directory / "malformed_ideas.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(original, ensure_ascii=False) + "\n")
                kind = "proposal_error"
                data.update(code="invalid_proposal", message="The model returned a proposal that is not a JSON object.")
        if kind in {"attempt_error", "attempt_failed", "proposal_error", "generation_error"} and isinstance(data.get("attempt"), int):
            failed_attempts.add(data["attempt"])
        if phase:
            store.update_job(job_id, phase=phase)
        store.add_event(job_id, kind, phase=phase, data=data)
        if should_stop():
            raise Cancelled("Stop requested")

    try:
        if should_stop():
            raise Cancelled("Stop requested")
        store.update_job(job_id, state="running", phase="preparing")
        store.add_event(job_id, "started", phase="preparing", data={"message": "Worker started"})
        if job["kind"] == "idea":
            from ai_scientist.llm import create_client
            from ai_scientist.perform_ideation_temp_free import generate_temp_free_idea

            client, model = create_client("role/ideation")
            description = "Research question\n" + request["research_question"]
            if request.get("context"):
                description += "\n\nConstraints and context\n" + request["context"]
            store.update_job(job_id, phase="generation")
            ideas = generate_temp_free_idea(
                idea_fname=str(directory / "generated_ideas.json"), client=client, model=model,
                workshop_description=description, max_num_generations=request["attempts"],
                num_reflections=request["rounds"], reload_ideas=False,
                on_event=on_event, should_stop=should_stop,
            )
            # Finalization hooks own persistence; a missing hook must never silently lose output.
            if not isinstance(ideas, list) or finalized != sum(isinstance(idea, dict) for idea in ideas):
                raise RuntimeError("Generator finalization events did not match its returned proposals")
            result = {"finalized": finalized, "attempted": len(started_attempts), "failed_attempts": max(len(failed_attempts), len(started_attempts) - finalized)}
            state = "failed" if finalized == 0 else "partial" if result["failed_attempts"] else "completed"
            error = {"code": "no_proposals", "message": "No proposals were created. Open technical details for recorded generation errors."} if not finalized else None
        elif job["kind"] == "experiment":
            from launch_scientist_bfts import parse_arguments, run_pipeline

            args = parse_arguments([
                "--writeup-type", "icbinb", "--load_ideas", str(directory / "idea.json"),
                "--idea_idx", "0", "--role-config", str(directory / "role_config.yaml"),
                "--bfts-config", str(directory / "bfts_config.yaml"),
                "--model_agg_plots", "role/plot_generation", "--model_writeup", "role/writeup",
                "--model_citation", "role/citation", "--model_writeup_small", "role/writeup_small",
                "--model_review", "role/review",
            ])
            run_directory = (store.root / "experiments" / job["run_id"]).resolve()
            raw = run_pipeline(args, run_dir=run_directory, on_event=on_event, should_stop=should_stop)
            if not isinstance(raw, dict) or not raw.get("pdf_path") or not raw.get("log_dir"):
                raise RuntimeError("Pipeline did not return the required completed outputs")
            result = {}
            for key in ("pdf_path", "log_dir", "figures"):
                values = raw.get(key, []) if key == "figures" else [raw[key]]
                paths = []
                for value in values:
                    path = Path(value)
                    if not path.is_absolute():
                        path = store.root / path
                    path = path.resolve()
                    if not path.is_relative_to(run_directory) or not path.exists():
                        raise RuntimeError("Pipeline returned an unavailable or out-of-run artifact")
                    paths.append(path.relative_to(run_directory).as_posix())
                result[key] = paths if key == "figures" else paths[0]
            state, error = "completed", None
        else:
            raise RuntimeError("Unknown job kind")
        if should_stop():
            raise Cancelled("Stop requested")
        exit_code = 0 if state in {"completed", "partial"} else 1
    except Cancelled:
        state, error, exit_code = "stopped", None, 0
        result = {"finalized": finalized, "attempted": len(started_attempts)} if job["kind"] == "idea" else None
    except BaseException:
        traceback.print_exc()
        stopped = should_stop()
        state = "stopped" if stopped else "partial" if job["kind"] == "idea" and finalized else "failed"
        error = None if stopped else {"code": "research_failed", "message": "Research execution failed. Open technical details for the recorded error."}
        result = {"finalized": finalized, "attempted": len(started_attempts)} if job["kind"] == "idea" else None
        exit_code = 1
    finally:
        finished.set()
        watcher.join(timeout=1)
        terminate_owned(capture_descendants(directory, process))
    # Keep the single compute slot occupied until launcher-owned children are gone.
    if should_stop():
        state, error = "stopped", None
    terminal = store.update_job(job_id, state=state, result=result, error=error, finished_at=now())
    if terminal["state"] == "stopping":
        terminal = store.update_job(job_id, state="stopped", error=None, finished_at=now())
    state = terminal["state"]
    data = error or (result if job["kind"] == "idea" else {"message": "Selected pipeline stages finished"})
    if state == "stopped":
        data = {"message": "Job stopped; saved outputs retained"}
    store.add_event(job_id, state, data=data)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one recorded AI-Scientist Studio job")
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args(argv)
    from uuid import UUID
    job_id = str(UUID(args.job_id))
    root = Path(__file__).resolve().parents[2]
    return run_job(Store(root), job_id)


if __name__ == "__main__":
    raise SystemExit(main())
