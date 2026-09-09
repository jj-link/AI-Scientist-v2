"""Configuration discovery, safe preset editing, and bounded availability checks."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import stat
from types import MappingProxyType
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

import io

from ruamel.yaml import YAML as _RoundTripYAML
from ruamel.yaml.error import YAMLError as _RuamelYAMLError
from ruamel.yaml.comments import CommentedMap as _CommentedMap
from filelock import FileLock, Timeout as _LockTimeout
import uuid as _uuid

from ai_scientist import model_routing
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


def _credential(value: object, *, provider: str = "openai") -> dict:
    if provider == model_routing.CODEX_PROVIDER:
        return {"env": None, "present": False, "method": "codex"}
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
        conflicts = self._provider_conflicts(cfg["roles"], cfg["endpoints"])
        if conflicts:
            raise InvalidConfiguration("The provider settings are invalid.", conflicts)
        return path

    def experiment_config(self, config_id: str, run_settings: dict) -> dict:
        """Load once and detach edited mappings so YAML aliases remain unrelated."""
        cfg = _load(self._path("bfts", config_id))
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

    # Editable fields are exactly these; anything else the YAML preserved verbatim.
    _ROLE_FIELDS = ("endpoint", "model", "max_tokens", "temperature", "timeout", "api_key_env")
    _ENDPOINT_FIELDS = ("base_url", "api_key_env", "timeout")

    def _read_role_bytes(self, config_id: str) -> tuple[Path, bytes]:
        """Resolved path plus the exact bytes a revision digest covers.

        Missing, unreadable, or non-YAML mappings surface as safe ValueError
        subclasses; the selected preset is never created or repaired here.
        """
        path = self._path("role", config_id)
        data = path.read_bytes()
        return path, data

    def editor(self, config_id: str) -> dict:
        """Lossless editable projection: configured values or None, never a default."""
        _, data = self._read_role_bytes(config_id)
        return self._editor_view(config_id, data)

    def _editor_tree(self, data: bytes) -> dict:
        try:
            tree = self._roundtrip_loader().load(data.decode("utf-8"))
        except (UnicodeError, _RuamelYAMLError) as exc:
            raise InvalidConfiguration("The selected configuration could not be parsed safely.", []) from exc
        if not isinstance(tree, dict):
            raise InvalidConfiguration("The selected configuration must be a YAML mapping.", [])
        seen: set[int] = set()
        def visit(value):
            if isinstance(value, (dict, list)):
                if id(value) in seen or getattr(value, "merge", None):
                    raise InvalidConfiguration("Shared YAML aliases or merge keys must be removed before editing.", [])
                seen.add(id(value))
                for item in value.values() if isinstance(value, dict) else value:
                    visit(item)
        visit(tree)
        _environment_credentials_only(tree)
        return tree

    def _editor_view(self, config_id: str, data: bytes) -> dict:
        cfg = self._editor_tree(data)
        endpoints = cfg.get("endpoints")
        roles = cfg.get("roles")
        if not isinstance(endpoints, dict) or not isinstance(roles, dict):
            raise InvalidConfiguration("Role configuration requires endpoints and roles mappings.", [])
        revision = hashlib.sha256(data).hexdigest()
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
        secrets = {os.environ.get(name, "") for name in credential_envs if name}
        secrets.discard("")
        def guarded(value: object) -> object:
            """Fail safely rather than return an editable redacted placeholder."""
            if isinstance(value, str):
                for secret in secrets:
                    if secret in value:
                        raise InvalidConfiguration("A configured credential value is stored in the selected preset; edit the file by hand.", [
                            {"field": "config_id", "message": "The selected configuration stores a credential value instead of an environment-variable name.",
                             "code": "embedded_secret"},
                        ])
                return value
            if isinstance(value, list):
                return [guarded(item) for item in value]
            if isinstance(value, dict):
                return {guarded(key): guarded(item) for key, item in value.items()}
            return value
        guarded(cfg)
        endpoint_rows: dict[str, dict] = {}
        for name, endpoint in endpoints.items():
            if not isinstance(name, str) or not isinstance(endpoint, dict):
                raise InvalidConfiguration("Each endpoint must be a named mapping.", [])
            endpoint_rows[name] = {field: endpoint.get(field) for field in self._ENDPOINT_FIELDS}
            endpoint_rows[name]["provides"] = _strings(endpoint.get("provides"))
            endpoint_rows[name]["provider"] = endpoint.get("provider", "openai")
        role_rows: dict[str, dict] = {}
        for name, role in roles.items():
            if not isinstance(name, str) or not isinstance(role, dict):
                raise InvalidConfiguration("Each role must be a named mapping.", [])
            role_rows[name] = {field: role.get(field) for field in self._ROLE_FIELDS}
            role_rows[name]["requires"] = _strings(role.get("requires"))
        return guarded({"config_id": config_id, "revision": revision,
                        "roles": role_rows, "endpoints": endpoint_rows})

    def _capability_conflicts(self, roles_tree: dict, endpoints_tree: dict) -> list[dict]:
        errors: list[dict] = []
        for name, role in roles_tree.items():
            if not isinstance(role, dict):
                continue
            endpoint_name = role.get("endpoint")
            if not isinstance(endpoint_name, str):
                continue
            required = set(_strings(role.get("requires")))
            endpoint = endpoints_tree.get(endpoint_name)
            provided = set(_strings(endpoint.get("provides"))) if isinstance(endpoint, dict) else set()
            missing = sorted(required - provided)
            if missing:
                errors.append({"field": f"roles.{name}", "message": "The selected endpoint does not declare a required capability.",
                               "code": "capability_mismatch"})
        return errors

    def _provider_conflicts(self, roles: dict, endpoints: dict) -> list[dict]:
        errors = []
        for name, endpoint in endpoints.items():
            if not isinstance(endpoint, dict):
                continue
            provider = endpoint.get("provider", "openai")
            if provider not in ("openai", model_routing.CODEX_PROVIDER):
                errors.append({"field": f"endpoints.{name}.provider", "message": "Unknown endpoint provider.", "code": "invalid_provider"})
            if provider == model_routing.CODEX_PROVIDER:
                for field in ("base_url", "api_key_env"):
                    if endpoint.get(field) is not None:
                        errors.append({"field": f"endpoints.{name}.{field}", "message": "Codex manages its server address and ChatGPT credentials.", "code": "managed_by_provider"})
        for name, role in roles.items():
            if not isinstance(role, dict):
                continue
            endpoint = endpoints.get(role.get("endpoint")) if isinstance(role.get("endpoint"), str) else None
            if isinstance(endpoint, dict) and endpoint.get("provider") == model_routing.CODEX_PROVIDER:
                for field in ("max_tokens", "temperature", "api_key_env"):
                    if role.get(field) is not None:
                        errors.append({"field": f"roles.{name}.{field}", "message": "Codex manages token limits, sampling, and ChatGPT credentials. Clear this override.", "code": "managed_by_provider"})
        return errors

    def _validate_editor_patch(self, roles: dict, endpoints: dict, configured_endpoints: dict, errors: list[dict]) -> None:
        """Field checks for submitted values before merged-file validation.

        Explicit null clears the field, so only concrete values are kind-checked.
        """
        for name, changes in endpoints.items():
            for field, value in changes.items():
                if value is None and (field != "base_url" or configured_endpoints.get(name, {}).get("provider") == model_routing.CODEX_PROVIDER):
                    continue
                if field == "base_url":
                    self._check_field(errors, f"endpoints.{name}.{field}", value, "url")
                elif field == "api_key_env":
                    self._check_field(errors, f"endpoints.{name}.{field}", value, "env")
                elif field == "timeout":
                    self._check_field(errors, f"endpoints.{name}.{field}", value, "duration")
        for name, changes in roles.items():
            for field, value in changes.items():
                if value is None and field not in ("endpoint", "model"):
                    continue
                if field == "endpoint":
                    if not isinstance(value, str) or value not in configured_endpoints:
                        errors.append({"field": f"roles.{name}.endpoint", "message": "Role endpoint must be an existing endpoint.", "code": "invalid_endpoint_reference"})
                elif field == "model":
                    self._check_field(errors, f"roles.{name}.{field}", value, "model")
                elif field == "api_key_env":
                    self._check_field(errors, f"roles.{name}.{field}", value, "env")
                elif field == "max_tokens":
                    self._check_field(errors, f"roles.{name}.{field}", value, "tokens")
                elif field == "temperature":
                    self._check_field(errors, f"roles.{name}.{field}", value, "ratio")
                elif field == "timeout":
                    self._check_field(errors, f"roles.{name}.{field}", value, "duration")

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

    def _apply_patch(self, patch: dict, target: dict, errors: list[dict], prefix: str, fields: tuple[str, ...]) -> None:
        for name, changes in patch.items():
            entry = target.get(name) if isinstance(name, str) else None
            if not isinstance(entry, dict) or not isinstance(changes, dict):
                continue
            for field, value in changes.items():
                if field not in fields:
                    errors.append({"field": f"{prefix}.{name}.{field}", "message": "Unknown field.", "code": "unknown_field"})
                    continue
                if value is None:
                    entry.pop(field, None)
                else:
                    entry[field] = value

    def _roundtrip_loader(self) -> _RoundTripYAML:
        loader = _RoundTripYAML(typ="rt", pure=False)
        loader.preserve_quotes = True
        return loader

    _LOCK_SUFFIX = ".studio.lock"

    def save_editor(self, config_id: str, expected_revision: str, roles: dict, endpoints: dict) -> dict:
        """Apply a validated patch and atomically rewrite the selected preset."""
        if not isinstance(roles, dict) or not isinstance(endpoints, dict):
            raise InvalidConfiguration("Only listed role and endpoint entries may be edited.", [])
        if not any(roles.values()) and not any(endpoints.values()):
            raise InvalidConfiguration("Submit at least one changed field.", [])
        path = self._path("role", config_id)
        lock_path = path.with_name(f".{path.name}{self._LOCK_SUFFIX}")
        lock = FileLock(str(lock_path), timeout=3)
        try:
            with lock:
                return self._save_editor_locked(config_id, path, expected_revision, roles, endpoints)
        except _LockTimeout as exc:
            raise EditorConflict("Another Studio save is in progress; try again.", [
                {"field": "expected_revision", "message": "Another save is in progress. Wait a moment and retry.", "code": "locked"}])

    def _save_editor_locked(self, config_id: str, path: Path, expected_revision: str, roles: dict, endpoints: dict) -> dict:
        data = path.read_bytes()
        revision = hashlib.sha256(data).hexdigest()
        if revision != expected_revision:
            raise EditorConflict("The configuration changed elsewhere. Reload before saving.", [
                {"field": "expected_revision", "message": "The configuration was modified while you were editing. Reload to continue.", "code": "configuration_changed"},
            ])
        errors: list[dict] = []
        before = self._editor_view(config_id, data)
        tree = self._editor_tree(data)
        roles_tree = tree.get("roles", None)
        endpoints_tree = tree.get("endpoints", None)
        if not isinstance(endpoints_tree, dict) or not isinstance(roles_tree, dict):
            raise InvalidConfiguration("Role configuration requires endpoints and roles mappings.", [])
        unknown_endpoint = [name for name in endpoints if not (isinstance(name, str) and name in endpoints_tree)]
        unknown_role = [name for name in roles if not (isinstance(name, str) and name in roles_tree)]
        for name in unknown_endpoint:
            errors.append({"field": f"endpoints.{name}", "message": "Unknown endpoint.", "code": "unknown_entry"})
        for name in unknown_role:
            errors.append({"field": f"roles.{name}", "message": "Unknown role.", "code": "unknown_entry"})
        self._validate_editor_patch(roles, endpoints, endpoints_tree, errors)
        self._apply_patch(roles, roles_tree, errors, "roles", self._ROLE_FIELDS)
        self._apply_patch(endpoints, endpoints_tree, errors, "endpoints", self._ENDPOINT_FIELDS)
        if errors:
            raise InvalidConfiguration("The change was rejected.", errors)
        self._validate_editor_patch(roles_tree, endpoints_tree, endpoints_tree, errors)
        for name, role in roles_tree.items():
            for field in ("endpoint", "model"):
                if field not in role:
                    errors.append({"field": f"roles.{name}.{field}", "message": "This field is required.", "code": "required"})
        for name, endpoint in endpoints_tree.items():
            if "base_url" not in endpoint and endpoint.get("provider") != model_routing.CODEX_PROVIDER:
                errors.append({"field": f"endpoints.{name}.base_url", "message": "Endpoint URL is required.", "code": "required"})
        errors.extend(self._capability_conflicts(roles_tree, endpoints_tree))
        errors.extend(self._provider_conflicts(roles_tree, endpoints_tree))
        if errors:
            raise InvalidConfiguration("The change was rejected.", errors)
        stream = io.StringIO()
        self._roundtrip_loader().dump(tree, stream)
        rendered = stream.getvalue().encode("utf-8")
        result = self._editor_view(config_id, rendered)
        if result["roles"] == before["roles"] and result["endpoints"] == before["endpoints"]:
            return before
        mode = stat.S_IMODE(path.stat().st_mode)
        if not mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError("The selected preset is read-only.")
        staged = path.with_name(f".{path.name}.{_uuid.uuid4().hex}.new")
        try:
            with staged.open("xb") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            staged.chmod(mode)
            recheck = path.read_bytes()
            if hashlib.sha256(recheck).hexdigest() != revision:
                raise EditorConflict("The configuration changed externally during saving. Reload to continue.", [
                    {"field": "expected_revision", "message": "The configuration was modified while you were editing. Reload to continue.", "code": "configuration_changed"},
                ])
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)
        return result

    def endpoint_models(self, config_id: str, endpoint: str) -> dict:
        """List models one configured endpoint currently advertises.

        Read-only and bounded like check(); never probes another preset.
        """
        view = self.models(config_id)
        row = next((row for row in view["endpoints"] if row["id"] == endpoint), None)
        if row is None:
            raise KeyError("Unknown endpoint.")
        result = {"config_id": config_id, "endpoint": endpoint, "checked_at": _now(),
                  "ok": False, "models": [], "error": None}
        try:
            available = model_routing.list_endpoint_models(
                endpoint, path=self._path("role", config_id), timeout=_PROBE_TIMEOUT)
            names = _strings(available)
            secrets = {os.environ.get(row["credential"]["env"], "") for row in view["endpoints"] + view["roles"]
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
                "Model listing failed. Check the endpoint, credentials, and server availability."
            )
        result["checked_at"] = _now()
        return result

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
            label = endpoint.get("label")
            endpoint_rows.append({"id": name, "label": label if isinstance(label, str) and label.strip() else name,
                "url": _url(model_routing.endpoint_base_url(endpoint)), "provider": endpoint.get("provider", "openai"),
                "timeout": _number(endpoint.get("timeout"), model_routing.DEFAULT_ENDPOINT_TIMEOUT),
                "capabilities": _strings(endpoint.get("provides")),
                "credential": _credential(endpoint.get("api_key_env"), provider=endpoint.get("provider", "openai"))})
        for name, role in roles.items():
            if not isinstance(name, str) or not isinstance(role, dict):
                raise ValueError("Each role must be a named mapping.")
            endpoint_name = role.get("endpoint")
            endpoint = endpoints.get(endpoint_name, {}) if isinstance(endpoint_name, str) else {}
            endpoint = endpoint if isinstance(endpoint, dict) else {}
            role_rows.append({"name": name, "endpoint": endpoint_name if isinstance(endpoint_name, str) else None,
                "model": role.get("model") if isinstance(role.get("model"), str) else None,
                "max_tokens": _number(role.get("max_tokens")),
                "effective_max_tokens": ((_number(role.get("max_tokens")) or 4096) if name == "ideation" else None)
                    if endpoint.get("provider") != model_routing.CODEX_PROVIDER else None,
                "timeout": _number(role.get("timeout"), _number(endpoint.get("timeout"),
                    model_routing.DEFAULT_ENDPOINT_TIMEOUT)),
                "requires": _strings(role.get("requires")),
                "credential": _credential(role.get("api_key_env") or endpoint.get("api_key_env"),
                                          provider=endpoint.get("provider", "openai"))})
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
        model_routing.validate_provider_settings(endpoint, selected)
        provider = model_routing.endpoint_provider(endpoint)
        base_url = model_routing.endpoint_base_url(endpoint)
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
        if provider == model_routing.CODEX_PROVIDER:
            max_tokens = None
            temperature = None
        assignment = {
            "config_id": config_id, "role": role, "endpoint": endpoint_name,
            "base_url": base_url, "provider": provider, "model": model.strip(), "api_key_env": api_key_env,
            "max_tokens": max_tokens, "temperature": float(temperature) if temperature is not None else None, "timeout": timeout,
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

    def validate_experiment(self, role_id: str, cfg: dict) -> list[str]:
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


