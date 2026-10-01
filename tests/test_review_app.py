"""Review API: accept/correct derivation happens server-side and the queue
head is enforced, so client bugs cannot corrupt labels.

The second half pins the pinned-release check (METHODS.md section 6): given
the ROR release the scorer will run against, the API refuses a label whose
ids that release does not contain and hides search results it does not
contain. Without a release (demo mode) nothing changes. The release is
either a small fake with the one method the API asks of it, or the real
RorDump over tests/fixtures/ror_dump_sample.json, so the Protocol is proven
against the object scripts/review_ui.py actually passes.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import structlog.testing
from fastapi.testclient import TestClient

from disambig.models import Candidate, Item, Proposal
from disambig.review_app import RorRelease, create_app
from disambig.ror_dump import RorDumpPin, iter_dump_records, sha256_file
from disambig.store import ReviewStore

REPO = Path(__file__).resolve().parents[1]
SAMPLE_DUMP = REPO / "tests" / "fixtures" / "ror_dump_sample.json"

OXFORD = "https://ror.org/052gg0110"
IOP = "https://ror.org/05cvf7v30"
# The parent of IOP. It is deliberately absent from the dump sample (see
# tests/test_ror_dump.py), which makes it the id a real release does not hold.
CAS = "https://ror.org/034t30j35"
# Well formed, never issued by ROR, absent from every dump; the matching tests
# use it for the same purpose.
NEVER_ISSUED = "https://ror.org/0zzzzzz99"


class FakeSearcher:
    def search(self, query: str, refresh: bool = False) -> list[Candidate]:
        return [
            Candidate(ror_id=OXFORD, name=f"Result for {query}"),
            Candidate(ror_id=IOP, name=f"Second result for {query}"),
        ]


class FakeRelease:
    """A pinned release reduced to the one thing the review API asks of it."""

    def __init__(self, *ror_ids: str) -> None:
        self._ids = frozenset(ror_ids)

    def __contains__(self, ror_id: str) -> bool:
        return ror_id in self._ids


def build_client(tmp_path: Path, ror_release: RorRelease | None = None) -> TestClient:
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
    return TestClient(create_app(store, ror_searcher=FakeSearcher(), ror_release=ror_release))


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
        assert "WARNING" not in banner

        # The real RorDump is the release object the API gets in production.
        client = build_client(tmp_path, ror_release=release)
        refused = client.post(
            "/api/label", json=label_body(current_item_id(client), selected_ror_ids=[CAS])
        )
        assert refused.status_code == 422
        assert CAS in refused.json()["detail"]
        saved = client.post("/api/label", json=label_body(current_item_id(client)))
        assert saved.status_code == 200
    finally:
        release.close()
