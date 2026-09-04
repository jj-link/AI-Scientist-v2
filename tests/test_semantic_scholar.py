"""Focused tests for bounded Semantic Scholar request handling."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_scientist.tools import semantic_scholar  # noqa: E402
from ai_scientist.tools.semantic_scholar import (  # noqa: E402
    S2_MAX_TRIES,
    S2_REQUEST_TIMEOUT_SECONDS,
    SemanticScholarSearchTool,
    search_for_papers,
)


def fake_response(status, json_data=None):
    rsp = MagicMock()
    rsp.status_code = status
    rsp.text = "payload"
    if json_data is None:
        rsp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            f"{status} error"
        )
    else:
        rsp.raise_for_status.return_value = None
        rsp.json.return_value = json_data
    return rsp


@pytest.fixture()
def no_sleep(monkeypatch):
    waits = []
    monkeypatch.setattr(semantic_scholar.time, "sleep", lambda s: waits.append(s))
    return waits


def test_repeated_429_makes_exactly_four_requests(monkeypatch, no_sleep):
    calls = []
    responses = [fake_response(429) for _ in range(S2_MAX_TRIES)]

    def fake_get(url, **kwargs):
        calls.append(kwargs)
        return responses[len(calls) - 1]

    monkeypatch.setattr(semantic_scholar.requests, "get", fake_get)
    tool = SemanticScholarSearchTool()
    with pytest.raises(requests.exceptions.HTTPError):
        tool.search_for_papers("attention")
    assert len(calls) == S2_MAX_TRIES == 4
    assert all(kwargs["timeout"] == S2_REQUEST_TIMEOUT_SECONDS == 30 for kwargs in calls)


def test_http_400_makes_one_request(monkeypatch, no_sleep):
    calls = []
    monkeypatch.setattr(
        semantic_scholar.requests,
        "get",
        lambda url, **kwargs: (calls.append(kwargs), fake_response(400))[1],
    )
    tool = SemanticScholarSearchTool()
    with pytest.raises(requests.exceptions.HTTPError):
        tool.search_for_papers("attention")
    assert len(calls) == 1
    assert calls[0]["timeout"] == 30


def test_successful_response_preserves_parsing(monkeypatch, no_sleep):
    payload = {
        "total": 2,
        "data": [
            {
                "title": "Low Cite",
                "authors": [{"name": "A"}],
                "citationCount": 3,
            },
            {
                "title": "High Cite",
                "authors": [{"name": "B"}],
                "citationCount": 99,
            },
        ],
    }
    calls = []
    monkeypatch.setattr(
        semantic_scholar.requests,
        "get",
        lambda url, **kwargs: (calls.append(kwargs), fake_response(200, payload))[1],
    )
    tool = SemanticScholarSearchTool()
    papers = tool.search_for_papers("attention")
    assert [p["title"] for p in papers] == ["High Cite", "Low Cite"]
    assert calls[0]["timeout"] == 30

    monkeypatch.delenv("S2_API_KEY", raising=False)
    papers = search_for_papers("attention")
    assert [p["title"] for p in papers] == ["High Cite", "Low Cite"]


def test_connection_errors_retry_then_raise(monkeypatch, no_sleep):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(kwargs)
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(semantic_scholar.requests, "get", fake_get)
    tool = SemanticScholarSearchTool()
    with pytest.raises(requests.exceptions.ConnectionError):
        tool.search_for_papers("attention")
    assert len(calls) == S2_MAX_TRIES == 4
    assert no_sleep, "bounded retries must back off via sleep"
