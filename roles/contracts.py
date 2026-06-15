from pydantic import BaseModel, Field


class PMOutput(BaseModel):
    """Contract output untuk role PM."""

    summary: str
    user_stories: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    priority: str = Field(default="medium", pattern="^(low|medium|high)$")
    open_questions: list[str] = Field(default_factory=list)


class QAOutput(BaseModel):
    """Contract output untuk role QA."""

    test_cases: list[str] = Field(default_factory=list)
    coverage_gaps: list[str] = Field(default_factory=list)
    severity_matrix: dict[str, str] = Field(default_factory=dict)
    pass_criteria: list[str] = Field(default_factory=list)


class DevOutput(BaseModel):
    """Contract output untuk role Dev."""

    approach: str
    files_changed: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    needs_review: bool = Field(default=True)


CONTRACT_REGISTRY: dict[str, type[BaseModel]] = {
    "pm": PMOutput,
    "qa": QAOutput,
    "dev": DevOutput,
}
