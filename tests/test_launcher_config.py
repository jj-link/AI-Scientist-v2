"""Launcher configuration edits preserve workload and isolate run outputs."""

import shutil
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import launch_scientist_bfts as launcher
from ai_scientist.treesearch.utils.config import load_cfg
from ai_scientist.ui.configs import Configs
from ai_scientist.ui.schemas import ExperimentRunSettings


def test_selected_workload_is_preserved_in_run_config():
    root = Path(__file__).parent / f"launcher_config_{uuid4().hex}"
    root.mkdir()
    try:
        source = root / "selected.yaml"
        workload = {
            "exp_name": "run",
            "num_workers": 2,
            "agent": {"stages": {"stage1_max_iters": 7}, "steps": 3},
            "exec": {"timeout": 19},
            "workspace_dir": "old-workspace",
            "log_dir": "old-logs",
            "data_dir": "old-data",
            "desc_file": "old-idea",
        }
        source.write_text(yaml.safe_dump(workload), encoding="utf-8")
        original = source.read_bytes()
        run = root / "run"
        run.mkdir()
        idea = run / "idea.json"
        idea.write_text('{"Name": "boundary"}', encoding="utf-8")
        args = launcher.parse_arguments(["--bfts-config", str(source)])

        result = Path(launcher.prepare_bfts_config(args, str(run), str(idea)))
        actual = yaml.safe_load(result.read_text(encoding="utf-8"))
        assert actual == {
            **workload,
            "workspace_dir": str(run),
            "log_dir": str(run / "logs"),
            "data_dir": str(run / "data"),
            "desc_file": str(idea),
        }
        assert Path(actual["log_dir"]).is_dir()
        assert Path(actual["data_dir"]).is_dir()
        assert source.read_bytes() == original
    finally:
        shutil.rmtree(root)


@pytest.mark.parametrize("timeout", [3600, 12.5])
def test_ui_timeout_snapshot_loads_without_truncation(timeout):
    root = Path(__file__).parent / f"launcher_config_{uuid4().hex}"
    root.mkdir()
    try:
        source = root / "bfts_config.yaml"
        source.write_bytes((Path(__file__).resolve().parents[1] / "bfts_config.yaml").read_bytes())
        settings = ExperimentRunSettings(
            num_workers=1, num_seeds=1, execution_timeout=timeout,
            stage_iterations={"stage1": 1, "stage2": 1, "stage3": 1, "stage4": 1},
        )
        configs = Configs(root)
        candidate = configs.experiment_config(
            configs.presets()["selected_bfts_config_id"], settings.model_dump(),
        )
        snapshot = root / "requested.yaml"
        snapshot.write_text(yaml.safe_dump(candidate), encoding="utf-8")
        run = root / "run"
        run.mkdir()
        idea = run / "idea.json"
        idea.write_text('{"Name": "timeout_boundary"}', encoding="utf-8")
        args = launcher.parse_arguments(["--bfts-config", str(snapshot)])

        prepared = Path(launcher.prepare_bfts_config(args, str(run), str(idea)))
        cfg = load_cfg(prepared)

        assert cfg.exec.timeout == timeout
    finally:
        shutil.rmtree(root)
