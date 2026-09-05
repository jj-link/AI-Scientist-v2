"""Read-only manifests for saved research outputs; never deserialize executable data."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import Store

_JSON_LIMIT = 2 * 1024 * 1024
_RASTER = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
_SOURCE = {".py", ".tex", ".bib", ".bst", ".sty", ".md", ".yaml", ".yml"}
_SUMMARIES = {"draft_summary.json", "baseline_summary.json", "research_summary.json", "ablation_summary.json"}
_ROOT_JSON = {"idea.json", "citations_progress.json"}
_STAGE_LABELS = {"1": "Initial implementation", "2": "Baseline tuning",
                 "3": "Research experiments", "4": "Ablation studies"}


def _id(namespace: str, value: str) -> str:
    return namespace + "_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _contained(path: Path, parent: Path, *, directory: bool = False) -> Path:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(parent)
        if not (resolved.is_dir() if directory else resolved.is_file()):
            raise FileNotFoundError
        return resolved
    except (OSError, ValueError, RuntimeError):
        raise FileNotFoundError("Saved output is missing or outside the run directory.") from None


def _children(path: Path) -> list[Path]:
    try:
        return sorted(path.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return []


def _json(path: Path, run: Path, limit: int = _JSON_LIMIT) -> tuple[object, str | None]:
    try:
        safe = _contained(path, run)
        with safe.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            return None, "This JSON output is too large to preview; download it to inspect it."
        value = json.loads(raw.decode("utf-8-sig"), parse_constant=lambda _: None)
        if value is None:
            return None, "The saved JSON output is null."
        return value, None
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None, "This output is unavailable or is not valid JSON. It may still be being written; refresh to retry."


def _runs(root: Path, store: Store) -> list[tuple[dict, Path | None]]:
    root = Path(root).resolve()
    jobs = {job["run_id"]: job for job in store.jobs() if job.get("kind") == "experiment" and job.get("run_id")}
    try:
        experiments = _contained(root / "experiments", root, directory=True)
    except FileNotFoundError:
        experiments = None
    paths = {}
    if experiments is not None:
        for child in _children(experiments):
            try:
                _contained(child, experiments, directory=True)
                paths[child.name] = child
            except FileNotFoundError:
                continue
    records = []
    for name in sorted(paths.keys() | jobs.keys()):
        job = jobs.get(name)
        path = paths.get(name)
        idea = None
        if path is not None:
            idea, _ = _json(path / "idea.json", path.resolve(), limit=256 * 1024)
        if not isinstance(idea, dict):
            idea = {}
        title = idea.get("Title")
        hypothesis = idea.get("Short Hypothesis")
        updated_at = job.get("updated_at") if job else None
        if updated_at is None and path is not None:
            try:
                updated_at = _timestamp(path.stat().st_mtime)
            except OSError:
                pass
        records.append(({"id": name if job else _id("run", name), "directory": name,
            "job_id": job["id"] if job else None, "historical": job is None,
            "title": title if isinstance(title, str) and title.strip() else name,
            "hypothesis": hypothesis if isinstance(hypothesis, str) else "",
            "state": job["state"] if job else "unavailable",
            "status_label": job["state"] if job else "Status unavailable · Saved outputs",
            "phase": job.get("phase") if job else None,
            "created_at": job.get("created_at") if job else None, "updated_at": updated_at,
            "started_at": job.get("started_at") if job else None,
            "finished_at": job.get("finished_at") if job else None,
            "outputs_available": path is not None}, path))
    records.sort(key=lambda item: (item[0]["updated_at"] or "", item[0]["id"]), reverse=True)
    return records


def list_runs(root: Path, store: Store) -> list[dict]:
    """Immediate directories and small idea headers only: no recursive artifact scans."""
    return [record for record, _ in _runs(root, store)]


def _run(root: Path, store: Store, run_id: str) -> tuple[dict, Path | None]:
    for record, path in _runs(root, store):
        if record["id"] == run_id:
            return record, path
    raise FileNotFoundError("Unknown run ID.")


def _walk(path: Path, run: Path):
    """Contain every entry, including Windows junctions, and avoid directory cycles."""
    pending = [path]
    seen = set()
    while pending:
        directory = pending.pop()
        try:
            real = _contained(directory, run, directory=True)
        except FileNotFoundError:
            continue
        if real in seen:
            continue
        seen.add(real)
        for child in _children(directory):
            if child.name.startswith(".") or child.name in {"__pycache__", "node_modules"}:
                continue
            try:
                resolved = child.resolve(strict=True)
                resolved.relative_to(run)
                if resolved.is_dir():
                    pending.append(child)
                elif resolved.is_file():
                    yield child
            except (OSError, ValueError, RuntimeError):
                continue


def _kind(relative: Path) -> str | None:
    name, suffix = relative.name, relative.suffix.lower()
    # No pickle/numpy/checkpoints, captured logs, or raw request telemetry.
    if suffix in {".pkl", ".pickle", ".npy", ".npz", ".log", ".jsonl"}:
        return None
    if suffix == ".pdf":
        return "paper"
    if suffix in _RASTER:
        return "figure" if relative.parts[0] == "figures" else "image"
    if name in {"review_text.txt", "review_img_cap_ref.json"} and len(relative.parts) == 1:
        return "review"
    if name in _SUMMARIES:
        return "summary"
    if name == "stage_progress.json":
        return "progress"
    if name in {"journal.json", "tree_data.json"}:
        return "journal" if name == "journal.json" else "tree"
    if suffix in {".html", ".htm"}:
        return "tree_download"
    if suffix in _SOURCE or name in _ROOT_JSON or name == "best_node_id.txt":
        return "source"
    return None


def _manifest(run: Path) -> list[tuple[dict, Path]]:
    run_real = run.resolve()
    candidates = []
    for child in _children(run):
        try:
            resolved = child.resolve(strict=True)
            resolved.relative_to(run_real)
            if resolved.is_file():
                candidates.append(child)
            elif resolved.is_dir() and (child.name in {"figures", "latex", "logs", "experiment_results"}
                or re.fullmatch(r"\d+-run", child.name) or child.name.endswith("_imgs")
                or child.name == "writeup_attempts"):
                candidates.extend(_walk(child, run_real))
        except (OSError, ValueError, RuntimeError):
            continue
    entries = []
    for path in candidates:
        relative = path.relative_to(run)
        kind = _kind(relative)
        if kind is None:
            continue
        try:
            resolved = _contained(path, run_real)
            stat = resolved.stat()
        except OSError:
            continue
        suffix = path.suffix.lower()
        mime = "application/pdf" if suffix == ".pdf" else _RASTER.get(suffix, "application/octet-stream")
        attachment = suffix != ".pdf" and suffix not in _RASTER
        relative_id = relative.as_posix()
        entries.append(({"id": _id("artifact", relative_id), "name": path.name,
            "relative_path": relative_id, "kind": kind, "mime": mime, "attachment": attachment,
            "size": stat.st_size, "updated_at": _timestamp(stat.st_mtime)}, path))
    entries.sort(key=lambda item: item[0]["relative_path"].casefold())
    return entries


def _pdf_key(artifact: dict) -> tuple:
    name = artifact["name"].lower()
    reflection = re.search(r"_reflection(\d+)(?:\D|$)", name)
    if name.endswith("_final.pdf"):
        priority, number = 0, 0
    elif name.endswith("_reflection_final_page_limit.pdf"):
        priority, number = 1, 0
    elif reflection:
        priority, number = 2, -int(reflection.group(1))
    else:
        priority, number = 3, 0
    return priority, number, artifact["relative_path"].casefold()


def run_detail(root: Path, store: Store, run_id: str) -> dict:
    record, run = _run(root, store, run_id)
    result = {**record, "papers": [], "default_paper_id": None, "figures": [], "reviews": [],
              "log_directories": [], "summaries": [], "stages": [], "artifacts": [],
              "idea": None, "idea_error": None, "missing_outputs": []}
    if run is None:
        result["missing_outputs"] = ["Paper", "Figures", "Reviews", "Experiment details"]
        return result
    run_real = run.resolve()
    manifest = _manifest(run)
    result["artifacts"] = [artifact for artifact, _ in manifest]
    by_relative = {artifact["relative_path"]: (artifact, path) for artifact, path in manifest}
    idea, error = _json(run / "idea.json", run_real, limit=256 * 1024)
    result["idea"] = idea if isinstance(idea, dict) else None
    result["idea_error"] = error or (None if isinstance(idea, dict) else "Saved idea is not a JSON object.")
    result["papers"] = sorted([artifact for artifact, _ in manifest if artifact["kind"] == "paper"], key=_pdf_key)
    result["figures"] = [artifact for artifact, _ in manifest if artifact["kind"] == "figure"]
    if result["papers"]:
        result["default_paper_id"] = result["papers"][0]["id"]
    if record["job_id"]:
        job = store.get_job(record["job_id"])
        pipeline = job.get("result") if job else None
        recorded_pdf = pipeline.get("pdf_path") if isinstance(pipeline, dict) else None
        if isinstance(recorded_pdf, str):
            supplied = Path(recorded_pdf)
            options = [supplied] if supplied.is_absolute() else [run / supplied, Path(root) / supplied]
            for option in options:
                try:
                    exact = _contained(option, run_real)
                    selected = next((artifact["id"] for artifact, path in manifest
                        if artifact["kind"] == "paper" and path.resolve() == exact), None)
                    if selected:
                        result["default_paper_id"] = selected
                        break
                except FileNotFoundError:
                    continue
    for filename, label in (("review_text.txt", "Paper review"), ("review_img_cap_ref.json", "Figure reviews")):
        pair = by_relative.get(filename)
        review = {"kind": "paper" if filename == "review_text.txt" else "figures",
                  "label": label, "attribution": "AI-generated review", "artifact_id": None,
                  "data": None, "error": "No saved review is available."}
        if pair:
            artifact, path = pair
            data, error = _json(path, run_real)
            if not error and not isinstance(data, dict):
                error = "Saved review must be a JSON object. Download the original to inspect it."
            review.update(artifact_id=artifact["id"], data=data if not error else None, error=error)
        result["reviews"].append(review)
    log_dirs = {}
    stages = {}
    for artifact, path in manifest:
        relative = Path(artifact["relative_path"])
        parts = relative.parts
        if len(parts) >= 3 and parts[0] == "logs":
            log_name = "/".join(parts[:2])
        elif len(parts) >= 2 and re.fullmatch(r"\d+-run", parts[0]):
            log_name = parts[0]
        else:
            log_name = None
        if log_name:
            log_dirs.setdefault(log_name, {"path": log_name, "artifact_ids": []})["artifact_ids"].append(artifact["id"])
        if artifact["kind"] == "summary":
            data, error = _json(path, run_real)
            result["summaries"].append({"name": artifact["name"], "log_directory": log_name,
                "artifact_id": artifact["id"], "data": data, "error": error})
        stage_index = next((index for index, part in enumerate(parts[:-1])
                            if re.match(r"stage_\d+_", part)), None)
        if stage_index is None:
            continue
        stage_name = parts[stage_index]
        stage_directory = "/".join(parts[:stage_index + 1])
        match = re.match(r"stage_(\d+)_(.+)", stage_name)
        stage = stages.setdefault(stage_directory, {
            "directory": stage_directory, "log_directory": log_name,
            "label": _STAGE_LABELS.get(match.group(1), stage_name),
            "substage": stage_name, "artifact_id": None, "updated_at": artifact["updated_at"],
            "progress": {}, "artifact_ids": [], "error": None})
        stage["artifact_ids"].append(artifact["id"])
        # Journals and tree JSON stay on demand; only small saved progress is parsed here.
        if artifact["kind"] == "progress":
            data, error = _json(path, run_real, limit=256 * 1024)
            if not isinstance(data, dict):
                data = {}
                error = error or "Saved stage progress must be a JSON object."
            stage.update(
                substage=data.get("stage") if isinstance(data.get("stage"), str) else stage_name,
                artifact_id=artifact["id"], updated_at=artifact["updated_at"],
                progress={key: data[key] for key in ("total_nodes", "good_nodes", "buggy_nodes",
                    "best_metric", "current_findings") if key in data}, error=error)
    result["log_directories"] = list(log_dirs.values())
    result["stages"] = list(stages.values())
    if not result["papers"]:
        result["missing_outputs"].append("Paper")
    if not result["figures"]:
        result["missing_outputs"].append("Figures")
    if not any(review["artifact_id"] for review in result["reviews"]):
        result["missing_outputs"].append("Reviews")
    if not result["log_directories"]:
        result["missing_outputs"].append("Experiment details")
    return result


def artifact_file(root: Path, store: Store, run_id: str, artifact_id: str) -> tuple[Path, str, bool]:
    """Resolve a server-generated ID, never interpret request input as a file path."""
    _, run = _run(root, store, run_id)
    if run is not None:
        for artifact, path in _manifest(run):
            if artifact["id"] == artifact_id:
                return _contained(path, run.resolve()), artifact["mime"], artifact["attachment"]
    raise FileNotFoundError("Unknown or unavailable artifact ID.")
