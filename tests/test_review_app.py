"""Review API: accept/correct derivation happens server-side and the queue
head is enforced, so client bugs cannot corrupt labels."""

from pathlib import Path

from fastapi.testclient import TestClient

from disambig.models import Candidate, Item, Proposal
from disambig.review_app import create_app
from disambig.store import ReviewStore

OXFORD = "https://ror.org/052gg0110"
IOP = "https://ror.org/05cvf7v30"


class FakeSearcher:
    def search(self, query: str, refresh: bool = False) -> list[Candidate]:
        return [Candidate(ror_id=OXFORD, name=f"Result for {query}")]


def build_client(tmp_path: Path) -> TestClient:
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
    return TestClient(create_app(store, ror_searcher=FakeSearcher()))


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
