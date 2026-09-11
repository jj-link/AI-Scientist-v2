"""Short SQLite transactions shared by the HTTP supervisor and research workers."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4
from .schemas import normalize_idea, validate_idea

ACTIVE = ("starting", "running", "stopping")
TERMINAL = ("stopped", "completed", "partial", "failed", "interrupted")


def now():
    return datetime.now(timezone.utc).isoformat()


class Conflict(RuntimeError):
    def __init__(self, detail):
        self.detail = detail
        super().__init__(detail.get("message", "Conflict"))


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.data_dir = self.root / "ui_data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "ui.sqlite3"
        from . import model_settings
        # Creates the model_servers/task_models tables and imports a legacy
        # ais_roles.yaml exactly once (the file is renamed after import).
        self._migrate_legacy_role_config = model_settings.maybe_import_legacy(self.root)
        self._migrate_legacy_role_config()
        model_settings.ensure_schema(self.root)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL, state TEXT NOT NULL, phase TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    started_at TEXT, finished_at TEXT, pid INTEGER,
                    process_created REAL, run_id TEXT, request TEXT NOT NULL,
                    error TEXT, result TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_compute_slot ON jobs((1))
                    WHERE state IN ('starting','running','stopping');
                CREATE TABLE IF NOT EXISTS deleted_jobs (
                    id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ideas (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    idea TEXT NOT NULL, original TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idea_conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, revision INTEGER NOT NULL,
                    role_config_id TEXT NOT NULL, state TEXT NOT NULL,
                    messages TEXT NOT NULL, pending_idea TEXT, candidate_revision INTEGER,
                    idea_id TEXT, idea_revision INTEGER, base_idea TEXT,
                    error TEXT, progress TEXT, owner TEXT, active_request TEXT,
                    approval_revision INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idea_conversation_requests (
                    request_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idea_conversation_sources (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    evidence TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                    type TEXT NOT NULL, phase TEXT, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_by_job ON events(job_id, sequence);
                CREATE TABLE IF NOT EXISTS crash_assistant_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    enabled INTEGER NOT NULL,
                    assignment TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_diagnostics (
                    job_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    assignment TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    owner TEXT,
                    dismissed INTEGER NOT NULL DEFAULT 0,
                    draft_revision INTEGER NOT NULL DEFAULT 0,
                    draft_title TEXT,
                    draft_body TEXT,
                    issue_state TEXT NOT NULL DEFAULT 'not_published',
                    issue_url TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS diagnostics_by_state
                    ON job_diagnostics(state, created_at);
            """)

    def current_settings(self) -> dict:
        """Current model settings in the routing layer's internal shape."""
        from . import model_settings
        return model_settings.current_settings(self.root)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def job_record(row):
        if row is None:
            raise KeyError("Job not found")
        result = dict(row)
        for key in ("request", "error", "result"):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        return result

    @staticmethod
    def idea_record(row):
        if row is None:
            raise KeyError("Proposal not found")
        result = dict(row)
        for key in ("idea", "original"):
            result[key] = json.loads(result[key])
        result["errors"] = validate_idea(result["idea"])
        return result

    def job_dir(self, id):
        return self.data_dir / "jobs" / str(UUID(str(id)))

    def job_for_request(self, request_id):
        request_id = str(UUID(str(request_id)))
        with self.connection() as db:
            if db.execute("SELECT 1 FROM deleted_jobs WHERE request_id=?", (request_id,)).fetchone():
                raise Conflict({"message": "This run was deleted. Use a new request to start another run."})
            row = db.execute("SELECT * FROM jobs WHERE request_id=?", (request_id,)).fetchone()
            return self.job_record(row) if row else None

    def create_job(self, kind, request_id, request, idea_id=None, idea_revision=None, *, restart_from=None):
        request_id = str(UUID(str(request_id)))
        request = dict(request)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM deleted_jobs WHERE request_id=?", (request_id,)).fetchone():
                raise Conflict({"message": "This run was deleted. Use a new request to start another run."})
            prior = db.execute("SELECT * FROM jobs WHERE request_id=?", (request_id,)).fetchone()
            if prior:
                if prior["kind"] != kind or json.loads(prior["request"]).get("restart_of") != restart_from:
                    raise Conflict({"message": "Request ID already used for another job", "job_id": prior["id"]})
                return self.job_record(prior)
            active = db.execute("SELECT id FROM jobs WHERE state IN ('starting','running','stopping')").fetchone()
            if active:
                raise Conflict({"message": "Another job is active", "job_id": active["id"]})
            if restart_from is not None:
                if db.execute("SELECT 1 FROM deleted_jobs WHERE id=?", (restart_from,)).fetchone():
                    raise Conflict({"message": "This run is being deleted and cannot be restarted."})
                source = self.job_record(db.execute("SELECT * FROM jobs WHERE id=?", (restart_from,)).fetchone())
                if kind != "experiment" or source["kind"] != "experiment" or source["state"] != "failed":
                    raise Conflict({"message": "Only failed experiments can be restarted."})
                request = {**source["request"], "request_id": request_id,
                           "execution_acknowledged": True, "restart_of": restart_from}
            elif idea_id is not None:
                idea = self.idea_record(db.execute("SELECT * FROM ideas WHERE id=?", (idea_id,)).fetchone())
                if idea["revision"] != idea_revision:
                    raise Conflict({"message": "Proposal changed; reload the saved revision", "revision": idea["revision"]})
                if idea["errors"]:
                    raise Conflict({"message": "Proposal is not ready for execution", "errors": idea["errors"]})
                request["idea"] = idea["idea"]
            id = request_id
            stamp = now()
            run_id = "ui_" + id if kind == "experiment" else None
            db.execute("INSERT INTO jobs(id,request_id,kind,state,phase,created_at,updated_at,run_id,request) VALUES(?,?,?,'starting','preparing',?,?,?,?)",
                       (id, request_id, kind, stamp, stamp, run_id, json.dumps(request)))
            setting = db.execute(
                "SELECT assignment FROM crash_assistant_settings WHERE id=1 AND enabled=1"
            ).fetchone()
            if setting and setting["assignment"]:
                db.execute(
                    "INSERT INTO job_diagnostics(job_id,state,assignment,created_at,updated_at) "
                    "VALUES(?,'watching',?,?,?)",
                    (id, setting["assignment"], stamp, stamp),
                )
            return self.job_record(db.execute("SELECT * FROM jobs WHERE id=?", (id,)).fetchone())

    def get_job(self, id):
        with self.connection() as db:
            return self.job_record(db.execute("SELECT * FROM jobs WHERE id=?", (id,)).fetchone())

    def jobs(self):
        with self.connection() as db:
            return [self.job_record(r) for r in db.execute("SELECT * FROM jobs ORDER BY created_at DESC")]

    def active_job(self):
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE state IN ('starting','running','stopping')").fetchone()
            return self.job_record(row) if row else None

    def update_job(self, id, **fields):
        allowed = {"state", "phase", "started_at", "finished_at", "pid", "process_created", "run_id", "error", "result"}
        if not fields.keys() <= allowed:
            raise ValueError("Unknown job fields")
        if "state" in fields and fields["state"] not in ACTIVE + TERMINAL:
            raise ValueError("Unknown job state")
        for key in ("error", "result"):
            if key in fields:
                fields[key] = json.dumps(fields[key]) if fields[key] is not None else None
        fields["updated_at"] = now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT state FROM jobs WHERE id=?", (id,)).fetchone()
            if current is None:
                raise KeyError("Job not found")
            # A late callback must never resurrect a terminal job or undo Stop.
            if current["state"] in TERMINAL:
                return self.get_job(id)
            if current["state"] == "stopping" and fields.get("state") in ("starting", "running", "completed", "partial"):
                fields["state"] = "stopping"
            db.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?", (*fields.values(), id))
        return self.get_job(id)

    @staticmethod
    def diagnostic_record(row):
        if row is None:
            return None
        result = dict(row)
        for key in ("assignment", "result", "error", "owner"):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        result["dismissed"] = bool(result["dismissed"])
        return result

    def assistant_settings(self):
        with self.connection() as db:
            row = db.execute(
                "SELECT enabled,assignment,updated_at FROM crash_assistant_settings WHERE id=1"
            ).fetchone()
        if row is None:
            return {"enabled": False, "assignment": None, "updated_at": None}
        return {
            "enabled": bool(row["enabled"]),
            "assignment": json.loads(row["assignment"]) if row["assignment"] else None,
            "updated_at": row["updated_at"],
        }

    def save_assistant_settings(self, enabled: bool, assignment: dict | None = None):
        stamp = now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT assignment FROM crash_assistant_settings WHERE id=1"
            ).fetchone()
            serialized = (
                json.dumps(dict(assignment), separators=(",", ":"))
                if assignment is not None
                else (current["assignment"] if current else None)
            )
            if enabled and serialized is None:
                raise ValueError("Enabling crash analysis requires a model assignment.")
            db.execute(
                "INSERT INTO crash_assistant_settings(id,enabled,assignment,updated_at) VALUES(1,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled,"
                "assignment=excluded.assignment,updated_at=excluded.updated_at",
                (int(enabled), serialized, stamp),
            )
            if not enabled:
                db.execute(
                    "UPDATE job_diagnostics SET state='skipped',owner=NULL,updated_at=? "
                    "WHERE state IN ('watching','pending','analyzing')",
                    (stamp,),
                )
        return self.assistant_settings()

    def get_diagnostic(self, job_id):
        with self.connection() as db:
            return self.diagnostic_record(
                db.execute("SELECT * FROM job_diagnostics WHERE job_id=?", (job_id,)).fetchone()
            )

    def scan_diagnostics(self):
        stamp = now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT d.job_id,j.state,j.error,j.result FROM job_diagnostics d "
                "JOIN jobs j ON j.id=d.job_id WHERE d.state='watching'"
            ).fetchall()
            for row in rows:
                error = json.loads(row["error"]) if row["error"] else None
                result = json.loads(row["result"]) if row["result"] else None
                failed_attempts = result.get("failed_attempts") if isinstance(result, dict) else None
                failed_attempts = failed_attempts if type(failed_attempts) is int and failed_attempts > 0 else 0
                if row["state"] in ("failed", "interrupted") or (
                    row["state"] == "partial" and (error is not None or failed_attempts)
                ):
                    next_state = "pending"
                elif row["state"] in ("completed", "stopped", "partial"):
                    next_state = "skipped"
                else:
                    continue
                db.execute(
                    "UPDATE job_diagnostics SET state=?,updated_at=? "
                    "WHERE job_id=? AND state='watching'",
                    (next_state, stamp, row["job_id"]),
                )

    def claim_diagnostic(self, owner: dict):
        stamp = now()
        serialized = json.dumps(owner, separators=(",", ":"))
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT job_id FROM job_diagnostics WHERE state='pending' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            changed = db.execute(
                "UPDATE job_diagnostics SET state='analyzing',owner=?,updated_at=? "
                "WHERE job_id=? AND state='pending'",
                (serialized, stamp, row["job_id"]),
            ).rowcount
            if not changed:
                return None
            return self.diagnostic_record(
                db.execute("SELECT * FROM job_diagnostics WHERE job_id=?", (row["job_id"],)).fetchone()
            )

    def finish_diagnostic(self, job_id, token: str, *, result=None, error=None):
        stamp = now()
        state = "ready" if result is not None else "unavailable"
        draft = result.get("issue") if isinstance(result, dict) else None
        with self.connection() as db:
            changed = db.execute(
                "UPDATE job_diagnostics SET state=?,result=?,error=?,owner=NULL,"
                "draft_revision=?,draft_title=?,draft_body=?,updated_at=? "
                "WHERE job_id=? AND state='analyzing' "
                "AND json_extract(owner,'$.token')=?",
                (
                    state,
                    json.dumps(result, separators=(",", ":")) if result is not None else None,
                    json.dumps(error, separators=(",", ":")) if error is not None else None,
                    1 if draft else 0,
                    draft.get("title") if draft else None,
                    draft.get("body") if draft else None,
                    stamp,
                    job_id,
                    token,
                ),
            ).rowcount
        return bool(changed)

    def abandon_diagnostic(self, job_id, token: str):
        return self.finish_diagnostic(
            job_id,
            token,
            error={
                "code": "analysis_interrupted",
                "message": "Crash analysis was interrupted before a result was saved.",
            },
        )

    def recover_diagnostic(self, job_id, state: str, error: dict):
        with self.connection() as db:
            db.execute(
                "UPDATE job_diagnostics SET state=?,error=?,owner=NULL,updated_at=? "
                "WHERE job_id=?",
                (state, json.dumps(error, separators=(",", ":")), now(), job_id),
            )

    def diagnostics_in_states(self, states):
        marks = ",".join("?" for _ in states)
        with self.connection() as db:
            return [
                self.diagnostic_record(row)
                for row in db.execute(
                    f"SELECT * FROM job_diagnostics WHERE state IN ({marks})",
                    tuple(states),
                )
            ]

    def dismiss_diagnostic(self, job_id):
        with self.connection() as db:
            changed = db.execute(
                "UPDATE job_diagnostics SET dismissed=1,updated_at=? WHERE job_id=?",
                (now(), job_id),
            ).rowcount
        if not changed:
            raise KeyError("Diagnostic not found")
        return self.get_diagnostic(job_id)

    def save_issue_draft(self, job_id, expected_revision: int, title: str, body: str):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state,issue_state,draft_revision FROM job_diagnostics WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError("Diagnostic not found")
            if row["state"] != "ready" or row["issue_state"] != "not_published":
                raise Conflict({"message": "This issue draft can no longer be edited."})
            if row["draft_revision"] != expected_revision:
                raise Conflict({"message": "The issue draft changed; review the current revision.",
                                "revision": row["draft_revision"]})
            db.execute(
                "UPDATE job_diagnostics SET draft_revision=draft_revision+1,"
                "draft_title=?,draft_body=?,updated_at=? WHERE job_id=?",
                (title, body, now(), job_id),
            )
        return self.get_diagnostic(job_id)

    def claim_issue(self, job_id, revision: int, owner: dict):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM job_diagnostics WHERE job_id=?", (job_id,)
            ).fetchone()
            diagnostic = self.diagnostic_record(row)
            if diagnostic is None:
                raise KeyError("Diagnostic not found")
            if diagnostic["issue_state"] == "published":
                return diagnostic, False
            if diagnostic["issue_state"] != "not_published":
                raise Conflict({"message": "Issue publication is already started or requires manual review."})
            if diagnostic["draft_revision"] != revision:
                raise Conflict({"message": "The issue draft changed; review the current revision.",
                                "revision": diagnostic["draft_revision"]})
            db.execute(
                "UPDATE job_diagnostics SET issue_state='publishing',owner=?,updated_at=? "
                "WHERE job_id=? AND issue_state='not_published'",
                (json.dumps(owner, separators=(",", ":")), now(), job_id),
            )
        return self.get_diagnostic(job_id), True

    def finish_issue(self, job_id, token: str, state: str, *, url=None, error=None):
        with self.connection() as db:
            changed = db.execute(
                "UPDATE job_diagnostics SET issue_state=?,issue_url=?,error=?,owner=NULL,updated_at=? "
                "WHERE job_id=? AND issue_state='publishing' "
                "AND json_extract(owner,'$.token')=?",
                (
                    state,
                    url,
                    json.dumps(error, separators=(",", ":")) if error is not None else None,
                    now(),
                    job_id,
                    token,
                ),
            ).rowcount
        return bool(changed)

    def diagnostics_by_issue_state(self, states):
        marks = ",".join("?" for _ in states)
        with self.connection() as db:
            return [
                self.diagnostic_record(row)
                for row in db.execute(
                    f"SELECT * FROM job_diagnostics WHERE issue_state IN ({marks})",
                    tuple(states),
                )
            ]

    def recover_issue(self, job_id, state: str, error: dict):
        with self.connection() as db:
            db.execute(
                "UPDATE job_diagnostics SET issue_state=?,error=?,owner=NULL,updated_at=? "
                "WHERE job_id=? AND issue_state='publishing'",
                (state, json.dumps(error, separators=(",", ":")), now(), job_id),
            )

    def add_event(self, id, type, phase=None, data=None):
        stamp = now()
        payload = json.dumps(data or {})
        if len(payload) > 16384:
            raise ValueError("Event metadata too large")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM jobs WHERE id=?", (id,)).fetchone() or db.execute(
                "SELECT 1 FROM deleted_jobs WHERE id=?", (id,)
            ).fetchone():
                raise KeyError("Job not found")
            cursor = db.execute("INSERT INTO events(job_id,timestamp,type,phase,data) VALUES(?,?,?,?,?)", (id, stamp, type, phase, payload))
            sequence = cursor.lastrowid
            db.execute("UPDATE jobs SET updated_at=? WHERE id=?", (stamp, id))
        return {"sequence": sequence, "timestamp": stamp, "type": type, "phase": phase, "data": data or {}}

    def events(self, id, after=0):
        with self.connection() as db:
            rows = db.execute("SELECT sequence,timestamp,type,phase,data FROM events WHERE job_id=? AND sequence>? ORDER BY sequence LIMIT 500", (id, after))
            return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def ideas(self):
        with self.connection() as db:
            return [self.idea_record(r) for r in db.execute("SELECT * FROM ideas ORDER BY created_at DESC")]

    def get_idea(self, id):
        with self.connection() as db:
            return self.idea_record(db.execute("SELECT * FROM ideas WHERE id=?", (id,)).fetchone())

    def add_idea(self, job_id, original):
        if not isinstance(original, dict):
            raise ValueError("Finalized proposal must be an object")
        id, stamp = str(uuid4()), now()
        with self.connection() as db:
            db.execute("INSERT INTO ideas VALUES(?,?,1,?,?,?,?)", (id, job_id, json.dumps(normalize_idea(original)), json.dumps(original), stamp, stamp))
        return self.get_idea(id)

    def save_idea(self, id, expected_revision, idea):
        if not isinstance(idea, dict):
            raise ValueError("Proposal must be an object")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self.idea_record(db.execute("SELECT * FROM ideas WHERE id=?", (id,)).fetchone())
            if record["revision"] != expected_revision:
                raise Conflict({"message": "Proposal changed; reload before saving", "revision": record["revision"]})
            db.execute("UPDATE ideas SET idea=?,revision=revision+1,updated_at=? WHERE id=?", (json.dumps(normalize_idea(idea)), now(), id))
        return self.get_idea(id)

    @staticmethod
    def conversation_record(row, *, internal=False):
        if row is None:
            raise KeyError("Conversation not found")
        result = dict(row)
        for key in ("messages", "pending_idea", "base_idea", "error", "owner"):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        if not internal:
            for key in ("owner", "active_request", "approval_revision", "idea_revision", "base_idea"):
                result.pop(key)
        return result

    def conversations(self, *, internal=False):
        with self.connection() as db:
            return [self.conversation_record(row, internal=internal) for row in db.execute(
                "SELECT * FROM idea_conversations ORDER BY updated_at DESC")]

    def get_conversation(self, id, *, internal=False):
        with self.connection() as db:
            return self.conversation_record(db.execute(
                "SELECT * FROM idea_conversations WHERE id=?", (id,)).fetchone(), internal=internal)

    def delete_conversation(self, id, expected_revision):
        """Delete a discussion without touching saved ideas or experiment jobs."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self.conversation_record(db.execute(
                "SELECT * FROM idea_conversations WHERE id=?", (id,)).fetchone(), internal=True)
            if record["revision"] != expected_revision:
                raise Conflict({"message": "This conversation changed. Reload before deleting it.",
                                "revision": record["revision"]})
            if record["state"] == "running":
                raise Conflict({"message": "Stop the response before deleting this conversation."})
            db.execute("DELETE FROM idea_conversation_sources WHERE conversation_id=?", (id,))
            # Keep request IDs as tombstones, but remove fingerprints containing old messages.
            # Delayed retries must not recreate deleted conversations.
            db.execute("UPDATE idea_conversation_requests SET fingerprint='' WHERE conversation_id=?", (id,))
            db.execute("DELETE FROM idea_conversations WHERE id=?", (id,))

    def claim_conversation(self, request_id, message, owner, *, conversation_id=None,
                           expected_revision=None, role_config_id=None, idea_id=None):
        """Persist user input and its unique turn claim before scheduling any network work."""
        from .idea_conversations import explicit_approval
        request_id = str(UUID(str(request_id)))
        fingerprint = json.dumps([conversation_id, expected_revision, role_config_id, idea_id, message])
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM idea_conversation_requests WHERE request_id=?",
                               (request_id,)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise Conflict({"message": "This request ID was already used for a different message."})
                return self.conversation_record(db.execute(
                    "SELECT * FROM idea_conversations WHERE id=?", (prior["conversation_id"],)).fetchone()), False
            stamp = now()
            if conversation_id is None:
                conversation_id = str(uuid4())
                baseline = self.idea_record(db.execute("SELECT * FROM ideas WHERE id=?", (idea_id,)).fetchone()) if idea_id else None
                db.execute(
                    "INSERT INTO idea_conversations(id,title,revision,role_config_id,state,messages,"
                    "idea_id,idea_revision,base_idea,created_at,updated_at) VALUES(?,?,0,?,'idle','[]',?,?,?,?,?)",
                    (conversation_id, message.strip()[:100], role_config_id or "current", idea_id,
                     baseline["revision"] if baseline else None,
                     json.dumps(baseline["idea"]) if baseline else None, stamp, stamp))
            record = self.conversation_record(db.execute(
                "SELECT * FROM idea_conversations WHERE id=?", (conversation_id,)).fetchone(), internal=True)
            if record["state"] == "running":
                raise Conflict({"message": "The assistant is still responding. Stop it or wait before sending."})
            if expected_revision is not None and record["revision"] != expected_revision:
                raise Conflict({"message": "This conversation changed. Reload before sending.",
                                "revision": record["revision"]})
            approval = record["candidate_revision"] if record["pending_idea"] is not None and explicit_approval(message) else None
            messages = record["messages"] + [{"role": "user", "content": message}]
            db.execute(
                "UPDATE idea_conversations SET messages=?,revision=revision+1,state='running',error=NULL,"
                "progress='Thinking about your idea…',owner=?,active_request=?,approval_revision=?,"
                "pending_idea=?,candidate_revision=?,updated_at=? WHERE id=?",
                (json.dumps(messages), json.dumps(owner), request_id, approval,
                 json.dumps(record["pending_idea"]) if approval is not None else None,
                 record["candidate_revision"] if approval is not None else None, stamp, conversation_id))
            db.execute("INSERT INTO idea_conversation_requests VALUES(?,?,?)",
                       (request_id, conversation_id, fingerprint))
            return self.conversation_record(db.execute(
                "SELECT * FROM idea_conversations WHERE id=?", (conversation_id,)).fetchone()), True

    def conversation_progress(self, id, request_id, progress):
        with self.connection() as db:
            db.execute("UPDATE idea_conversations SET progress=?,updated_at=? "
                       "WHERE id=? AND active_request=? AND state='running'",
                       (progress, now(), id, request_id))

    def finish_conversation(self, id, request_id, *, message=None, candidate=None,
                            approve_revision=None, error=None):
        """Commit the exact presented JSON and acknowledgement together, or nothing."""
        from .idea_conversations import explicit_approval
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM idea_conversations WHERE id=?", (id,)).fetchone()
            if row is None:
                return False
            record = self.conversation_record(row, internal=True)
            if record["state"] != "running" or record["active_request"] != request_id:
                return False
            stamp = now()
            pending = record["pending_idea"]
            pending_revision = record["candidate_revision"]
            if approve_revision is not None:
                if (type(approve_revision) is not int or pending is None
                        or approve_revision != pending_revision
                        or approve_revision != record["approval_revision"]
                        or not explicit_approval(record["messages"][-1]["content"])
                        or candidate is not None):
                    raise Conflict({"message": "Approval no longer matches the presented idea. Please review it again."})
                payload = json.dumps(pending, ensure_ascii=False, allow_nan=False)
                if record["idea_id"] is None:
                    record["idea_id"] = str(uuid4())
                    record["idea_revision"] = 1
                    db.execute("INSERT INTO ideas VALUES(?,?,1,?,?,?,?)",
                               (record["idea_id"], id, payload, payload, stamp, stamp))
                else:
                    changed = db.execute(
                        "UPDATE ideas SET idea=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                        (payload, stamp, record["idea_id"], record["idea_revision"])).rowcount
                    if not changed:
                        raise Conflict({"message": "The saved idea changed elsewhere. Start a new discussion from its latest revision; nothing was overwritten."})
                    record["idea_revision"] += 1
                record["base_idea"] = pending
                message = "Saved the exact idea you approved to the ideas backlog."
                pending, pending_revision = None, None
            elif candidate is not None:
                pending, pending_revision = candidate, record["revision"] + 1
                message = message or "Here is the complete idea for your review."
            elif error is None:
                pending, pending_revision = None, None
            messages = record["messages"]
            if message:
                reply = {"role": "assistant", "content": message}
                if candidate is not None:
                    reply["idea"] = candidate
                messages.append(reply)
            db.execute(
                "UPDATE idea_conversations SET state=?,revision=revision+1,messages=?,pending_idea=?,"
                "candidate_revision=?,idea_id=?,idea_revision=?,base_idea=?,error=?,progress=NULL,owner=NULL,"
                "active_request=NULL,approval_revision=NULL,updated_at=? WHERE id=?",
                ("failed" if error else "idle", json.dumps(messages),
                 json.dumps(pending, ensure_ascii=False, allow_nan=False) if pending is not None else None,
                 pending_revision, record["idea_id"], record["idea_revision"],
                 json.dumps(record["base_idea"]) if record["base_idea"] is not None else None,
                 json.dumps(error) if error else None, stamp, id))
            return True

    def stop_conversation(self, id, *, request_id=None, interrupted=False):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM idea_conversations WHERE id=?", (id,)).fetchone()
            if row is None and request_id is not None:
                return None
            record = self.conversation_record(row, internal=True)
            if record["state"] == "running" and (request_id is None or request_id == record["active_request"]):
                error = {"message": "The response was interrupted. Your discussion is retained; send a message to continue."} if interrupted else None
                messages = record["messages"] + [{"role": "assistant", "content":
                    "Response interrupted; nothing new was saved." if interrupted else "Stopped. Nothing new was saved; you can continue this discussion."}]
                db.execute(
                    "UPDATE idea_conversations SET state=?,revision=revision+1,messages=?,error=?,progress=NULL,"
                    "owner=NULL,active_request=NULL,approval_revision=NULL,updated_at=? WHERE id=?",
                    ("failed" if interrupted else "idle", json.dumps(messages), json.dumps(error) if error else None, now(), id))
            return self.conversation_record(db.execute(
                "SELECT * FROM idea_conversations WHERE id=?", (id,)).fetchone())

    def conversation_sources(self, id):
        with self.connection() as db:
            return [json.loads(row["evidence"]) for row in db.execute(
                "SELECT evidence FROM idea_conversation_sources WHERE conversation_id=? ORDER BY sequence", (id,))]

    def add_conversation_source(self, id, request_id, evidence):
        """Retain actual tool evidence separately from user/assistant dialogue."""
        with self.connection() as db:
            return bool(db.execute(
                "INSERT INTO idea_conversation_sources(conversation_id,request_id,evidence) "
                "SELECT id,active_request,? FROM idea_conversations "
                "WHERE id=? AND active_request=? AND state='running'",
                (json.dumps(evidence, ensure_ascii=False, allow_nan=False), id, request_id)).rowcount)
