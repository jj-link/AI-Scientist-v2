"""Browser request validation and lossless scientific proposal normalization."""
from copy import deepcopy
import re
from uuid import UUID
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

REPOSITORY = "jj-link/AI-Scientist-v2"

def normalize_idea(idea: dict) -> dict:
    value = deepcopy(idea)
    for key in ("Experiments", "Risk Factors and Limitations"):
        if isinstance(value.get(key), str):
            value[key] = [value[key]] if value[key].strip() else []
    return value


def validate_idea(idea: dict) -> dict[str, str]:
    errors = {}
    if not isinstance(idea.get("Name"), str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", idea["Name"]):
        errors["Name"] = "Use 1–64 lowercase letters, digits or underscores, starting with a letter."
    for key in ("Title", "Short Hypothesis", "Abstract"):
        if not isinstance(idea.get(key), str) or not idea[key].strip():
            errors[key] = "Enter nonempty text."
    experiments = idea.get("Experiments")
    if not isinstance(experiments, list) or not experiments or any(
        not (isinstance(item, str) and item.strip() or isinstance(item, dict) and item)
        for item in experiments
    ):
        errors["Experiments"] = "Add at least one nonempty experiment (text or a structured object)."
    risks = idea.get("Risk Factors and Limitations")
    if not isinstance(risks, list) or any(not isinstance(item, str) or not item.strip() for item in risks):
        errors["Risk Factors and Limitations"] = "Use a list of nonempty risk descriptions; an empty list is allowed."
    return errors


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IdeaJobRequest(Request):
    request_id: UUID
    research_question: str = Field(min_length=1, max_length=50000)
    context: str = Field(default="", max_length=50000)
    attempts: int = Field(default=1, ge=1, le=10, strict=True)
    rounds: int = Field(default=5, ge=2, le=20, strict=True)
    role_config_id: str


class ExperimentRequest(Request):
    request_id: UUID
    idea_id: str
    idea_revision: int = Field(ge=1, strict=True)
    role_config_id: str
    bfts_config_id: str
    execution_acknowledged: bool


class IdeaUpdate(Request):
    expected_revision: int = Field(ge=1, strict=True)
    idea: dict


class ModelCheck(Request):
    config_id: str


class AssistantSettingsUpdate(Request):
    enabled: bool = Field(strict=True)
    config_id: str | None = None
    role: str | None = None

    @model_validator(mode="after")
    def _require_selection(self):
        if self.enabled and (not self.config_id or not self.config_id.strip()
                             or not self.role or not self.role.strip()):
            raise ValueError("Enable with a selected configuration and role.")
        return self


class DiagnosticResultIssue(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=12000)

    @field_validator("title")
    @classmethod
    def _single_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value or "\x00" in value or not value.strip():
            raise ValueError("Issue titles must be nonempty single-line text.")
        return value

    @field_validator("body")
    @classmethod
    def _no_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Issue bodies must not contain NUL characters.")
        return value


class DiagnosticResult(BaseModel):
    classification: Literal["user_action", "bug", "uncertain"]
    summary: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(default_factory=list, max_length=8)
    steps: list[str] = Field(default_factory=list, max_length=8)
    issue: DiagnosticResultIssue | None = None

    @field_validator("summary", "evidence", "steps")
    @classmethod
    def _nonempty(cls, value, info):
        limit = {"summary": 2000, "evidence": 500, "steps": 1000}[info.field_name]
        items = value if isinstance(value, list) else [value]
        if any(not isinstance(item, str) or not item.strip() or len(item) > limit for item in items):
            raise ValueError(f"Diagnostic {info.field_name} entries must be nonempty text of at most {limit} characters.")
        return value

    @model_validator(mode="after")
    def _issue_required_for_bug(self):
        if self.classification == "bug" and self.issue is None:
            raise ValueError("A bug diagnosis requires an issue draft.")
        return self


class IssueDraftUpdate(Request):
    expected_revision: int = Field(ge=1, strict=True)
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=12000)

    @field_validator("title")
    @classmethod
    def _single_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value or "\x00" in value or not value.strip():
            raise ValueError("Issue titles must be nonempty single-line text.")
        return value

    @field_validator("body")
    @classmethod
    def _no_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Issue bodies must not contain NUL characters.")
        return value


class IssuePublish(Request):
    revision: int = Field(ge=1, strict=True)
    confirmed: bool = Field(strict=True)

    @model_validator(mode="after")
    def _explicit_confirmation(self):
        if self.confirmed is not True:
            raise ValueError("Explicit confirmation is required to publish an issue.")
        return self
