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
- Given the pinned ROR release, a label may only carry ids the release
  contains (METHODS.md section 6): the scorer resolves hierarchy against that
  release and crashes on a gold id it cannot see, and the only remedies at
  that point, moving the pin or editing a frozen gold standard, are both
  forbidden. So the refusal happens here, at labelling time, with a 422 that
  names the id, and ROR search results the release does not contain are
  hidden with a warning in the log per hidden candidate. Without a release
  (demo mode) nothing is checked.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from disambig.models import Candidate, Decision, Label
from disambig.ror_ids import normalise_ror_id
from disambig.store import ReviewStore, now_iso

log = structlog.get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class RorSearcher(Protocol):
    def search(self, query: str, refresh: bool = False) -> list[Candidate]: ...


class RorRelease(Protocol):
    """The pinned ROR release, as far as labelling needs it: membership only.

    Spelled ``__contains__`` to match ``matching.RorGraph``, so the one
    ``RorDump`` the scorer loads serves the review UI unchanged. Any set of
    canonical ids satisfies it too, which is what the tests pass.
    """

    def __contains__(self, ror_id: str, /) -> bool: ...


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


def create_app(
    store: ReviewStore,
    ror_searcher: RorSearcher | None = None,
    ror_release: RorRelease | None = None,
) -> FastAPI:
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
        if ror_release is not None:
            unknown = ids_outside_release(request.selected_ror_ids, ror_release)
            if unknown:
                log.warning(
                    "label_refused_ids_outside_pinned_release",
                    item_id=request.item_id,
                    annotator=request.annotator,
                    outcome=request.outcome,
                    unknown_ror_ids=unknown,
                )
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Refusing to label with ROR ids the pinned ROR release does not "
                        "contain (the scorer could not resolve them): " + ", ".join(unknown)
                    ),
                )
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
        query = q.strip()
        if not query:
            return {"candidates": []}
        candidates = ror_searcher.search(query)
        if ror_release is not None:
            candidates = within_release(candidates, ror_release, query=query)
        return {"candidates": [candidate.model_dump() for candidate in candidates[:10]]}

    return app


def ids_outside_release(ror_ids: Iterable[str], release: RorRelease) -> list[str]:
    """The ids the pinned release does not contain, spelled as they were sent.

    Membership is checked on the normalised form, the same canonicalisation
    the scorer applies to every id, so a formatting difference is never
    mistaken for an unknown organisation. An id that cannot be normalised is
    not shaped like a ROR id at all, so the release cannot contain it either.
    """
    unknown: list[str] = []
    for ror_id in dict.fromkeys(ror_ids):
        try:
            canonical = normalise_ror_id(ror_id)
        except ValueError:
            unknown.append(ror_id)
            continue
        if canonical not in release:
            unknown.append(ror_id)
    return unknown


def within_release(
    candidates: list[Candidate], release: RorRelease, *, query: str
) -> list[Candidate]:
    """Drop search results the pinned release does not contain, one warning each.

    The annotator never sees a dropped organisation, so the log is the only
    place its absence is visible. Each warning names the candidate and the
    query, so a run of them against one query is legible afterwards as "the
    organisation the annotator wanted was newer than the pinned release".
    """
    kept: list[Candidate] = []
    for candidate in candidates:
        if ids_outside_release((candidate.ror_id,), release):
            log.warning(
                "ror_search_candidate_outside_pinned_release",
                ror_id=candidate.ror_id,
                name=candidate.name,
                query=query,
            )
            continue
        kept.append(candidate)
    return kept


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
