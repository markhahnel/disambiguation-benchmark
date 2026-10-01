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
  contains, and only ids that are active in it (METHODS.md sections 2 and
  6). The scorer resolves hierarchy against that release and crashes on a
  gold id it cannot see, and it raises on a gold id that is not active,
  because a gold label must name the active record: an assigned id that is
  not active in the pinned release never grounds under any rule, so a gold
  label on a superseded record would score every source that reports the
  current identifier as wrong. The only remedies at scoring time, moving the
  pin or editing a frozen gold standard, are both forbidden. So both
  refusals happen here, at labelling time, with a 422 that names each
  offending id. For an id that is not active the refusal has to be
  actionable: it follows the successor chain, to any depth, to the first
  active record on each branch and names those records with their display
  names as the ones to pick. The chain is read from the scorer's own
  :class:`disambig.matching.SuccessionIndex`, never from the record's
  ``successor`` field alone, so an edge counts whichever end of it ROR wrote
  down: a successor edge on the old record or a predecessor edge on the new
  one. The release holds dead records that only the new record points at
  (NHS Digital 03am1eg44 names no successor, and NHS England 00xm3h672
  names it as predecessor), and reading one end would tell the annotator
  there is no successor and to label the item no_ror, after which every
  source that correctly assigns the live record would be charged a false
  positive. Going through the one index means the UI and the scorer can
  never disagree about who succeeded whom. The walk stops at an active
  record rather than continuing past it, because an active record is a live
  organisation whatever its own successor edges say (the release does hold
  a few active records with successors, a university whose institute was
  spun out, say), and it keeps a seen set so a curation error that closed a
  loop ends the walk instead of hanging the request. Where no active record
  is reachable, because no edge from either end leaves the record or every
  chain ends at another record that is not active (the release holds a
  cluster of withdrawn Argosy University campuses whose shared successor is
  itself inactive), the message says so and tells the annotator to label
  the item no_ror, since the organisation has no active record in the
  pinned release, rather than naming a dead successor the save would refuse
  again. ROR search
  results the release does not contain are hidden with a warning in the log
  per hidden candidate. Results that are not active are deliberately left
  visible: an annotator looking up a historical name needs to find the old
  record and read its successor relationship on the candidate card, and
  only the save is refused. Without a release (demo mode) nothing is
  checked.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from disambig.matching import RorGraph, SuccessionIndex, succession_index_for
from disambig.models import Candidate, Decision, Label
from disambig.ror_ids import normalise_ror_id
from disambig.store import ReviewStore, now_iso

log = structlog.get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class RorSearcher(Protocol):
    def search(self, query: str, refresh: bool = False) -> list[Candidate]: ...


class NamedRecord(Protocol):
    """What the refusal message reads off a release record: its display name.

    ``RorDump.get`` returns a ``RorRecord`` whose ``name`` property is the
    ROR display name, which is all this needs.
    """

    @property
    def name(self) -> str: ...


class RorRelease(RorGraph, Protocol):
    """The pinned ROR release, as far as labelling needs it.

    Everything :class:`disambig.matching.RorGraph` asks of a release
    (membership, status, the relationship sets and the whole relationship
    index, which is what :class:`SuccessionIndex` is built from), plus
    ``get`` for the display name the refusal message prints. Each is spelled
    exactly as ``RorDump`` spells it, so the one ``RorDump`` the scorer loads
    serves the review UI unchanged, and the succession walk here runs over
    the same index the scorer uses. ``status``, ``relationships`` and ``get``
    may raise ``KeyError`` for an id the release does not contain, as
    ``RorDump`` does; the membership check always runs first.
    """

    def get(self, ror_id: str) -> NamedRecord: ...


def succession_of(release: RorRelease) -> SuccessionIndex:
    """The scorer's succession index over this release, built once per release object.

    Thin alias over :func:`disambig.matching.succession_index_for` so the
    review app has exactly one way of asking who succeeded whom, and it is
    the scorer's. The cache is matching's own, keyed weakly on the release
    object, so the UI and a scoring run over the same ``RorDump`` share one
    index.
    """
    return succession_index_for(release)


ACTIVE_STATUS = "active"


@dataclass(frozen=True)
class ReleaseRecord:
    """One record of the pinned release, as the refusal message names it."""

    ror_id: str
    name: str
    status: str

    def describe(self) -> str:
        return f"{self.ror_id} ({self.name})"

    def describe_with_status(self) -> str:
        return f"{self.ror_id} ({self.name}, {self.status})"

    def log_fields(self) -> dict[str, str]:
        return {"ror_id": self.ror_id, "name": self.name, "status": self.status}


def release_record(ror_id: str, release: RorRelease) -> ReleaseRecord:
    """The record behind a canonical id the release contains."""
    return ReleaseRecord(
        ror_id=ror_id,
        name=release.get(ror_id).name,
        status=release.status(ror_id),
    )


@dataclass(frozen=True)
class SuccessionResolution:
    """Where a record's successor chain leads in the pinned release.

    ``active`` holds the first active record reached on each branch, in the
    order the walk reached them (breadth first, ids sorted within a hop), so
    a message built from it is deterministic. ``passed_through`` holds the
    records that are not active which the walk crossed on the way, in the
    same order: each is either a link to the next hop or a dead end.
    ``outside_release`` holds successor ids the release does not contain,
    whose status is therefore unknowable here. The pinned v2.13 release has
    none, but a walk has to handle one explicitly rather than crash the UI or
    quietly drop it. Only a successor edge on the old record can produce one:
    an edge asserted from the new end is asserted by a record the release
    holds, by construction.
    """

    active: tuple[ReleaseRecord, ...]
    passed_through: tuple[ReleaseRecord, ...]
    outside_release: tuple[str, ...]

    def log_fields(self) -> dict[str, Any]:
        return {
            "active_successors": [record.log_fields() for record in self.active],
            "passed_through": [record.log_fields() for record in self.passed_through],
            "successors_outside_release": list(self.outside_release),
        }


def resolve_active_successors(ror_id: str, release: RorRelease) -> SuccessionResolution:
    """Follow successor edges from a canonical id the release contains, to any depth.

    The edges come from the scorer's :class:`SuccessionIndex`, so an edge is
    followed whether the old record asserts a successor or the new record
    asserts a predecessor; see the module docstring for why reading one end
    is wrong. Breadth first, stopping each branch at the first active record:
    an active record is a live organisation whatever its own successor edges
    say, so naming the successor of a live record would send the annotator
    past the right answer. A seen set bounds the walk. The pinned release has
    no cycle among its successor edges, and this code does not assume that: a
    curation error that closed a loop ends the walk rather than hanging the
    request. A successor the release does not contain is recorded and logged,
    not followed, because nothing can be said about its status.
    """
    succession = succession_of(release)
    seen = {ror_id}
    frontier = [ror_id]
    active: list[ReleaseRecord] = []
    passed_through: list[ReleaseRecord] = []
    outside_release: list[str] = []
    while frontier:
        next_frontier: list[str] = []
        for current in frontier:
            for successor in sorted(succession.successors_of(current)):
                if successor in seen:
                    continue
                seen.add(successor)
                if successor not in release:
                    log.warning(
                        "successor_outside_pinned_release",
                        ror_id=current,
                        successor=successor,
                    )
                    outside_release.append(successor)
                    continue
                record = release_record(successor, release)
                if record.status == ACTIVE_STATUS:
                    active.append(record)
                else:
                    passed_through.append(record)
                    next_frontier.append(successor)
        frontier = next_frontier
    return SuccessionResolution(
        active=tuple(active),
        passed_through=tuple(passed_through),
        outside_release=tuple(outside_release),
    )


NO_ROR_INSTRUCTION = (
    "the organisation has no active ROR record in the pinned release: label the item no_ror"
)


@dataclass(frozen=True)
class NotActiveId:
    """A selected id the pinned release holds under a status other than active.

    ``ror_id`` is spelled as the annotator sent it; ``name`` and ``status``
    are the release's. ``successors`` are the record's direct successors in
    the scorer's succession index, canonical and sorted, whichever end of the
    edge the release wrote down, and ``resolution`` is where following them
    leads, which is what the message is built from.
    """

    ror_id: str
    name: str
    status: str
    successors: tuple[str, ...]
    resolution: SuccessionResolution

    def describe(self) -> str:
        """One sentence telling the annotator what to do about this id.

        No semicolons here: the save handler joins one description per id
        with them.
        """
        head = f"{self.ror_id} ({self.name}) is {self.status}"
        if not self.successors:
            return (
                f"{head} and has no successor in the release (it names no successor and "
                f"no record names it as a predecessor), so {NO_ROR_INSTRUCTION}"
            )
        active = self.resolution.active
        if not active:
            return (
                f"{head} and its successor chain reaches no active record "
                f"({self._dead_ends()}), so {NO_ROR_INSTRUCTION}"
            )
        via = ""
        if self.resolution.passed_through:
            crossed = ", ".join(
                record.describe_with_status() for record in self.resolution.passed_through
            )
            via = f", reached through {crossed}"
        if len(active) == 1:
            return f"{head}, pick its active successor {active[0].describe()} instead{via}"
        named = ", ".join(record.describe() for record in active)
        return (
            f"{head}, its successor chain leads to {len(active)} active records{via}, "
            f"pick the one the affiliation names: {named}"
        )

    def _dead_ends(self) -> str:
        parts = [
            record.describe_with_status() for record in self.resolution.passed_through
        ]
        parts.extend(
            f"{successor} is not in the pinned release"
            for successor in self.resolution.outside_release
        )
        if not parts:
            # Every successor edge pointed back at the record itself.
            return "its successor edges lead only back to itself"
        return "it leads only to " + ", ".join(parts)

    def log_fields(self) -> dict[str, Any]:
        return {
            "ror_id": self.ror_id,
            "name": self.name,
            "status": self.status,
            "successors": list(self.successors),
            **self.resolution.log_fields(),
        }


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
            not_active = ids_not_active(request.selected_ror_ids, ror_release)
            if not_active:
                log.warning(
                    "label_refused_ids_not_active_in_pinned_release",
                    item_id=request.item_id,
                    annotator=request.annotator,
                    outcome=request.outcome,
                    not_active_ror_ids=[entry.log_fields() for entry in not_active],
                )
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Refusing to label with ROR ids that are not active in the pinned "
                        "ROR release, because a gold label must name the active record: "
                        + "; ".join(entry.describe() for entry in not_active)
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


def ids_not_active(ror_ids: Iterable[str], release: RorRelease) -> list[NotActiveId]:
    """The ids the release holds under a status other than active, spelled as sent,
    each with its successor chain resolved to the active records to pick instead.

    Status is looked up on the normalised form, like membership, so a
    formatting difference never hides a superseded record. Ids the release
    does not contain, or that are not shaped like ROR ids, are not this
    function's concern: ids_outside_release reports them, and the save
    handler runs that first, so a status is never asked of an id the release
    cannot answer for. Successors are read from the scorer's succession
    index, from either end of each edge, never from the record's own
    ``successor`` field.
    """
    succession = succession_of(release)
    not_active: list[NotActiveId] = []
    for ror_id in dict.fromkeys(ror_ids):
        try:
            canonical = normalise_ror_id(ror_id)
        except ValueError:
            continue
        if canonical not in release:
            continue
        status = release.status(canonical)
        if status != ACTIVE_STATUS:
            not_active.append(
                NotActiveId(
                    ror_id=ror_id,
                    name=release.get(canonical).name,
                    status=status,
                    successors=tuple(sorted(succession.successors_of(canonical))),
                    resolution=resolve_active_successors(canonical, release),
                )
            )
    return not_active


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
