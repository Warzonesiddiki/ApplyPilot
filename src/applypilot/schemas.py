"""Strict Pydantic schemas for structured LLM outputs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StructuredResponse(BaseModel):
    """Base for provider outputs: reject unknown fields and trim strings."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ScoreResponse(StructuredResponse):
    score: int = Field(ge=1, le=10)
    skills_score: int = Field(ge=1, le=10)
    qualifications_score: int = Field(ge=1, le=10)
    domain_score: int = Field(ge=1, le=10)
    location_score: int = Field(ge=1, le=10)
    keywords: list[str]
    missing_keywords: list[str]
    reasoning: str = Field(min_length=1)


class ResumeEntry(StructuredResponse):
    header: str = Field(min_length=1)
    subtitle: str = ""
    bullets: list[str] = Field(default_factory=list)


class TailoredResumeResponse(StructuredResponse):
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    skills: dict[str, str]
    experience: list[ResumeEntry] = Field(min_length=1)
    projects: list[ResumeEntry] = Field(default_factory=list)
    certifications: str = ""
    education: str = ""


class ResumeJudgeResponse(StructuredResponse):
    verdict: Literal["PASS", "FAIL"]
    issues: str


class JobDescriptionResponse(StructuredResponse):
    full_description: str | None = None
    application_url: str | None = None


class ApiRelevanceResponse(StructuredResponse):
    relevant: bool
    reason: str = Field(min_length=1)


class SelectorPlanResponse(StructuredResponse):
    job_card: str | None = None
    title: str | None = None
    salary: str | None = None
    description: str | None = None
    location: str | None = None
    url: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def require_card_or_error(self) -> SelectorPlanResponse:
        if not self.error and not self.job_card:
            raise ValueError("job_card is required unless an error is returned")
        return self


class StrategyExtractionResponse(StructuredResponse):
    url_pattern: str | None = None
    items_path: str | None = None
    title: str | None = None
    salary: str | None = None
    description: str | None = None
    location: str | None = None
    url: str | None = None


class StrategyResponse(StructuredResponse):
    strategy: Literal["json_ld", "api_response", "css_selectors"]
    reasoning: str = Field(min_length=1)
    extraction: StrategyExtractionResponse = Field(default_factory=StrategyExtractionResponse)

    @model_validator(mode="after")
    def require_api_paths(self) -> StrategyResponse:
        if self.strategy == "api_response" and not (self.extraction.url_pattern and self.extraction.items_path):
            raise ValueError("api_response strategy requires url_pattern and items_path")
        return self


class ScreeningAnswerResponse(StructuredResponse):
    """Safe, auditable answer shape for structured screening-question responses."""

    answer: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    requires_user_review: bool = False
