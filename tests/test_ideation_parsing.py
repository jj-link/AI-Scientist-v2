"""Regression tests for ideation action/arguments text parsing."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_scientist.perform_ideation_temp_free import parse_action_response  # noqa: E402

KNOWN = {"SearchSemanticScholar", "FinalizeIdea"}


def test_canonical_action_marked_form():
    text = 'ACTION: SearchSemanticScholar\nARGUMENTS: {"query": "diffusion models"}'
    assert parse_action_response(text, KNOWN) == (
        "SearchSemanticScholar",
        {"query": "diffusion models"},
    )


def test_bare_action_with_colon():
    text = 'SearchSemanticScholar:\nARGUMENTS:\n{"query": "diffusion models"}'
    assert parse_action_response(text, KNOWN) == (
        "SearchSemanticScholar",
        {"query": "diffusion models"},
    )


def test_bare_action_without_colon():
    text = 'SearchSemanticScholar\nARGUMENTS:\n{"query": "diffusion models"}'
    assert parse_action_response(text, KNOWN) == (
        "SearchSemanticScholar",
        {"query": "diffusion models"},
    )


def test_fenced_json_block():
    text = (
        "ACTION: FinalizeIdea\nARGUMENTS:\n"
        '```json\n{"idea": {"Name": "Test idea"}}\n```'
    )
    assert parse_action_response(text, KNOWN) == (
        "FinalizeIdea",
        {"idea": {"Name": "Test idea"}},
    )


def test_trailing_thought_text_is_not_consumed():
    text = (
        'ACTION: SearchSemanticScholar\nARGUMENTS: {"query": "x"}\n'
        "THOUGHT: I will search the literature now."
    )
    assert parse_action_response(text, KNOWN) == (
        "SearchSemanticScholar",
        {"query": "x"},
    )


def test_unknown_action_rejected():
    text = "ACTION: NotATool\nARGUMENTS: {}"
    with pytest.raises(ValueError, match="Unknown action 'NotATool'"):
        parse_action_response(text, KNOWN)


def test_bare_unknown_action_rejected():
    text = "MysteryTool\nARGUMENTS: {}"
    with pytest.raises(ValueError, match="Unknown action 'MysteryTool'"):
        parse_action_response(text, KNOWN)


def test_missing_arguments_section_rejected():
    text = "ACTION: SearchSemanticScholar\nI forgot the arguments."
    with pytest.raises(ValueError, match="ARGUMENTS"):
        parse_action_response(text, KNOWN)


def test_non_object_json_rejected():
    text = 'ACTION: SearchSemanticScholar\nARGUMENTS: ["not", "an", "object"]'
    with pytest.raises(ValueError, match="must be a JSON object"):
        parse_action_response(text, KNOWN)


def test_malformed_json_rejected():
    text = "ACTION: SearchSemanticScholar\nARGUMENTS: {query: unclosed"
    with pytest.raises(ValueError, match="Invalid JSON arguments"):
        parse_action_response(text, KNOWN)


def test_reflection_prompt_final_round_instructs_finalization():
    from ai_scientist.perform_ideation_temp_free import build_reflection_prompt

    final = build_reflection_prompt(1, 2, "429 error from search tool")
    assert "Round 2/2" in final
    assert "429 error from search tool" in final
    assert "final round" in final
    assert "FinalizeIdea" in final


def test_reflection_prompt_nonfinal_round_has_no_finalize_instruction():
    from ai_scientist.perform_ideation_temp_free import build_reflection_prompt

    mid = build_reflection_prompt(0, 2, "")
    assert "Round 1/2" in mid
    assert "No new results." in mid
    assert "final round" not in mid
    assert "Do not call any more tools" not in mid
