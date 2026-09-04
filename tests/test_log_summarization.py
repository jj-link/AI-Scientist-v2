"""Regression tests for stage-number-based journal summarization."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_scientist.treesearch import log_summarization  # noqa: E402


class FakeNode:
    def __init__(self, name, is_leaf=False, ablation_name=None):
        self.name = name
        self.is_leaf = is_leaf
        self.ablation_name = ablation_name
        self.is_seed_node = False
        self.is_seed_agg_node = False
        self.children = []


class FakeJournal:
    def __init__(self, stage_name, nodes=()):
        self.stage_name = stage_name
        self.nodes = list(nodes)
        self.good_nodes = list(nodes)

    def get_best_node(self, cfg=None):
        return self.nodes[0]



class DotDict(dict):
    """Dict with attribute access, mimicking OmegaConf config nodes."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


@pytest.fixture()
def stubbed(monkeypatch):
    annotated = []

    def fake_annotate(journal, cfg=None):
        annotated.append(journal.stage_name)

    monkeypatch.setattr(log_summarization, "annotate_history", fake_annotate)
    monkeypatch.setattr(
        log_summarization, "get_node_log", lambda node: f"log:{node.name}"
    )
    monkeypatch.setattr(
        log_summarization,
        "get_stage_summary",
        lambda journal, stage_name, model, client: {"stage": journal.stage_name},
    )
    monkeypatch.setattr(log_summarization, "get_ai_client", lambda model: object())
    cfg = DotDict(agent=DotDict(summary={"model": "test-model"}))
    return SimpleNamespace(annotated=annotated, cfg=cfg)


def saved_run_journals():
    stage1 = FakeJournal("stage_1_draft", [FakeNode("draft-node")])
    stage2 = FakeJournal("stage_2_baseline", [FakeNode("baseline-node")])
    stage3 = FakeJournal("stage_3_research", [FakeNode("research-node")])
    stage4a = FakeJournal(
        "stage_4_ablation_a",
        [
            FakeNode("keep1", is_leaf=True, ablation_name="lr"),
            FakeNode("keep2", is_leaf=True, ablation_name="batch"),
            FakeNode("not-ablation", is_leaf=True),
        ],
    )
    stage4b = FakeJournal(
        "stage_4_ablation_b",
        [FakeNode("keep3", is_leaf=True, ablation_name="seed")],
    )
    # manager.journals.items() shape: (stage_name, journal) pairs.
    return [
        ("stage_1_draft", stage1),
        ("stage_2_baseline", stage2),
        ("stage_3_research", stage3),
        ("stage_4_ablation_a", stage4a),
        ("stage_4_ablation_b", stage4b),
    ]


def test_saved_run_five_journals_map_correctly(stubbed):
    draft, baseline, research, ablation = log_summarization.overall_summarize(
        saved_run_journals(), cfg=stubbed.cfg
    )
    assert draft == {"stage": "stage_1_draft"}
    assert baseline == {
        "best node": "log:baseline-node",
        "best node with different seeds": [],
    }
    assert research == {
        "best node": "log:research-node",
        "best node with different seeds": [],
    }
    # concatenated; non-ablation leaves are excluded.
    assert ablation == ["log:keep1", "log:keep2", "log:keep3"]


def test_stage4_annotates_every_stage4_journal(stubbed):
    log_summarization.overall_summarize(saved_run_journals(), cfg=stubbed.cfg)
    assert stubbed.annotated.count("stage_4_ablation_a") == 1
    assert stubbed.annotated.count("stage_4_ablation_b") == 1


def test_missing_stage_returns_none(stubbed):
    journals = [
        ("stage_1_draft", FakeJournal("stage_1_draft", [FakeNode("draft-node")])),
        (
            "stage_2_baseline",
            FakeJournal("stage_2_baseline", [FakeNode("baseline-node")]),
        ),
        ("stage_4_ablation_a", FakeJournal("stage_4_ablation_a", [])),
    ]
    draft, baseline, research, ablation = log_summarization.overall_summarize(
        journals, cfg=stubbed.cfg
    )
    assert research is None
    assert draft is not None
    assert baseline is not None
    assert ablation == []


def test_multiple_stage_entries_use_last_for_stages_one_to_three(stubbed):
    journals = [
        (
            "stage_1_draft_first",
            FakeJournal("stage_1_draft_first", [FakeNode("first")]),
        ),
        (
            "stage_1_draft_second",
            FakeJournal("stage_1_draft_second", [FakeNode("second")]),
        ),
        ("stage_4_ablation_a", FakeJournal("stage_4_ablation_a", [])),
    ]
    draft, _, _, ablation = log_summarization.overall_summarize(
        journals, cfg=stubbed.cfg
    )
    assert draft == {"stage": "stage_1_draft_second"}
    assert ablation == []


def test_invalid_stage_name_fails_clearly():
    with pytest.raises(ValueError, match="stage name 'run_final'"):
        log_summarization._stage_number("run_final")
    with pytest.raises(ValueError, match="stage number"):
        log_summarization.overall_summarize([("run_final", FakeJournal("run_final"))])


def test_stage_number_parsing():
    assert log_summarization._stage_number("stage_1_idea_draft") == 1
    assert log_summarization._stage_number("stage_4_ablation") == 4
    # Journal keys use a numeric prefix without the 'stage_' token.
    assert log_summarization._stage_number("1_initial_implementation_1_preliminary") == 1
    assert log_summarization._stage_number("4_ablation_studies_2_component") == 4
