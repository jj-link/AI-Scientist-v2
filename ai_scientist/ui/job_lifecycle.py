"""Frozen restart inputs and deletion limited to a failed job's owned files."""
from __future__ import annotations

import json
import math
from pathlib import Path
import shutil
from uuid import UUID

from filelock import FileLock, Timeout
import yaml

from . import worker
from .schemas import validate_idea
from .store import Conflict, Store

_SNAPSHOT_LIMIT = 2 * 1024 * 1024


def _owned_directory(path: Path, root: Path) -> Path:
    """Reject redirected roots, job directories, and Windows junctions before mutation."""
    try:
        path.relative_to(root)
        for part in (path, *path.parents):
            if part == root:
                break
            if part.is_symlink() or part.resolve() != part or (part.exists() and not part.is_dir()):
                raise ValueError()
        return path
    except (OSError, ValueError, RuntimeError):
        raise Conflict({"message": "Run directories must not be redirected. No files were removed."}) from None


def job_directories(store: Store, job: dict) -> tuple[Path, Path]:
    job_id = str(UUID(job["id"]))
    if job["run_id"] != "ui_" + job_id:
        raise Conflict({"message": "This run does not have a recognized owned output directory."})
    return (
        _owned_directory(store.job_dir(job_id), store.root),
        _owned_directory(store.root / "experiments" / job["run_id"], store.root),
    )


def _read_snapshot(path: Path) -> bytes:
    try:
        if path.is_symlink() or path.resolve() != path or not path.is_file():
            raise ValueError()
        with path.open("rb") as stream:
            content = stream.read(_SNAPSHOT_LIMIT + 1)
        if len(content) > _SNAPSHOT_LIMIT:
            raise ValueError()
        return content
    except (OSError, ValueError, RuntimeError):
        raise Conflict({"message": "The original run snapshots are missing or unsafe. Prepare a new experiment from the saved idea instead."}) from None


def ensure_worker_exited(job: dict, directory: Path) -> None:
    if worker.job_process(job) is not None:
        raise Conflict({"message": "The failed worker is still exiting. Wait before restarting or deleting this run."})
    ownership = directory / "processes.json"
    if ownership.exists() or ownership.is_symlink():
        try:
            records = json.loads(_read_snapshot(ownership))
            if not isinstance(records, list) or any(
                not isinstance(record, dict) or type(record.get("pid")) is not int or record["pid"] <= 0
                or type(record.get("created")) not in (int, float) or not math.isfinite(record["created"])
                or record["created"] <= 0
                for record in records
            ):
                raise ValueError()
        except (ValueError, TypeError):
            raise Conflict({"message": "The run's process ownership record is invalid. No files were removed."}) from None
        if any(worker.matching_process(record) is not None for record in records):
            raise Conflict({"message": "A process owned by this run is still active. Wait before restarting or deleting it."})


def restart_snapshots(store: Store, source: dict) -> tuple[bytes, bytes, dict, dict]:
    if source["kind"] != "experiment" or source["state"] != "failed":
        raise Conflict({"message": "Only failed experiments can be restarted."})
    store.job_for_request(source["request_id"])
    directory, _ = job_directories(store, source)
    ensure_worker_exited(source, directory)
    try:
        original_request = json.loads(_read_snapshot(directory / "request.json"))
        idea = json.loads(_read_snapshot(directory / "idea.json"))
        if original_request != source["request"] or idea != [source["request"].get("idea")]:
            raise ValueError()
        if not isinstance(idea[0], dict) or validate_idea(idea[0]):
            raise ValueError()
        bfts_bytes = _read_snapshot(directory / "bfts_config.yaml")
        candidate = yaml.safe_load(bfts_bytes)
        model_path = directory / "model_settings.json"
        if model_path.exists() or model_path.is_symlink():
            model_bytes = _read_snapshot(model_path)
            settings = json.loads(model_bytes)
        else:
            settings = yaml.safe_load(_read_snapshot(directory / "role_config.yaml"))
            model_bytes = json.dumps(settings, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if not isinstance(candidate, dict) or not isinstance(settings, dict):
            raise ValueError()
        return bfts_bytes, model_bytes, candidate, settings
    except (ValueError, TypeError, KeyError, UnicodeError, yaml.YAMLError):
        raise Conflict({"message": "The original run snapshots are invalid. Prepare a new experiment from the saved idea instead."}) from None


def delete_failed_job(store: Store, job_id: str) -> None:
    """Quarantine under a DB claim, then remove files without blocking worker writes.

    A failed cleanup leaves the failed job visible for retry. The durable request
    tombstone prevents restart or late launch retries during and after deletion.
    """
    job_id = str(UUID(job_id))
    deletions = _owned_directory(store.data_dir / "deletions", store.root)
    deletions.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(deletions / f"{job_id}.lock"), timeout=0):
            staging = _owned_directory(deletions / job_id, store.root)
            with store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                deleted = db.execute("SELECT * FROM deleted_jobs WHERE id=?", (job_id,)).fetchone()
                if row is None and deleted is None:
                    raise KeyError("Job not found")
                if row is not None:
                    job = store.job_record(row)
                    if job["kind"] != "experiment" or job["state"] != "failed":
                        raise Conflict({"message": "Only failed experiments can be deleted."})
                    directory, outputs = job_directories(store, job)
                    for source, target in ((directory, staging / "job"), (outputs, staging / "run")):
                        _owned_directory(target, store.root)
                        if source.exists() and target.exists():
                            raise Conflict({"message": "Run files changed during deletion. No additional files were removed."})
                    ensure_worker_exited(job, directory if directory.exists() else staging / "job")
                    diagnostic = db.execute("SELECT state,issue_state FROM job_diagnostics WHERE job_id=?", (job_id,)).fetchone()
                    if diagnostic and (diagnostic["state"] == "analyzing" or diagnostic["issue_state"] in ("publishing", "unknown")):
                        raise Conflict({"message": "Crash analysis or issue publication must finish before this run can be deleted."})
                    staging.mkdir(exist_ok=True)
                    moved = []
                    try:
                        for source, target in ((directory, staging / "job"), (outputs, staging / "run")):
                            if source.exists():
                                source.rename(target)
                                moved.append((source, target))
                        db.execute("INSERT OR IGNORE INTO deleted_jobs(id,request_id,kind) VALUES(?,?,?)",
                                   (job_id, job["request_id"], job["kind"]))
                        db.execute("DELETE FROM job_diagnostics WHERE job_id=?", (job_id,))
                    except Exception:
                        for source, target in reversed(moved):
                            target.rename(source)
                        raise
            if staging.exists():
                shutil.rmtree(staging)
            with store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM events WHERE job_id=?", (job_id,))
                db.execute("DELETE FROM job_diagnostics WHERE job_id=?", (job_id,))
                db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    except Timeout:
        raise Conflict({"message": "Deletion is already in progress for this run."}) from None
