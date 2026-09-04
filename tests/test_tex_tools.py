"""Tests for portable TeX tool resolution."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_scientist.utils.latex import (  # noqa: E402
    TEX_BIN_DIR_ENV,
    resolve_tex_tool,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(TEX_BIN_DIR_ENV, raising=False)


def test_configured_directory_wins(monkeypatch):
    monkeypatch.setenv(TEX_BIN_DIR_ENV, str(Path("/fake/texbin")))
    monkeypatch.setattr(
        "ai_scientist.utils.latex.os.path.isfile", lambda p: p.endswith("pdflatex")
    )
    assert resolve_tex_tool("pdflatex") == str(Path("/fake/texbin/pdflatex"))


def test_configured_directory_resolves_windows_suffix(monkeypatch):
    monkeypatch.setenv(TEX_BIN_DIR_ENV, "/fake/texbin")
    monkeypatch.setattr(
        "ai_scientist.utils.latex.os.path.isfile",
        lambda p: p == os.path.join("/fake/texbin", "pdflatex.exe"),
    )
    assert resolve_tex_tool("pdflatex") == os.path.join("/fake/texbin", "pdflatex.exe")


def test_invalid_configured_directory_fails_clearly(monkeypatch):
    monkeypatch.setenv(TEX_BIN_DIR_ENV, "/fake/empty")
    monkeypatch.setattr("ai_scientist.utils.latex.os.path.isfile", lambda p: False)
    with pytest.raises(FileNotFoundError, match=TEX_BIN_DIR_ENV):
        resolve_tex_tool("pdflatex")


def test_path_resolution_when_no_directory_configured(monkeypatch):
    monkeypatch.setattr(
        "ai_scientist.utils.latex.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    assert resolve_tex_tool("bibtex") == "/usr/bin/bibtex"


def test_missing_everywhere_fails_with_install_hint(monkeypatch):
    monkeypatch.setattr("ai_scientist.utils.latex.shutil.which", lambda name: None)
    with pytest.raises(FileNotFoundError, match="Install a TeX distribution"):
        resolve_tex_tool("pdflatex")


@pytest.fixture()
def mocked_compile(monkeypatch):
    """Stub tool resolution, subprocess.run, and PDF move for both writeups."""
    import ai_scientist.perform_icbinb_writeup as icbinb
    import ai_scientist.perform_writeup as writeup

    run_calls = []
    for module in (icbinb, writeup):
        monkeypatch.setattr(
            module,
            "resolve_tex_tool",
            lambda name: f"/fake/texbin/{name}",
        )
        monkeypatch.setattr(
            module.subprocess,
            "run",
            lambda command, **kwargs: run_calls.append(command)
            or SimpleNamespace(stdout="", stderr=""),
        )
        monkeypatch.setattr(module.shutil, "move", lambda src, dst: None)
    return run_calls


def test_icbinb_command_list_uses_resolved_paths(mocked_compile, tmp_path):
    icbinb = sys.modules["ai_scientist.perform_icbinb_writeup"]
    icbinb.compile_latex(str(tmp_path), str(tmp_path / "out.pdf"), timeout=5)
    executables = {command[0] for command in mocked_compile}
    assert executables == {"/fake/texbin/pdflatex", "/fake/texbin/bibtex"}


def test_perform_writeup_command_lists_use_resolved_paths(mocked_compile, tmp_path):
    writeup = sys.modules["ai_scientist.perform_writeup"]
    writeup.compile_latex(str(tmp_path), str(tmp_path / "out.pdf"), timeout=5)
    executables = {command[0] for command in mocked_compile}
    assert executables == {"/fake/texbin/pdflatex", "/fake/texbin/bibtex"}

    mocked_compile.clear()
    assert writeup.detect_pages_before_impact(str(tmp_path), timeout=5) is None
    executables = {command[0] for command in mocked_compile}
    assert executables == {"/fake/texbin/pdflatex", "/fake/texbin/bibtex"}
