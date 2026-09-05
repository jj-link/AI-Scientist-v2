"""Failure and cancellation boundaries for experiment stage execution."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_scientist.progress import Cancelled, PipelineFailure
from ai_scientist.treesearch.agent_manager import AgentManager, Stage, StageTransition
from ai_scientist.treesearch.journal import Journal, Node


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
            Node(id="implementation", ctime=0, is_buggy=not self.working,
                 is_buggy_plots=not self.working)
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
