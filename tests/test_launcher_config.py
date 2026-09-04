"""Focused tests for launcher BFTS configuration selection."""

import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import launch_scientist_bfts as launcher  # noqa: E402


def test_default_bfts_config_selected(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["launch_scientist_bfts.py"])
    args = launcher.parse_arguments()
    assert args.bfts_config == "bfts_config.yaml"


def test_explicit_bfts_config_reaches_editor_unchanged(monkeypatch):
    calls = []

    def fake_edit(config_path, idea_dir, idea_path_json):
        calls.append((config_path, idea_dir, idea_path_json))
        return "edited-config-path"

    monkeypatch.setattr(launcher, "edit_bfts_config_file", fake_edit)
    args = argparse.Namespace(bfts_config="bfts_config.acceptance.yaml")
    result = launcher.prepare_bfts_config(args, "idea-dir", "idea.json")
    assert result == "edited-config-path"
    assert calls == [("bfts_config.acceptance.yaml", "idea-dir", "idea.json")]
