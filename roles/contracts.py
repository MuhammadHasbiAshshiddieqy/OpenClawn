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


class DataOutput(BaseModel):
    """Contract output untuk role Data (analisis, eksplorasi, statistik, modeling dasar).

    Kaya secara sengaja: analisis tanpa metodologi & keterbatasan eksplisit
    mudah menyesatkan. `confidence` (low/medium/high) menandai seberapa kuat
    bukti di balik temuan; `caveats` memaksa agent jujur soal batasannya.
    """

    summary: str
    findings: list[str] = Field(default_factory=list)
    methodology: str = Field(default="")
    metrics: dict[str, str] = Field(default_factory=dict)
    caveats: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    confidence: str = Field(default="medium", pattern="^(low|medium|high)$")


class SecurityOutput(BaseModel):
    """Contract output untuk role Security & Privacy (advisory).

    Lapisan saran governance — BUKAN jaminan keamanan (pertahanan utama tetap
    isolasi container & Vault, lihat CLAUDE.md §1 & §17). `risk_level` global
    + `findings` terperinci dengan mitigasi agar rekomendasi dapat ditindak.
    `pii_detected` menandai apakah data pribadi teridentifikasi dalam ruang lingkup.
    """

    summary: str
    pii_detected: bool = Field(default=False)
    findings: list[str] = Field(default_factory=list)
    severity_matrix: dict[str, str] = Field(default_factory=dict)
    mitigations: list[str] = Field(default_factory=list)
    compliance_notes: list[str] = Field(default_factory=list)
    risk_level: str = Field(default="medium", pattern="^(low|medium|high|critical)$")


CONTRACT_REGISTRY: dict[str, type[BaseModel]] = {
    "pm": PMOutput,
    "qa": QAOutput,
    "dev": DevOutput,
    "data": DataOutput,
    "security": SecurityOutput,
}
