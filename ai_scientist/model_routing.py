"""Role-based routing for self-hosted OpenAI-compatible endpoints.

Additive routing layer for AI-Scientist-v2. Existing model strings
(cborg/..., spark/..., ollama/..., gpt/o1/claude/gemini names) keep working
unchanged. New model-string forms:

- ``role/<role>``: the role's endpoint, served model id, and per-role
  request settings are looked up from the role config at request time. The
  ``role/<role>`` string itself is the client model token end to end, so
  each role keeps its own identity (no shared-state, no races).
- ``selfhosted/<endpoint>/<model>``: direct reference to an endpoint defined
  in the same YAML registry.

The role config file is discovered from the ``AI_SCIENTIST_ROLE_CONFIG``
environment variable, defaulting to ``ais_roles.yaml`` in the repository
root. Endpoints are peers: there is no primary endpoint and no automatic
fallback. Credentials are read from environment variables named in the
config and are never logged.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import yaml

ROLE_CONFIG_ENV = "AI_SCIENTIST_ROLE_CONFIG"
DEFAULT_ROLE_CONFIG_FILENAME = "ais_roles.yaml"

SELFHOSTED_PREFIX = "selfhosted/"
ROLE_PREFIX = "role/"

REQUEST_LOG_ENV = "AI_SCIENTIST_REQUEST_LOG"
DEFAULT_REQUEST_LOG = os.path.join("logs", "model_requests.jsonl")

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}


class RoleConfigError(ValueError):
    """Raised when the role configuration is missing or invalid."""


def role_config_path() -> Path:
    override = os.environ.get(ROLE_CONFIG_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / DEFAULT_ROLE_CONFIG_FILENAME


def load_role_config(path: str | os.PathLike | None = None) -> dict:
    """Load (and mtime-cache) the role configuration YAML."""
    cfg_path = Path(path) if path is not None else role_config_path()
    if not cfg_path.exists():
        raise RoleConfigError(
            f"Role config not found: {cfg_path}. Set {ROLE_CONFIG_ENV} or create "
            f"{DEFAULT_ROLE_CONFIG_FILENAME} in the repository root."
        )
    key = str(cfg_path)
    mtime = cfg_path.stat().st_mtime
    with _lock:
        cached = _cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise RoleConfigError(f"Role config {cfg_path} must be a mapping.")
    if "endpoints" not in cfg or not isinstance(cfg["endpoints"], dict):
        raise RoleConfigError(f"Role config {cfg_path} requires an 'endpoints' mapping.")
    with _lock:
        _cache[key] = (mtime, cfg)
    return cfg


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
    cfg = load_role_config()
    endpoints = cfg["endpoints"]
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
        cfg = load_role_config()
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
    endpoint = load_role_config()["endpoints"].get(endpoint_name)
    if endpoint is None:
        raise RoleConfigError(
            f"Endpoint {endpoint_name!r} is not configured. "
            f"Configured endpoints: {sorted(load_role_config()['endpoints'])}."
        )
    return endpoint or {}


DEFAULT_ENDPOINT_TIMEOUT = 600  # seconds; finite so a wedged endpoint fails
                                # clearly instead of stalling the pipeline.


def create_selfhosted_client(model: str, max_retries: int = 2):
    """Create an OpenAI client for a role/ or selfhosted/ model string."""
    import openai

    parsed = parse_model(model)
    if parsed is None:
        raise RoleConfigError(
            f"Model string {model!r} is not a self-hosted or role model string."
        )
    cfg = load_role_config()
    endpoint = cfg["endpoints"][parsed["endpoint"]] or {}
    # Per-role auth overrides the endpoint default; env var NAMES only.
    api_key_env = (
        parsed["settings"].get("api_key_env") or endpoint.get("api_key_env")
    )
    api_key = os.environ.get(api_key_env) if api_key_env else None
    if not api_key:
        api_key = "unused"
    base_url = endpoint.get("base_url")
    if not base_url:
        raise RoleConfigError(
            f"Endpoint {parsed['endpoint']!r} has no 'base_url'."
        )
    timeout = float(
        parsed["settings"].get(
            "timeout", endpoint.get("timeout", DEFAULT_ENDPOINT_TIMEOUT)
        )
    )
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
    endpoint_name: str, *, path: str | os.PathLike | None = None,
    timeout: float | None = None,
) -> list[str]:
    """GET <base_url>/models; raises a clear error when unreachable."""
    import openai

    cfg = load_role_config(path)
    endpoints = cfg["endpoints"]
    if endpoint_name not in endpoints:
        raise RoleConfigError(
            f"Unknown endpoint {endpoint_name!r}. Configured: {sorted(endpoints)}."
        )
    endpoint = endpoints[endpoint_name] or {}
    base_url = endpoint.get("base_url")
    if not base_url:
        raise RoleConfigError(f"Endpoint {endpoint_name!r} has no 'base_url'.")
    client = openai.OpenAI(
        base_url=base_url,
        api_key=os.environ.get(endpoint.get("api_key_env") or "") or "unused",
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


def validate_roles(path: str | os.PathLike | None = None) -> dict[str, dict]:
    """Validate every role: model present on its endpoint, capabilities declared.

    Returns {role: {endpoint, model, capabilities}}. Raises RoleConfigError
    with a readable message on the first problem found.
    """
    cfg = load_role_config(path)
    endpoints = cfg["endpoints"]
    model_lists: dict[str, list[str]] = {}

    def _models(name: str) -> list[str]:
        if name not in model_lists:
            model_lists[name] = list_endpoint_models(name, path=path)
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


def describe_assignment(path: str | os.PathLike | None = None) -> str:
    """One-line-per-role summary of the active mapping (for startup logs)."""
    try:
        validated = validate_roles(path)
    except RoleConfigError as e:
        return f"role config INVALID: {e}"
    lines = ["role mapping:"]
    for role, info in validated.items():
        lines.append(
            f"  {role:<22} -> {info['endpoint']:<8} {info['model']}"
            + (f"  (requires {', '.join(info['capabilities'])})" if info["capabilities"] else "")
        )
    return "\n".join(lines)
