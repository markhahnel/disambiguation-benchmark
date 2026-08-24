"""Typed data models shared across harvesting, proposal, and review."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class PublicationContext(BaseModel):
    """What the annotator sees alongside the raw affiliation string."""

    title: str | None = None
    venue: str | None = None
    year: int | None = None
    doi: str | None = None
    author_name: str | None = None
    coauthor_affiliations: list[str] = Field(default_factory=list)


class Item(BaseModel):
    """One gold-standard candidate: a work-author-affiliation instance."""

    item_id: str
    raw_affiliation: str
    stratum: str
    sub_stratum: str | None = None
    source_frame: str
    context: PublicationContext | None = None
    demo: bool = False


class Relationship(BaseModel):
    rel_type: str
    label: str
    ror_id: str


class Candidate(BaseModel):
    """A ROR organisation offered to the annotator."""

    ror_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    acronyms: list[str] = Field(default_factory=list)
    city: str | None = None
    country: str | None = None
    org_types: list[str] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    match_score: float | None = None
    llm_confidence: float | None = None
    llm_justification: str | None = None
    llm_proposed: bool = False


class LlmAssessment(StrEnum):
    SINGLE = "single"
    MULTIPLE = "multiple"
    NONE = "none"
    AMBIGUOUS = "ambiguous"


class Proposal(BaseModel):
    """First-pass output: LLM proposes, it does not decide."""

    item_id: str
    candidates: list[Candidate]
    llm_assessment: LlmAssessment | None = None
    llm_model: str | None = None
    llm_error: str | None = None
    run_id: str
    created_at: str


class Decision(StrEnum):
    ACCEPTED = "accepted"  # selection equals the LLM-proposed set
    CORRECTED = "corrected"  # selection differs from the LLM-proposed set
    AMBIGUOUS = "ambiguous"  # valid label: cannot be resolved from evidence
    NO_ROR = "no_ror"  # valid label: no ROR record exists for this org
    SKIPPED = "skipped"


class Label(BaseModel):
    """One adjudication event. Superseded rows are kept for audit."""

    item_id: str
    annotator: str
    decision: Decision
    ror_ids: list[str] = Field(default_factory=list)
    chosen_ranks: list[int] = Field(default_factory=list)
    justification_viewed: bool = False
    note: str | None = None
    elapsed_ms: int
    submitted_at: str
