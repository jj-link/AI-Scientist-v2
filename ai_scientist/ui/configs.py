"""Model settings, workload presets, and bounded availability checks.

Model settings live in Studio's SQLite database (see ``model_settings``): one
editable set of servers and task assignments, edited on the Models page and
snapshotted into each experiment at start time. This module exposes safe
views and validation for those rows, plus the untouched workload (``bfts``)
preset discovery.
"""

from __future__ import annotations

import hashlib
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
from types import MappingProxyType
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from filelock import FileLock, Timeout as _LockTimeout

import yaml

from ai_scientist import model_routing
from ai_scientist.ui import model_settings
from ai_scientist.utils.latex import resolve_tex_tool

_PROBE_TIMEOUT = 5.0
_SECRET_KEY = re.compile(r"(?:api[_-]?key|token|secret|password|passwd|authorization|credential|signature)", re.I)


class InvalidConfiguration(ValueError):
    """Field-specific validation failure with browser-safe messages."""

    def __init__(self, message: str, errors: list[dict]):
        super().__init__(message)
        self.errors = errors


class EditorConflict(Exception):
    """Optimistic-concurrency failure; revision changed or lock busy."""

    def __init__(self, message: str, errors: list[dict]):
        super().__init__(message)
        self.message = message
        self.errors = errors


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_REQUIRED_ROLES = ("ideation", "experiment_code", "experiment_feedback", "visual_feedback",
                   "findings_synthesis", "tree_scoring", "plot_generation", "citation",
                   "writeup", "writeup_small", "review")

# Editable fields are exactly these; anything else is rejected as unknown.
_SERVER_FIELDS = ("provider", "base_url", "api_key_env", "timeout")
_TASK_FIELDS = ("endpoint", "model", "max_tokens", "temperature", "timeout", "api_key_env")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _url(value: object) -> str:
    """Never expose URL userinfo, fragments, or credential-bearing query fields."""
    if not isinstance(value, str):
        return ""
    try:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return ""
        host = parts.hostname
        if ":" in host:
            host = f"[{host}]"
        if parts.port is not None:
            host += f":{parts.port}"
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not _SECRET_KEY.search(k)])
        return urlunsplit((parts.scheme, host, parts.path, query, ""))
    except ValueError:
        return ""


def _credential(value: object, *, provider: str = "openai") -> dict:
    if provider == model_routing.CODEX_PROVIDER:
        return {"env": None, "present": False, "method": "codex"}
    name = value if isinstance(value, str) and _ENV_NAME.fullmatch(value) else None
    return {"env": name, "present": bool(name and os.environ.get(name))}


def _strings(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _number(value: object, default: int | float | None = None):
    return value if type(value) in (int, float) and math.isfinite(value) else default


class Configs:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    # ------------------------------------------------------------------ workloads

    def _bfts_paths(self) -> tuple[dict[str, Path], str]:
        filename = "bfts_config.yaml"
        paths = {path.resolve() for path in self.root.glob("bfts_config*.yaml") if path.is_file()}
        selected = (self.root / filename).resolve()
        paths.add(selected)
        return {_id("bfts", path): path for path in sorted(paths)}, _id("bfts", selected)

    def _bfts_path(self, config_id: str) -> Path:
        paths, _ = self._bfts_paths()
        if config_id not in paths:
            raise KeyError("Unknown configuration ID.")
        return paths[config_id]

    def experiment_config(self, config_id: str, run_settings: dict) -> dict:
        """Load once and detach edited mappings so YAML aliases remain unrelated."""
        cfg = _load(self._bfts_path(config_id))
        _environment_credentials_only(cfg)
        agent, execution = cfg.get("agent"), cfg.get("exec")
        if not isinstance(agent, dict) or not isinstance(execution, dict):
            raise ValueError("The experiment configuration requires agent and exec mappings.")
        stages, seeds = agent.get("stages"), agent.get("multi_seed_eval")
        if stages is None:
            stages = {}
        if seeds is None:
            seeds = {}
        if not isinstance(stages, dict) or not isinstance(seeds, dict):
            raise ValueError("Stage limits and multi-seed settings must be mappings.")
        cfg["agent"] = {
            **agent,
            "num_workers": run_settings["num_workers"],
            "multi_seed_eval": {**seeds, "num_seeds": run_settings["num_seeds"]},
            "stages": {
                **stages,
                **{f"stage{i}_max_iters": run_settings["stage_iterations"][f"stage{i}"]
                   for i in range(1, 5)},
            },
        }
        cfg["exec"] = {**execution, "timeout": run_settings["execution_timeout"]}
        return cfg

    def presets(self) -> dict:
        workloads, selected_bfts = self._bfts_paths()
        bfts_options = []
        for key, path in workloads.items():
            error = None
            settings = None
            try:
                cfg = _load(path)
                agent = cfg.get("agent") or {}
                execution = cfg.get("exec") or {}
                if not isinstance(agent, dict) or not isinstance(execution, dict):
                    raise ValueError("Workload agent and exec settings must be mappings.")
                stages = agent.get("stages") or {}
                seeds = agent.get("multi_seed_eval") or {}
                if not isinstance(stages, dict) or not isinstance(seeds, dict):
                    raise ValueError("Workload stages and multi_seed_eval settings must be mappings.")
                settings = {"exp_name": cfg.get("exp_name") if isinstance(cfg.get("exp_name"), str) else None,
                            "num_workers": _number(agent.get("num_workers")),
                            "stage_iterations": {f"stage{i}": _number(stages.get(f"stage{i}_max_iters"),
                                _number(agent.get("steps"))) for i in range(1, 5)},
                            "num_seeds": _number(seeds.get("num_seeds")),
                            "execution_timeout": _number(execution.get("timeout")),
                            "output_directory": "experiments/ui_<job UUID>/"}
            except ValueError as exc:
                error = str(exc)
            bfts_options.append({"id": key, "label": "Reduced validation workload" if
                path.name == "bfts_config.acceptance.yaml" else path.name,
                "settings": settings, "error": error})
        return {"bfts_configs": bfts_options, "selected_bfts_config_id": selected_bfts}

    # -------------------------------------------------------------- model settings

    def _settings_dicts(self) -> tuple[dict[str, dict], dict[str, dict]]:
        """Current rows in the routing layer's internal shape, plus name index."""
        settings = model_settings.current_settings(self.root)
        return settings["endpoints"], settings["roles"]

    def _endpoint_rows(self, endpoints: dict[str, dict]) -> list[dict]:
        rows = []
        for name, endpoint in endpoints.items():
            label = endpoint.get("label")
            provider = endpoint.get("provider", "openai")
            rows.append({"id": name,
                "label": label if isinstance(label, str) and label.strip() else name,
                "url": _url(model_routing.endpoint_base_url(endpoint)), "provider": provider,
                "timeout": _number(endpoint.get("timeout"), model_routing.DEFAULT_ENDPOINT_TIMEOUT),
                "capabilities": _strings(endpoint.get("provides")),
                "credential": _credential(model_routing.endpoint_api_key_env(endpoint), provider=provider)})
        return rows

    def _task_rows(self, endpoints: dict[str, dict], roles: dict[str, dict]) -> list[dict]:
        rows = []
        for name, role in roles.items():
            endpoint_name = role.get("endpoint")
            endpoint = endpoints.get(endpoint_name, {}) if isinstance(endpoint_name, str) else {}
            endpoint = endpoint if isinstance(endpoint, dict) else {}
            provider = endpoint.get("provider", "openai")
            rows.append({"name": name, "endpoint": endpoint_name if isinstance(endpoint_name, str) else None,
                "model": role.get("model") if isinstance(role.get("model"), str) else None,
                "max_tokens": _number(role.get("max_tokens")),
                "effective_max_tokens": ((_number(role.get("max_tokens")) or 4096) if name == "ideation" else None)
                    if provider != model_routing.CODEX_PROVIDER else None,
                "timeout": _number(role.get("timeout"), _number(endpoint.get("timeout"),
                    model_routing.DEFAULT_ENDPOINT_TIMEOUT)),
                "requires": _strings(role.get("requires")),
                "credential": _credential(role.get("api_key_env") or model_routing.endpoint_api_key_env(endpoint),
                                          provider=provider)})
        return rows

    def models(self) -> dict:
        endpoints, roles = self._settings_dicts()
        for name in _REQUIRED_ROLES:
            roles.setdefault(name, {})
        view = {"roles": self._task_rows(endpoints, roles), "endpoints": self._endpoint_rows(endpoints)}
        return _scrub_credentials(view, view["endpoints"] + view["roles"])

    def editor(self) -> dict:
        """Lossless editable projection: configured values or None, never a default."""
        endpoints, roles = self._settings_dicts()
        endpoint_rows = {name: {field: endpoint.get({"provider": "provider", "base_url": "base_url",
            "api_key_env": "api_key_env", "timeout": "timeout"}[field])
            for field in _SERVER_FIELDS}
            for name, endpoint in endpoints.items()}
        for name, endpoint in endpoints.items():
            endpoint_rows[name]["provides"] = _strings(endpoint.get("provides"))
        task_names = sorted(set(roles) | set(_REQUIRED_ROLES))
        role_rows = {}
        for name in task_names:
            role = roles.get(name) or {}
            role_rows[name] = {field: role.get({"endpoint": "endpoint", "model": "model",
                "max_tokens": "max_tokens", "temperature": "temperature", "timeout": "timeout",
                "api_key_env": "api_key_env"}[field])
                for field in _TASK_FIELDS}
            role_rows[name]["requires"] = _strings(role.get("requires"))
        return {"revision": model_settings.revision(self.root),
                "roles": role_rows, "endpoints": endpoint_rows}

    def save_editor(self, expected_revision: str, roles: dict, endpoints: dict,
                    delete_servers: list[str] | None = None,
                    delete_tasks: list[str] | None = None) -> dict:
        """Apply a validated patch to the database rows atomically."""
        if not isinstance(roles, dict) or not isinstance(endpoints, dict):
            raise InvalidConfiguration("Only listed task and server entries may be edited.", [])
        if not any(roles.values()) and not any(endpoints.values()) and not delete_servers and not delete_tasks:
            raise InvalidConfiguration("Submit at least one changed field.", [])
        lock_path = self.root / "ui_data" / ".model_settings.lock"
        lock = FileLock(str(lock_path), timeout=3)
        try:
            with lock:
                return self._save_editor_locked(expected_revision, roles, endpoints,
                                                delete_servers or [], delete_tasks or [])
        except _LockTimeout as exc:
            raise EditorConflict("Another Studio save is in progress; try again.", [
                {"field": "expected_revision", "message": "Another save is in progress. Wait a moment and retry.", "code": "locked"}])

    def _save_editor_locked(self, expected_revision: str, roles: dict, endpoints: dict,
                            delete_servers: list[str], delete_tasks: list[str]) -> dict:
        current_revision = model_settings.revision(self.root)
        if current_revision != expected_revision:
            raise EditorConflict("The configuration changed elsewhere. Reload before saving.", [
                {"field": "expected_revision", "message": "The configuration was modified while you were editing. Reload to continue.", "code": "configuration_changed"},
            ])
        endpoints_now, tasks_now = self._settings_dicts()
        errors: list[dict] = []

        known_servers = set(endpoints_now)
        for name, changes in endpoints.items():
            if not isinstance(name, str) or not name.strip() or name != name.strip():
                errors.append({"field": "endpoints", "message": "Server names must be nonempty text.", "code": "unknown_entry"})
                continue
            if not isinstance(changes, dict):
                errors.append({"field": f"endpoints.{name}", "message": "Server entries must be mappings.", "code": "unknown_entry"})
                continue
            for field, value in changes.items():
                if field not in _SERVER_FIELDS:
                    errors.append({"field": f"endpoints.{name}.{field}", "message": "Unknown field.", "code": "unknown_field"})
                    continue
                if field == "provider":
                    if value is not None and value not in model_routing.ENDPOINT_PROVIDERS:
                        errors.append({"field": f"endpoints.{name}.provider", "message": "Unknown endpoint provider.", "code": "invalid_provider"})
                elif value is None:
                    continue
                elif field == "base_url":
                    self._check_field(errors, f"endpoints.{name}.{field}", value, "url")
                elif field == "api_key_env":
                    self._check_field(errors, f"endpoints.{name}.{field}", value, "env")
                elif field == "timeout":
                    self._check_field(errors, f"endpoints.{name}.{field}", value, "duration")

        for name in delete_servers or []:
            if name not in known_servers:
                errors.append({"field": f"endpoints.{name}", "message": "Unknown server.", "code": "unknown_entry"})
        for name in delete_tasks or []:
            if name not in tasks_now and name not in _REQUIRED_ROLES:
                errors.append({"field": f"roles.{name}", "message": "Unknown task.", "code": "unknown_entry"})
        if set(delete_servers or []) & set(delete_tasks or []):
            pass  # disjoint namespaces; no action needed

        # Final server state after applying patches (base_url default resolves at request time).
        final_servers: dict[str, dict] = {}
        for name, endpoint in endpoints_now.items():
            final_servers[name] = dict(endpoint)
        for name, changes in endpoints.items():
            if not isinstance(name, str) or not isinstance(changes, dict):
                continue
            target = final_servers.setdefault(name, {"provider": "openai", "base_url": None,
                                                     "api_key_env": None, "timeout": None,
                                                     "provides": ["text"]})
            for field, value in changes.items():
                if field == "provider" and value is not None:
                    target["provider"] = value
                elif field == "base_url":
                    if value is None:
                        target.pop("base_url", None)
                    else:
                        target["base_url"] = value
                elif field == "api_key_env":
                    if value is None:
                        target.pop("api_key_env", None)
                    else:
                        target["api_key_env"] = value
                elif field == "timeout":
                    if value is None:
                        target.pop("timeout", None)
                    else:
                        target["timeout"] = value
        for name, server in final_servers.items():
            provider = server.get("provider", "openai")
            if provider == model_routing.CODEX_PROVIDER:
                if any(server.get(key) not in (None, "") for key in ("base_url", "api_key_env")):
                    errors.append({"field": f"endpoints.{name}.base_url", "message": "Codex manages its server address and uses ChatGPT sign-in, not API-key overrides.", "code": "managed_by_provider"})
            elif not server.get("base_url") and provider not in (model_routing.CODEX_PROVIDER, model_routing.CBORG_PROVIDER):
                errors.append({"field": f"endpoints.{name}.base_url", "message": "Endpoint URL is required.", "code": "required"})

        # Final task state.
        final_tasks: dict[str, dict] = {}
        for name, task in tasks_now.items():
            final_tasks[name] = dict(task)
        for name, changes in roles.items():
            if not isinstance(name, str) or not isinstance(changes, dict):
                continue
            if name not in final_tasks and name not in _REQUIRED_ROLES:
                errors.append({"field": f"roles.{name}", "message": "Unknown task.", "code": "unknown_entry"})
                continue
            target = final_tasks.setdefault(name, {})
            for field, value in changes.items():
                if field == "endpoint":
                    if value is None:
                        target.pop("endpoint", None)
                    else:
                        target["endpoint"] = value
                elif field == "model":
                    if value is None:
                        target.pop("model", None)
                    else:
                        target["model"] = value
                elif value is None:
                    target.pop(field, None)
                else:
                    target[field] = value

        for name, task in final_tasks.items():
            endpoint_name = task.get("endpoint")
            if not endpoint_name:
                if name in _REQUIRED_ROLES and not delete_tasks:
                    continue  # unassigned required task; the run check reports it
                errors.append({"field": f"roles.{name}.endpoint", "message": "This field is required.", "code": "required"})
                continue
            if endpoint_name not in final_servers:
                errors.append({"field": f"roles.{name}.endpoint", "message": "Role endpoint must be an existing server.", "code": "invalid_endpoint_reference"})
                continue
            if not task.get("model"):
                errors.append({"field": f"roles.{name}.model", "message": "This field is required.", "code": "required"})
                continue
            server = final_servers[endpoint_name]
            provider = server.get("provider", "openai")
            if provider == model_routing.CODEX_PROVIDER and any(
                    task.get(key) is not None for key in ("max_tokens", "temperature", "api_key_env")):
                errors.append({"field": f"roles.{name}.max_tokens", "message": "Codex manages token limits, sampling, and ChatGPT credentials. Clear this override.", "code": "managed_by_provider"})
            missing = sorted(set(_strings(task.get("requires"))) - set(_strings(server.get("provides"))))
            if missing:
                errors.append({"field": f"roles.{name}", "message": "The selected server does not declare a required capability.", "code": "capability_mismatch"})

        if errors:
            raise InvalidConfiguration("The change was rejected.", errors)

        deleted_server_names = set(delete_servers or [])
        server_writes = {}
        for name, server in final_servers.items():
            if name in deleted_server_names:
                continue
            server_writes[name] = {
                "api_format": server.get("provider", "openai"),
                "address": server.get("base_url"),
                "credential_env": server.get("api_key_env"),
                "timeout": server.get("timeout"),
                "capabilities": _strings(server.get("provides")),
                "requires_user_message": bool(server.get("requires_user_message")),
            }
        task_writes = {}
        for name, task in final_tasks.items():
            if name in (delete_tasks or []):
                continue
            if not task.get("endpoint") or not task.get("model"):
                continue  # unassigned required task stays absent from the table
            task_writes[name] = {
                "server": task["endpoint"],
                "model": task["model"],
                "max_tokens": task.get("max_tokens"),
                "temperature": task.get("temperature"),
                "timeout": task.get("timeout"),
                "credential_env": task.get("api_key_env"),
                "requires": _strings(task.get("requires")),
            }
        model_settings.save_settings_atomic(self.root, server_writes, task_writes,
                                            delete_servers=delete_servers or [],
                                            delete_tasks=delete_tasks or [])
        return self.editor()

    def _check_field(self, errors: list[dict], field: str, value: object, kind: str) -> None:
        if kind == "url":
            if not isinstance(value, str) or not value:
                errors.append({"field": field, "message": "Endpoint URL is required.", "code": "required"})
                return
            try:
                parts = urlsplit(value)
            except ValueError:
                errors.append({"field": field, "message": "Endpoint URL is not a valid HTTP or HTTPS URL.", "code": "invalid_url"})
                return
            if parts.scheme not in ("http", "https") or not parts.hostname:
                errors.append({"field": field, "message": "Endpoint URL must use HTTP or HTTPS with a hostname.", "code": "invalid_url"})
                return
            try:
                if parts.port is not None and not 1 <= parts.port <= 65535:
                    raise ValueError
            except ValueError:
                errors.append({"field": field, "message": "Endpoint URL port is out of range.", "code": "invalid_url"})
                return
            if parts.username is not None or parts.password is not None or parts.fragment:
                errors.append({"field": field, "message": "Endpoint URL must not contain credentials or a fragment.", "code": "credential_bearing"})
                return
            if any(_SECRET_KEY.search(k) for k, _ in parse_qsl(parts.query)):
                errors.append({"field": field, "message": "Endpoint URL must not contain credential-bearing query fields.", "code": "credential_bearing"})
        elif kind == "env":
            if not isinstance(value, str) or not _ENV_NAME.fullmatch(value):
                errors.append({"field": field, "message": "Credential must name an environment variable.", "code": "invalid_env_name"})
        elif kind == "model":
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                errors.append({"field": field, "message": "Model identifier is required.", "code": "required"})
        elif kind == "tokens":
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                errors.append({"field": field, "message": "Output token limit must be a positive whole number.", "code": "invalid_number"})
        elif kind == "ratio":
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 2:
                errors.append({"field": field, "message": "Temperature must be a number between 0 and 2.", "code": "invalid_number"})
        elif kind == "duration":
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                errors.append({"field": field, "message": "Timeout must be a positive number of seconds.", "code": "invalid_number"})

    def endpoint_models(self, endpoint: str) -> dict:
        """List models one configured server currently advertises.

        Read-only and bounded like check(); never probes another server.
        """
        endpoints, _ = self._settings_dicts()
        if endpoint not in endpoints:
            raise KeyError("Unknown server.")
        row = next(row for row in self.models()["endpoints"] if row["id"] == endpoint)
        result = {"endpoint": endpoint, "checked_at": _now(),
                  "ok": False, "models": [], "error": None}
        try:
            available = model_routing.list_endpoint_models(endpoint, {"endpoints": endpoints}, timeout=_PROBE_TIMEOUT)
            names = _strings(available)
            secrets = {os.environ.get(row["credential"]["env"], "") for row in self.models()["endpoints"] + self.models()["roles"]
                       if row["credential"]["env"]}
            result["models"] = list(dict.fromkeys(
                name for name in names if name.strip() and not any(secret and secret in name for secret in secrets)
            ))
            result["ok"] = True
        except Exception:
            # SDK exception strings may contain authorization headers or raw URLs.
            result["error"] = (
                "Codex model listing failed. Check Codex sign-in above and retry."
                if row.get("provider") == model_routing.CODEX_PROVIDER else
                "Model listing failed. Check the server, credentials, and availability."
            )
        result["checked_at"] = _now()
        return result

    def diagnostic_assignment(self, task: str) -> dict:
        """Validate and snapshot one explicit task assignment without probing it."""
        endpoints, roles = self._settings_dicts()
        selected = roles.get(task) if isinstance(task, str) and task.strip() else None
        if not isinstance(selected, dict):
            raise ValueError("The selected crash-assistant task is not configured.")
        endpoint_name = selected.get("endpoint")
        endpoint = endpoints.get(endpoint_name) if isinstance(endpoint_name, str) else None
        if not endpoint_name or not isinstance(endpoint, dict):
            raise ValueError("The selected crash-assistant server is not configured.")
        model = selected.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("The selected crash-assistant model is not configured.")
        model_routing.validate_provider_settings(endpoint, selected)
        provider = model_routing.endpoint_provider(endpoint)
        base_url = model_routing.endpoint_base_url(endpoint)
        if not isinstance(base_url, str) or not _url(base_url):
            raise ValueError("The selected crash-assistant server URL is invalid.")
        if "text" not in _strings(endpoint.get("provides")):
            raise ValueError("The selected server does not declare text capability.")
        max_tokens = selected["max_tokens"] if "max_tokens" in selected else 4096
        if type(max_tokens) is not int or max_tokens <= 0:
            raise ValueError("The crash-assistant token budget must be a positive integer.")
        max_tokens = min(max_tokens, 32768)
        temperature = selected["temperature"] if "temperature" in selected else 0.2
        if type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise ValueError("The crash-assistant temperature must be between 0 and 2.")
        timeout = selected.get("timeout")
        if timeout is None:
            timeout = endpoint.get("timeout")
        if timeout is None:
            timeout = model_routing.DEFAULT_ENDPOINT_TIMEOUT
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("The crash-assistant timeout must be a positive number.")
        timeout = min(float(timeout), 120.0)
        credential_envs: set[str] = set()
        for endpoint_row in endpoints.values():
            credential_env = model_routing.endpoint_api_key_env(endpoint_row)
            if credential_env:
                credential_envs.add(credential_env)
        if selected.get("api_key_env"):
            credential_envs.add(selected["api_key_env"])
        api_key_env = selected.get("api_key_env") or model_routing.endpoint_api_key_env(endpoint)
        if api_key_env is not None and (not isinstance(api_key_env, str) or not _ENV_NAME.fullmatch(api_key_env)):
            raise ValueError("The crash-assistant credential must name an environment variable.")
        if provider == model_routing.CODEX_PROVIDER:
            max_tokens = None
            temperature = None
        assignment = {
            "role": task, "endpoint": endpoint_name,
            "base_url": base_url, "provider": provider, "model": model.strip(), "api_key_env": api_key_env,
            "max_tokens": max_tokens, "temperature": float(temperature) if temperature is not None else None, "timeout": timeout,
            "credential_envs": tuple(sorted(credential_envs)),
        }
        return MappingProxyType(assignment)

    def check(self) -> dict:
        endpoints, roles = self._settings_dicts()
        for name in _REQUIRED_ROLES:
            roles.setdefault(name, {})
        view = {"roles": self._task_rows(endpoints, roles), "endpoints": self._endpoint_rows(endpoints)}
        checked_at = _now()
        credentials = {os.environ.get(row["credential"]["env"], "") for row in view["endpoints"] + view["roles"]
                       if row["credential"]["env"]}
        def probe(endpoint):
            result = {"id": endpoint["id"], "label": endpoint["label"], "url": endpoint["url"],
                      "checked_at": _now(), "ok": False, "models": [], "roles": [], "error": None}
            try:
                # Probing resolves against exactly these settings, never live edits.
                available = model_routing.list_endpoint_models(endpoint["id"], {"endpoints": endpoints}, timeout=_PROBE_TIMEOUT)
                result["models"] = _strings(available)
                for index, model in enumerate(result["models"]):
                    for secret in credentials:
                        if secret:
                            model = model.replace(secret, "[redacted]")
                    result["models"][index] = model
                for role in view["roles"]:
                    if role["endpoint"] != endpoint["id"]:
                        continue
                    missing = sorted(set(role["requires"]) - set(endpoint["capabilities"]))
                    result["roles"].append({"name": role["name"], "model": role["model"],
                        "listed": bool(role["model"] and role["model"] in available),
                        "capabilities_declared": not missing, "missing_capabilities": missing})
                result["ok"] = all(row["listed"] and row["capabilities_declared"] for row in result["roles"]) if result["roles"] else True
                if not result["ok"]:
                    result["error"] = "A configured model is not listed or a required capability is not declared."
            except Exception:
                # SDK exception strings may contain authorization headers or raw URLs.
                result["error"] = "Model listing failed. Check the server, credentials, and availability."
            result["checked_at"] = _now()
            return result
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(probe, view["endpoints"]))
        assigned = {row["id"] for row in view["endpoints"]}
        valid_roles = all(role["endpoint"] in assigned and role["model"] for role in view["roles"] if role["endpoint"] is not None)
        unassigned = [role["name"] for role in view["roles"] if role["endpoint"] is None and role["name"] in _REQUIRED_ROLES]
        return {"checked_at": checked_at,
                "ok": bool(valid_roles and not unassigned and all(row["ok"] for row in results) and results),
                "unassigned_tasks": unassigned, "endpoints": results}

    def prerequisites(self) -> dict:
        tools = []
        for name in ("pdflatex", "bibtex", "pdftotext"):
            try:
                path = shutil.which(name) if name == "pdftotext" else resolve_tex_tool(name)
                available = bool(path)
            except FileNotFoundError:
                available = False
            message = None if available else (f"{name} was not found on PATH." if name == "pdftotext"
                else f"{name} was not found. Check AI_SCIENTIST_TEX_BIN_DIR or your TeX installation on PATH.")
            tools.append({"name": name, "available": available, "error": message})
        return {"ok": all(tool["available"] for tool in tools), "tools": tools}

    def validate_experiment(self, cfg: dict) -> list[str]:
        errors = []
        try:
            view = self.models()
            names = {role["name"] for role in view["roles"]}
            unassigned = {role["name"] for role in view["roles"] if not role["endpoint"] or not role["model"]}
            missing = sorted(set(_REQUIRED_ROLES) - names)
            if missing:
                errors.append("Role configuration is missing required tasks: " + ", ".join(missing) + ".")
            unassigned_missing = sorted(unassigned & set(_REQUIRED_ROLES))
            if unassigned_missing:
                errors.append("These research tasks have no model assigned yet: " + ", ".join(unassigned_missing) + ".")
        except (ValueError, KeyError):
            errors.append("The model settings are unavailable or invalid; credentials must use environment variables.")
        if cfg.get("exp_name") != "run":
            errors.append("The experiment configuration must set exp_name: run for this paper workflow.")
        agent, execution = cfg.get("agent"), cfg.get("exec")
        if not isinstance(agent, dict) or not isinstance(execution, dict):
            errors.append("The experiment configuration requires agent and exec mappings.")
        else:
            stages, seeds = agent.get("stages") or {}, agent.get("multi_seed_eval") or {}
            if not isinstance(stages, dict) or not isinstance(seeds, dict):
                errors.append("Stage limits and multi-seed settings must be mappings.")
            else:
                counts = [agent.get("num_workers"), seeds.get("num_seeds")]
                counts.extend(stages.get(f"stage{i}_max_iters", agent.get("steps")) for i in range(1, 5))
                if any(type(value) is not int or value < 1 for value in counts):
                    errors.append("Worker count, seeds, and all stage iteration limits must be positive integers.")
            timeout = _number(execution.get("timeout"))
            if timeout is None or timeout <= 0:
                errors.append("Execution timeout must be a positive number.")
        errors.extend(tool["error"] for tool in self.prerequisites()["tools"] if not tool["available"])
        if not errors:
            result = self.check()
            errors.extend(f"Server {endpoint['label']}: {endpoint['error']}" for endpoint in result["endpoints"]
                          if not endpoint["ok"])
            if result["unassigned_tasks"]:
                errors.append("Assign models to these research tasks before running: " + ", ".join(result["unassigned_tasks"]) + ".")
            if not result["ok"] and not errors:
                errors.append("Task assignments must reference configured servers and served model IDs.")
        return errors


def _load(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as stream:
            value = yaml.safe_load(stream)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("The selected configuration could not be read as YAML.") from exc
    if not isinstance(value, dict):
        raise ValueError("The selected configuration must be a YAML mapping.")
    return value


def _environment_credentials_only(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Configuration keys must be text.")
            if _SECRET_KEY.search(key) and key not in ("max_tokens", "max_completion_tokens") and item not in (None, ""):
                if key.endswith("_env"):
                    if not isinstance(item, str) or not _ENV_NAME.fullmatch(item):
                        raise ValueError("Credential settings must name environment variables.")
                else:
                    raise ValueError("Inline credentials are not supported; use environment-variable references.")
            if key == "base_url":
                if not _url(item):
                    raise ValueError("Endpoint base URLs must be valid HTTP or HTTPS URLs.")
                parts = urlsplit(item)
                if parts.username is not None or parts.password is not None or any(
                    _SECRET_KEY.search(k) for k, _ in parse_qsl(parts.query)
                ):
                    raise ValueError("Endpoint URLs must not contain credentials; use environment-variable references.")
            _environment_credentials_only(item)
    elif isinstance(value, list):
        for item in value:
            _environment_credentials_only(item)


def _scrub_credentials(view: dict, credential_rows: list[dict]) -> dict:
    """Remove any configured credential value accidentally embedded in a display field."""
    secrets = {os.environ.get(row["credential"]["env"], "") for row in credential_rows
               if row["credential"]["env"]}
    def scrub(value):
        if isinstance(value, str):
            for secret in secrets:
                if secret:
                    value = value.replace(secret, "[redacted]")
            return value
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, dict):
            return {key: (item if key == "credential" else scrub(item)) for key, item in value.items()}
        return value
    return scrub(view)


def _id(kind: str, path: Path) -> str:
    return kind + "_" + hashlib.sha256(str(path).encode()).hexdigest()[:24]



def assistant_settings_view(saved: dict) -> dict:
    """Safe display metadata for saved assistant settings; never base URLs or values."""
    assignment = saved.get("assignment") if isinstance(saved.get("assignment"), dict) else None
    if assignment is None:
        return {"enabled": False, "role": None, "model": None, "endpoint": None,
                "max_tokens": None, "timeout": None, "credential": None, "repository": "jj-link/AI-Scientist-v2"}
    view = {
        "enabled": bool(saved.get("enabled")),
        "role": assignment.get("role"),
        "model": assignment.get("model"),
        "endpoint": assignment.get("endpoint"),
        "max_tokens": assignment.get("max_tokens"),
        "timeout": assignment.get("timeout"),
        "provider": assignment.get("provider", "openai"),
        "credential": _credential(assignment.get("api_key_env"), provider=assignment.get("provider", "openai")),
        "repository": "jj-link/AI-Scientist-v2",
    }
    secrets = {os.environ.get(name, "") for name in assignment.get("credential_envs", []) if isinstance(name, str)}
    def scrub(value):
        if isinstance(value, str):
            for secret in secrets:
                if secret:
                    value = value.replace(secret, "[redacted]")
            return value
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, dict):
            return {key: (item if key == "credential" else scrub(item)) for key, item in value.items()}
        return value
    return scrub(view)
