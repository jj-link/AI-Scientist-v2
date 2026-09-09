"""Role-based routing for OpenAI-compatible model servers.

Additive routing layer for AI-Scientist-v2. Existing model strings
(cborg/..., spark/..., ollama/..., gpt/o1/claude/gemini names) keep working
unchanged. New model-string forms:

- ``role/<task>``: the task's server, served model id, and per-task request
  settings are looked up from the model settings at request time. The
  ``role/<task>`` string itself is the client model token end to end, so
  each task keeps its own identity (no shared-state, no races).
- ``selfhosted/<server>/<model>``: direct reference to a server defined
  in the same settings registry.

Settings live in Studio's SQLite database (``ui_data/ui.sqlite3``) and are
edited on the Models page. A running job reads a frozen JSON snapshot instead
(``AI_SCIENTIST_ROLE_CONFIG`` points at it), so editing settings never affects
an experiment that already started. Servers are peers: there is no primary
server and no automatic fallback. Credentials are read from environment
variables named in the settings and are never logged.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import sqlite3

ROLE_CONFIG_ENV = "AI_SCIENTIST_ROLE_CONFIG"


SELFHOSTED_PREFIX = "selfhosted/"
ROLE_PREFIX = "role/"
CODEX_PROVIDER = "openai-codex"
CBORG_PROVIDER = "cborg"
CBORG_BASE_URL = "https://api.cborg.lbl.gov/v1"
CBORG_API_KEY_ENV = "CBORG_API_KEY"
ENDPOINT_PROVIDERS = ("openai", CODEX_PROVIDER, CBORG_PROVIDER)


def endpoint_provider(endpoint: dict) -> str:
    """Resolve the transport without allowing OAuth credentials at arbitrary URLs."""
    provider = endpoint.get("provider", "openai")
    if provider not in ENDPOINT_PROVIDERS:
        raise RoleConfigError("Endpoint provider must be openai, openai-codex, or cborg.")
    if provider == CODEX_PROVIDER and any(endpoint.get(key) not in (None, "") for key in ("base_url", "api_key_env")):
        raise RoleConfigError("Codex manages its server address and uses ChatGPT sign-in, not API-key overrides.")
    return provider


def endpoint_base_url(endpoint: dict) -> str | None:
    provider = endpoint_provider(endpoint)
    if provider == CODEX_PROVIDER:
        from .codex_provider import CODEX_BASE_URL
        return CODEX_BASE_URL
    return endpoint.get("base_url") or (CBORG_BASE_URL if provider == CBORG_PROVIDER else None)


def endpoint_api_key_env(endpoint: dict) -> str | None:
    """Resolve the credential environment-variable name, never its secret value."""
    provider = endpoint_provider(endpoint)
    if provider == CODEX_PROVIDER:
        return None
    return endpoint.get("api_key_env") or (CBORG_API_KEY_ENV if provider == CBORG_PROVIDER else None)


def validate_provider_settings(endpoint: dict, settings: dict) -> None:
    if endpoint_provider(endpoint) == CODEX_PROVIDER and any(
        settings.get(key) is not None for key in ("max_tokens", "temperature", "api_key_env")
    ):
        raise RoleConfigError("Codex roles use managed token limits, sampling, and ChatGPT credentials; remove those overrides.")

REQUEST_LOG_ENV = "AI_SCIENTIST_REQUEST_LOG"
DEFAULT_REQUEST_LOG = os.path.join("logs", "model_requests.jsonl")

_lock = threading.Lock()
_cache: dict[str, tuple[tuple[int, int, int], dict]] = {}


class RoleConfigError(ValueError):
    """Raised when the role configuration is missing or invalid."""


class NoModelSettings(RoleConfigError):
    """No server or task settings exist yet (fresh installation)."""


NO_SETTINGS_MESSAGE = (
    "No model settings are configured. Open AI-Scientist Studio's Models page to "
    "add a server and assign models to research tasks."
)


def settings_snapshot_path() -> Path | None:
    """Frozen settings file for a running job, or None for live database reads."""
    override = os.environ.get(ROLE_CONFIG_ENV)
    return Path(override) if override else None


def _validate_settings_shape(cfg: object, source: Path) -> dict:
    if not isinstance(cfg, dict) or not isinstance(cfg.get("endpoints"), dict):
        raise RoleConfigError(f"Model settings {source} must contain an 'endpoints' mapping.")
    if not isinstance(cfg.get("roles") or {}, dict):
        raise RoleConfigError(f"Model settings {source} requires a 'roles' mapping.")
    cfg.setdefault("roles", {})
    cfg.setdefault("experiment_execution", {})
    return cfg


def load_settings() -> dict:
    """Load model settings: a frozen snapshot inside a running job, else the database.

    Returns {"endpoints": {name: cfg}, "roles": {task: cfg}, "experiment_execution": {}}.
    Job processes receive AI_SCIENTIST_ROLE_CONFIG pointing at their snapshot, so a
    settings edit never affects an experiment that already started. Direct runs read
    the database current set.
    """
    snapshot = settings_snapshot_path()
    if snapshot is not None:
        key = str(snapshot)
        try:
            stamp = snapshot.stat()
        except OSError as exc:
            raise RoleConfigError(f"Model settings snapshot not found: {snapshot}.") from exc
        fingerprint = (stamp.st_mtime_ns, stamp.st_size)
        with _lock:
            cached = _cache.get(key)
            if cached and cached[0] == fingerprint:
                return cached[1]
        try:
            with open(snapshot, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError) as exc:
            raise RoleConfigError(
                f"Model settings snapshot {snapshot} is not readable JSON: {exc}") from exc
        cfg = _validate_settings_shape(cfg, snapshot)
        with _lock:
            _cache[key] = (fingerprint, cfg)
        return cfg
    from .ui import model_settings
    try:
        cfg = model_settings.current_settings()
    except sqlite3.Error:
        raise NoModelSettings(NO_SETTINGS_MESSAGE) from None
    return _validate_settings_shape(cfg, settings_db_display())


def settings_db_display() -> str:
    from .ui import model_settings
    return str(model_settings.settings_db_path())


def is_selfhosted(model: str) -> bool:
    """True when a model string routes through this module (role/ or selfhosted/)."""
    return model.startswith((ROLE_PREFIX, SELFHOSTED_PREFIX))


def endpoint_config(model: str) -> tuple[str, dict]:
    """Return (endpoint_name, endpoint_cfg) for a selfhosted/ model string."""
    parts = model.split("/")
    if len(parts) < 3 or parts[0] != "selfhosted":
        raise RoleConfigError(
            f"Invalid self-hosted model string {model!r}. "
            "Expected 'selfhosted/<endpoint>/<model>'."
        )
    endpoint_name = parts[1]
    endpoints = load_settings()["endpoints"]
    if endpoint_name not in endpoints:
        raise RoleConfigError(
            f"Unknown endpoint {endpoint_name!r} in {model!r}. "
            f"Configured endpoints: {sorted(endpoints)}."
        )
    return endpoint_name, endpoints[endpoint_name] or {}


def selfhosted_model_name(model: str) -> str:
    """Strip 'selfhosted/<endpoint>/' to get the served model id."""
    parts = model.split("/", 2)
    if len(parts) < 3:
        raise RoleConfigError(
            f"Invalid self-hosted model string {model!r}. "
            "Expected 'selfhosted/<endpoint>/<model>'."
        )
    return parts[2]


def parse_model(model: str) -> dict | None:
    """Parse a role/ or selfhosted/ model string into its routing parts.

    Returns None for legacy provider model strings. For 'role/<name>' the
    lookup happens against the CURRENT config at call time, so a config edit
    applies to the next request without any code change.
    """
    if model.startswith(ROLE_PREFIX):
        role = model[len(ROLE_PREFIX):]
        cfg = load_settings()
        roles = cfg.get("roles") or {}
        if role not in roles:
            raise RoleConfigError(
                f"Unknown role {role!r}. Configured roles: {sorted(roles)}."
            )
        entry = roles[role] or {}
        endpoint = entry.get("endpoint")
        model_name = entry.get("model")
        if not endpoint or not model_name:
            raise RoleConfigError(
                f"Role {role!r} must set both 'endpoint' and 'model'."
            )
        if endpoint not in cfg["endpoints"]:
            raise RoleConfigError(
                f"Role {role!r} references unknown endpoint {endpoint!r}. "
                f"Configured endpoints: {sorted(cfg['endpoints'])}."
            )
        settings = {k: v for k, v in entry.items() if k not in ("endpoint", "model")}
        return {
            "role": role,
            "endpoint": endpoint,
            "served_model": model_name,
            "settings": settings,
        }
    if model.startswith(SELFHOSTED_PREFIX):
        endpoint_name, _ = endpoint_config(model)
        return {
            "role": None,
            "endpoint": endpoint_name,
            "served_model": selfhosted_model_name(model),
            "settings": {},
        }
    return None


def served_model_for(model: str) -> str:
    """Served model id for a role/ or selfhosted/ model string."""
    parsed = parse_model(model)
    if parsed is None:
        raise RoleConfigError(
            f"Model string {model!r} is not a self-hosted or role model string."
        )
    return parsed["served_model"]


def role_settings(model: str) -> dict:
    """Per-role request settings (max_tokens, temperature, ...) if declared.

    Empty for direct 'selfhosted/' strings and legacy providers.
    """
    parsed = parse_model(model)
    return parsed["settings"] if parsed else {}


def endpoint_settings(model: str) -> dict[str, Any]:
    """Endpoint mapping for a role/ or selfhosted/ model string.

    Returns the raw endpoint configuration from the current role config
    (base_url, requires_user_message, ...). Legacy provider strings are
    rejected, matching served_model_for.
    """
    parsed = parse_model(model)
    if parsed is None:
        raise RoleConfigError(
            f"Model string {model!r} is not a self-hosted or role model string."
        )
    endpoint_name = parsed["endpoint"]
    endpoint = load_settings()["endpoints"].get(endpoint_name)
    if endpoint is None:
        raise RoleConfigError(
            f"Endpoint {endpoint_name!r} is not configured. "
            f"Configured endpoints: {sorted(load_settings()['endpoints'])}."
        )
    return endpoint or {}


DEFAULT_ENDPOINT_TIMEOUT = 600  # seconds; finite so a wedged endpoint fails
                                # clearly instead of stalling the pipeline.


def _resolve_timeout(settings: dict, endpoint: dict):
    """Task override, else server value, else default; stored None means default."""
    for source in (settings.get("timeout"), endpoint.get("timeout")):
        if source is not None:
            return float(source)
    return float(DEFAULT_ENDPOINT_TIMEOUT)


def create_selfhosted_client(model: str, max_retries: int = 2):
    """Create a chat-completion-compatible client for a configured role or endpoint."""
    import openai

    parsed = parse_model(model)
    if parsed is None:
        raise RoleConfigError(
            f"Model string {model!r} is not a self-hosted or role model string."
        )
    cfg = load_settings()
    endpoint = cfg["endpoints"][parsed["endpoint"]] or {}
    validate_provider_settings(endpoint, parsed["settings"])
    if endpoint_provider(endpoint) == CODEX_PROVIDER:
        from .codex_provider import CodexClient
        return CodexClient(timeout=_resolve_timeout(parsed["settings"], endpoint))
    # Per-role auth overrides the endpoint default; env var NAMES only.
    api_key_env = (
        parsed["settings"].get("api_key_env") or endpoint_api_key_env(endpoint)
    )
    api_key = os.environ.get(api_key_env) if api_key_env else None
    if not api_key:
        api_key = "unused"
    base_url = endpoint_base_url(endpoint)
    if not base_url:
        raise RoleConfigError(
            f"Endpoint {parsed['endpoint']!r} has no 'base_url'."
        )
    timeout = _resolve_timeout(parsed["settings"], endpoint)
    return openai.OpenAI(
        base_url=base_url, api_key=api_key, max_retries=max_retries, timeout=timeout
    )


def request_log_path() -> str:
    return os.environ.get(REQUEST_LOG_ENV, DEFAULT_REQUEST_LOG)


def log_request(
    model: str,
    ok: bool,
    latency_ms: float | None = None,
    error: str | None = None,
) -> None:
    """Append one JSONL record per model request. Never logs credentials.

    Records include the role (for role/ strings), endpoint name, and served
    model id for self-hosted routes.
    """
    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "role": None,
        "endpoint": None,
        "served_model": None,
        "ok": bool(ok),
    }
    if is_selfhosted(model):
        try:
            parsed = parse_model(model)
            if parsed:
                record["role"] = parsed["role"]
                record["endpoint"] = parsed["endpoint"]
                record["served_model"] = parsed["served_model"]
        except RoleConfigError:
            pass
    if latency_ms is not None:
        record["latency_ms"] = round(latency_ms, 1)
    if error:
        record["error"] = str(error)[:500]
    path = Path(request_log_path())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
    except OSError:
        pass  # logging must never break inference
def list_endpoint_models(
    endpoint_name: str, settings: dict | None = None,
    *, timeout: float | None = None,
) -> list[str]:
    """GET <base_url>/models; raises a clear error when unreachable."""
    import openai

    cfg = settings if settings is not None else load_settings()
    endpoints = cfg["endpoints"]
    if endpoint_name not in endpoints:
        raise RoleConfigError(
            f"Unknown endpoint {endpoint_name!r}. Configured: {sorted(endpoints)}."
        )
    endpoint = endpoints[endpoint_name] or {}
    if endpoint_provider(endpoint) == CODEX_PROVIDER:
        from .codex_provider import list_models
        return list_models(timeout=timeout if timeout is not None else DEFAULT_ENDPOINT_TIMEOUT)
    base_url = endpoint_base_url(endpoint)
    if not base_url:
        raise RoleConfigError(f"Endpoint {endpoint_name!r} has no 'base_url'.")
    client = openai.OpenAI(
        base_url=base_url,
        api_key=os.environ.get(endpoint_api_key_env(endpoint) or "") or "unused",
        max_retries=0 if timeout is not None else 1,
        timeout=timeout if timeout is not None else DEFAULT_ENDPOINT_TIMEOUT,
    )
    try:
        return [m.id for m in client.models.list().data]
    except Exception as e:
        raise RoleConfigError(
            f"Endpoint {endpoint_name!r} unreachable at {base_url}: {e}"
        ) from e
    finally:
        client.close()


def validate_roles(settings: dict | None = None) -> dict[str, dict]:
    """Validate every task: model present on its server, capabilities declared.

    Returns {task: {endpoint, model, capabilities}}. Raises RoleConfigError
    with a readable message on the first problem found.
    """
    cfg = settings if settings is not None else load_settings()
    endpoints = cfg["endpoints"]
    model_lists: dict[str, list[str]] = {}

    def _models(name: str) -> list[str]:
        if name not in model_lists:
            model_lists[name] = list_endpoint_models(name, cfg)
        return model_lists[name]

    validated: dict[str, dict] = {}
    for role, entry in sorted((cfg.get("roles") or {}).items()):
        entry = entry or {}
        endpoint_name = entry.get("endpoint")
        model_name = entry.get("model")
        if not endpoint_name or not model_name:
            raise RoleConfigError(
                f"Role {role!r} must set both 'endpoint' and 'model'."
            )
        if endpoint_name not in endpoints:
            raise RoleConfigError(
                f"Role {role!r} references unknown endpoint {endpoint_name!r}."
            )
        validate_provider_settings(endpoints[endpoint_name] or {}, entry)
        available = _models(endpoint_name)
        if model_name not in available:
            raise RoleConfigError(
                f"Role {role!r}: model {model_name!r} not served by endpoint "
                f"{endpoint_name!r}. Available: {available}"
            )
        endpoint_caps = set((endpoints[endpoint_name] or {}).get("provides") or [])
        required = set(entry.get("requires") or [])
        missing = required - endpoint_caps
        if missing:
            raise RoleConfigError(
                f"Role {role!r} requires capabilities {sorted(missing)} that endpoint "
                f"{endpoint_name!r} does not declare (declares {sorted(endpoint_caps)})."
            )
        validated[role] = {
            "endpoint": endpoint_name,
            "model": model_name,
            "capabilities": sorted(required),
        }
    return validated

def describe_assignment(settings: dict | None = None) -> str:
    """One-line-per-task summary of the active mapping (for startup logs)."""
    try:
        validated = validate_roles(settings)
    except RoleConfigError as e:
        return f"model settings INVALID: {e}"
    lines = ["role mapping:"]
    for role, info in validated.items():
        lines.append(
            f"  {role:<22} -> {info['endpoint']:<8} {info['model']}"
            + (f"  (requires {', '.join(info['capabilities'])})" if info["capabilities"] else "")
        )
    return "\n".join(lines)
