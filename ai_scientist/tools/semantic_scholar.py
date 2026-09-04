import os
import requests
import time
import warnings
from typing import Dict, List, Optional, Union

from ai_scientist.tools.base_tool import BaseTool

S2_REQUEST_TIMEOUT_SECONDS = 30
S2_MAX_TRIES = 4
S2_MAX_BACKOFF_SECONDS = 30


def _s2_get(url: str, **kwargs):
    """GET with bounded retry for transient Semantic Scholar failures.

    Retries HTTP 429, 5xx, connection failures, and timeouts with capped
    exponential backoff, stopping after S2_MAX_TRIES attempts. Permanent 4xx
    responses and the final transient exception propagate to the caller.
    """
    kwargs.setdefault("timeout", S2_REQUEST_TIMEOUT_SECONDS)
    for attempt in range(1, S2_MAX_TRIES + 1):
        try:
            rsp = requests.get(url, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt == S2_MAX_TRIES:
                raise
        else:
            transient = rsp.status_code == 429 or 500 <= rsp.status_code < 600
            if not transient:
                rsp.raise_for_status()
                return rsp
            if attempt == S2_MAX_TRIES:
                rsp.raise_for_status()
        wait = min(2 ** (attempt - 1), S2_MAX_BACKOFF_SECONDS)
        print(
            f"Semantic Scholar request failed (attempt {attempt}/{S2_MAX_TRIES}); "
            f"retrying in {wait} seconds."
        )
        time.sleep(wait)
    raise requests.exceptions.RequestException(
        "Semantic Scholar request loop exited without a response."
    )




class SemanticScholarSearchTool(BaseTool):
    def __init__(
        self,
        name: str = "SearchSemanticScholar",
        description: str = (
            "Search for relevant literature using Semantic Scholar. "
            "Provide a search query to find relevant papers."
        ),
        max_results: int = 10,
    ):
        parameters = [
            {
                "name": "query",
                "type": "str",
                "description": "The search query to find relevant papers.",
            }
        ]
        super().__init__(name, description, parameters)
        self.max_results = max_results
        self.S2_API_KEY = os.getenv("S2_API_KEY")
        if not self.S2_API_KEY:
            warnings.warn(
                "No Semantic Scholar API key found. Requests will be subject to stricter rate limits. "
                "Set the S2_API_KEY environment variable for higher limits."
            )

    def use_tool(self, query: str) -> Optional[str]:
        papers = self.search_for_papers(query)
        if papers:
            return self.format_papers(papers)
        else:
            return "No papers found."

    def search_for_papers(self, query: str) -> Optional[List[Dict]]:
        if not query:
            return None

        headers = {}
        if self.S2_API_KEY:
            headers["X-API-KEY"] = self.S2_API_KEY

        rsp = _s2_get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            headers=headers,
            params={
                "query": query,
                "limit": self.max_results,
                "fields": "title,authors,venue,year,abstract,citationCount",
            },
        )
        print(f"Response Status Code: {rsp.status_code}")
        print(f"Response Content: {rsp.text[:500]}")
        results = rsp.json()
        total = results.get("total", 0)
        if total == 0:
            return None

        papers = results.get("data", [])
        # Sort papers by citationCount in descending order
        papers.sort(key=lambda x: x.get("citationCount", 0), reverse=True)
        return papers

    def format_papers(self, papers: List[Dict]) -> str:
        paper_strings = []
        for i, paper in enumerate(papers):
            authors = ", ".join(
                [author.get("name", "Unknown") for author in paper.get("authors", [])]
            )
            paper_strings.append(
                f"""{i + 1}: {paper.get("title", "Unknown Title")}. {authors}. {paper.get("venue", "Unknown Venue")}, {paper.get("year", "Unknown Year")}.
Number of citations: {paper.get("citationCount", "N/A")}
Abstract: {paper.get("abstract", "No abstract available.")}"""
            )
        return "\n\n".join(paper_strings)


def search_for_papers(query, result_limit=10) -> Union[None, List[Dict]]:
    S2_API_KEY = os.getenv("S2_API_KEY")
    headers = {}
    if not S2_API_KEY:
        warnings.warn(
            "No Semantic Scholar API key found. Requests will be subject to stricter rate limits."
        )
    else:
        headers["X-API-KEY"] = S2_API_KEY
    
    if not query:
        return None
    
    rsp = _s2_get(
        "https://api.semanticscholar.org/graph/v1/paper/search",
        headers=headers,
        params={
            "query": query,
            "limit": result_limit,
            "fields": "title,authors,venue,year,abstract,citationStyles,citationCount",
        },
    )
    print(f"Response Status Code: {rsp.status_code}")
    print(
        f"Response Content: {rsp.text[:500]}"
    )  # Print the first 500 characters of the response content
    results = rsp.json()
    total = results["total"]
    time.sleep(1.0)
    if not total:
        return None

    papers = results["data"]
    return papers
