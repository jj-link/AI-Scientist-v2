"""Focused tests for self-hosted runtime compatibility fixes."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_scientist import model_routing  # noqa: E402
from ai_scientist.llm import AVAILABLE_LLMS, get_available_llms  # noqa: E402


def test_get_available_llms_appends_roles(monkeypatch):
    existing = Path(__file__)
    monkeypatch.setattr(model_routing, "role_config_path", lambda: existing)
    monkeypatch.setattr(
        model_routing,
        "load_role_config",
        lambda: {"roles": {"ideation": {}, "writeup": {}}},
    )
    available = get_available_llms()
    assert "role/ideation" in available
    assert "role/writeup" in available


def test_get_available_llms_keeps_legacy_list_intact(monkeypatch):
    existing = Path(__file__)
    monkeypatch.setattr(model_routing, "role_config_path", lambda: existing)
    monkeypatch.setattr(
        model_routing,
        "load_role_config",
        lambda: {"roles": {"ideation": {}, "writeup": {}}},
    )
    available = get_available_llms()
    assert available[: len(AVAILABLE_LLMS)] == AVAILABLE_LLMS
    # The static list itself must never be mutated.
    assert AVAILABLE_LLMS[0] == "claude-3-5-sonnet-20240620"
    assert not any(entry.startswith("role/") for entry in AVAILABLE_LLMS)


def test_get_available_llms_missing_config_returns_legacy(monkeypatch):
    monkeypatch.setattr(
        model_routing,
        "role_config_path",
        lambda: Path("does/not/exist/ais_roles.yaml"),
    )
    assert get_available_llms() == AVAILABLE_LLMS


def test_get_available_llms_malformed_config_raises(monkeypatch):
    existing = Path(__file__)
    monkeypatch.setattr(model_routing, "role_config_path", lambda: existing)

    def malformed():
        raise model_routing.RoleConfigError("Role config must be a mapping.")

    monkeypatch.setattr(model_routing, "load_role_config", malformed)
    with pytest.raises(model_routing.RoleConfigError, match="must be a mapping"):
        get_available_llms()


def make_completion(
    text="ok", finish_reason="stop", reasoning_content=None, tool_calls=None
):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=text,
                    reasoning_content=reasoning_content,
                    tool_calls=tool_calls,
                ),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2),
        system_fingerprint="fp",
        model="served",
        created=1,
    )
class FakeClient:
    def __init__(self, completion):
        self.calls = []
        outer = self

        class Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return completion

        class Chat:
            completions = Completions()

        self.chat = Chat()


@pytest.fixture()
def flagged_cfg(tmp_path, monkeypatch):
    cfg = {
        "endpoints": {
            "local": {
                "base_url": "http://localhost:8000/v1",
                "requires_user_message": True,
            },
            "spark": {"base_url": "http://localhost:18888/v1"},
        },
        "roles": {
            "citation": {"endpoint": "local", "model": "qwen", "max_tokens": 4096},
            "writeup": {"endpoint": "spark", "model": "big", "max_tokens": 8192},
        },
    }
    path = tmp_path / "roles.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.setenv(model_routing.ROLE_CONFIG_ENV, str(path))
    monkeypatch.setenv(model_routing.REQUEST_LOG_ENV, str(tmp_path / "req.jsonl"))
    model_routing._cache.clear()


def run_query(monkeypatch, model, system_message, user_message):
    import ai_scientist.treesearch.backend.backend_openai as backend

    client = FakeClient(make_completion())
    monkeypatch.setattr(backend, "get_ai_client", lambda m, max_retries=0: client)
    out, req_time, in_tok, out_tok, info = backend.query(
        system_message, user_message, model=model
    )
    assert out == "ok"
    return client.calls


def test_flagged_endpoint_system_only_becomes_exact_user(flagged_cfg, monkeypatch):
    calls = run_query(monkeypatch, "role/citation", "Do the compiled task.", None)
    assert calls[0]["messages"] == [
        {"role": "user", "content": "Do the compiled task."}
    ]
    assert calls[0]["model"] == "qwen"
    # No dummy filler may be appended.
    assert "Proceed with the task" not in calls[0]["messages"][0]["content"]


def test_flagged_endpoint_both_messages_unchanged(flagged_cfg, monkeypatch):
    calls = run_query(monkeypatch, "role/citation", "sys", "user task")
    assert calls[0]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user task"},
    ]


def test_flagged_endpoint_without_content_raises(flagged_cfg, monkeypatch):
    import ai_scientist.treesearch.backend.backend_openai as backend

    client = FakeClient(make_completion())
    monkeypatch.setattr(backend, "get_ai_client", lambda m, max_retries=0: client)
    with pytest.raises(ValueError, match="requires task content"):
        backend.query(None, None, model="role/citation")
    assert client.calls == []


def test_unflagged_routed_endpoint_keeps_system_only(flagged_cfg, monkeypatch):
    calls = run_query(monkeypatch, "role/writeup", "Do the compiled task.", None)
    assert calls[0]["messages"] == [
        {"role": "system", "content": "Do the compiled task."}
    ]


def test_legacy_provider_request_unchanged(monkeypatch, tmp_path):
    import ai_scientist.treesearch.backend.backend_openai as backend

    monkeypatch.setenv(model_routing.REQUEST_LOG_ENV, str(tmp_path / "req.jsonl"))
    client = FakeClient(make_completion())
    monkeypatch.setattr(backend, "get_ai_client", lambda m, max_retries=0: client)
    backend.query("sys prompt", None, model="gpt-4o")
    assert client.calls[0]["messages"] == [
        {"role": "system", "content": "sys prompt"}
    ]
    assert client.calls[0]["model"] == "gpt-4o"


def _read_log(tmp_path):
    import json

    log = tmp_path / "req.jsonl"
    if not log.exists():
        return []
    return [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_llm_empty_selfhosted_reply_raises_and_logs(flagged_cfg, tmp_path):
    from ai_scientist.llm import get_response_from_llm

    for empty in (None, ""):
        client = FakeClient(
            make_completion(text=empty, finish_reason="length", reasoning_content="think")
        )
        with pytest.raises(ValueError, match="empty content") as excinfo:
            get_response_from_llm(
                prompt="p", client=client, model="role/citation", system_message="s"
            )
        message = str(excinfo.value)
        assert "role/citation" in message
        assert "qwen" in message
        assert "finish_reason='length'" in message
        assert "reasoning_content_chars=5" in message
        assert "tool_calls=no" in message
    records = _read_log(tmp_path)
    assert len(records) == 2
    assert all(record["ok"] is False for record in records)
    assert all("think" not in (record.get("error") or "") for record in records)


def test_llm_valid_text_logs_success(flagged_cfg, tmp_path):
    from ai_scientist.llm import get_response_from_llm

    client = FakeClient(make_completion(text="hello"))
    content, history = get_response_from_llm(
        prompt="p", client=client, model="role/citation", system_message="s"
    )
    assert content == "hello"
    records = _read_log(tmp_path)
    assert records[-1]["ok"] is True


def test_backend_empty_selfhosted_text_fails_with_metadata(
    flagged_cfg, monkeypatch, tmp_path
):
    import ai_scientist.treesearch.backend.backend_openai as backend

    completion = make_completion(text=None, reasoning_content="abc")
    client = FakeClient(completion)
    monkeypatch.setattr(backend, "get_ai_client", lambda m, max_retries=0: client)
    with pytest.raises(ValueError, match="empty content") as excinfo:
        backend.query("task", None, model="role/citation")
    message = str(excinfo.value)
    assert "role/citation" in message
    assert "qwen" in message
    assert "finish_reason='stop'" in message
    assert "reasoning_content_chars=3" in message
    assert "tool_calls=no" in message
    records = _read_log(tmp_path)
    assert records[-1]["ok"] is False
    assert "abc" not in records[-1]["error"]
    assert client.calls


def test_backend_empty_string_text_fails(flagged_cfg, monkeypatch, tmp_path):
    import ai_scientist.treesearch.backend.backend_openai as backend

    client = FakeClient(make_completion(text=""))
    monkeypatch.setattr(backend, "get_ai_client", lambda m, max_retries=0: client)
    with pytest.raises(ValueError, match="empty content"):
        backend.query("task", None, model="role/citation")
    assert _read_log(tmp_path)[-1]["ok"] is False


def test_backend_valid_text_logs_success(flagged_cfg, monkeypatch, tmp_path):
    run_query(monkeypatch, "role/citation", "task", None)
    records = _read_log(tmp_path)
    assert records[-1]["ok"] is True
