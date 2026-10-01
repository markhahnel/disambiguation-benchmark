"""Review API: accept/correct derivation happens server-side and the queue
head is enforced, so client bugs cannot corrupt labels.

The second half pins the pinned-release checks (METHODS.md sections 2 and 6):
given the ROR release the scorer will run against, the API refuses a label
whose ids that release does not contain, refuses a label whose ids are not
active in it (gate decision D1), and hides search results it does not
contain while leaving records that are not active findable. The refusal of
an id that is not active has to be actionable, so it follows the successor
chain to the first active record on each branch and names those records
with their display names, or, where no active record is reachable, tells
the annotator to label the item no_ror. Without a release (demo mode)
nothing changes. The release is either a small fake (matching.StaticRorGraph
wrapped with display names, so it is a real graph in miniature and the API
reads it through the same Protocol as the real one), or the real RorDump,
over tests/fixtures/ror_dump_sample.json and, when it is on disk, over the
full pinned release, so the Protocol is proven against the object
scripts/review_ui.py actually passes.

Succession is read through the scorer's matching.SuccessionIndex, which
takes an edge from whichever end asserts it. That matters: the release holds
dead records that only the new record points at, through its predecessor
list, and never name a successor themselves (NHS Digital, BEIS, Salford
Royal, the New Zealand district health boards). Reading the old record's
successor field alone would tell the annotator to label those items no_ror,
and a no_ror gold would charge every source that correctly assigns the live
successor a false positive. The one-sided cases below pin that the UI names
the same active record the scorer would credit.

Every organisation named below is a real record in the pinned release, with
its status, display name and succession edges quoted from the dump, on the
end the dump spells them. The succession topology the dump does not offer
(a cycle, a chain that runs on past an active record, a successor outside
the release) is built on the fake release from those real records and
flagged as fabricated where it is used; the dump has no such shape today
and the code must not assume it never will.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest
import structlog.testing
from fastapi.testclient import TestClient

from disambig.matching import (
    GoldLabel,
    GraphRecord,
    MatchRule,
    Outcome,
    Relation,
    StaticRorGraph,
    score_item,
    succession_index_for,
)
from disambig.models import Candidate, Decision, Item, Proposal, Relationship
from disambig.review_app import (
    RorRelease,
    RorSearcher,
    create_app,
    ids_not_active,
    resolve_active_successors,
)
from disambig.ror_dump import (
    RelationshipSets,
    RorDump,
    RorDumpPin,
    iter_dump_records,
    sha256_file,
)
from disambig.store import ReviewStore

REPO = Path(__file__).resolve().parents[1]
SAMPLE_DUMP = REPO / "tests" / "fixtures" / "ror_dump_sample.json"
PIN = REPO / "config" / "ror_dump.yaml"
DATA_ROOT = REPO / "data"

OXFORD = "https://ror.org/052gg0110"
IOP = "https://ror.org/05cvf7v30"
# The parent of IOP. It is deliberately absent from the dump sample (see
# tests/test_ror_dump.py), which makes it the id a real release does not hold.
CAS = "https://ror.org/034t30j35"
# Well formed, never issued by ROR, absent from every dump; the matching tests
# use it for the same purpose.
NEVER_ISSUED = "https://ror.org/0zzzzzz99"

# Records that are not active in the pinned release, each with the active
# record ROR names as its successor. The release records Kyushu Tokai
# University as inactive with Tokai University as its successor, and holds a
# withdrawn Public Library of Science record pointing at the live one.
KYUSHU_TOKAI = "https://ror.org/00w0f8567"
TOKAI = "https://ror.org/01p7qe739"
PLOS_WITHDRAWN = "https://ror.org/03bdvdc06"
PLOS = "https://ror.org/008zgvp64"
# Argosy University is inactive and names no successor at all.
ARGOSY = "https://ror.org/028ydjm20"
# The Argosy University cluster: nineteen withdrawn campus records (seventeen
# named Argosy University, two Art Institute of California) share one
# successor, 001p2e958, which is itself inactive and names no successor, so no
# chain out of the cluster reaches an active record.
ARGOSY_WITHDRAWN = "https://ror.org/05krtjb56"
ARGOSY_HUB = "https://ror.org/001p2e958"
# A real two-hop chain: Weston Area Health NHS Trust (inactive) names
# University Hospitals Bristol and Weston NHS Foundation Trust (inactive) as
# its successor, which names Bristol NHS Foundation Trust (active). The last
# two are in the dump sample; Weston is not.
WESTON = "https://ror.org/03fpf5m04"
UHBW = "https://ror.org/03jzzxg14"
BRISTOL_NHS = "https://ror.org/054vvq170"
# A real two-hop chain that splits: NSW Office of Environment & Heritage
# (inactive) names NSW Department of Planning and Environment (inactive),
# which names two active departments.
NSW_OEH = "https://ror.org/005bs2a16"
NSW_DPE = "https://ror.org/00067tc54"
NSW_DPHI = "https://ror.org/00a7xgz80"
NSW_DCCEEW = "https://ror.org/038pwz535"
# A real chain whose branches reach an active record at different depths: the
# Northern Territory's Department of Natural Resources, Environment, The Arts
# and Sport (inactive) names Department of Arts and Museums (active) and
# Department of Land Resource Management (inactive), which in turn names
# Department of Environment, Parks and Water Security (active).
NT_NRETAS = "https://ror.org/02x005z82"
NT_DLRM = "https://ror.org/01drzr533"
NT_DEPWS = "https://ror.org/00a50j303"
NT_DAM = "https://ror.org/03ga33f34"
# One of the release's active records that carries a successor edge to another
# active record: Academisch Ziekenhuis Rotterdam names Erasmus MC.
AZR = "https://ror.org/00xtwy257"
ERASMUS_MC = "https://ror.org/018906e22"
# Sholokhov Moscow State University for Humanities is inactive with Moscow
# State Pedagogical University as its successor. The former is in the dump
# sample and the latter is not, which makes the sample the one real RorDump
# whose successor edge points outside the release it was loaded from.
SHOLOKHOV = "https://ror.org/04dstfg97"
MSPU = "https://ror.org/03a9mf398"

# One-sided succession, as the dump records it. Each of these dead records
# names NO successor of its own; the edge exists only as a predecessor entry
# on the active record. NHS Digital is inactive and NHS England names it as
# predecessor.
NHS_DIGITAL = "https://ror.org/03am1eg44"
NHS_ENGLAND = "https://ror.org/00xm3h672"
# The Department for Business, Energy and Industrial Strategy is inactive and
# is named as predecessor by three active departments, none of which it names.
BEIS = "https://ror.org/019ya6433"
DESNZ = "https://ror.org/01s5a4v70"
DSIT = "https://ror.org/028z36n30"
DBT = "https://ror.org/030tdpg67"
# A real two-hop chain that is one-sided at both hops: Edinboro University
# (inactive, names nothing) is named as predecessor by California University
# of Pennsylvania (inactive, names nothing), which is named as predecessor by
# Pennsylvania Western University (active).
EDINBORO = "https://ror.org/001n0zm21"
CAL_U_PA = "https://ror.org/01spssf70"
PENN_WEST = "https://ror.org/01w8vjw54"
# Mixed ends on one record: Daimler (Germany) is inactive and names
# Mercedes-Benz (Germany) as its successor, while Daimler Truck (Germany),
# active, names Daimler (Germany) as its predecessor and is not named back.
DAIMLER_DE = "https://ror.org/00m0j3d84"
MERCEDES_DE = "https://ror.org/055rn2a38"
DAIMLER_TRUCK_DE = "https://ror.org/049ahjd53"

# ROR display names, as RorDump.get(ror_id).name returns them from the pinned
# release. The fake release serves these so every name a test quotes is real.
NAMES: Mapping[str, str] = {
    OXFORD: "University of Oxford",
    IOP: "Institute of Physics",
    KYUSHU_TOKAI: "Kyushu Tokai University",
    TOKAI: "Tokai University",
    PLOS_WITHDRAWN: "Public Library of Science",
    PLOS: "Public Library of Science",
    ARGOSY: "Argosy University",
    ARGOSY_WITHDRAWN: "Argosy University",
    ARGOSY_HUB: "Argosy University",
    WESTON: "Weston Area Health NHS Trust",
    UHBW: "University Hospitals Bristol and Weston NHS Foundation Trust",
    BRISTOL_NHS: "Bristol NHS Foundation Trust",
    NSW_OEH: "NSW Office of Environment & Heritage",
    NSW_DPE: "NSW Department of Planning and Environment",
    NSW_DPHI: "NSW Department of Planning, Housing and Infrastructure",
    NSW_DCCEEW: "NSW Department of Climate Change, Energy, the Environment and Water",
    NT_NRETAS: "Department of Natural Resources, Environment, The Arts and Sport",
    NT_DLRM: "Department of Land Resource Management",
    NT_DEPWS: "Department of Environment, Parks and Water Security",
    NT_DAM: "Department of Arts and Museums",
    AZR: "Academisch Ziekenhuis Rotterdam",
    ERASMUS_MC: "Erasmus MC",
    SHOLOKHOV: "Sholokhov Moscow State University for Humanities",
    MSPU: "Moscow State Pedagogical University",
    NHS_DIGITAL: "NHS Digital",
    NHS_ENGLAND: "NHS England",
    BEIS: "Department for Business, Energy and Industrial Strategy",
    DESNZ: "Department for Energy Security and Net Zero",
    DSIT: "Department for Science, Innovation and Technology",
    DBT: "Department for Business and Trade",
    EDINBORO: "Edinboro University",
    CAL_U_PA: "California University of Pennsylvania",
    PENN_WEST: "Pennsylvania Western University",
    DAIMLER_DE: "Daimler (Germany)",
    MERCEDES_DE: "Mercedes-Benz (Germany)",
    DAIMLER_TRUCK_DE: "Daimler Truck (Germany)",
}

NO_ROR_ADVICE = "label the item no_ror"


class FakeSearcher:
    def search(self, query: str, refresh: bool = False) -> list[Candidate]:
        return [
            Candidate(ror_id=OXFORD, name=f"Result for {query}"),
            Candidate(ror_id=IOP, name=f"Second result for {query}"),
        ]


class HistoricalNameSearcher:
    """What ROR search returns for a name that is no longer the current one:
    the superseded record, carrying its successor relationship, alongside a
    record the pinned release does not hold."""

    def search(self, query: str, refresh: bool = False) -> list[Candidate]:
        return [
            Candidate(
                ror_id=KYUSHU_TOKAI,
                name="Kyushu Tokai University",
                relationships=[
                    Relationship(rel_type="successor", label="Tokai University", ror_id=TOKAI)
                ],
            ),
            Candidate(ror_id=NEVER_ISSUED, name=f"Newer than the release: {query}"),
        ]


@dataclass(frozen=True)
class FakeRecord:
    """The one attribute the review API reads off a release record."""

    name: str


def record(
    status: str, successors: Iterable[str] = (), predecessors: Iterable[str] = ()
) -> GraphRecord:
    """A graph record with the succession edges this record itself asserts.

    ``successors`` is the record's own successor field, ``predecessors`` its
    predecessor field. Which end an edge is put on here is which end the dump
    puts it on, so a one-sided case in the fake is one-sided for the same
    reason it is in the release.
    """
    return GraphRecord(
        status=status,
        relationships=RelationshipSets(
            successor=frozenset(successors), predecessor=frozenset(predecessors)
        ),
    )


class FakeRelease:
    """A pinned release in miniature: matching.StaticRorGraph plus display names.

    Everything the RorRelease Protocol asks for is delegated to the graph
    (membership, status, relationship sets and the relationship index the
    SuccessionIndex is built from), so the review API reads the fake through
    exactly the path it reads a RorDump through, and succession in a test is
    whatever the scorer's index derives from the edges, never a hand-rolled
    successor table. Positional ids are active with no edges. ``records``
    maps an id to a GraphRecord built with :func:`record`. Names come from
    NAMES so that every name the fake serves is the real display name.

    A plain class on purpose: matching caches one SuccessionIndex per graph
    object in a WeakKeyDictionary, so the release has to be hashable by
    identity and weak-referenceable, as RorDump is.
    """

    def __init__(
        self,
        *active_ids: str,
        records: Mapping[str, GraphRecord] | None = None,
    ) -> None:
        graph_records: dict[str, GraphRecord] = {
            ror_id: GraphRecord(status="active") for ror_id in active_ids
        }
        graph_records.update(records or {})
        self._graph = StaticRorGraph(graph_records)

    def __contains__(self, ror_id: str) -> bool:
        return ror_id in self._graph

    def status(self, ror_id: str) -> str:
        return self._graph.status(ror_id)

    def relationships(self, ror_id: str) -> RelationshipSets:
        return self._graph.relationships(ror_id)

    @property
    def relationship_index(self) -> Mapping[str, RelationshipSets]:
        return self._graph.relationship_index

    def get(self, ror_id: str) -> FakeRecord:
        if ror_id not in self._graph:
            raise KeyError(ror_id)
        return FakeRecord(name=NAMES[ror_id])


def superseded_release() -> FakeRelease:
    """Active and not-active records side by side, as a real release holds them.

    Every edge here is one the pinned release records, on the end the release
    records it: the NHS Digital, BEIS, Edinboro and Daimler Truck edges exist
    in the dump only as predecessor entries on the newer record.
    """
    return FakeRelease(
        OXFORD,
        IOP,
        TOKAI,
        PLOS,
        BRISTOL_NHS,
        NSW_DPHI,
        NSW_DCCEEW,
        NT_DEPWS,
        NT_DAM,
        MERCEDES_DE,
        records={
            KYUSHU_TOKAI: record("inactive", successors={TOKAI}),
            PLOS_WITHDRAWN: record("withdrawn", successors={PLOS}),
            ARGOSY: record("inactive"),
            ARGOSY_WITHDRAWN: record("withdrawn", successors={ARGOSY_HUB}),
            ARGOSY_HUB: record("inactive"),
            WESTON: record("inactive", successors={UHBW}),
            UHBW: record("inactive", successors={BRISTOL_NHS}),
            NSW_OEH: record("inactive", successors={NSW_DPE}),
            NSW_DPE: record("inactive", successors={NSW_DPHI, NSW_DCCEEW}),
            NT_NRETAS: record("inactive", successors={NT_DLRM, NT_DAM}),
            NT_DLRM: record("inactive", successors={NT_DEPWS}),
            # One-sided: the dead record names nothing, the live one names it.
            NHS_DIGITAL: record("inactive"),
            NHS_ENGLAND: record("active", predecessors={NHS_DIGITAL}),
            BEIS: record("inactive"),
            DESNZ: record("active", predecessors={BEIS}),
            DSIT: record("active", predecessors={BEIS}),
            DBT: record("active", predecessors={BEIS}),
            EDINBORO: record("inactive"),
            CAL_U_PA: record("inactive", predecessors={EDINBORO}),
            PENN_WEST: record("active", predecessors={CAL_U_PA}),
            # Mixed: one edge from the old end, one from the new end.
            DAIMLER_DE: record("inactive", successors={MERCEDES_DE}),
            DAIMLER_TRUCK_DE: record("active", predecessors={DAIMLER_DE}),
        },
    )


def build_client(
    tmp_path: Path,
    ror_release: RorRelease | None = None,
    searcher: RorSearcher | None = None,
) -> TestClient:
    store = ReviewStore(tmp_path / "review.sqlite")
    items = [
        Item(
            item_id=f"item{i}",
            raw_affiliation=f"String {i}",
            stratum="anglophone_university",
            source_frame="test",
        )
        for i in range(2)
    ]
    proposals = [
        Proposal(
            item_id=item.item_id,
            candidates=[
                Candidate(ror_id=OXFORD, name="University of Oxford", llm_proposed=True,
                          match_score=0.4),
                Candidate(ror_id=IOP, name="Institute of Physics", match_score=0.9),
            ],
            run_id="test",
            created_at="2026-08-24T00:00:00Z",
        )
        for item in items
    ]
    store.load_items(items, proposals)
    return TestClient(
        create_app(store, ror_searcher=searcher or FakeSearcher(), ror_release=ror_release)
    )


def label_body(item_id: str, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "annotator": "mark",
        "item_id": item_id,
        "outcome": "resolved",
        "selected_ror_ids": [OXFORD],
        "selected_ranks": [1],
        "justification_viewed": False,
        "elapsed_ms": 900,
    }
    body.update(overrides)
    return body


def current_item_id(client: TestClient) -> str:
    payload = client.get("/api/next", params={"annotator": "mark"}).json()
    item_id = payload["item"]["item_id"]
    assert isinstance(item_id, str)
    return item_id


def refusal_detail(client: TestClient, *ror_ids: str) -> str:
    """Post a resolved label with these ids, assert it is refused, return the message."""
    response = client.post(
        "/api/label", json=label_body(current_item_id(client), selected_ror_ids=list(ror_ids))
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    return detail


def pick_advice(ror_id: str) -> str:
    """The phrase the message uses to name a single active record to pick."""
    return f"pick its active successor {ror_id} ({NAMES[ror_id]}) instead"


def test_candidates_ordered_by_match_score_not_llm(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    payload = client.get("/api/next", params={"annotator": "mark"}).json()
    candidates = payload["proposal"]["candidates"]
    assert candidates[0]["ror_id"] == IOP  # higher matcher score, not LLM pick


def test_selecting_llm_set_derives_accepted(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    response = client.post("/api/label", json=label_body(current_item_id(client)))
    assert response.status_code == 200
    assert response.json()["decision"] == "accepted"


def test_selecting_different_set_derives_corrected(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    response = client.post(
        "/api/label",
        json=label_body(current_item_id(client), selected_ror_ids=[IOP]),
    )
    assert response.json()["decision"] == "corrected"


def test_ambiguous_and_no_ror_are_valid_labels(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    first = client.post(
        "/api/label",
        json=label_body(current_item_id(client), outcome="ambiguous", selected_ror_ids=[]),
    )
    assert first.json()["decision"] == "ambiguous"
    second = client.post(
        "/api/label",
        json=label_body(current_item_id(client), outcome="no_ror", selected_ror_ids=[]),
    )
    assert second.json()["decision"] == "no_ror"


def test_resolved_without_selection_is_rejected(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    response = client.post(
        "/api/label", json=label_body(current_item_id(client), selected_ror_ids=[])
    )
    assert response.status_code == 422


def test_stale_item_is_rejected_with_409(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    head = current_item_id(client)
    client.post("/api/label", json=label_body(head))
    response = client.post("/api/label", json=label_body(head))  # already labelled
    assert response.status_code == 409


def test_ror_search_endpoint_uses_injected_searcher(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    payload = client.get("/api/ror/search", params={"q": "oxford"}).json()
    assert payload["candidates"][0]["ror_id"] == OXFORD


def test_queue_exhaustion_returns_null_item(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    for _ in range(2):
        client.post("/api/label", json=label_body(current_item_id(client)))
    payload = client.get("/api/next", params={"annotator": "mark"}).json()
    assert payload["item"] is None
    assert payload["progress"]["done"] == 2


# ---------------------------------------------------------------------------
# Gold ids are confined to the pinned ROR release
# ---------------------------------------------------------------------------


def test_label_with_an_id_outside_the_release_is_refused_with_422(tmp_path: Path) -> None:
    client = build_client(tmp_path, ror_release=FakeRelease(IOP))
    head = current_item_id(client)
    with structlog.testing.capture_logs() as entries:
        response = client.post("/api/label", json=label_body(head, selected_ror_ids=[OXFORD]))
    assert response.status_code == 422
    assert OXFORD in response.json()["detail"]
    # Nothing was saved: the item is still the queue head.
    assert current_item_id(client) == head
    refusals = [e for e in entries if e["event"] == "label_refused_ids_outside_pinned_release"]
    assert len(refusals) == 1
    assert refusals[0]["log_level"] == "warning"
    assert refusals[0]["unknown_ror_ids"] == [OXFORD]


def test_422_names_every_unknown_id_and_only_those(tmp_path: Path) -> None:
    client = build_client(tmp_path, ror_release=FakeRelease(IOP))
    response = client.post(
        "/api/label",
        json=label_body(current_item_id(client), selected_ror_ids=[OXFORD, IOP, NEVER_ISSUED]),
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert OXFORD in detail
    assert NEVER_ISSUED in detail
    assert IOP not in detail


def test_label_within_the_release_saves_with_unchanged_semantics(tmp_path: Path) -> None:
    client = build_client(tmp_path, ror_release=FakeRelease(OXFORD, IOP))
    response = client.post("/api/label", json=label_body(current_item_id(client)))
    assert response.status_code == 200
    assert response.json()["decision"] == "accepted"


def test_release_membership_is_checked_on_the_normalised_id(tmp_path: Path) -> None:
    client = build_client(tmp_path, ror_release=FakeRelease(OXFORD))
    # Bare, upper-cased spelling of the same organisation: a formatting
    # difference, not an unknown id, so it saves. The accepted/corrected
    # derivation still compares the strings as sent (label semantics are not
    # this change's to alter), so it reads as corrected.
    response = client.post(
        "/api/label",
        json=label_body(current_item_id(client), selected_ror_ids=["ROR.ORG/052GG0110"]),
    )
    assert response.status_code == 200
    assert response.json()["decision"] == "corrected"


def test_a_malformed_id_is_refused_when_a_release_is_pinned(tmp_path: Path) -> None:
    client = build_client(tmp_path, ror_release=FakeRelease(OXFORD))
    not_a_ror_id = "https://example.org/institutes/1234"
    response = client.post(
        "/api/label",
        json=label_body(current_item_id(client), selected_ror_ids=[not_a_ror_id]),
    )
    assert response.status_code == 422
    assert not_a_ror_id in response.json()["detail"]


def test_the_check_applies_to_every_outcome_that_carries_ids(tmp_path: Path) -> None:
    # The stored ror_ids travel with every outcome, so an ambiguous label with
    # a stray unknown selection is refused as well, rather than saved with an
    # id the release cannot resolve.
    client = build_client(tmp_path, ror_release=FakeRelease(IOP))
    response = client.post(
        "/api/label",
        json=label_body(current_item_id(client), outcome="ambiguous", selected_ror_ids=[OXFORD]),
    )
    assert response.status_code == 422


def test_ror_search_hides_candidates_outside_the_release_and_logs_each(tmp_path: Path) -> None:
    client = build_client(tmp_path, ror_release=FakeRelease(IOP))
    with structlog.testing.capture_logs() as entries:
        payload = client.get("/api/ror/search", params={"q": "oxford"}).json()
    assert [candidate["ror_id"] for candidate in payload["candidates"]] == [IOP]
    dropped = [e for e in entries if e["event"] == "ror_search_candidate_outside_pinned_release"]
    assert len(dropped) == 1
    assert dropped[0]["log_level"] == "warning"
    assert dropped[0]["ror_id"] == OXFORD
    assert dropped[0]["query"] == "oxford"


def test_demo_mode_without_a_release_checks_and_filters_nothing(tmp_path: Path) -> None:
    client = build_client(tmp_path, ror_release=None)
    response = client.post(
        "/api/label",
        json=label_body(current_item_id(client), selected_ror_ids=[NEVER_ISSUED]),
    )
    assert response.status_code == 200
    with structlog.testing.capture_logs() as entries:
        payload = client.get("/api/ror/search", params={"q": "oxford"}).json()
    assert [candidate["ror_id"] for candidate in payload["candidates"]] == [OXFORD, IOP]
    assert not [e for e in entries if e["event"].startswith("ror_search_candidate")]


# ---------------------------------------------------------------------------
# Gold ids must be ACTIVE in the pinned release (gate decision D1): a
# superseded or withdrawn record is refused, naming the successor to pick
# ---------------------------------------------------------------------------


def test_label_with_an_inactive_id_is_refused_naming_its_successor(tmp_path: Path) -> None:
    client = build_client(tmp_path, ror_release=superseded_release())
    head = current_item_id(client)
    with structlog.testing.capture_logs() as entries:
        response = client.post(
            "/api/label", json=label_body(head, selected_ror_ids=[KYUSHU_TOKAI])
        )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert KYUSHU_TOKAI in detail
    assert "inactive" in detail
    assert TOKAI in detail
    assert "successor" in detail
    # Nothing was saved: the item is still the queue head.
    assert current_item_id(client) == head
    refusals = [
        e for e in entries if e["event"] == "label_refused_ids_not_active_in_pinned_release"
    ]
    assert len(refusals) == 1
    assert refusals[0]["log_level"] == "warning"
    assert refusals[0]["not_active_ror_ids"] == [
        {
            "ror_id": KYUSHU_TOKAI,
            "name": "Kyushu Tokai University",
            "status": "inactive",
            "successors": [TOKAI],
            "active_successors": [
                {"ror_id": TOKAI, "name": "Tokai University", "status": "active"}
            ],
            "passed_through": [],
            "successors_outside_release": [],
        }
    ]


def test_label_with_a_withdrawn_id_is_refused_naming_its_status_and_successor(
    tmp_path: Path,
) -> None:
    client = build_client(tmp_path, ror_release=superseded_release())
    response = client.post(
        "/api/label", json=label_body(current_item_id(client), selected_ror_ids=[PLOS_WITHDRAWN])
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert PLOS_WITHDRAWN in detail
    assert "withdrawn" in detail
    assert PLOS in detail


def test_an_inactive_id_with_no_successor_is_refused_and_says_so(tmp_path: Path) -> None:
    # The release holds no active record to point the annotator at, so the
    # message has to say that, and say what to do instead, rather than name
    # a successor that is not there.
    client = build_client(tmp_path, ror_release=superseded_release())
    response = client.post(
        "/api/label", json=label_body(current_item_id(client), selected_ror_ids=[ARGOSY])
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert ARGOSY in detail
    assert "names no successor" in detail
    assert "no active ROR record" in detail
    assert NO_ROR_ADVICE in detail
    assert "pick" not in detail


def test_label_with_an_active_id_saves_when_the_release_holds_superseded_records(
    tmp_path: Path,
) -> None:
    client = build_client(tmp_path, ror_release=superseded_release())
    accepted = client.post("/api/label", json=label_body(current_item_id(client)))
    assert accepted.status_code == 200
    assert accepted.json()["decision"] == "accepted"
    # The successor the refusal points at is itself an ordinary active record.
    corrected = client.post(
        "/api/label", json=label_body(current_item_id(client), selected_ror_ids=[TOKAI])
    )
    assert corrected.status_code == 200
    assert corrected.json()["decision"] == "corrected"


def test_422_lists_only_the_ids_that_are_not_active(tmp_path: Path) -> None:
    client = build_client(tmp_path, ror_release=superseded_release())
    response = client.post(
        "/api/label",
        json=label_body(current_item_id(client), selected_ror_ids=[OXFORD, KYUSHU_TOKAI]),
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert KYUSHU_TOKAI in detail
    assert OXFORD not in detail


def test_one_422_names_every_id_that_is_not_active(tmp_path: Path) -> None:
    client = build_client(tmp_path, ror_release=superseded_release())
    with structlog.testing.capture_logs() as entries:
        response = client.post(
            "/api/label",
            json=label_body(
                current_item_id(client), selected_ror_ids=[KYUSHU_TOKAI, PLOS_WITHDRAWN]
            ),
        )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert KYUSHU_TOKAI in detail and TOKAI in detail
    assert PLOS_WITHDRAWN in detail and PLOS in detail
    refusals = [
        e for e in entries if e["event"] == "label_refused_ids_not_active_in_pinned_release"
    ]
    assert len(refusals) == 1
    assert [entry["ror_id"] for entry in refusals[0]["not_active_ror_ids"]] == [
        KYUSHU_TOKAI,
        PLOS_WITHDRAWN,
    ]


def test_status_is_checked_on_the_normalised_id(tmp_path: Path) -> None:
    # A bare, upper-cased spelling of the superseded record is the same
    # record, so it is refused, and the refusal spells the id as it was sent.
    client = build_client(tmp_path, ror_release=superseded_release())
    response = client.post(
        "/api/label",
        json=label_body(current_item_id(client), selected_ror_ids=["ROR.ORG/00W0F8567"]),
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "ROR.ORG/00W0F8567" in detail
    assert TOKAI in detail


def test_the_outside_release_refusal_comes_before_the_status_check(tmp_path: Path) -> None:
    # An id the release does not contain has no status to check, so a mixed
    # selection gets the membership refusal first; the status refusal follows
    # once the annotator has fixed that.
    client = build_client(tmp_path, ror_release=superseded_release())
    with structlog.testing.capture_logs() as entries:
        response = client.post(
            "/api/label",
            json=label_body(current_item_id(client), selected_ror_ids=[NEVER_ISSUED, KYUSHU_TOKAI]),
        )
    assert response.status_code == 422
    assert NEVER_ISSUED in response.json()["detail"]
    assert [e["event"] for e in entries if e["event"].startswith("label_refused")] == [
        "label_refused_ids_outside_pinned_release"
    ]


def test_the_status_check_applies_to_every_outcome_that_carries_ids(tmp_path: Path) -> None:
    client = build_client(tmp_path, ror_release=superseded_release())
    response = client.post(
        "/api/label",
        json=label_body(
            current_item_id(client), outcome="ambiguous", selected_ror_ids=[KYUSHU_TOKAI]
        ),
    )
    assert response.status_code == 422
    assert TOKAI in response.json()["detail"]


def test_ror_search_keeps_records_that_are_not_active_so_the_successor_can_be_read(
    tmp_path: Path,
) -> None:
    # An annotator looking up a historical name must be able to find the old
    # record and read its successor relationship on the candidate card. Only
    # the save is refused; the existing filtering of ids outside the release
    # is unchanged.
    client = build_client(
        tmp_path, ror_release=superseded_release(), searcher=HistoricalNameSearcher()
    )
    with structlog.testing.capture_logs() as entries:
        payload = client.get("/api/ror/search", params={"q": "kyushu tokai"}).json()
    assert [candidate["ror_id"] for candidate in payload["candidates"]] == [KYUSHU_TOKAI]
    relationships = payload["candidates"][0]["relationships"]
    assert relationships == [
        {"rel_type": "successor", "label": "Tokai University", "ror_id": TOKAI}
    ]
    dropped = [e for e in entries if e["event"] == "ror_search_candidate_outside_pinned_release"]
    assert [e["ror_id"] for e in dropped] == [NEVER_ISSUED]


def test_demo_mode_without_a_release_saves_an_inactive_id_unchecked(tmp_path: Path) -> None:
    client = build_client(tmp_path, ror_release=None)
    with structlog.testing.capture_logs() as entries:
        response = client.post(
            "/api/label",
            json=label_body(current_item_id(client), selected_ror_ids=[KYUSHU_TOKAI]),
        )
    assert response.status_code == 200
    assert not [e for e in entries if e["event"].startswith("label_refused")]


# ---------------------------------------------------------------------------
# The refusal is actionable: the successor chain is followed, to any depth,
# to the first active record on each branch, and those records are named with
# their display names. Where none is reachable the annotator is told to
# label the item no_ror instead of being pointed at another dead record.
# ---------------------------------------------------------------------------


def test_the_refusal_names_the_active_successor_with_its_display_name(tmp_path: Path) -> None:
    client = build_client(tmp_path, ror_release=superseded_release())
    detail = refusal_detail(client, KYUSHU_TOKAI)
    assert f"{KYUSHU_TOKAI} (Kyushu Tokai University) is inactive" in detail
    assert pick_advice(TOKAI) in detail
    assert "Tokai University" in detail
    assert NO_ROR_ADVICE not in detail


def test_a_two_hop_chain_resolves_to_the_final_active_record(tmp_path: Path) -> None:
    # Weston names UHBW, which is itself inactive, so naming Weston's direct
    # successor would send the annotator to a record the save refuses again.
    # The message names Bristol NHS Foundation Trust, two hops on, as the one
    # to pick, and shows the inactive record it passed through.
    client = build_client(tmp_path, ror_release=superseded_release())
    head = current_item_id(client)
    with structlog.testing.capture_logs() as entries:
        detail = refusal_detail(client, WESTON)
    assert f"{WESTON} (Weston Area Health NHS Trust) is inactive" in detail
    assert pick_advice(BRISTOL_NHS) in detail
    assert (
        f"reached through {UHBW} "
        "(University Hospitals Bristol and Weston NHS Foundation Trust, inactive)"
    ) in detail
    assert pick_advice(UHBW) not in detail
    refusal = next(
        e for e in entries if e["event"] == "label_refused_ids_not_active_in_pinned_release"
    )
    [entry] = refusal["not_active_ror_ids"]
    assert entry["successors"] == [UHBW]
    assert entry["active_successors"] == [
        {"ror_id": BRISTOL_NHS, "name": "Bristol NHS Foundation Trust", "status": "active"}
    ]
    assert entry["passed_through"] == [
        {
            "ror_id": UHBW,
            "name": "University Hospitals Bristol and Weston NHS Foundation Trust",
            "status": "inactive",
        }
    ]
    # Following the advice saves.
    saved = client.post("/api/label", json=label_body(head, selected_ror_ids=[BRISTOL_NHS]))
    assert saved.status_code == 200
    assert saved.json()["decision"] == "corrected"


def test_a_chain_that_splits_names_every_active_record_it_reaches(tmp_path: Path) -> None:
    # The NSW Office of Environment & Heritage was folded into a department
    # that was later split in two. Both active departments are named, in id
    # order, and the annotator is told to pick the one the affiliation names.
    client = build_client(tmp_path, ror_release=superseded_release())
    detail = refusal_detail(client, NSW_OEH)
    assert f"{NSW_OEH} (NSW Office of Environment & Heritage) is inactive" in detail
    assert "leads to 2 active records" in detail
    assert f"reached through {NSW_DPE} (NSW Department of Planning and Environment, inactive)" in (
        detail
    )
    assert (
        "pick the one the affiliation names: "
        f"{NSW_DPHI} (NSW Department of Planning, Housing and Infrastructure), "
        f"{NSW_DCCEEW} (NSW Department of Climate Change, Energy, the Environment and Water)"
    ) in detail
    assert NO_ROR_ADVICE not in detail


def test_each_branch_stops_at_its_own_first_active_record(tmp_path: Path) -> None:
    # One branch reaches an active record in one hop, the other in two. Both
    # are named; the inactive record on the longer branch is shown as passed
    # through, not as something to pick.
    resolution = resolve_active_successors(NT_NRETAS, superseded_release())
    assert [record.ror_id for record in resolution.active] == [NT_DAM, NT_DEPWS]
    assert [record.ror_id for record in resolution.passed_through] == [NT_DLRM]
    assert resolution.outside_release == ()


def test_the_walk_does_not_continue_past_an_active_record(tmp_path: Path) -> None:
    # The release holds a few active records that carry a successor edge to
    # another active record (Academisch Ziekenhuis Rotterdam names Erasmus
    # MC). An active record is a live organisation whatever its edges say, so
    # the walk names it and does not run on to its successor. No record that
    # is not active names one of those in the pinned release, so the edge
    # from Kyushu Tokai University to Rotterdam here is FABRICATED to put a
    # real active-to-active edge at the end of a chain; the three records and
    # the Rotterdam edge are real.
    release = FakeRelease(
        ERASMUS_MC,
        records={
            KYUSHU_TOKAI: record("inactive", successors={AZR}),
            AZR: record("active", successors={ERASMUS_MC}),
        },
    )
    # The index sees both edges; the walk still stops at the live record.
    assert succession_index_for(release).supersedes(KYUSHU_TOKAI, ERASMUS_MC)
    client = build_client(tmp_path, ror_release=release)
    detail = refusal_detail(client, KYUSHU_TOKAI)
    assert pick_advice(AZR) in detail
    assert ERASMUS_MC not in detail
    resolution = resolve_active_successors(KYUSHU_TOKAI, release)
    assert [record.ror_id for record in resolution.active] == [AZR]
    assert resolution.passed_through == ()


def test_no_active_successor_reachable_yields_the_no_ror_instruction(tmp_path: Path) -> None:
    # A withdrawn Argosy University campus names the Argosy University hub
    # record, which is inactive and names nothing, so no chain out of the
    # cluster reaches an active record. The message must not name the hub as
    # the record to pick (the save would refuse it again); it says the
    # organisation has no active record and tells the annotator what to do.
    client = build_client(tmp_path, ror_release=superseded_release())
    head = current_item_id(client)
    with structlog.testing.capture_logs() as entries:
        detail = refusal_detail(client, ARGOSY_WITHDRAWN)
    assert f"{ARGOSY_WITHDRAWN} (Argosy University) is withdrawn" in detail
    assert "reaches no active record" in detail
    assert f"it leads only to {ARGOSY_HUB} (Argosy University, inactive)" in detail
    assert "no active ROR record" in detail
    assert NO_ROR_ADVICE in detail
    assert "pick" not in detail
    refusal = next(
        e for e in entries if e["event"] == "label_refused_ids_not_active_in_pinned_release"
    )
    [entry] = refusal["not_active_ror_ids"]
    assert entry["active_successors"] == []
    assert [record["ror_id"] for record in entry["passed_through"]] == [ARGOSY_HUB]
    # Following the advice saves.
    saved = client.post(
        "/api/label", json=label_body(head, outcome="no_ror", selected_ror_ids=[])
    )
    assert saved.status_code == 200
    assert saved.json()["decision"] == "no_ror"


def test_a_cycle_in_the_successor_chain_terminates(tmp_path: Path) -> None:
    # The pinned release has no cycle among its successor edges (checked
    # against v2.13), and the walk must not assume that. The back edge from
    # the Argosy hub to the Argosy record is FABRICATED; both records are real
    # and both are inactive, so the loop reaches no active record and the
    # message gives the no_ror instruction instead of hanging the request.
    release = FakeRelease(
        OXFORD,
        records={
            ARGOSY: record("inactive", successors={ARGOSY_HUB}),
            ARGOSY_HUB: record("inactive", successors={ARGOSY}),
        },
    )
    resolution = resolve_active_successors(ARGOSY, release)
    assert resolution.active == ()
    assert [record.ror_id for record in resolution.passed_through] == [ARGOSY_HUB]
    client = build_client(tmp_path, ror_release=release)
    detail = refusal_detail(client, ARGOSY)
    assert f"it leads only to {ARGOSY_HUB} (Argosy University, inactive)" in detail
    assert NO_ROR_ADVICE in detail


def test_a_self_loop_terminates_and_says_so(tmp_path: Path) -> None:
    # The degenerate cycle: a record naming itself as its successor. FABRICATED
    # on the real Argosy record. Nothing is reached, and the message says so
    # rather than listing an empty chain.
    release = FakeRelease(OXFORD, records={ARGOSY: record("inactive", successors={ARGOSY})})
    resolution = resolve_active_successors(ARGOSY, release)
    assert resolution.active == ()
    assert resolution.passed_through == ()
    client = build_client(tmp_path, ror_release=release)
    detail = refusal_detail(client, ARGOSY)
    assert "lead only back to itself" in detail
    assert NO_ROR_ADVICE in detail


def test_a_cycle_beside_an_active_branch_still_resolves(tmp_path: Path) -> None:
    # A loop on one branch must not stop the walk finding the active records
    # on another. The back edge from the NSW department to the office is
    # FABRICATED; the forward edges and the four records are real.
    release = FakeRelease(
        NSW_DPHI,
        NSW_DCCEEW,
        records={
            NSW_OEH: record("inactive", successors={NSW_DPE}),
            NSW_DPE: record("inactive", successors={NSW_OEH, NSW_DPHI, NSW_DCCEEW}),
        },
    )
    resolution = resolve_active_successors(NSW_OEH, release)
    assert [record.ror_id for record in resolution.active] == [NSW_DPHI, NSW_DCCEEW]
    assert [record.ror_id for record in resolution.passed_through] == [NSW_DPE]
    client = build_client(tmp_path, ror_release=release)
    detail = refusal_detail(client, NSW_OEH)
    assert "leads to 2 active records" in detail
    assert NO_ROR_ADVICE not in detail


def test_a_successor_outside_the_release_is_reported_not_followed(tmp_path: Path) -> None:
    # Every successor edge in v2.13 points at a record the release contains.
    # A release where that is not so (this edge is FABRICATED) cannot say
    # whether the successor is active, so the walk records the id, logs it,
    # and the message says the pinned release holds no active record to
    # name, rather than crashing on the status lookup or dropping the edge.
    release = FakeRelease(
        OXFORD, records={ARGOSY: record("inactive", successors={NEVER_ISSUED})}
    )
    client = build_client(tmp_path, ror_release=release)
    with structlog.testing.capture_logs() as entries:
        detail = refusal_detail(client, ARGOSY)
    assert f"{NEVER_ISSUED} is not in the pinned release" in detail
    assert NO_ROR_ADVICE in detail
    outside = [e for e in entries if e["event"] == "successor_outside_pinned_release"]
    assert len(outside) == 1
    assert outside[0]["log_level"] == "warning"
    assert outside[0]["ror_id"] == ARGOSY
    assert outside[0]["successor"] == NEVER_ISSUED
    refusal = next(
        e for e in entries if e["event"] == "label_refused_ids_not_active_in_pinned_release"
    )
    [entry] = refusal["not_active_ror_ids"]
    assert entry["successors_outside_release"] == [NEVER_ISSUED]
    assert entry["active_successors"] == []


def test_one_message_gives_each_refused_id_its_own_advice(tmp_path: Path) -> None:
    # A pick instruction for the id that has an active successor and the
    # no_ror instruction for the one that has none, in the order sent.
    client = build_client(tmp_path, ror_release=superseded_release())
    detail = refusal_detail(client, WESTON, ARGOSY_WITHDRAWN)
    weston_part, argosy_part = detail.split("; ")
    assert weston_part.endswith(
        f"reached through {UHBW} "
        "(University Hospitals Bristol and Weston NHS Foundation Trust, inactive)"
    )
    assert pick_advice(BRISTOL_NHS) in weston_part
    assert argosy_part.startswith(f"{ARGOSY_WITHDRAWN} (Argosy University) is withdrawn")
    assert argosy_part.endswith(NO_ROR_ADVICE)


# ---------------------------------------------------------------------------
# Succession is read from either end of the edge, through the scorer's own
# SuccessionIndex. A dead record that names no successor but is named as a
# predecessor by an active record resolves to that record, exactly as the
# scorer would credit it; the UI and the scorer cannot disagree.
# ---------------------------------------------------------------------------


def test_a_record_named_only_as_predecessor_resolves_to_that_active_successor(
    tmp_path: Path,
) -> None:
    # NHS Digital names no successor; NHS England names NHS Digital as its
    # predecessor. Reading the old record's successor field would say "no
    # successor, label no_ror". The index reads the edge from the new end.
    release = superseded_release()
    assert release.relationships(NHS_DIGITAL).successor == frozenset()
    assert release.relationships(NHS_ENGLAND).predecessor == frozenset({NHS_DIGITAL})
    client = build_client(tmp_path, ror_release=release)
    head = current_item_id(client)
    with structlog.testing.capture_logs() as entries:
        detail = refusal_detail(client, NHS_DIGITAL)
    assert f"{NHS_DIGITAL} (NHS Digital) is inactive" in detail
    assert pick_advice(NHS_ENGLAND) in detail
    assert "NHS England" in detail
    assert NO_ROR_ADVICE not in detail
    refusal = next(
        e for e in entries if e["event"] == "label_refused_ids_not_active_in_pinned_release"
    )
    [entry] = refusal["not_active_ror_ids"]
    assert entry["successors"] == [NHS_ENGLAND]
    assert entry["active_successors"] == [
        {"ror_id": NHS_ENGLAND, "name": "NHS England", "status": "active"}
    ]
    assert entry["passed_through"] == []
    # Following the advice saves.
    saved = client.post("/api/label", json=label_body(head, selected_ror_ids=[NHS_ENGLAND]))
    assert saved.status_code == 200
    assert saved.json()["decision"] == "corrected"


def test_a_two_hop_one_sided_chain_resolves_to_the_final_active_record(
    tmp_path: Path,
) -> None:
    # Both hops exist only as predecessor entries on the newer record:
    # California University of Pennsylvania names Edinboro, Pennsylvania
    # Western University names California. Neither dead record names
    # anything. The walk crosses the inactive middle record and names the
    # active one at the end.
    release = superseded_release()
    assert release.relationships(EDINBORO).successor == frozenset()
    assert release.relationships(CAL_U_PA).successor == frozenset()
    client = build_client(tmp_path, ror_release=release)
    head = current_item_id(client)
    with structlog.testing.capture_logs() as entries:
        detail = refusal_detail(client, EDINBORO)
    assert f"{EDINBORO} (Edinboro University) is inactive" in detail
    assert pick_advice(PENN_WEST) in detail
    assert f"reached through {CAL_U_PA} (California University of Pennsylvania, inactive)" in (
        detail
    )
    assert pick_advice(CAL_U_PA) not in detail
    assert NO_ROR_ADVICE not in detail
    refusal = next(
        e for e in entries if e["event"] == "label_refused_ids_not_active_in_pinned_release"
    )
    [entry] = refusal["not_active_ror_ids"]
    assert entry["successors"] == [CAL_U_PA]
    assert [r["ror_id"] for r in entry["active_successors"]] == [PENN_WEST]
    assert [r["ror_id"] for r in entry["passed_through"]] == [CAL_U_PA]
    saved = client.post("/api/label", json=label_body(head, selected_ror_ids=[PENN_WEST]))
    assert saved.status_code == 200


def test_a_record_named_as_predecessor_by_several_active_records_lists_all_of_them(
    tmp_path: Path,
) -> None:
    # BEIS was split three ways in 2023. It names no successor; each of the
    # three new departments names it as predecessor. All three are listed,
    # in id order, and the annotator is told to pick the one the affiliation
    # names.
    release = superseded_release()
    assert release.relationships(BEIS).successor == frozenset()
    client = build_client(tmp_path, ror_release=release)
    detail = refusal_detail(client, BEIS)
    assert (
        f"{BEIS} (Department for Business, Energy and Industrial Strategy) is inactive"
    ) in detail
    assert "leads to 3 active records" in detail
    assert (
        "pick the one the affiliation names: "
        f"{DESNZ} (Department for Energy Security and Net Zero), "
        f"{DSIT} (Department for Science, Innovation and Technology), "
        f"{DBT} (Department for Business and Trade)"
    ) in detail
    assert "reached through" not in detail
    assert NO_ROR_ADVICE not in detail
    [entry] = ids_not_active([BEIS], release)
    assert entry.successors == (DESNZ, DSIT, DBT)


def test_edges_asserted_from_both_ends_are_merged_into_one_successor_set(
    tmp_path: Path,
) -> None:
    # Daimler (Germany) names Mercedes-Benz (Germany) as successor; Daimler
    # Truck (Germany) names Daimler (Germany) as predecessor and is not named
    # back. Reading one end gives one successor; the index gives both.
    release = superseded_release()
    assert release.relationships(DAIMLER_DE).successor == frozenset({MERCEDES_DE})
    assert release.relationships(DAIMLER_TRUCK_DE).predecessor == frozenset({DAIMLER_DE})
    client = build_client(tmp_path, ror_release=release)
    detail = refusal_detail(client, DAIMLER_DE)
    assert "leads to 2 active records" in detail
    assert (
        "pick the one the affiliation names: "
        f"{DAIMLER_TRUCK_DE} (Daimler Truck (Germany)), "
        f"{MERCEDES_DE} (Mercedes-Benz (Germany))"
    ) in detail
    [entry] = ids_not_active([DAIMLER_DE], release)
    assert entry.successors == (DAIMLER_TRUCK_DE, MERCEDES_DE)


def test_the_ui_names_exactly_the_record_the_scorer_credits() -> None:
    # The contract this whole block exists for. For every record that is not
    # active in the fake, the active records the refusal names are precisely
    # the active ids the scorer's SuccessionIndex says it supersedes; and
    # scoring the one-sided dead id against the gold the UI pointed at comes
    # out STALE (the right institution under a superseded identifier), not
    # WRONG, under every rule.
    release = superseded_release()
    index = succession_index_for(release)
    not_active = [
        ror_id for ror_id in NAMES if ror_id in release and release.status(ror_id) != "active"
    ]
    assert NHS_DIGITAL in not_active and BEIS in not_active and EDINBORO in not_active
    for ror_id in not_active:
        resolution = resolve_active_successors(ror_id, release)
        named = {r.ror_id for r in resolution.active}
        credited = {
            other
            for other in NAMES
            if other in release
            and release.status(other) == "active"
            and index.supersedes(ror_id, other)
        }
        assert named == credited, ror_id
    gold = GoldLabel(decision=Decision.CORRECTED, ror_ids=frozenset({NHS_ENGLAND}))
    result = score_item(gold, [NHS_DIGITAL], release)
    assert result.relations[0].relation is Relation.PREDECESSOR_OF_GOLD
    assert all(result.outcome(rule) is Outcome.STALE for rule in MatchRule)
    assert result.stale_assigned == frozenset({NHS_DIGITAL})


def test_a_dead_record_with_no_edge_from_either_end_says_so(tmp_path: Path) -> None:
    # Argosy University names nothing and nothing names it, so the message
    # has to say both: the annotator should not go looking for a predecessor
    # entry elsewhere either.
    client = build_client(tmp_path, ror_release=superseded_release())
    detail = refusal_detail(client, ARGOSY)
    assert "names no successor and no record names it as a predecessor" in detail
    assert NO_ROR_ADVICE in detail


# ---------------------------------------------------------------------------
# scripts/review_ui.py: the pin is loaded through RorDump.from_pin, and the
# real RorDump satisfies the release Protocol end to end
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def review_ui() -> Iterator[ModuleType]:
    """Import scripts/review_ui.py by path; it is a script, not a module."""
    path = REPO / "scripts" / "review_ui.py"
    spec = importlib.util.spec_from_file_location("review_ui", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        del sys.modules[spec.name]


def sample_pin() -> RorDumpPin:
    """A pin describing the dump sample, so from_pin can verify it.

    The sample is a JSON file lifted from the v2.13 release, so the JSON hash
    and the organisation count are computed from it here. The zip-only fields
    (archive size, Zenodo md5, archive sha256) describe no file that exists
    and are zero placeholders: test scaffolding, not a measurement.
    """
    return RorDumpPin(
        version="v2.13",
        zenodo_doi="10.5281/zenodo.22902037",
        zenodo_concept_doi="10.5281/zenodo.6347574",
        record_id=22902037,
        publication_date="2026-09-22",
        filename="v2.13-2026-09-22-ror-data.zip",
        size_bytes=0,
        md5_zenodo="0" * 32,
        sha256_zip="0" * 64,
        v2_json_filename="v2.13-2026-09-22-ror-data.json",
        sha256_v2_json=sha256_file(SAMPLE_DUMP),
        organisation_count=sum(1 for _ in iter_dump_records(SAMPLE_DUMP)),
        downloaded_at="2026-10-01T00:00:00+00:00",
        source_url="https://zenodo.org/api/records/22902037/files/"
        "v2.13-2026-09-22-ror-data.zip/content",
        listing_url="https://zenodo.org/api/records?q=parent.id:6347574",
    )


def test_review_ui_without_a_pin_runs_unchecked_and_says_so(
    review_ui: ModuleType, tmp_path: Path
) -> None:
    with structlog.testing.capture_logs() as entries:
        release = review_ui.load_ror_release(tmp_path / "ror_dump.yaml", tmp_path / "data")
    assert release is None
    assert any(
        e["event"] == "review_ui_no_ror_pin" and e["log_level"] == "warning" for e in entries
    )
    banner = review_ui.startup_banner(8377, None)
    warnings = [line for line in banner.splitlines() if "WARNING" in line]
    assert len(warnings) == 1
    assert "not being checked against a pinned ROR release" in warnings[0]


def test_review_ui_with_a_pin_loads_the_release_and_confines_labels_to_it(
    review_ui: ModuleType, tmp_path: Path
) -> None:
    pin = sample_pin()
    data_root = tmp_path / "data"
    json_path = pin.json_path(data_root)
    json_path.parent.mkdir(parents=True)
    shutil.copy(SAMPLE_DUMP, json_path)
    pin_path = tmp_path / "ror_dump.yaml"
    pin.to_yaml(pin_path)

    release = review_ui.load_ror_release(pin_path, data_root)
    assert release is not None
    try:
        banner = review_ui.startup_banner(8377, release.pin)
        assert pin.version in banner
        assert pin.publication_date in banner
        assert "not active" in banner
        assert "WARNING" not in banner

        # The real RorDump is the release object the API gets in production.
        client = build_client(tmp_path, ror_release=release)
        refused = client.post(
            "/api/label", json=label_body(current_item_id(client), selected_ror_ids=[CAS])
        )
        assert refused.status_code == 422
        assert CAS in refused.json()["detail"]
        # The sample holds the superseded Bristol trust record with its
        # successor, and the names come off the real records.
        detail = refusal_detail(client, UHBW)
        assert (
            f"{UHBW} (University Hospitals Bristol and Weston NHS Foundation Trust) is inactive"
        ) in detail
        assert pick_advice(BRISTOL_NHS) in detail
        # The sample also holds Sholokhov Moscow State University for
        # Humanities but not its successor, so against this release the chain
        # leaves the release: the real RorDump path for a successor it does
        # not contain, which the full release never exercises.
        with structlog.testing.capture_logs() as entries:
            detail = refusal_detail(client, SHOLOKHOV)
        assert f"{SHOLOKHOV} (Sholokhov Moscow State University for Humanities) is inactive" in (
            detail
        )
        assert f"{MSPU} is not in the pinned release" in detail
        assert NO_ROR_ADVICE in detail
        outside = [e for e in entries if e["event"] == "successor_outside_pinned_release"]
        assert [e["successor"] for e in outside] == [MSPU]
        saved = client.post("/api/label", json=label_body(current_item_id(client)))
        assert saved.status_code == 200
    finally:
        release.close()


@pytest.fixture(scope="module")
def pinned_release() -> Iterator[RorDump]:
    """The real pinned release, loaded as scripts/review_ui.py loads it.

    A fresh clone has the pin but not the dump: the dump is gitignored and
    fetched by scripts/pin_ror_dump.py. Without it the test skips, saying so,
    rather than failing or quietly passing against something smaller.
    """
    pin = RorDumpPin.from_yaml(PIN)
    json_path = pin.json_path(DATA_ROOT)
    if not json_path.exists():
        pytest.skip(
            f"pinned ROR release {pin.version} is not on disk at {json_path}; "
            "run scripts/pin_ror_dump.py to fetch it"
        )
    with RorDump.from_pin(PIN, DATA_ROOT) as release:
        yield release


def test_real_pinned_release_refuses_a_real_inactive_id_and_names_its_successor(
    pinned_release: RorDump, tmp_path: Path
) -> None:
    # Hand-checked against the dump sample: the release records Kyushu Tokai
    # University as inactive, with Tokai University as its one successor.
    assert pinned_release.status(KYUSHU_TOKAI) == "inactive"
    assert pinned_release.successors(KYUSHU_TOKAI) == frozenset({TOKAI})
    assert pinned_release.status(TOKAI) == "active"
    assert pinned_release.get(KYUSHU_TOKAI).name == "Kyushu Tokai University"
    assert pinned_release.get(TOKAI).name == "Tokai University"

    client = build_client(tmp_path, ror_release=pinned_release)
    head = current_item_id(client)
    refused = client.post("/api/label", json=label_body(head, selected_ror_ids=[KYUSHU_TOKAI]))
    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert f"{KYUSHU_TOKAI} (Kyushu Tokai University) is inactive" in detail
    assert pick_advice(TOKAI) in detail
    assert current_item_id(client) == head

    # Following the message's advice saves.
    saved = client.post("/api/label", json=label_body(head, selected_ror_ids=[TOKAI]))
    assert saved.status_code == 200
    assert saved.json()["decision"] == "corrected"


def test_real_pinned_release_follows_a_two_hop_chain_to_the_active_record(
    pinned_release: RorDump, tmp_path: Path
) -> None:
    # The release records Weston Area Health NHS Trust as inactive, naming
    # University Hospitals Bristol and Weston NHS Foundation Trust, itself
    # inactive, which names Bristol NHS Foundation Trust, active.
    assert pinned_release.status(WESTON) == "inactive"
    assert pinned_release.successors(WESTON) == frozenset({UHBW})
    assert pinned_release.status(UHBW) == "inactive"
    assert pinned_release.successors(UHBW) == frozenset({BRISTOL_NHS})
    assert pinned_release.status(BRISTOL_NHS) == "active"
    assert pinned_release.get(WESTON).name == "Weston Area Health NHS Trust"
    assert (
        pinned_release.get(UHBW).name
        == "University Hospitals Bristol and Weston NHS Foundation Trust"
    )
    assert pinned_release.get(BRISTOL_NHS).name == "Bristol NHS Foundation Trust"

    client = build_client(tmp_path, ror_release=pinned_release)
    head = current_item_id(client)
    detail = refusal_detail(client, WESTON)
    assert f"{WESTON} (Weston Area Health NHS Trust) is inactive" in detail
    assert pick_advice(BRISTOL_NHS) in detail
    assert (
        f"reached through {UHBW} "
        "(University Hospitals Bristol and Weston NHS Foundation Trust, inactive)"
    ) in detail
    assert pick_advice(UHBW) not in detail

    saved = client.post("/api/label", json=label_body(head, selected_ror_ids=[BRISTOL_NHS]))
    assert saved.status_code == 200
    assert saved.json()["decision"] == "corrected"


def test_real_pinned_release_gives_the_no_ror_instruction_for_the_argosy_cluster(
    pinned_release: RorDump, tmp_path: Path
) -> None:
    # Every withdrawn Argosy campus record names the hub record 001p2e958,
    # which the release holds as inactive with no successor of its own.
    assert pinned_release.status(ARGOSY_WITHDRAWN) == "withdrawn"
    assert pinned_release.successors(ARGOSY_WITHDRAWN) == frozenset({ARGOSY_HUB})
    assert pinned_release.status(ARGOSY_HUB) == "inactive"
    assert pinned_release.successors(ARGOSY_HUB) == frozenset()
    assert pinned_release.get(ARGOSY_WITHDRAWN).name == "Argosy University"
    assert pinned_release.get(ARGOSY_HUB).name == "Argosy University"

    client = build_client(tmp_path, ror_release=pinned_release)
    head = current_item_id(client)
    detail = refusal_detail(client, ARGOSY_WITHDRAWN)
    assert f"{ARGOSY_WITHDRAWN} (Argosy University) is withdrawn" in detail
    assert f"it leads only to {ARGOSY_HUB} (Argosy University, inactive)" in detail
    assert NO_ROR_ADVICE in detail
    assert "pick" not in detail

    saved = client.post("/api/label", json=label_body(head, outcome="no_ror", selected_ror_ids=[]))
    assert saved.status_code == 200
    assert saved.json()["decision"] == "no_ror"


def test_real_pinned_release_names_both_ends_of_a_split_chain(
    pinned_release: RorDump, tmp_path: Path
) -> None:
    assert pinned_release.status(NSW_OEH) == "inactive"
    assert pinned_release.successors(NSW_OEH) == frozenset({NSW_DPE})
    assert pinned_release.status(NSW_DPE) == "inactive"
    assert pinned_release.successors(NSW_DPE) == frozenset({NSW_DPHI, NSW_DCCEEW})
    assert pinned_release.status(NSW_DPHI) == "active"
    assert pinned_release.status(NSW_DCCEEW) == "active"

    client = build_client(tmp_path, ror_release=pinned_release)
    detail = refusal_detail(client, NSW_OEH)
    assert "leads to 2 active records" in detail
    assert (
        "pick the one the affiliation names: "
        f"{NSW_DPHI} (NSW Department of Planning, Housing and Infrastructure), "
        f"{NSW_DCCEEW} (NSW Department of Climate Change, Energy, the Environment and Water)"
    ) in detail


def test_the_real_ror_dump_satisfies_the_release_protocol_unchanged(
    pinned_release: RorDump,
) -> None:
    # The Protocol is now RorGraph plus get(). RorDump was not touched; this
    # pins that it still fits structurally (the annotated assignment is what
    # mypy checks) and that the review app's index is the very object the
    # scorer caches for the same release.
    release: RorRelease = pinned_release
    assert KYUSHU_TOKAI in release
    assert release.status(KYUSHU_TOKAI) == "inactive"
    assert release.relationships(KYUSHU_TOKAI).successor == frozenset({TOKAI})
    assert KYUSHU_TOKAI in release.relationship_index
    assert release.get(KYUSHU_TOKAI).name == "Kyushu Tokai University"
    assert succession_index_for(release) is succession_index_for(pinned_release)


def test_real_pinned_release_refuses_nhs_digital_naming_nhs_england(
    pinned_release: RorDump, tmp_path: Path
) -> None:
    # The one-sided case in the release itself. NHS Digital is inactive and
    # carries no successor edge; NHS England is active and names NHS Digital
    # as its predecessor. RorDump.successors, the record's own field, is
    # empty, and the scorer's index is not.
    assert pinned_release.status(NHS_DIGITAL) == "inactive"
    assert pinned_release.successors(NHS_DIGITAL) == frozenset()
    assert pinned_release.status(NHS_ENGLAND) == "active"
    assert pinned_release.predecessors(NHS_ENGLAND) == frozenset({NHS_DIGITAL})
    assert pinned_release.get(NHS_DIGITAL).name == "NHS Digital"
    assert pinned_release.get(NHS_ENGLAND).name == "NHS England"
    assert succession_index_for(pinned_release).successors_of(NHS_DIGITAL) == frozenset(
        {NHS_ENGLAND}
    )

    client = build_client(tmp_path, ror_release=pinned_release)
    head = current_item_id(client)
    with structlog.testing.capture_logs() as entries:
        detail = refusal_detail(client, NHS_DIGITAL)
    assert f"{NHS_DIGITAL} (NHS Digital) is inactive" in detail
    assert pick_advice(NHS_ENGLAND) in detail
    assert "NHS England" in detail
    assert NHS_ENGLAND in detail
    assert NO_ROR_ADVICE not in detail
    refusal = next(
        e for e in entries if e["event"] == "label_refused_ids_not_active_in_pinned_release"
    )
    [entry] = refusal["not_active_ror_ids"]
    assert entry["successors"] == [NHS_ENGLAND]
    assert entry["active_successors"] == [
        {"ror_id": NHS_ENGLAND, "name": "NHS England", "status": "active"}
    ]
    assert current_item_id(client) == head

    # Following the advice saves, and the scorer agrees with the advice.
    saved = client.post("/api/label", json=label_body(head, selected_ror_ids=[NHS_ENGLAND]))
    assert saved.status_code == 200
    assert saved.json()["decision"] == "corrected"
    gold = GoldLabel(decision=Decision.CORRECTED, ror_ids=frozenset({NHS_ENGLAND}))
    result = score_item(gold, [NHS_DIGITAL], pinned_release)
    assert all(result.outcome(rule) is Outcome.STALE for rule in MatchRule)


def test_real_pinned_release_lists_the_three_departments_that_name_beis(
    pinned_release: RorDump, tmp_path: Path
) -> None:
    assert pinned_release.status(BEIS) == "inactive"
    assert pinned_release.successors(BEIS) == frozenset()
    for department in (DESNZ, DSIT, DBT):
        assert pinned_release.status(department) == "active"
        assert BEIS in pinned_release.predecessors(department)
    client = build_client(tmp_path, ror_release=pinned_release)
    detail = refusal_detail(client, BEIS)
    assert "leads to 3 active records" in detail
    assert (
        "pick the one the affiliation names: "
        f"{DESNZ} (Department for Energy Security and Net Zero), "
        f"{DSIT} (Department for Science, Innovation and Technology), "
        f"{DBT} (Department for Business and Trade)"
    ) in detail


def test_real_pinned_release_follows_a_two_hop_one_sided_chain(
    pinned_release: RorDump, tmp_path: Path
) -> None:
    assert pinned_release.status(EDINBORO) == "inactive"
    assert pinned_release.successors(EDINBORO) == frozenset()
    assert pinned_release.status(CAL_U_PA) == "inactive"
    assert pinned_release.successors(CAL_U_PA) == frozenset()
    assert EDINBORO in pinned_release.predecessors(CAL_U_PA)
    assert pinned_release.status(PENN_WEST) == "active"
    assert pinned_release.predecessors(PENN_WEST) == frozenset({CAL_U_PA})
    client = build_client(tmp_path, ror_release=pinned_release)
    detail = refusal_detail(client, EDINBORO)
    assert pick_advice(PENN_WEST) in detail
    assert f"reached through {CAL_U_PA} (California University of Pennsylvania, inactive)" in (
        detail
    )
    assert pick_advice(CAL_U_PA) not in detail
