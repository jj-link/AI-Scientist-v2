"""Browser request validation and lossless scientific proposal normalization."""
from copy import deepcopy
import re
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


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
