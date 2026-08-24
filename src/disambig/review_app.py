"""FastAPI backend for the adjudication UI.

Design decisions that matter methodologically:

- Candidates are ordered by ROR matcher score (a neutral, non-evaluated
  system), never by LLM confidence, and the queue order is a deterministic
  hash shuffle so strata interleave.
- The LLM's justification is collapsed until the annotator asks for it, and
  whether it was viewed is recorded on the label, so anchoring on the LLM
  first pass is measurable.
- accepted vs corrected is derived server-side by comparing the selected ROR
  set to the LLM-proposed set, so the distinction cannot drift with client
  bugs.
- Every label stores elapsed milliseconds; the time distribution per stratum
  is itself a finding.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from disambig.models import Candidate, Decision, Label
from disambig.store import ReviewStore, now_iso

log = structlog.get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class RorSearcher(Protocol):
    def search(self, query: str, refresh: bool = False) -> list[Candidate]: ...


class LabelRequest(BaseModel):
    annotator: str
    item_id: str
    outcome: str  # resolved | ambiguous | no_ror | skipped
    selected_ror_ids: list[str] = Field(default_factory=list)
    selected_ranks: list[int] = Field(default_factory=list)
    justification_viewed: bool = False
    note: str | None = None
    elapsed_ms: int


class UndoRequest(BaseModel):
    annotator: str


def create_app(store: ReviewStore, ror_searcher: RorSearcher | None = None) -> FastAPI:
    app = FastAPI(title="disambig review UI")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/next")
    def next_item(annotator: str) -> dict[str, Any]:
        result = store.next_item(annotator)
        progress = store.progress(annotator)
        if result is None:
            return {"item": None, "proposal": None, "progress": progress}
        item, proposal = result
        proposal_payload: dict[str, Any] | None = None
        if proposal is not None:
            candidates = sorted(
                proposal.candidates,
                key=lambda candidate: -(candidate.match_score or 0.0),
            )
            proposal_payload = {
                "candidates": [candidate.model_dump() for candidate in candidates],
                "llm_assessment": proposal.llm_assessment,
                "llm_model": proposal.llm_model,
                "llm_error": proposal.llm_error,
            }
        return {"item": item.model_dump(), "proposal": proposal_payload, "progress": progress}

    @app.post("/api/label")
    def save_label(request: LabelRequest) -> dict[str, Any]:
        result = store.next_item(request.annotator)
        if result is None or result[0].item_id != request.item_id:
            raise HTTPException(
                status_code=409,
                detail="Item is not the annotator's current queue head; reload.",
            )
        _, proposal = result
        decision = _derive_decision(request, proposal_ror_ids=_proposed_set(proposal))
        label = Label(
            item_id=request.item_id,
            annotator=request.annotator,
            decision=decision,
            ror_ids=sorted(set(request.selected_ror_ids)),
            chosen_ranks=request.selected_ranks,
            justification_viewed=request.justification_viewed,
            note=request.note,
            elapsed_ms=request.elapsed_ms,
            submitted_at=now_iso(),
        )
        label_id = store.save_label(label)
        log.info(
            "label_saved",
            label_id=label_id,
            item_id=label.item_id,
            decision=label.decision.value,
            elapsed_ms=label.elapsed_ms,
        )
        return {"label_id": label_id, "decision": label.decision.value}

    @app.post("/api/undo")
    def undo(request: UndoRequest) -> dict[str, Any]:
        item_id = store.undo_last(request.annotator)
        return {"undone_item_id": item_id}

    @app.get("/api/progress")
    def progress(annotator: str) -> dict[str, Any]:
        return store.progress(annotator)

    @app.get("/api/ror/search")
    def ror_search(q: str) -> dict[str, Any]:
        if ror_searcher is None:
            raise HTTPException(status_code=503, detail="ROR search not configured")
        if not q.strip():
            return {"candidates": []}
        candidates = ror_searcher.search(q.strip())
        return {"candidates": [candidate.model_dump() for candidate in candidates[:10]]}

    return app


def _proposed_set(proposal: Any) -> set[str]:
    if proposal is None:
        return set()
    return {candidate.ror_id for candidate in proposal.candidates if candidate.llm_proposed}


def _derive_decision(request: LabelRequest, proposal_ror_ids: set[str]) -> Decision:
    if request.outcome == "skipped":
        return Decision.SKIPPED
    if request.outcome == "ambiguous":
        return Decision.AMBIGUOUS
    if request.outcome == "no_ror":
        return Decision.NO_ROR
    if request.outcome == "resolved":
        if not request.selected_ror_ids:
            raise HTTPException(status_code=422, detail="resolved outcome needs >= 1 ROR id")
        if set(request.selected_ror_ids) == proposal_ror_ids:
            return Decision.ACCEPTED
        return Decision.CORRECTED
    raise HTTPException(status_code=422, detail=f"Unknown outcome {request.outcome!r}")
