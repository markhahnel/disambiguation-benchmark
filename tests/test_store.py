"""Label store: idempotent loads, deterministic queue, undo audit trail."""

from pathlib import Path

from disambig.models import Decision, Item, Label
from disambig.store import ReviewStore, now_iso


def make_items(n: int) -> list[Item]:
    return [
        Item(
            item_id=f"item{i:03d}",
            raw_affiliation=f"Affiliation {i}",
            stratum="anglophone_university" if i % 2 == 0 else "hospital_medical",
            source_frame="test",
        )
        for i in range(n)
    ]


def make_label(item_id: str, decision: Decision = Decision.CORRECTED) -> Label:
    return Label(
        item_id=item_id,
        annotator="mark",
        decision=decision,
        ror_ids=["https://ror.org/052gg0110"] if decision != Decision.SKIPPED else [],
        chosen_ranks=[1],
        elapsed_ms=1200,
        submitted_at=now_iso(),
    )


def test_load_is_idempotent(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "review.sqlite")
    assert store.load_items(make_items(5), []) == 5
    assert store.load_items(make_items(5), []) == 0


def test_queue_is_deterministic_and_advances_on_label(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "review.sqlite")
    store.load_items(make_items(5), [])
    first = store.next_item("mark")
    assert first is not None
    again = store.next_item("mark")
    assert again is not None and again[0].item_id == first[0].item_id
    store.save_label(make_label(first[0].item_id))
    third = store.next_item("mark")
    assert third is not None and third[0].item_id != first[0].item_id


def test_skip_returns_item_to_queue(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "review.sqlite")
    store.load_items(make_items(1), [])
    head = store.next_item("mark")
    assert head is not None
    store.save_label(make_label(head[0].item_id, Decision.SKIPPED))
    assert store.next_item("mark") is not None  # skipped items come back


def test_undo_supersedes_without_deleting(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "review.sqlite")
    store.load_items(make_items(2), [])
    head = store.next_item("mark")
    assert head is not None
    store.save_label(make_label(head[0].item_id))
    undone = store.undo_last("mark")
    assert undone == head[0].item_id
    assert store.next_item("mark") is not None
    assert store.next_item("mark")[0].item_id == head[0].item_id  # type: ignore[index]
    assert store.export_labels() == []  # superseded labels do not export


def test_annotators_have_independent_queues(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "review.sqlite")
    store.load_items(make_items(1), [])
    head = store.next_item("mark")
    assert head is not None
    store.save_label(make_label(head[0].item_id))
    assert store.next_item("mark") is None
    assert store.next_item("second") is not None  # IAA annotator sees it fresh


def test_progress_counts_and_timing(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "review.sqlite")
    store.load_items(make_items(4), [])
    head = store.next_item("mark")
    assert head is not None
    store.save_label(make_label(head[0].item_id))
    progress = store.progress("mark")
    assert progress["done"] == 1 and progress["total"] == 4
    stratum = progress["strata"][head[0].stratum]
    assert stratum["done"] == 1
    assert stratum["mean_elapsed_ms"] == 1200


def test_export_includes_item_and_label_fields(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "review.sqlite")
    store.load_items(make_items(1), [])
    head = store.next_item("mark")
    assert head is not None
    store.save_label(make_label(head[0].item_id))
    exported = store.export_labels()
    assert len(exported) == 1
    assert exported[0]["item"]["item_id"] == head[0].item_id
    assert exported[0]["ror_ids"] == ["https://ror.org/052gg0110"]
    assert exported[0]["elapsed_ms"] == 1200
