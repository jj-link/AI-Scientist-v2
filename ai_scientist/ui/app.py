"""Loopback-only HTTP boundary for AI-Scientist Studio."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
import secrets
import threading
from uuid import uuid4

import psutil
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware

from . import diagnostics
from .artifacts import artifact_file, list_runs, run_detail
from .configs import Configs, assistant_settings_view
from .diagnostics import CrashAssistant, PublishUnknown, PublishUnavailable, REPOSITORY, sanitize_text
from .schemas import AssistantSettingsUpdate, ExperimentRequest, IdeaJobRequest, IdeaUpdate, IssueDraftUpdate, IssuePublish, ModelCheck
from .store import Conflict, Store
from .worker import Supervisor, identity, log_download, log_preview, now, write_json


HOSTS = frozenset({"127.0.0.1:8765", "localhost:8765"})
ORIGINS = frozenset({"http://127.0.0.1:8765", "http://localhost:8765"})
DEV_ORIGIN = "http://127.0.0.1:5173"
MAX_BODY = 512 * 1024
CSP = "; ".join((
    "default-src 'self'", "script-src 'self'", "style-src 'self'", "img-src 'self' data:",
    "font-src 'self'", "connect-src 'self'", "object-src 'none'", "frame-src 'self'",
    "base-uri 'none'", "frame-ancestors 'none'", "form-action 'self'",
))


def public_job(job: dict | None) -> dict | None:
    if job is None:
        return None
    keys = ("id", "kind", "state", "phase", "created_at", "updated_at", "started_at", "finished_at", "run_id", "error", "result")
    result = {key: job.get(key) for key in keys}
    request = job.get("request") or {}
    idea = request.get("idea") or {}
    result["title"] = idea.get("Title") if job["kind"] == "experiment" else "Proposal generation"
    result["idea_id"] = request.get("idea_id")
    return result


class LocalBoundary(BaseHTTPMiddleware):
    def __init__(self, app, *, token: str, development: bool):
        super().__init__(app)
        self.token = token
        self.origins = ORIGINS | ({DEV_ORIGIN} if development else set())

    async def dispatch(self, request: Request, call_next):
        response = None
        if request.headers.get("host", "").lower() not in HOSTS:
            response = JSONResponse({"detail": {"message": "Unrecognized local Host header"}}, status_code=400)
        elif request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") not in self.origins:
                response = JSONResponse({"detail": {"message": "A trusted local Origin is required"}}, status_code=403)
            elif not secrets.compare_digest(request.headers.get("x-studio-token", "").encode("utf-8"), self.token.encode("ascii")):
                response = JSONResponse({"detail": {"message": "Request token is missing or expired. Reload the application."}}, status_code=403)
            elif request.url.path.startswith("/api/") and request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
                response = JSONResponse({"detail": {"message": "Use an application/json request body"}}, status_code=415)
            else:
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > MAX_BODY:
                        response = JSONResponse({"detail": {"message": "Request body exceeds 512 KiB"}}, status_code=413)
                        break
                if response is None:
                    # BaseHTTPMiddleware replays its cached body to the downstream app.
                    request._body = bytes(body)
        if response is None:
            try:
                response = await call_next(request)
            except Exception:
                logging.exception("Unhandled Studio HTTP error")
                response = JSONResponse({"detail": {"message": "The local server could not complete this request."}}, status_code=500)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if response.headers.get("content-type", "").split(";", 1)[0] == "application/pdf":
            response.headers["Content-Security-Policy"] = CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'")
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response


def create_app(root: Path | None = None, *, development: bool = False) -> FastAPI:
    root = (root or Path(__file__).resolve().parents[2]).resolve()
    store = Store(root)
    configs = Configs(root)
    supervisor = Supervisor(store)
    request_token = secrets.token_urlsafe(32)
    launch_lock = threading.RLock()
    assets = (root / "frontend" / "dist").resolve()
    assistant = CrashAssistant(store, configs)

    @asynccontextmanager
    async def lifespan(app):
        supervisor.start()
        await assistant.start()
        try:
            yield
        finally:
            await assistant.close()
            supervisor.close()

    app = FastAPI(title="AI-Scientist Studio", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.configs = configs
    app.state.supervisor = supervisor
    app.state.crash_assistant = assistant
    app.add_middleware(LocalBoundary, token=request_token, development=development)


    @app.exception_handler(Conflict)
    async def conflict_error(request, exc):
        detail = dict(exc.detail)
        if detail.get("job_id"):
            detail["job_url"] = f"/api/jobs/{detail['job_id']}"
        return JSONResponse({"detail": detail}, status_code=409)

    @app.exception_handler(KeyError)
    async def missing_error(request, exc):
        return JSONResponse({"detail": {"message": "The requested record or configuration was not found."}}, status_code=404)

    @app.exception_handler(FileNotFoundError)
    async def missing_file(request, exc):
        return JSONResponse({"detail": {"message": "The requested output is not available."}}, status_code=404)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        # Configuration helpers promise safe errors, but never echo arbitrary exception content.
        logging.warning("Invalid Studio request: %s", exc)
        return JSONResponse({"detail": {"message": "The selected configuration or request is invalid. Check the Models page and local configuration."}}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        errors = [{"field": ".".join(str(part) for part in error["loc"] if part != "body"), "message": error["msg"], "code": error["type"]} for error in exc.errors()]
        return JSONResponse({"detail": {"message": "Check the highlighted request fields.", "errors": errors}}, status_code=422)

    @app.get("/api/bootstrap")
    def bootstrap():
        return {**configs.presets(), "prerequisites": configs.prerequisites(), "active_job": public_job(store.active_job()), "request_token": request_token, "experiments_directory": str(root / "experiments")}

    @app.get("/api/ideas")
    def ideas():
        return {"ideas": store.ideas()}

    @app.get("/api/ideas/{idea_id}")
    def idea(idea_id: str):
        return store.get_idea(idea_id)

    @app.patch("/api/ideas/{idea_id}")
    def update_idea(idea_id: str, body: IdeaUpdate):
        return store.save_idea(idea_id, body.expected_revision, body.idea)

    @app.get("/api/ideas/{idea_id}/export")
    def export_idea(idea_id: str):
        record = store.get_idea(idea_id)
        if record["errors"].get("Name"):
            raise HTTPException(422, detail={"message": "Save a valid internal Name before exporting.", "errors": {"Name": record["errors"]["Name"]}})
        return PlainTextResponse(json.dumps([record["idea"]], ensure_ascii=False, indent=2), media_type="application/json", headers={"Content-Disposition": 'attachment; filename="proposal.json"'})

    def existing_request(request_id: str, kind: str) -> dict | None:
        for job in store.jobs():
            if job.get("request_id") == request_id:
                if job["kind"] != kind:
                    raise Conflict({"message": "Request ID already used for another job", "job_id": job["id"]})
                return job
        return None

    def accepted(job: dict) -> dict:
        result = {"job_id": job["id"]}
        if job.get("run_id"):
            result["run_id"] = job["run_id"]
        return result

    def launch(kind: str, payload: dict) -> dict:
        with launch_lock:
            prior = existing_request(payload["request_id"], kind)
            if prior:
                return accepted(prior)
            # Local validation and endpoint availability happen before reserving compute.
            role_path = configs.role_path(payload["role_config_id"])
            bfts_path = None
            if kind == "experiment":
                if not payload["execution_acknowledged"]:
                    raise HTTPException(422, detail={"message": "Acknowledge that generated Python code runs with your account permissions.", "errors": {"execution_acknowledged": "Acknowledgement is required"}})
                record = store.get_idea(payload["idea_id"])
                if record["revision"] != payload["idea_revision"]:
                    raise Conflict({"message": "Proposal changed; reload the saved revision", "revision": record["revision"]})
                if record["errors"]:
                    raise HTTPException(422, detail={"message": "Save a valid proposal before starting an experiment.", "errors": record["errors"]})
                bfts_path = configs.bfts_path(payload["bfts_config_id"])
                blockers = configs.validate_experiment(payload["role_config_id"], payload["bfts_config_id"])
                if blockers:
                    raise HTTPException(422, detail={"message": "Experiment prerequisites are not satisfied.", "blockers": blockers})
            elif not payload["research_question"].strip():
                raise HTTPException(422, detail={"message": "Enter a research question.", "errors": {"research_question": "Enter nonempty text"}})
            role_bytes = role_path.read_bytes()
            bfts_bytes = bfts_path.read_bytes() if bfts_path else None
            job = store.create_job(kind, payload["request_id"], payload, idea_id=payload.get("idea_id"), idea_revision=payload.get("idea_revision"))
            directory = store.job_dir(job["id"])
            try:
                try:
                    directory.mkdir(parents=True, exist_ok=False)
                except FileExistsError:
                    # A second application instance may have claimed this idempotent request.
                    return accepted(store.get_job(job["id"]))
                write_json(directory / "request.json", job["request"], exclusive=True)
                with (directory / "role_config.yaml").open("xb") as handle:
                    handle.write(role_bytes)
                if kind == "experiment":
                    with (directory / "bfts_config.yaml").open("xb") as handle:
                        handle.write(bfts_bytes)
                    write_json(directory / "idea.json", [job["request"]["idea"]], exclusive=True)
                    parent = (root / "experiments").resolve()
                    if parent != root / "experiments":
                        raise ValueError("Experiments directory must not redirect outside the repository")
                    parent.mkdir(exist_ok=True)
                    (parent / job["run_id"]).mkdir(exist_ok=False)
                store.add_event(job["id"], "reserved", phase="preparing", data={"message": "Job reserved; starting worker"})
                supervisor.launch(job)
            except Exception:
                logging.exception("Unable to prepare or launch Studio worker %s", job["id"])
                store.update_job(job["id"], state="failed", finished_at=now(), error={"code": "worker_start_failed", "message": "The worker could not be started. Check local filesystem permissions and Python dependencies."})
                store.add_event(job["id"], "failed", data={"code": "worker_start_failed", "message": "Worker startup failed"})
                raise HTTPException(503, detail={"message": "The worker could not be started. Saved request files were retained.", "job_id": job["id"]}) from None
            return accepted(job)

    @app.post("/api/idea-jobs", status_code=202)
    def create_idea_job(body: IdeaJobRequest):
        return launch("idea", body.model_dump(mode="json"))

    @app.post("/api/experiments", status_code=202)
    def create_experiment(body: ExperimentRequest):
        return launch("experiment", body.model_dump(mode="json"))

    @app.get("/api/jobs")
    def jobs():
        return {"jobs": [public_job(job) for job in store.jobs()]}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return public_job(store.get_job(job_id))

    @app.get("/api/jobs/{job_id}/events")
    def events(job_id: str, after: int = Query(default=0, ge=0)):
        store.get_job(job_id)
        rows = store.events(job_id, after)
        return {"events": rows, "last_sequence": rows[-1]["sequence"] if rows else after}

    @app.get("/api/jobs/{job_id}/log")
    def technical_log(job_id: str, download: bool = False):
        store.get_job(job_id)
        directory = store.job_dir(job_id)
        if download:
            if not (directory / "technical.log").is_file():
                raise HTTPException(404, detail={"message": "No technical log is available yet."})
            return StreamingResponse(log_download(directory), media_type="text/plain", headers={"Content-Disposition": 'attachment; filename="technical.log"'})
        return PlainTextResponse(log_preview(directory))

    @app.post("/api/jobs/{job_id}/stop")
    def stop(job_id: str):
        with launch_lock:
            store.get_job(job_id)
            return public_job(supervisor.request_stop(job_id))

    def public_diagnostic(diagnostic: dict | None) -> dict | None:
        if diagnostic is None:
            return None
        draft = None
        if diagnostic.get("draft_title") is not None and diagnostic.get("draft_body") is not None:
            draft = {"revision": diagnostic["draft_revision"], "title": diagnostic["draft_title"],
                     "body": diagnostic["draft_body"], "repository": REPOSITORY}
        return {
            "job_id": diagnostic["job_id"], "state": diagnostic["state"],
            "result": diagnostic.get("result"), "error": diagnostic.get("error"),
            "dismissed": diagnostic["dismissed"], "draft": draft,
            "issue": {"state": diagnostic["issue_state"], "url": diagnostic["issue_url"]},
        }

    def editable_draft(diagnostic: dict) -> dict:
        if (diagnostic is None or diagnostic["state"] != "ready"
                or diagnostic.get("draft_title") is None or diagnostic.get("draft_body") is None):
            raise HTTPException(409, detail={"message": "This diagnosis has no editable issue draft."})
        return diagnostic

    @app.get("/api/crash-assistant/settings")
    def read_assistant_settings():
        return assistant_settings_view(store.assistant_settings())

    @app.put("/api/crash-assistant/settings")
    async def save_assistant_settings(body: AssistantSettingsUpdate):
        if body.enabled:
            try:
                snapshot = dict(configs.diagnostic_assignment(body.config_id, body.role))
            except KeyError:
                raise HTTPException(404, detail={"message": "The selected configuration or role was not found."}) from None
            saved = store.save_assistant_settings(True, snapshot)
        else:
            saved = store.save_assistant_settings(False)
            await assistant.cancel_active()
        return assistant_settings_view(saved)

    @app.get("/api/jobs/{job_id}/diagnostic")
    def read_diagnostic(job_id: str):
        store.get_job(job_id)
        return {"diagnostic": public_diagnostic(store.get_diagnostic(job_id))}

    @app.post("/api/jobs/{job_id}/diagnostic/dismiss")
    def dismiss_diagnostic(job_id: str):
        store.get_job(job_id)
        if store.get_diagnostic(job_id) is None:
            raise KeyError("Diagnostic not found")
        return {"diagnostic": public_diagnostic(store.dismiss_diagnostic(job_id))}

    @app.post("/api/jobs/{job_id}/diagnostic/draft")
    def save_issue_draft(job_id: str, body: IssueDraftUpdate):
        store.get_job(job_id)
        diagnostic = editable_draft(store.get_diagnostic(job_id))
        secrets = diagnostics.result_secrets(store, job_id, diagnostic.get("assignment") or {})
        scrubbed = lambda value: sanitize_text(value, secrets, str(store.root), str(Path.home())).strip()
        title, issue_body = scrubbed(body.title), sanitize_text(body.body, secrets, str(store.root), str(Path.home()))
        if not title or not issue_body.strip() or len(title) > 256 or len(issue_body) > 12000:
            raise HTTPException(422, detail={"message": "The reviewed draft is empty or oversized after credential redaction."})
        saved = store.save_issue_draft(job_id, body.expected_revision, title, issue_body)
        return {"diagnostic": public_diagnostic(saved)}

    @app.post("/api/jobs/{job_id}/diagnostic/issue")
    async def publish_issue(job_id: str, body: IssuePublish):
        store.get_job(job_id)
        diagnostic = editable_draft(store.get_diagnostic(job_id))
        if diagnostic["issue_state"] == "published":
            # Idempotent: the exact reviewed revision is already public.
            return {"diagnostic": public_diagnostic(diagnostic)}
        if diagnostic["issue_state"] != "not_published" or diagnostic["draft_revision"] != body.revision:
            raise Conflict({"message": "The issue draft changed or is no longer publishable; review the current revision.",
                            "revision": diagnostic["draft_revision"]})
        secrets = diagnostics.result_secrets(store, job_id, diagnostic.get("assignment") or {})
        scrubbed = lambda value: sanitize_text(value, secrets, str(store.root), str(Path.home())).strip()
        title, issue_body = scrubbed(diagnostic["draft_title"]), sanitize_text(diagnostic["draft_body"], secrets, str(store.root), str(Path.home()))
        if (title, issue_body) != (diagnostic["draft_title"].strip(), diagnostic["draft_body"]):
            saved = store.save_issue_draft(job_id, body.revision, title, issue_body)
            raise Conflict({"message": "A newly recognized credential changed this draft. Review the updated revision.",
                            "revision": saved["draft_revision"]})
        try:
            await diagnostics.preflight_gh()
        except diagnostics.PublishUnavailable as exc:
            raise HTTPException(503, detail={"message": str(exc)}) from None
        owner = {**identity(psutil.Process()), "token": str(uuid4())}
        claimed, proceed = store.claim_issue(job_id, body.revision, owner)
        if not proceed:
            return {"diagnostic": public_diagnostic(claimed)}
        token = owner["token"]
        try:
            url = await diagnostics.create_issue(claimed["draft_title"], claimed["draft_body"])
        except asyncio.CancelledError:
            await asyncio.to_thread(store.finish_issue, job_id, token, "unknown", error=dict(zip(("code", "message"), diagnostics.ISSUE_UNKNOWN)))
            raise
        except PublishUnknown:
            await asyncio.to_thread(store.finish_issue, job_id, token, "unknown", error=dict(zip(("code", "message"), diagnostics.ISSUE_UNKNOWN)))
        else:
            await asyncio.to_thread(store.finish_issue, job_id, token, "published", url=url)
        return {"diagnostic": public_diagnostic(store.get_diagnostic(job_id))}

    @app.get("/api/runs")
    def runs():
        return {"runs": list_runs(root, store)}

    @app.get("/api/runs/{run_id}")
    def run(run_id: str):
        return run_detail(root, store, run_id)

    @app.get("/api/runs/{run_id}/artifacts/{artifact_id}")
    def artifact(run_id: str, artifact_id: str, download: bool = False):
        path, mime, attachment = artifact_file(root, store, run_id, artifact_id)
        return FileResponse(path, media_type=mime, filename=path.name, content_disposition_type="attachment" if attachment or download else "inline")

    @app.get("/api/models")
    def models(config_id: str):
        return configs.models(config_id)

    @app.post("/api/models/check")
    def model_check(body: ModelCheck):
        return configs.check(body.config_id)

    @app.get("/{path:path}")
    def frontend(path: str, request: Request):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, detail={"message": "API endpoint not found"})
        if not (assets / "index.html").is_file():
            return PlainTextResponse("Frontend assets are missing. Run npm ci and npm run build in frontend, then restart AI-Scientist Studio.", status_code=503)
        candidate = (assets / path).resolve()
        if not candidate.is_relative_to(assets):
            raise HTTPException(404, detail={"message": "Asset not found"})
        if path and candidate.is_file():
            return FileResponse(candidate)
        if Path(path).suffix or (path and "text/html" not in request.headers.get("accept", "")):
            raise HTTPException(404, detail={"message": "Asset not found"})
        return FileResponse(assets / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})

    return app
