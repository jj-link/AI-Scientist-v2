"""Launcher configuration edits preserve workload and isolate run outputs."""

import shutil
import sys
from pathlib import Path
from uuid import uuid4

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import launch_scientist_bfts as launcher


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
