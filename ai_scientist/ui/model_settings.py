"""Database-backed model settings: one editable set, stored in Studio's SQLite.

Replaces ``ais_roles.yaml`` as the single source of truth for

* ``model_servers`` — a server address, API format, credential reference, and
  declared capabilities; and
* ``task_models``   — which server and model each research task uses, with the
  optional tuning overrides.

There is exactly one current settings set: saving updates rows in place, so
nothing accumulates. Experiments snapshot these tables at start time (see
``snapshot``), so each run keeps the settings it started with.

Storage rules:
* Credential columns hold environment-variable NAMES, never secret values.
* ``api_format='openai-codex'`` servers are managed by ChatGPT sign-in: their
  address and credential columns stay NULL.
* A fresh database has empty tables; Studio renders that as "nothing
  configured yet", never as an error.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    api_format TEXT NOT NULL,
    address TEXT,
    credential_env TEXT,
    timeout REAL,
    capabilities TEXT NOT NULL DEFAULT '[]',
    requires_user_message INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_models (
    task TEXT PRIMARY KEY,
    server_id TEXT REFERENCES model_servers(id),
    model TEXT,
    max_tokens INTEGER,
    temperature REAL,
    timeout REAL,
    credential_env TEXT,
    requires TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_settings_extra (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS role_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    roles TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@contextmanager
def _connect(path: Path, *, read_only: bool = False):
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    try:
        db.row_factory = sqlite3.Row
        if read_only:
            db.execute("PRAGMA query_only=1")
        else:
            db.executescript(_SCHEMA)
            db.execute("PRAGMA journal_mode=WAL")
        with db:
            yield db
    finally:
        db.close()

_ENV = "AI_SCIENTIST_ROOT"
_SERVER_FIELDS = ("name", "api_format", "address", "credential_env", "timeout",
                  "capabilities", "requires_user_message")
_TASK_FIELDS = ("server_id", "model", "max_tokens", "temperature", "timeout",
                "credential_env", "requires")
_stamp_lock = threading.Lock()


class SettingsConflict(Exception):
    """A revision or profile-name constraint prevented an atomic write."""



def ensure_schema(root: str | os.PathLike) -> None:
    """Create the settings tables if missing; safe to call at every startup."""
    with _connect(settings_db_path(root)) as db:
        db.execute("BEGIN IMMEDIATE")
        columns = db.execute("PRAGMA table_info(task_models)").fetchall()
        if any(row["name"] in ("server_id", "model") and row["notnull"] for row in columns):
            db.execute("ALTER TABLE task_models RENAME TO task_models_assigned")
            db.execute("""CREATE TABLE task_models (
                task TEXT PRIMARY KEY, server_id TEXT REFERENCES model_servers(id), model TEXT,
                max_tokens INTEGER, temperature REAL, timeout REAL, credential_env TEXT,
                requires TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL)""")
            db.execute("INSERT INTO task_models SELECT * FROM task_models_assigned")
            db.execute("DROP TABLE task_models_assigned")


def save_settings_atomic(root: str | os.PathLike, servers: dict[str, dict],
                         tasks: dict[str, dict], delete_servers=(), delete_tasks=(),
                         *, expected_revision: str | None = None) -> None:
    """Write already-validated server/task rows in one transaction.

    ``servers`` maps name -> {api_format, address, credential_env, timeout,
    capabilities, requires_user_message}; ``tasks`` maps task ->
    {server, model, max_tokens, temperature, timeout, credential_env, requires}.
    """
    stamp = _now()
    with _connect(settings_db_path(root)) as db:
        db.execute("BEGIN IMMEDIATE")
        if expected_revision is not None and read_snapshot(root, db=db)[0] != expected_revision:
            raise SettingsConflict("The configuration changed elsewhere. Reload before saving.")
        for task in delete_tasks:
            db.execute("DELETE FROM task_models WHERE task=?", (task,))
        for name, server in servers.items():
            row = db.execute("SELECT id FROM model_servers WHERE name=?", (name,)).fetchone()
            server_id = row["id"] if row else _uuid()
            db.execute(
                "INSERT INTO model_servers(id,name,api_format,address,credential_env,timeout,"
                "capabilities,requires_user_message,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "api_format=excluded.api_format,address=excluded.address,"
                "credential_env=excluded.credential_env,timeout=excluded.timeout,"
                "capabilities=excluded.capabilities,"
                "requires_user_message=excluded.requires_user_message,updated_at=excluded.updated_at",
                (server_id, name, server["api_format"], server.get("address"),
                 server.get("credential_env"), server.get("timeout"),
                 json.dumps(server.get("capabilities") or [], separators=(",", ":")),
                 1 if server.get("requires_user_message") else 0, stamp, stamp))
        for task, entry in tasks.items():
            server_row = db.execute("SELECT id FROM model_servers WHERE name=?",
                                    (entry["server"],)).fetchone()
            if entry.get("server") is not None and server_row is None:
                raise KeyError(f"Unknown server {entry['server']!r}.")
            db.execute(
                "INSERT INTO task_models(task,server_id,model,max_tokens,temperature,timeout,"
                "credential_env,requires,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(task) DO UPDATE SET server_id=excluded.server_id,model=excluded.model,"
                "max_tokens=excluded.max_tokens,temperature=excluded.temperature,"
                "timeout=excluded.timeout,credential_env=excluded.credential_env,"
                "requires=excluded.requires,updated_at=excluded.updated_at",
                (task, server_row["id"] if server_row else None, entry.get("model"), entry.get("max_tokens"),
                 entry.get("temperature"), entry.get("timeout"), entry.get("credential_env"),
                 json.dumps(entry.get("requires") or [], separators=(",", ":")), stamp))
        for name in delete_servers:
            used = db.execute(
                "SELECT COUNT(*) AS uses FROM task_models t JOIN model_servers s "
                "ON s.id=t.server_id WHERE s.name=?", (name,)).fetchone()["uses"]
            if used:
                raise ValueError(f"Server {name!r} still has tasks assigned; reassign them first.")
            db.execute("DELETE FROM model_servers WHERE name=?", (name,))


def settings_db_path(root: str | os.PathLike | None = None) -> Path:
    base = Path(root) if root is not None else Path(
        os.environ.get(_ENV) or Path(__file__).resolve().parents[2])
    return Path(base) / "ui_data" / "ui.sqlite3"



def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _uuid() -> str:
    from uuid import uuid4
    return str(uuid4())


def _decode(row: sqlite3.Row, fields: tuple[str, ...], *, json_fields: tuple[str, ...]) -> dict:
    record = {field: row[field] for field in fields}
    for field in json_fields:
        try:
            record[field] = json.loads(record[field] or "[]")
        except (TypeError, ValueError):
            record[field] = []
    return record


# --------------------------------------------------------------------------- read

def list_servers(root: str | os.PathLike | None = None) -> list[dict]:
    with _connect(settings_db_path(root), read_only=True) as db:
        rows = db.execute("SELECT * FROM model_servers ORDER BY name").fetchall()
    return [_decode(row, _SERVER_FIELDS, json_fields=("capabilities",)) | {"id": row["id"]}
            for row in rows]


def list_tasks(root: str | os.PathLike | None = None) -> list[dict]:
    with _connect(settings_db_path(root), read_only=True) as db:
        rows = db.execute(
            "SELECT t.*, s.name AS server_name FROM task_models t "
            "LEFT JOIN model_servers s ON s.id = t.server_id ORDER BY t.task").fetchall()
    tasks = []
    for row in rows:
        record = _decode(row, _TASK_FIELDS, json_fields=("requires",)) | {
            "task": row["task"], "server": row["server_name"]}
        record.pop("server_id")
        tasks.append(record)
    return tasks


def extra_setting(key: str, root: str | os.PathLike | None = None) -> dict | None:
    with _connect(settings_db_path(root), read_only=True) as db:
        row = db.execute("SELECT value FROM model_settings_extra WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return None


def read_snapshot(root: str | os.PathLike | None = None, *, db=None) -> tuple[str, dict]:
    """Read the revision and routing configuration from one SQLite snapshot."""
    with nullcontext(db) if db is not None else _connect(settings_db_path(root), read_only=True) as connection:
        if not connection.in_transaction:
            connection.execute("BEGIN")
        servers = [_decode(row, _SERVER_FIELDS, json_fields=("capabilities",)) | {"id": row["id"]}
                   for row in connection.execute("SELECT * FROM model_servers ORDER BY name")]
        tasks = []
        for row in connection.execute(
                "SELECT t.*, s.name AS server_name FROM task_models t "
                "LEFT JOIN model_servers s ON s.id=t.server_id ORDER BY t.task"):
            task = _decode(row, _TASK_FIELDS, json_fields=("requires",)) | {
                "task": row["task"], "server": row["server_name"]}
            task.pop("server_id")
            tasks.append(task)
        extra_row = connection.execute(
            "SELECT value FROM model_settings_extra WHERE key='experiment_execution'").fetchone()
        extra = json.loads(extra_row["value"]) if extra_row else None
        canonical = json.dumps({"servers": servers, "tasks": tasks,
                                "extra": {"experiment_execution": extra}},
                               sort_keys=True, separators=(",", ":"))
        token = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        endpoints = {server["name"]: {
            "provider": server["api_format"], "base_url": server["address"],
            "api_key_env": server["credential_env"], "timeout": server["timeout"],
            "provides": list(server["capabilities"]),
            "requires_user_message": bool(server["requires_user_message"]),
        } for server in servers}
        roles = {}
        for task in tasks:
            entry = {"endpoint": task["server"], "model": task["model"]}
            for field in ("max_tokens", "temperature", "timeout", "credential_env"):
                if task[field] is not None:
                    entry[{"credential_env": "api_key_env"}.get(field, field)] = task[field]
            if task["requires"]:
                entry["requires"] = list(task["requires"])
            roles[task["task"]] = entry
        return token, {"endpoints": endpoints, "roles": roles, "experiment_execution": extra or {}}


def revision(root: str | os.PathLike | None = None) -> str:
    return read_snapshot(root)[0]


def current_settings(root: str | os.PathLike | None = None) -> dict:
    return read_snapshot(root)[1]


def _profile(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in ("id", "name", "revision", "created_at", "updated_at")} | {
        "roles": json.loads(row["roles"])}


def list_profiles(root: str | os.PathLike) -> list[dict]:
    with _connect(settings_db_path(root), read_only=True) as db:
        return [_profile(row) for row in db.execute("SELECT * FROM role_profiles ORDER BY name_key, id")]


def save_profile_atomic(root: str | os.PathLike, roles: dict, validate, *,
                        name: str | None = None, profile_id: str | None = None,
                        expected_revision: int | None = None) -> dict:
    """Validate against current servers and change only one profile in a transaction."""
    with _connect(settings_db_path(root)) as db:
        db.execute("BEGIN IMMEDIATE")
        if profile_id is not None:
            row = db.execute("SELECT * FROM role_profiles WHERE id=?", (profile_id,)).fetchone()
            if row is None:
                raise KeyError("Unknown role profile.")
            if row["revision"] != expected_revision:
                raise SettingsConflict("This profile changed elsewhere. Reload it before overwriting.")
        elif db.execute("SELECT 1 FROM role_profiles WHERE name_key=?", (name.casefold(),)).fetchone():
            raise SettingsConflict("A profile with this name already exists. Select it to overwrite explicitly.")
        validate(roles, read_snapshot(root, db=db)[1])
        stamp = _now()
        encoded = json.dumps(roles, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if profile_id is None:
            profile_id = _uuid()
            db.execute("INSERT INTO role_profiles(id,name,name_key,revision,roles,created_at,updated_at) "
                       "VALUES(?,?,?,1,?,?,?)", (profile_id, name, name.casefold(), encoded, stamp, stamp))
        else:
            db.execute("UPDATE role_profiles SET roles=?,revision=revision+1,updated_at=? "
                       "WHERE id=? AND revision=?", (encoded, stamp, profile_id, expected_revision))
        return _profile(db.execute("SELECT * FROM role_profiles WHERE id=?", (profile_id,)).fetchone())


# -------------------------------------------------------------------------- write

def upsert_server(settings: dict, *, name: str, created_ok: bool = True) -> dict:
    """Insert or update one server row from an already-validated mapping."""
    stamp = _now()
    with _connect(settings_db_path(settings["root"])) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT id FROM model_servers WHERE name=?", (name,)).fetchone()
        server_id = row["id"] if row else _uuid()
        if row is None and not created_ok:
            raise KeyError(name)
        db.execute(
            "INSERT INTO model_servers(id,name,api_format,address,credential_env,timeout,"
            "capabilities,requires_user_message,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
            "api_format=excluded.api_format,address=excluded.address,"
            "credential_env=excluded.credential_env,timeout=excluded.timeout,"
            "capabilities=excluded.capabilities,requires_user_message=excluded.requires_user_message,"
            "updated_at=excluded.updated_at",
            (server_id, name, settings["api_format"], settings.get("address"),
             settings.get("credential_env"), settings.get("timeout"),
             json.dumps(settings.get("capabilities") or [], separators=(",", ":")),
             1 if settings.get("requires_user_message") else 0, stamp, stamp))
    return {"id": server_id, "name": name}


def upsert_task(settings: dict, *, task: str) -> None:
    """Insert or update one task row; ``settings`` is already validated."""
    stamp = _now()
    with _connect(settings_db_path(settings["root"])) as db:
        db.execute("BEGIN IMMEDIATE")
        server = db.execute("SELECT id FROM model_servers WHERE name=?",
                            (settings["server"],)).fetchone()
        if server is None:
            raise KeyError(f"Unknown server {settings['server']!r}.")
        db.execute(
            "INSERT INTO task_models(task,server_id,model,max_tokens,temperature,timeout,"
            "credential_env,requires,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(task) DO UPDATE SET server_id=excluded.server_id,model=excluded.model,"
            "max_tokens=excluded.max_tokens,temperature=excluded.temperature,"
            "timeout=excluded.timeout,credential_env=excluded.credential_env,"
            "requires=excluded.requires,updated_at=excluded.updated_at",
            (task, server["id"], settings["model"], settings.get("max_tokens"),
             settings.get("temperature"), settings.get("timeout"),
             settings.get("credential_env"),
             json.dumps(settings.get("requires") or [], separators=(",", ":")), stamp))


def delete_server(root: str | os.PathLike, name: str) -> bool:
    with _connect(settings_db_path(root)) as db:
        db.execute("BEGIN IMMEDIATE")
        used = db.execute(
            "SELECT COUNT(*) AS uses FROM task_models t JOIN model_servers s "
            "ON s.id=t.server_id WHERE s.name=?", (name,)).fetchone()["uses"]
        if used:
            raise ValueError("Reassign the tasks using this server before deleting it.")
        cursor = db.execute("DELETE FROM model_servers WHERE name=?", (name,))
        return cursor.rowcount > 0


def delete_task(root: str | os.PathLike, task: str) -> bool:
    with _connect(settings_db_path(root)) as db:
        cursor = db.execute("DELETE FROM task_models WHERE task=?", (task,))
        return cursor.rowcount > 0


def set_extra_setting(key: str, value: dict | None, root: str | os.PathLike) -> None:
    with _connect(settings_db_path(root)) as db:
        if value is None:
            db.execute("DELETE FROM model_settings_extra WHERE key=?", (key,))
        else:
            db.execute(
                "INSERT INTO model_settings_extra(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, separators=(",", ":"))))


# ----------------------------------------------------------------------- migration

def import_legacy_role_config(path: str | os.PathLike, root: str | os.PathLike) -> dict:
    """One-time import of an ``ais_roles.yaml`` file into the database tables.

    Returns {"servers": n, "tasks": n}. Raises ValueError on a structurally
    invalid file; the caller decides whether to keep or discard the file.
    """
    import yaml

    source = Path(path)
    cfg = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError("Role configuration must be a YAML mapping.")
    endpoints = cfg.get("endpoints")
    roles = cfg.get("roles")
    if not isinstance(endpoints, dict) or not isinstance(roles, dict):
        raise ValueError("Role configuration requires endpoints and roles mappings.")

    imported_servers = 0
    for name, endpoint in endpoints.items():
        if not isinstance(name, str) or not isinstance(endpoint, dict):
            raise ValueError("Each endpoint must be a named mapping.")
        provides = endpoint.get("provides")
        upsert_server({
            "root": root,
            "api_format": endpoint.get("provider", "openai"),
            "address": endpoint.get("base_url"),
            "credential_env": endpoint.get("api_key_env"),
            "timeout": endpoint.get("timeout"),
            "capabilities": [item for item in provides if isinstance(item, str)] if isinstance(provides, list) else [],
            "requires_user_message": bool(endpoint.get("requires_user_message")),
        }, name=name)
        imported_servers += 1

    imported_tasks = 0
    for task, entry in roles.items():
        if not isinstance(task, str) or not isinstance(entry, dict):
            raise ValueError("Each role must be a named mapping.")
        server_name = entry.get("endpoint")
        if not isinstance(server_name, str) or server_name not in endpoints:
            raise ValueError(f"Role {task!r} references unknown endpoint {server_name!r}.")
        requires = entry.get("requires")
        upsert_task({
            "root": root,
            "server": server_name,
            "model": entry.get("model"),
            "max_tokens": entry.get("max_tokens"),
            "temperature": entry.get("temperature"),
            "timeout": entry.get("timeout"),
            "credential_env": entry.get("api_key_env"),
            "requires": [item for item in requires if isinstance(item, str)] if isinstance(requires, list) else [],
        }, task=task)
        imported_tasks += 1

    execution = cfg.get("experiment_execution")
    if isinstance(execution, dict):
        set_extra_setting("experiment_execution", execution, root)
    return {"servers": imported_servers, "tasks": imported_tasks}


def maybe_import_legacy(root: str | os.PathLike):
    """Return a callable that imports root/ais_roles.yaml once, then renames it.

    The rename makes the import idempotent and preserves the user's original
    file as ``ais_roles.yaml.migrated`` next to it. Import failures leave the
    file untouched and surface the error to the caller.
    """
    legacy = Path(root) / "ais_roles.yaml"
    migrated = legacy.with_name(legacy.name + ".migrated")

    def run() -> dict | None:
        if not legacy.exists() or migrated.exists():
            return None
        result = import_legacy_role_config(legacy, root)
        legacy.rename(migrated)
        return result

    return run
