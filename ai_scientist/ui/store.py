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
                CREATE TABLE IF NOT EXISTS ideas (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    idea TEXT NOT NULL, original TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                    type TEXT NOT NULL, phase TEXT, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_by_job ON events(job_id, sequence);
            """)

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

    def create_job(self, kind, request_id, request, idea_id=None, idea_revision=None):
        request_id = str(UUID(str(request_id)))
        request = dict(request)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM jobs WHERE request_id=?", (request_id,)).fetchone()
            if prior:
                if prior["kind"] != kind:
                    raise Conflict({"message": "Request ID already used for another job", "job_id": prior["id"]})
                return self.job_record(prior)
            active = db.execute("SELECT id FROM jobs WHERE state IN ('starting','running','stopping')").fetchone()
            if active:
                raise Conflict({"message": "Another job is active", "job_id": active["id"]})
            if idea_id is not None:
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

    def add_event(self, id, type, phase=None, data=None):
        stamp = now()
        payload = json.dumps(data or {})
        if len(payload) > 16384:
            raise ValueError("Event metadata too large")
        with self.connection() as db:
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
