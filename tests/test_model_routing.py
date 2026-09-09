"""Offline tests for role-based self-hosted model routing.

These tests never touch the network: endpoint listing and validation are
monkeypatched. Live-endpoint smoke tests live in scripts/smoke_selfhosted.py.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_scientist import model_routing  # noqa: E402


@pytest.fixture()
def role_cfg(tmp_path, monkeypatch):
    cfg = {
        "endpoints": {
            "local": {
                "base_url": "http://localhost:8000/v1",
                "api_key_env": "LOCAL_MODEL_API_KEY",
                "provides": ["text", "function_calling", "vision"],
            },
            "spark": {
                "base_url": "http://spark:8888/v1",
                "api_key_env": "SPARK_MODEL_API_KEY",
                "provides": ["text", "vision"],
            },
        },
        "roles": {
            "writeup": {
                "endpoint": "spark",
                "model": "big-model",
                "max_tokens": 16384,
                "requires": ["text"],
            },
            "citation": {
                "endpoint": "local",
                "model": "small-model",
                "max_tokens": 4096,
            },
            "visual_feedback": {
                "endpoint": "spark",
                "model": "big-model",
                "max_tokens": 4096,
                "requires": ["vision"],
            },
        },
        "experiment_execution": {},
    }
    path = tmp_path / "model_settings.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv(model_routing.ROLE_CONFIG_ENV, str(path))
    model_routing._cache.clear()
    return path


def test_is_selfhosted_classification(role_cfg):
    assert model_routing.is_selfhosted("role/writeup")
    assert model_routing.is_selfhosted("selfhosted/local/small-model")
    for model in (
        "cborg/lbl/cborg-mini",
        "spark/deepseek-v4-flash-vision-exp",
        "ollama/llama4:16x17b",
        "gpt-4o-2024-11-20",
        "claude-3-5-sonnet-20241022",
    ):
        assert not model_routing.is_selfhosted(model)
        assert model_routing.parse_model(model) is None


def test_parse_role_keeps_identity(role_cfg):
    info = model_routing.parse_model("role/writeup")
    assert info["role"] == "writeup"
    assert info["endpoint"] == "spark"
    assert info["served_model"] == "big-model"
    assert info["settings"]["max_tokens"] == 16384
    # Different role sharing the same served model keeps its own settings.
    info_vlm = model_routing.parse_model("role/visual_feedback")
    assert info_vlm["role"] == "visual_feedback"
    assert info_vlm["served_model"] == "big-model"
    assert info_vlm["settings"]["max_tokens"] == 4096


def test_parse_selfhosted_direct(role_cfg):
    info = model_routing.parse_model("selfhosted/local/small-model")
    assert info["role"] is None
    assert info["endpoint"] == "local"
    assert info["served_model"] == "small-model"
    assert info["settings"] == {}


def test_served_model_for(role_cfg):
    assert model_routing.served_model_for("role/citation") == "small-model"
    assert (
        model_routing.served_model_for("selfhosted/spark/big-model") == "big-model"
    )
    with pytest.raises(model_routing.RoleConfigError):
        model_routing.served_model_for("gpt-4o-2024-11-20")


def test_unknown_role_fails_clearly(role_cfg):
    with pytest.raises(model_routing.RoleConfigError, match="Unknown role 'nope'"):
        model_routing.parse_model("role/nope")


def test_unknown_endpoint_fails_clearly(role_cfg):
    with pytest.raises(model_routing.RoleConfigError, match="Unknown endpoint"):
        model_routing.endpoint_config("selfhosted/doesnotexist/model")


def test_missing_role_config_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.setenv(
        model_routing.ROLE_CONFIG_ENV, str(tmp_path / "missing.json")
    )
    model_routing._cache.clear()
    with pytest.raises(
        model_routing.RoleConfigError, match="Model settings snapshot not found"
    ):
        model_routing.parse_model("role/writeup")


def test_role_settings_apply_at_request_time(role_cfg):
    assert model_routing.role_settings("role/writeup")["max_tokens"] == 16384
    assert model_routing.role_settings("role/citation")["max_tokens"] == 4096
    assert model_routing.role_settings("gpt-4o-2024-11-20") == {}


def test_validation_requires_model_on_endpoint(role_cfg, monkeypatch):
    monkeypatch.setattr(
        model_routing,
        "list_endpoint_models",
        lambda name, settings=None, **kwargs: ["big-model"] if name == "spark" else ["other"],
    )
    with pytest.raises(model_routing.RoleConfigError, match="not served by endpoint"):
        model_routing.validate_roles()


def test_validation_checks_declared_capabilities(tmp_path, role_cfg, monkeypatch):
    # spark does not declare function_calling; a role requiring it must fail.
    cfg = json.loads(role_cfg.read_text(encoding="utf-8"))
    cfg["roles"]["needy"] = {
        "endpoint": "spark",
        "model": "big-model",
        "requires": ["vision", "function_calling"],
    }
    role_cfg.write_text(json.dumps(cfg), encoding="utf-8")
    os.utime(role_cfg, None)
    monkeypatch.setattr(
        model_routing,
        "list_endpoint_models",
        lambda name, settings=None, **kwargs: ["big-model"] if name == "spark" else ["small-model"],
    )
    with pytest.raises(model_routing.RoleConfigError, match="function_calling"):
        model_routing.validate_roles()


def test_validation_success_summary(role_cfg, monkeypatch):
    monkeypatch.setattr(
        model_routing,
        "list_endpoint_models",
        lambda name, settings=None, **kwargs: ["big-model", "small-model"],
    )
    summary = model_routing.validate_roles()
    assert summary["writeup"]["endpoint"] == "spark"
    assert summary["citation"]["model"] == "small-model"


def test_request_log_records_role_endpoint_model_no_credentials(
    role_cfg, tmp_path, monkeypatch
):
    log_path = tmp_path / "requests.jsonl"
    monkeypatch.setenv(model_routing.REQUEST_LOG_ENV, str(log_path))
    monkeypatch.setenv("LOCAL_MODEL_API_KEY", "super-secret-key")
    model_routing.log_request("role/citation", ok=True, latency_ms=1.5)
    model_routing.log_request(
        "selfhosted/spark/big-model", ok=False, error="boom"
    )
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    record_role, record_direct = json.loads(lines[0]), json.loads(lines[1])
    assert record_role["role"] == "citation"
    assert record_role["endpoint"] == "local"
    assert record_role["served_model"] == "small-model"
    assert record_role["ok"] is True
    assert record_direct["role"] is None
    assert record_direct["endpoint"] == "spark"
    assert record_direct["ok"] is False
    assert "super-secret-key" not in log_path.read_text(encoding="utf-8")


def test_client_build_uses_env_credentials(role_cfg, monkeypatch):
    from openai import OpenAI

    monkeypatch.setenv("SPARK_MODEL_API_KEY", "sk-test")
    client = model_routing.create_selfhosted_client("role/writeup")
    assert isinstance(client, OpenAI)
    assert str(client.base_url).rstrip("/") == "http://spark:8888/v1"
    assert client.api_key == "sk-test"


def test_client_build_without_key_uses_placeholder(role_cfg, monkeypatch):
    monkeypatch.delenv("SPARK_MODEL_API_KEY", raising=False)
    client = model_routing.create_selfhosted_client("selfhosted/spark/big-model")
    assert client.api_key == "unused"


def test_config_cache_invalidated_on_mtime_change(role_cfg):
    assert model_routing.parse_model("role/writeup")["endpoint"] == "spark"
    cfg = json.loads(role_cfg.read_text(encoding="utf-8"))
    cfg["roles"]["writeup"]["endpoint"] = "local"
    role_cfg.write_text(json.dumps(cfg), encoding="utf-8")
    os.utime(role_cfg, None)  # ensure mtime bump on coarse filesystems
    assert model_routing.parse_model("role/writeup")["endpoint"] == "local"


def test_gpu_count_respects_visible_devices(monkeypatch):
    from ai_scientist.treesearch.parallel_agent import get_gpu_count

    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1")
    assert get_gpu_count() == 1
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0,1")
    assert get_gpu_count() == 2
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    # No nvidia-smi stub here: the fallback path must not crash.
    count = get_gpu_count()
    assert isinstance(count, int)


def test_per_role_api_key_overrides_endpoint(role_cfg, monkeypatch):
    from openai import OpenAI

    monkeypatch.setenv("SPARK_MODEL_API_KEY", "sk-endpoint-default")
    monkeypatch.setenv("REVIEW_KEY", "sk-role-override")
    monkeypatch.setenv("LOCAL_MODEL_API_KEY", "sk-local")

    # Two roles on the SAME endpoint, different key env vars.
    cfg = json.loads(role_cfg.read_text(encoding="utf-8"))
    cfg["roles"]["review_override"] = {
        "endpoint": "spark",
        "model": "big-model",
        "api_key_env": "REVIEW_KEY",
    }
    role_cfg.write_text(json.dumps(cfg), encoding="utf-8")
    model_routing._cache.clear()

    assert model_routing.create_selfhosted_client("role/writeup").api_key == (
        "sk-endpoint-default"
    )
    override = model_routing.create_selfhosted_client("role/review_override")
    assert isinstance(override, OpenAI)
    assert override.api_key == "sk-role-override"


def test_client_timeout_finite_and_overridable(role_cfg, monkeypatch):
    monkeypatch.delenv("SPARK_MODEL_API_KEY", raising=False)

    # Default is finite so a wedged endpoint fails clearly.
    assert model_routing.DEFAULT_ENDPOINT_TIMEOUT > 0
    default = model_routing.create_selfhosted_client("role/writeup")
    assert float(default.timeout) == model_routing.DEFAULT_ENDPOINT_TIMEOUT

    # Endpoint-level override.
    cfg = json.loads(role_cfg.read_text(encoding="utf-8"))
    cfg["endpoints"]["spark"]["timeout"] = 123.5
    role_cfg.write_text(json.dumps(cfg), encoding="utf-8")
    os.utime(role_cfg, None)
    assert float(
        model_routing.create_selfhosted_client("role/writeup").timeout
    ) == 123.5

    # Role-level override wins over the endpoint value.
    cfg["roles"]["writeup"]["timeout"] = 7
    role_cfg.write_text(json.dumps(cfg), encoding="utf-8")
    os.utime(role_cfg, None)
    assert float(model_routing.create_selfhosted_client("role/writeup").timeout) == 7.0


def test_endpoint_settings_returns_endpoint_mapping(role_cfg):
    settings = model_routing.endpoint_settings("role/citation")
    assert settings["base_url"] == "http://localhost:8000/v1"
    assert "requires_user_message" not in settings


def test_endpoint_settings_direct_selfhosted(role_cfg):
    settings = model_routing.endpoint_settings("selfhosted/spark/big-model")
    assert settings["api_key_env"] == "SPARK_MODEL_API_KEY"


def test_endpoint_settings_rejects_legacy_strings(role_cfg):
    with pytest.raises(model_routing.RoleConfigError, match="not a self-hosted"):
        model_routing.endpoint_settings("gpt-4o-2024-11-20")
