"""Read-only configuration discovery and explicit, bounded availability checks."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
from types import MappingProxyType
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

from ai_scientist import model_routing
from ai_scientist.utils.latex import resolve_tex_tool

_PROBE_TIMEOUT = 5.0
_SECRET_KEY = re.compile(r"(?:api[_-]?key|token|secret|password|passwd|authorization|credential|signature)", re.I)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_REQUIRED_ROLES = ("ideation", "experiment_code", "experiment_feedback", "visual_feedback",
                   "findings_synthesis", "tree_scoring", "plot_generation", "citation",
                   "writeup", "writeup_small", "review", "report")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(kind: str, path: Path) -> str:
    return kind + "_" + hashlib.sha256(str(path).encode()).hexdigest()[:24]


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


def _credential(value: object) -> dict:
    name = value if isinstance(value, str) and _ENV_NAME.fullmatch(value) else None
    return {"env": name, "present": bool(name and os.environ.get(name))}


def _strings(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _number(value: object, default: int | float | None = None):
    return value if type(value) in (int, float) and math.isfinite(value) else default


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


class Configs:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def _paths(self, kind: str) -> tuple[dict[str, Path], str]:
        pattern, filename = (("ais_roles*.yaml", "ais_roles.yaml") if kind == "role"
                             else ("bfts_config*.yaml", "bfts_config.yaml"))
        paths = {path.resolve() for path in self.root.glob(pattern) if path.is_file()}
        override = os.environ.get(model_routing.ROLE_CONFIG_ENV) if kind == "role" else None
        selected = Path(override).expanduser() if override else self.root / filename
        if not selected.is_absolute():
            selected = self.root / selected
        selected = selected.resolve()
        # A missing environment-selected file stays selected: no silent fallback.
        paths.add(selected)
        return {_id(kind, path): path for path in sorted(paths)}, _id(kind, selected)

    def _path(self, kind: str, config_id: str) -> Path:
        paths, _ = self._paths(kind)
        if config_id not in paths:
            raise KeyError("Unknown configuration ID.")
        return paths[config_id]

    def role_path(self, config_id: str) -> Path:
        path = self._path("role", config_id)
        cfg = _load(path)
        _environment_credentials_only(cfg)
        if not isinstance(cfg.get("endpoints"), dict) or not isinstance(cfg.get("roles"), dict):
            raise ValueError("Role configuration requires endpoints and roles mappings.")
        return path

    def bfts_path(self, config_id: str) -> Path:
        path = self._path("bfts", config_id)
        _environment_credentials_only(_load(path))
        return path

    def presets(self) -> dict:
        roles, selected_role = self._paths("role")
        workloads, selected_bfts = self._paths("bfts")
        role_options = [{"id": key, "label": path.name if path.parent == self.root else
                         "Environment-selected configuration"} for key, path in roles.items()]
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
        return {"role_configs": role_options, "bfts_configs": bfts_options,
                "selected_role_config_id": selected_role, "selected_bfts_config_id": selected_bfts}

    def models(self, config_id: str) -> dict:
        # Do not use describe_assignment/parse_model: they probe or use global selection.
        cfg = _load(self._path("role", config_id))
        endpoints = cfg.get("endpoints")
        roles = cfg.get("roles")
        if not isinstance(endpoints, dict) or not isinstance(roles, dict):
            raise ValueError("Role configuration requires endpoints and roles mappings.")
        endpoint_rows = []
        role_rows = []
        for name, endpoint in endpoints.items():
            if not isinstance(name, str) or not isinstance(endpoint, dict):
                raise ValueError("Each endpoint must be a named mapping.")
            endpoint_rows.append({"id": name, "label": name, "url": _url(endpoint.get("base_url")),
                "timeout": _number(endpoint.get("timeout"), model_routing.DEFAULT_ENDPOINT_TIMEOUT),
                "capabilities": _strings(endpoint.get("provides")),
                "credential": _credential(endpoint.get("api_key_env"))})
        for name, role in roles.items():
            if not isinstance(name, str) or not isinstance(role, dict):
                raise ValueError("Each role must be a named mapping.")
            endpoint_name = role.get("endpoint")
            endpoint = endpoints.get(endpoint_name, {}) if isinstance(endpoint_name, str) else {}
            endpoint = endpoint if isinstance(endpoint, dict) else {}
            role_rows.append({"name": name, "endpoint": endpoint_name if isinstance(endpoint_name, str) else None,
                "model": role.get("model") if isinstance(role.get("model"), str) else None,
                "max_tokens": _number(role.get("max_tokens")),
                "effective_max_tokens": (_number(role.get("max_tokens")) or 4096) if name == "ideation" else None,
                "timeout": _number(role.get("timeout"), _number(endpoint.get("timeout"),
                    model_routing.DEFAULT_ENDPOINT_TIMEOUT)),
                "requires": _strings(role.get("requires")),
                "credential": _credential(role.get("api_key_env") or endpoint.get("api_key_env"))})
        view = {"config_id": config_id, "roles": role_rows, "endpoints": endpoint_rows}
        # Also remove any configured credential value accidentally embedded in a display field.
        secrets = {os.environ.get(row["credential"]["env"], "") for row in endpoint_rows + role_rows
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

    def diagnostic_assignment(self, config_id: str, role: str) -> dict:
        """Validate and snapshot one explicit text assignment without probing it."""
        cfg = _load(self._path("role", config_id))
        _environment_credentials_only(cfg)
        endpoints = cfg.get("endpoints")
        roles = cfg.get("roles")
        if not isinstance(endpoints, dict) or not isinstance(roles, dict):
            raise ValueError("Role configuration requires endpoints and roles mappings.")
        selected = roles.get(role)
        if not isinstance(role, str) or not role.strip() or not isinstance(selected, dict):
            raise ValueError("The selected crash-assistant role is not configured.")
        endpoint_name = selected.get("endpoint")
        endpoint = endpoints.get(endpoint_name) if isinstance(endpoint_name, str) else None
        if not endpoint_name or not isinstance(endpoint, dict):
            raise ValueError("The selected crash-assistant endpoint is not configured.")
        model = selected.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("The selected crash-assistant model is not configured.")
        base_url = endpoint.get("base_url")
        if not isinstance(base_url, str) or not _url(base_url):
            raise ValueError("The selected crash-assistant endpoint URL is invalid.")
        if "text" not in _strings(endpoint.get("provides")):
            raise ValueError("The selected endpoint does not declare text capability.")
        max_tokens = selected["max_tokens"] if "max_tokens" in selected else 4096
        if type(max_tokens) is not int or max_tokens <= 0:
            raise ValueError("The crash-assistant token budget must be a positive integer.")
        max_tokens = min(max_tokens, 32768)
        temperature = selected["temperature"] if "temperature" in selected else 0.2
        if type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise ValueError("The crash-assistant temperature must be between 0 and 2.")
        timeout = selected.get("timeout", endpoint.get("timeout", model_routing.DEFAULT_ENDPOINT_TIMEOUT))
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("The crash-assistant timeout must be a positive number.")
        timeout = min(float(timeout), 120.0)
        credential_envs: set[str] = set()
        def collect(value: object) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if isinstance(key, str) and key.endswith("_env") and isinstance(item, str) and _ENV_NAME.fullmatch(item):
                        credential_envs.add(item)
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)
        collect(cfg)
        api_key_env = selected.get("api_key_env", endpoint.get("api_key_env"))
        if api_key_env is not None and (not isinstance(api_key_env, str) or not _ENV_NAME.fullmatch(api_key_env)):
            raise ValueError("The crash-assistant credential must name an environment variable.")
        assignment = {
            "config_id": config_id, "role": role, "endpoint": endpoint_name,
            "base_url": base_url, "model": model.strip(), "api_key_env": api_key_env,
            "max_tokens": max_tokens, "temperature": float(temperature), "timeout": timeout,
            "credential_envs": tuple(sorted(credential_envs)),
        }
        return MappingProxyType(assignment)

    def check(self, config_id: str) -> dict:
        path = self.role_path(config_id)
        view = self.models(config_id)
        checked_at = _now()
        credentials = {os.environ.get(row["credential"]["env"], "") for row in view["endpoints"] + view["roles"]
                       if row["credential"]["env"]}
        def probe(endpoint):
            result = {"id": endpoint["id"], "label": endpoint["label"], "url": endpoint["url"],
                      "checked_at": _now(), "ok": False, "models": [], "roles": [], "error": None}
            try:
                # Integration adds these explicit parameters; never probe a different config.
                available = model_routing.list_endpoint_models(endpoint["id"], path=path, timeout=_PROBE_TIMEOUT)
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
                result["ok"] = all(row["listed"] and row["capabilities_declared"] for row in result["roles"])
                if not result["ok"]:
                    result["error"] = "A configured model is not listed or a required capability is not declared."
            except Exception:
                # SDK exception strings may contain authorization headers or raw URLs.
                result["error"] = "Model listing failed. Check the endpoint, credentials, and server availability."
            result["checked_at"] = _now()
            return result
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(probe, view["endpoints"]))
        assigned = {row["id"] for row in view["endpoints"]}
        valid_roles = all(role["endpoint"] in assigned and role["model"] for role in view["roles"])
        return {"config_id": config_id, "checked_at": checked_at,
                "ok": bool(results and valid_roles and all(row["ok"] for row in results)), "endpoints": results}

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

    def validate_experiment(self, role_id: str, bfts_id: str) -> list[str]:
        errors = []
        try:
            self.role_path(role_id)
            view = self.models(role_id)
            names = {role["name"] for role in view["roles"]}
            missing = sorted(set(_REQUIRED_ROLES) - names)
            if missing:
                errors.append("Role configuration is missing required roles: " + ", ".join(missing) + ".")
        except (ValueError, KeyError):
            errors.append("The selected role configuration is unavailable or invalid; credentials must use environment variables.")
        try:
            cfg = _load(self.bfts_path(bfts_id))
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
        except (ValueError, KeyError):
            errors.append("The selected experiment configuration is unavailable or invalid.")
        errors.extend(tool["error"] for tool in self.prerequisites()["tools"] if not tool["available"])
        if not errors:
            result = self.check(role_id)
            errors.extend(f"Endpoint {endpoint['label']}: {endpoint['error']}" for endpoint in result["endpoints"]
                          if not endpoint["ok"])
            if not result["ok"] and not errors:
                errors.append("Role assignments must reference configured endpoints and served model IDs.")
        return errors

def assistant_settings_view(saved: dict) -> dict:
    """Safe display metadata for saved assistant settings; never base URLs or values."""
    assignment = saved.get("assignment") if isinstance(saved.get("assignment"), dict) else None
    if assignment is None:
        return {"enabled": False, "config_id": None, "role": None, "model": None, "endpoint": None,
                "max_tokens": None, "timeout": None, "credential": None, "repository": "jj-link/AI-Scientist-v2"}
    view = {
        "enabled": bool(saved.get("enabled")),
        "config_id": assignment.get("config_id"),
        "role": assignment.get("role"),
        "model": assignment.get("model"),
        "endpoint": assignment.get("endpoint"),
        "max_tokens": assignment.get("max_tokens"),
        "timeout": assignment.get("timeout"),
        "credential": _credential(assignment.get("api_key_env")),
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


