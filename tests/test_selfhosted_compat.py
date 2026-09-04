"""Focused tests for self-hosted runtime compatibility fixes."""

import sys
from pathlib import Path

import pytest

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
