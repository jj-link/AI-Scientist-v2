"""Observable postprocessing and cancellation boundaries, without model calls."""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import launch_scientist_bfts as launcher
from ai_scientist import perform_plotting as plotting
from ai_scientist.progress import Cancelled, PipelineFailure


@pytest.fixture
def workspace():
    root = Path(__file__).parent / f"pipeline_outcomes_{uuid4().hex}"
    root.mkdir()
    try:
        yield root.resolve()
    finally:
        shutil.rmtree(root)


@pytest.fixture
def pipeline(workspace, monkeypatch):
    idea = workspace / "input.json"
    idea.write_text(json.dumps([{"Name": "boundary", "Title": "A question"}]), encoding="utf-8")
    config = workspace / "input.yaml"
    config.write_text("exp_name: run\n", encoding="utf-8")
    run = workspace / "run"
    run.mkdir()
    args = launcher.parse_arguments([
        "--load_ideas", str(idea), "--bfts-config", str(config),
        "--writeup-retries", "2",
    ])
    monkeypatch.setattr(launcher.model_routing, "validate_roles", lambda: {})
    monkeypatch.setattr(launcher.model_routing, "role_config_path", lambda: workspace / "absent.yaml")
    monkeypatch.setattr(launcher, "get_available_gpus", lambda: [])
    monkeypatch.setattr(launcher, "save_token_tracker", lambda path: None)
    monkeypatch.setenv("AI_SCIENTIST_ROOT", str(workspace))

    def experiments(config_path, **kwargs):
        log = run / "logs" / "7-run"
        results = log / "experiment_results"
        results.mkdir(parents=True)
        (results / "measurements.json").write_text('{"value": 3}', encoding="utf-8")
        return log

    monkeypatch.setattr(launcher, "perform_experiments_bfts", experiments)
    monkeypatch.setattr(launcher, "aggregate_plots", lambda **kwargs: {"success": True, "figures": []})
    monkeypatch.setattr(launcher, "gather_citations", lambda *args, **kwargs: "")
    monkeypatch.setattr(launcher, "perform_icbinb_writeup", lambda **kwargs: False)

    def unexpected_review(*args, **kwargs):
        pytest.fail("Reviews must not run after postprocessing failure")

    monkeypatch.setattr(launcher, "create_client", unexpected_review)
    return args, run


def test_failed_plotting_retains_actual_log_outputs_and_stops_pipeline(pipeline, monkeypatch):
    args, run = pipeline
    events = []
    monkeypatch.setattr(launcher, "aggregate_plots", lambda **kwargs: {"success": False, "figures": []})
    with pytest.raises(PipelineFailure, match="Plot aggregation"):
        launcher.run_pipeline(args, run_dir=run, on_event=events.append)
    assert json.loads((run / "experiment_results" / "measurements.json").read_text()) == {"value": 3}
    assert [(event["type"], event["phase"]) for event in events][-1] == ("phase_failed", "figures")
    assert not any(event["phase"] in {"citations", "paper", "reviews"} for event in events)


def test_writeup_failures_archive_partial_outputs_and_never_review(pipeline, monkeypatch):
    args, run = pipeline
    events = []
    attempts = 0

    def failed_writeup(**kwargs):
        nonlocal attempts
        attempts += 1
        latex = run / "latex"
        latex.mkdir()
        (latex / "template.tex").write_text(f"draft {attempts}", encoding="utf-8")
        (run / "paper_reflection_1.pdf").write_bytes(f"partial {attempts}".encode())
        return False

    monkeypatch.setattr(launcher, "perform_icbinb_writeup", failed_writeup)
    with pytest.raises(PipelineFailure, match="Writeup failed"):
        launcher.run_pipeline(args, run_dir=run, on_event=events.append)
    archive = run / "writeup_attempts" / "attempt_1"
    assert (archive / "paper_reflection_1.pdf").read_bytes() == b"partial 1"
    assert (archive / "latex" / "template.tex").read_text() == "draft 1"
    assert (run / "paper_reflection_1.pdf").read_bytes() == b"partial 2"
    assert not any(event["phase"] == "reviews" for event in events)
    assert not any(event["type"] == "phase_finished" and event["phase"] == "paper" for event in events)


def test_reported_writeup_success_without_pdf_is_failure(pipeline, monkeypatch):
    args, run = pipeline
    monkeypatch.setattr(launcher, "perform_icbinb_writeup", lambda **kwargs: True)
    with pytest.raises(PipelineFailure, match="Writeup failed"):
        launcher.run_pipeline(args, run_dir=run)


def test_stop_between_experiments_and_figures_preserves_saved_results(pipeline):
    args, run = pipeline
    events = []
    stop = False

    def event_callback(event):
        nonlocal stop
        events.append(event)
        if event["type"] == "phase_finished" and event["phase"] == "experiments":
            stop = True

    with pytest.raises(Cancelled):
        launcher.run_pipeline(args, run_dir=run, on_event=event_callback, should_stop=lambda: stop)
    assert (run / "logs" / "7-run" / "experiment_results" / "measurements.json").is_file()
    assert not any(event["phase"] == "figures" for event in events)


def test_existing_run_directory_is_not_reused(pipeline):
    args, run = pipeline
    marker = run / "saved.pdf"
    marker.write_bytes(b"original")
    with pytest.raises(PipelineFailure, match="empty"):
        launcher.run_pipeline(args, run_dir=run)
    assert {path.name for path in run.iterdir()} == {"saved.pdf"}
    assert marker.read_bytes() == b"original"


def test_pdf_selection_handles_absence_final_priority_and_numbered_formats(workspace):
    assert launcher.find_pdf_path_for_review(workspace) is None
    (workspace / "not-a-file.pdf").mkdir()
    assert launcher.find_pdf_path_for_review(workspace) is None
    for name in ["paper_1.pdf", "paper_10.pdf"]:
        (workspace / name).write_bytes(b"pdf")
    assert Path(launcher.find_pdf_path_for_review(workspace)).name == "paper_10.pdf"
    for name in ["paper_reflection_2.pdf", "paper_reflection_12.pdf"]:
        (workspace / name).write_bytes(b"pdf")
    assert Path(launcher.find_pdf_path_for_review(workspace)).name == "paper_reflection_12.pdf"
    (workspace / "paper_reflection_final_page_limit.pdf").write_bytes(b"pdf")
    assert Path(launcher.find_pdf_path_for_review(workspace)).name == "paper_reflection_final_page_limit.pdf"
    (workspace / "paper_final.pdf").write_bytes(b"pdf")
    assert Path(launcher.find_pdf_path_for_review(workspace)).name == "paper_final.pdf"


def test_failure_finally_stops_owned_child_but_not_preexisting_child(pipeline, monkeypatch):
    args, run = pipeline
    preexisting = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    owned = []

    def failed_experiments(*args, **kwargs):
        owned.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]))
        raise PipelineFailure("experiment failed")

    monkeypatch.setattr(launcher, "perform_experiments_bfts", failed_experiments)
    try:
        with pytest.raises(PipelineFailure, match="experiment failed"):
            launcher.run_pipeline(args, run_dir=run)
        assert owned[0].poll() is not None
        assert preexisting.poll() is None
    finally:
        for child in [preexisting, *owned]:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)


def test_invalid_text_review_cannot_finish_reviews(pipeline, monkeypatch):
    args, run = pipeline
    events = []
    review = None

    def writeup(**kwargs):
        (run / "paper_final.pdf").write_bytes(b"pdf")
        return True

    monkeypatch.setattr(launcher, "perform_icbinb_writeup", writeup)
    monkeypatch.setattr(launcher, "load_paper", lambda path: "paper")
    monkeypatch.setattr(launcher, "create_client", lambda model: (object(), model))
    monkeypatch.setattr(launcher, "perform_review", lambda *args: review)
    with pytest.raises(PipelineFailure, match="review object"):
        launcher.run_pipeline(args, run_dir=run, on_event=events.append)
    assert json.loads((run / "review_text.txt").read_text()) == review
    assert events[-1]["type"] == "phase_failed"
    assert events[-1]["phase"] == "reviews"


def test_negative_review_is_valid_and_exact_document_is_returned(pipeline, monkeypatch):
    args, run = pipeline

    def writeup(**kwargs):
        (run / "paper_reflection_3.pdf").write_bytes(b"draft")
        (run / "paper_final.pdf").write_bytes(b"selected")
        return True

    monkeypatch.setattr(launcher, "perform_icbinb_writeup", writeup)
    monkeypatch.setattr(launcher, "load_paper", lambda path: Path(path).read_bytes().decode())
    monkeypatch.setattr(launcher, "create_client", lambda model: (object(), model))
    monkeypatch.setattr(launcher, "perform_review", lambda paper, *args: {"Decision": "Reject", "Summary": paper})
    monkeypatch.setattr(launcher, "perform_imgs_cap_ref_review", lambda *args: {})
    result = launcher.run_pipeline(args, run_dir=run)
    assert result["pdf_path"] == str(run / "paper_final.pdf")
    assert result["log_dir"] == str(run / "logs" / "7-run")
    assert json.loads((run / "review_text.txt").read_text()) == {"Decision": "Reject", "Summary": "selected"}


def test_aggregator_nonzero_exit_is_not_success_despite_partial_figure(workspace, monkeypatch):
    script = "from pathlib import Path\nPath('figures').mkdir(exist_ok=True)\nPath('figures/partial.png').write_bytes(b'partial')\nraise RuntimeError('plot failed')\n"
    monkeypatch.setattr(plotting, "load_idea_text", lambda folder: "question")
    monkeypatch.setattr(plotting, "load_exp_summaries", lambda folder: {})
    monkeypatch.setattr(plotting, "filter_experiment_summaries", lambda summaries, **kwargs: summaries)
    monkeypatch.setattr(plotting, "create_client", lambda model: (object(), model))
    responses = iter([f"```python\n{script}```", "I am done"])
    monkeypatch.setattr(plotting, "get_response_from_llm", lambda **kwargs: (next(responses), []))
    result = plotting.aggregate_plots(str(workspace), n_reflections=1)
    assert result == {"success": False, "figures": [str(workspace / "figures" / "partial.png")]}
    assert (workspace / "figures" / "partial.png").read_bytes() == b"partial"
