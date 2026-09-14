"""Failure and cancellation boundaries for experiment stage execution."""

import sys
from concurrent.futures import Future
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_scientist.progress import Cancelled, PipelineFailure
from ai_scientist.treesearch.agent_manager import AgentManager, Stage, StageTransition
from ai_scientist.treesearch.journal import Journal, Node
from ai_scientist.treesearch import parallel_agent


class LocalAgent:
    """A deterministic execution resource; no model calls or child processes."""

    def __init__(self, journal, *, working=False):
        self.journal = journal
        self.working = working
        self.closed = False
        self.steps = 0
        self.seed_evaluated = False
        self.aggregated = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def step(self, exec_callback):
        self.steps += 1
        self.journal.append(
            Node(
                id="implementation",
                ctime=0,
                is_buggy=not self.working,
                is_buggy_plots=not self.working,
            )
        )

    def _run_multi_seed_evaluation(self, best_node):
        self.seed_evaluated = True
        seed = Node(id="seed", ctime=0, is_buggy=False, is_buggy_plots=False)
        self.journal.append(seed)
        return [seed]

    def _run_plot_aggregation(self, best_node, seed_nodes):
        self.aggregated = True


def manager_at_stage(number, *, working=False):
    manager = AgentManager.__new__(AgentManager)
    stage = Stage(
        name=f"{number}_initial_implementation_1_preliminary",
        description="preliminary",
        goals="Private scientific details must not enter progress events",
        max_iterations=1,
        num_drafts=1,
        stage_number=number,
    )
    journal = Journal()
    manager.cfg = SimpleNamespace()
    manager.current_stage = stage
    manager.stages = [stage]
    manager.journals = {stage.name: journal}
    manager.stage_history = []
    agent = LocalAgent(journal, working=working)
    manager._create_agent_for_stage = lambda stage: agent
    return manager, agent


@pytest.mark.parametrize(
    "boundary,number,error_code",
    [
        ("initial_limit", 1, "no_working_implementation"),
        ("missing_previous", 2, "no_previous_best"),
        ("missing_multiseed_best", 2, "no_best_for_multiseed"),
    ],
)
def test_failed_stage_raises_without_completion(boundary, number, error_code):
    manager, agent = manager_at_stage(number)
    if boundary == "missing_previous":
        manager.stage_history.append(
            StageTransition(
                from_stage="1_initial_implementation_1_previous",
                to_stage=manager.current_stage.name,
                reason="transition",
                config_adjustments={},
            )
        )
    events = []

    with pytest.raises(PipelineFailure):
        manager.run(exec_callback=None, on_event=events.append)

    assert agent.closed
    assert [event["type"] for event in events] == ["stage_started", "phase_failed"]
    assert events[-1]["data"]["code"] == error_code
    assert manager.current_stage is not None
    assert not agent.seed_evaluated


def test_stop_after_multiseed_preserves_saved_work_and_skips_aggregation():
    manager, agent = manager_at_stage(2, working=True)
    events = []
    saved_node_ids = []

    def save_step(stage, journal):
        saved_node_ids.append([node.id for node in journal.nodes])

    with pytest.raises(Cancelled):
        manager.run(
            exec_callback=None,
            step_callback=save_step,
            on_event=events.append,
            should_stop=lambda: agent.seed_evaluated,
        )

    assert agent.closed
    assert saved_node_ids == [["implementation"], ["implementation", "seed"]]
    assert not agent.aggregated
    assert [event["type"] for event in events] == ["stage_started"]
    assert [node.id for node in agent.journal.nodes] == ["implementation", "seed"]


def test_worker_timeout_propagates_and_releases_gpu(monkeypatch):
    result = Future()
    result.set_exception(TimeoutError("Worker model request timed out"))
    executor = SimpleNamespace(submit=lambda *args: result)
    monkeypatch.setattr(
        parallel_agent, "ProcessPoolExecutor", lambda **kwargs: executor
    )
    monkeypatch.setattr(parallel_agent, "get_gpu_count", lambda: 1)
    monkeypatch.setattr(
        parallel_agent.ParallelAgent, "_define_global_metrics", lambda self: []
    )
    journal = Journal()
    monkeypatch.setattr(journal, "generate_summary", lambda **kwargs: "")
    cfg = SimpleNamespace(
        agent=SimpleNamespace(num_workers=1, get=lambda *args: None),
        exec=SimpleNamespace(timeout=300),
    )
    agent = parallel_agent.ParallelAgent(
        "A bounded research study",
        cfg,
        journal,
        stage_name="1_initial_implementation_1_preliminary",
    )
    monkeypatch.setattr(agent, "_select_parallel_nodes", lambda: [None])

    with pytest.raises(TimeoutError):
        agent.step(exec_callback=None)

    assert not journal.nodes
    assert agent.gpu_manager.acquire_gpu("replacement") == 0


@pytest.mark.parametrize(
    "planner", ["_generate_hyperparam_tuning_idea", "_generate_ablation_idea"]
)
def test_unparseable_stage_plan_does_not_invent_an_experiment(monkeypatch, planner):
    agent = parallel_agent.ParallelAgent.__new__(parallel_agent.ParallelAgent)
    agent.task_desc = "Only the saved comparison is permitted."
    agent.cfg = SimpleNamespace(
        agent=SimpleNamespace(
            code=SimpleNamespace(model="role/experiment_code", temp=0)
        )
    )
    agent.best_stage1_node = SimpleNamespace(code="recorded_baseline()")
    agent.best_stage3_node = agent.best_stage1_node
    agent._hyperparam_tuning_state = {"tried_hyperparams": set()}
    agent._ablation_state = {"completed_ablations": set()}
    monkeypatch.setattr(
        parallel_agent, "query", lambda **kwargs: "Unparseable response"
    )

    with pytest.raises(RuntimeError):
        getattr(agent, planner)()


def _plot_review_agent(monkeypatch):
    agent = parallel_agent.MinimalAgent.__new__(parallel_agent.MinimalAgent)
    agent.task_desc = "Review the recorded paired comparison."
    agent.cfg = SimpleNamespace(
        agent=SimpleNamespace(
            vlm_feedback=SimpleNamespace(model="role/visual_feedback", temp=0)
        )
    )
    monkeypatch.setattr(
        agent, "_determine_datasets_successfully_tested", lambda node: []
    )
    monkeypatch.setattr(
        parallel_agent, "open", lambda path, mode: BytesIO(path.encode()), raising=False
    )
    return agent


def test_plot_review_keeps_invalid_evidence_beyond_provider_image_limit(monkeypatch):
    agent = _plot_review_agent(monkeypatch)
    node = Node(code="pass", plan="Saved experiment")
    node.plot_paths = [f"plot-{index}.png" for index in range(7)]

    def review(**kwargs):
        images = [
            part["image_url"]["url"]
            for part in kwargs["user_message"]
            if part["type"] == "image_url"
        ]
        if len(images) > 4:
            raise ValueError("At most 4 images may be provided in one prompt.")
        paths = [
            parallel_agent.base64.b64decode(image.split(",", 1)[1]).decode()
            for image in images
        ]
        valid = "plot-5.png" not in paths
        return {
            "valid_plots_received": valid,
            "plot_analyses": [
                {"analysis": "Unreadable axes." if path == "plot-5.png" else "Valid axes."}
                for path in paths
            ],
            "vlm_feedback_summary": "Valid plots." if valid else "Repair unreadable axes.",
        }

    monkeypatch.setattr(parallel_agent, "query", review)
    agent._analyze_plots_with_vlm(node)

    assert node.is_buggy_plots is True
    assert "Repair unreadable axes." in node.vlm_feedback_summary


def test_interrupted_plot_review_cannot_retain_a_successful_verdict(monkeypatch):
    agent = _plot_review_agent(monkeypatch)
    node = Node(code="pass", plan="Saved experiment")
    node.plot_paths = ["updated-plot.png"]
    node.is_buggy_plots = False
    node.plot_analyses = [{"analysis": "Old successful review."}]
    node.datasets_successfully_tested = ["old-data"]

    def unavailable(**kwargs):
        raise ConnectionError("Vision endpoint disconnected.")

    monkeypatch.setattr(parallel_agent, "query", unavailable)
    with pytest.raises(ConnectionError):
        agent._analyze_plots_with_vlm(node)

    assert node.is_buggy_plots is True
    assert node.plot_analyses == []
    assert not node.datasets_successfully_tested


@pytest.mark.parametrize(
    ("payload", "expected"),
    [("[]", []), ('["synthetic_noisy_binary"]', ["synthetic_noisy_binary"])],
)
def test_dataset_coverage_preserves_empty_and_named_json_arrays(monkeypatch, payload, expected):
    agent = parallel_agent.MinimalAgent.__new__(parallel_agent.MinimalAgent)
    agent.cfg = SimpleNamespace(
        agent=SimpleNamespace(
            feedback=SimpleNamespace(model="role/experiment_feedback", temp=0)
        )
    )
    monkeypatch.setattr(
        parallel_agent, "query",
        lambda **kwargs: f"REASONING: Recorded plot coverage.\nSUCCESSFULLY_TESTED_DATASETS: {payload}",
    )
    node = Node(code="pass", plan="Saved experiment")

    assert agent._determine_datasets_successfully_tested(node) == expected
