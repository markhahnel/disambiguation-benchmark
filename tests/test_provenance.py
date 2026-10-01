"""Provenance entries, and the snapshot aggregate hash they cite.

The two belong together: provenance is only worth anything if the
source_snapshot it records is a stable digest of the data that produced the
artefact, so both halves of that chain are checked here, along with the
script that writes the manifest.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from disambig.provenance import (
    ProvenanceEntry,
    ProvenanceError,
    load_provenance,
    record_provenance,
)
from disambig.snapshot import build_manifest, load_aggregate, sha256_file, write_manifest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from hash_snapshot import main as hash_main  # noqa: E402
from hash_snapshot import snapshot_date_from_config  # noqa: E402


@pytest.fixture(autouse=True)
def reset_root_logging() -> Iterator[None]:
    """Drop the handlers configure_logging installs, so a stream captured by
    one test is not written to after that test has closed it."""
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


def make(
    rows_in: int = 100,
    rows_out: int = 50,
    query: str = "stratum == 'company'",
    run_at: str = "2026-09-08T00:00:00+00:00",
) -> ProvenanceEntry:
    return ProvenanceEntry(
        script="scripts/figure_example.py",
        source_snapshot="0123456789abcdef0123",
        query=query,
        rows_in=rows_in,
        rows_out=rows_out,
        run_id="20260908T000000Z-abc123",
        run_at=run_at,
    )


def seed_snapshot(tmp_path: Path) -> Path:
    raw = tmp_path / "raw"
    (raw / "openalex" / "2026-08-25").mkdir(parents=True)
    (raw / "openalex" / "2026-08-25" / "pilot_company_0a1b2c3d.jsonl.gz").write_bytes(
        b"\x1f\x8b\x08\x00"
    )
    (raw / "crossref" / "2026-08-25").mkdir(parents=True)
    (raw / "crossref" / "2026-08-25" / "page_0001.jsonl.gz").write_bytes(b"\x1f\x8b\x08\x01")
    return raw


def write_payload(path: Path, artefact: str, **overrides: object) -> None:
    """Write a provenance file by hand with one field altered, as a hand edit would."""
    record_provenance(path, artefact, make())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[artefact].update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_roundtrip_and_replace(tmp_path: Path) -> None:
    path = tmp_path / "provenance.json"
    record_provenance(path, "figures/second.html", make())
    record_provenance(path, "figures/first.html", make(rows_out=7))
    record_provenance(path, "figures/second.html", make(rows_out=9))
    loaded = load_provenance(path)
    assert list(loaded) == ["figures/first.html", "figures/second.html"]  # sorted
    assert loaded["figures/second.html"].rows_out == 9
    assert loaded["figures/first.html"].query == "stratum == 'company'"
    assert loaded["figures/first.html"] == make(rows_out=7)


def test_missing_file_is_empty_not_an_error(tmp_path: Path) -> None:
    assert load_provenance(tmp_path / "nothing.json") == {}


def test_gold_standard_freeze_is_recordable(tmp_path: Path) -> None:
    # METHODS.md commits to hashing the frozen gold standard before any
    # evaluated source is queried; that hash lives here.
    path = tmp_path / "provenance.json"
    record_provenance(
        path,
        "gold/benchmark_a_frozen.jsonl",
        ProvenanceEntry(
            script="scripts/freeze_gold.py",
            source_snapshot="0123456789abcdef0123",
            query="all rows",
            rows_in=50,
            rows_out=50,
            run_id="20260908T000000Z-abc123",
            run_at="2026-09-08T00:00:00+00:00",
        ),
    )
    assert load_provenance(path)["gold/benchmark_a_frozen.jsonl"].script == (
        "scripts/freeze_gold.py"
    )


def test_empty_query_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProvenanceError, match=r"empty query .use 'all rows'"):
        record_provenance(tmp_path / "p.json", "figures/x.html", make(query="  "))


def test_negative_rows_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProvenanceError, match="negative rows_out"):
        record_provenance(tmp_path / "p.json", "figures/x.html", make(rows_out=-1))


def test_empty_artefact_name_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProvenanceError, match="artefact name is empty"):
        record_provenance(tmp_path / "p.json", " ", make())


def test_malformed_file_rejected(tmp_path: Path) -> None:
    path = tmp_path / "provenance.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ProvenanceError, match="top level"):
        load_provenance(path)


# --- real type checks --------------------------------------------------------


@pytest.mark.parametrize("field", ["script", "source_snapshot", "query", "run_id", "run_at"])
def test_none_text_field_is_refused_not_recorded_as_the_word_none(
    tmp_path: Path, field: str
) -> None:
    # str(None).strip() is "None", which the old check accepted. A None must
    # be refused as a None.
    entry = ProvenanceEntry(**{**make().__dict__, field: None})
    with pytest.raises(ProvenanceError, match=f"non-string {field}"):
        record_provenance(tmp_path / "p.json", "figures/x.html", entry)
    assert not (tmp_path / "p.json").exists()


@pytest.mark.parametrize(
    ("field", "bad"),
    [("script", 7), ("source_snapshot", ["h"]), ("query", {"q": 1}), ("run_id", 1.5)],
)
def test_non_string_text_field_rejected(tmp_path: Path, field: str, bad: object) -> None:
    entry = ProvenanceEntry(**{**make().__dict__, field: bad})
    with pytest.raises(ProvenanceError, match=f"non-string {field}"):
        entry.validate("figures/x.html")


@pytest.mark.parametrize("field", ["rows_in", "rows_out"])
@pytest.mark.parametrize("bad", [True, False, "50", 50.0, None])
def test_non_integer_row_count_rejected(tmp_path: Path, field: str, bad: object) -> None:
    # bool is a subclass of int: a row count of True is a bug, not one row.
    entry = ProvenanceEntry(**{**make().__dict__, field: bad})
    with pytest.raises(ProvenanceError, match=f"non-integer {field}"):
        entry.validate("figures/x.html")


def test_more_rows_out_than_in_is_allowed(tmp_path: Path) -> None:
    # A per-stratum-by-source pivot legitimately emits more rows than it
    # reads; see the comment in ProvenanceEntry.validate.
    path = tmp_path / "provenance.json"
    record_provenance(path, "tables/pivot.csv", make(rows_in=50, rows_out=200))
    assert load_provenance(path)["tables/pivot.csv"].rows_out == 200


@pytest.mark.parametrize("run_at", ["yesterday", "08/09/2026", "2026-13-40T00:00:00"])
def test_non_iso_run_at_rejected(tmp_path: Path, run_at: str) -> None:
    with pytest.raises(ProvenanceError, match="ISO 8601"):
        record_provenance(tmp_path / "p.json", "figures/x.html", make(run_at=run_at))


# --- loading validates every entry ------------------------------------------


def test_hand_edited_none_script_rejected_on_load(tmp_path: Path) -> None:
    path = tmp_path / "provenance.json"
    write_payload(path, "figures/x.html", script=None)
    with pytest.raises(ProvenanceError, match=r"'figures/x.html' has non-string script"):
        load_provenance(path)


def test_hand_edited_missing_field_rejected_on_load(tmp_path: Path) -> None:
    path = tmp_path / "provenance.json"
    path.write_text('{"figures/x.html": {"script": "s", "rows_in": 1}}', encoding="utf-8")
    with pytest.raises(ProvenanceError, match="missing source_snapshot, query, rows_out"):
        load_provenance(path)


def test_hand_edited_unexpected_field_rejected_on_load(tmp_path: Path) -> None:
    path = tmp_path / "provenance.json"
    write_payload(path, "figures/x.html", notes="looked fine")
    with pytest.raises(ProvenanceError, match="unexpected fields notes"):
        load_provenance(path)


def test_hand_edited_non_object_entry_rejected_on_load(tmp_path: Path) -> None:
    path = tmp_path / "provenance.json"
    path.write_text('{"figures/x.html": "scripts/figure_example.py"}', encoding="utf-8")
    with pytest.raises(ProvenanceError, match=r"'figures/x.html' must be an object"):
        load_provenance(path)


def test_hand_edited_boolean_row_count_rejected_on_load(tmp_path: Path) -> None:
    path = tmp_path / "provenance.json"
    write_payload(path, "figures/x.html", rows_in=True)
    with pytest.raises(ProvenanceError, match="non-integer rows_in"):
        load_provenance(path)


def test_a_bad_entry_anywhere_in_the_file_fails_the_whole_load(tmp_path: Path) -> None:
    # Validation is per entry, so one hand-edited row cannot hide behind
    # the good ones; and a record into that file is refused too.
    path = tmp_path / "provenance.json"
    record_provenance(path, "figures/a.html", make())
    record_provenance(path, "figures/b.html", make())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["figures/b.html"]["rows_out"] = -3
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ProvenanceError, match=r"'figures/b.html' has negative rows_out"):
        load_provenance(path)
    with pytest.raises(ProvenanceError, match=r"'figures/b.html' has negative rows_out"):
        record_provenance(path, "figures/c.html", make())


# --- snapshot hashing ------------------------------------------------------


def test_manifest_is_deterministic_and_complete(tmp_path: Path) -> None:
    raw = seed_snapshot(tmp_path)
    first = build_manifest(raw, "2026-08-25")
    second = build_manifest(raw, "2026-08-25")
    assert first.aggregate == second.aggregate
    assert set(first.files) == {
        "openalex/2026-08-25/pilot_company_0a1b2c3d.jsonl.gz",
        "crossref/2026-08-25/page_0001.jsonl.gz",
    }
    assert first.files["crossref/2026-08-25/page_0001.jsonl.gz"] == sha256_file(
        raw / "crossref" / "2026-08-25" / "page_0001.jsonl.gz"
    )


def test_content_change_changes_the_aggregate(tmp_path: Path) -> None:
    raw = seed_snapshot(tmp_path)
    before = build_manifest(raw, "2026-08-25").aggregate
    (raw / "crossref" / "2026-08-25" / "page_0001.jsonl.gz").write_bytes(b"\x1f\x8b\x08\x02")
    assert build_manifest(raw, "2026-08-25").aggregate != before


def test_a_per_source_root_gets_its_own_aggregate(tmp_path: Path) -> None:
    raw = seed_snapshot(tmp_path)
    whole = build_manifest(raw, "2026-08-25").aggregate
    per_source = build_manifest(raw / "crossref", "2026-08-25").aggregate
    assert whole != per_source


def test_manifest_excluded_from_itself(tmp_path: Path) -> None:
    raw = seed_snapshot(tmp_path)
    manifest = build_manifest(raw, "2026-08-25")
    write_manifest(manifest, raw)
    assert build_manifest(raw, "2026-08-25").aggregate == manifest.aggregate
    assert load_aggregate(raw, "2026-08-25") == manifest.aggregate


def test_empty_snapshot_crashes_loudly(tmp_path: Path) -> None:
    empty = tmp_path / "raw"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        build_manifest(empty, "2026-08-25")


def test_bad_snapshot_date_rejected(tmp_path: Path) -> None:
    raw = seed_snapshot(tmp_path)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        build_manifest(raw, "August 2026")


def test_manifest_without_aggregate_crashes_loudly(tmp_path: Path) -> None:
    raw = seed_snapshot(tmp_path)
    (raw / "manifest-2026-08-25.json").write_text('{"files": {}}', encoding="utf-8")
    with pytest.raises(ValueError, match="missing aggregate"):
        load_aggregate(raw, "2026-08-25")


def test_pilot_snapshot_date_is_read_from_config(tmp_path: Path) -> None:
    config = tmp_path / "snapshot.yaml"
    config.write_text("pilot_snapshot_date: '2026-08-25'\npilot_random_seed: 1\n", encoding="utf-8")
    assert snapshot_date_from_config(config) == "2026-08-25"


def test_production_snapshot_date_wins_over_pilot(tmp_path: Path) -> None:
    config = tmp_path / "snapshot.yaml"
    config.write_text(
        "pilot_snapshot_date: '2026-08-25'\nsnapshot_date: '2026-11-01'\n", encoding="utf-8"
    )
    assert snapshot_date_from_config(config) == "2026-11-01"


def test_config_without_a_date_crashes_loudly(tmp_path: Path) -> None:
    config = tmp_path / "snapshot.yaml"
    config.write_text("pilot_random_seed: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no snapshot date"):
        snapshot_date_from_config(config)
    config.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="no snapshot date"):
        snapshot_date_from_config(config)


def test_config_that_is_not_a_mapping_crashes_loudly(tmp_path: Path) -> None:
    config = tmp_path / "snapshot.yaml"
    config.write_text("- 2026-08-25\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a mapping"):
        snapshot_date_from_config(config)


def test_missing_config_crashes_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="pinned there"):
        snapshot_date_from_config(tmp_path / "absent.yaml")


def test_hash_snapshot_script_writes_the_manifest_the_analysis_will_cite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = seed_snapshot(tmp_path)
    config = tmp_path / "snapshot.yaml"
    config.write_text("pilot_snapshot_date: '2026-08-25'\n", encoding="utf-8")
    logs = tmp_path / "logs"
    hash_main(["--raw-root", str(raw), "--config", str(config), "--logs-dir", str(logs)])

    expected = build_manifest(raw, "2026-08-25").aggregate
    assert load_aggregate(raw, "2026-08-25") == expected
    assert f"aggregate {expected}" in capsys.readouterr().out
    events = [
        json.loads(line)
        for log_file in logs.glob("*.jsonl")
        for line in log_file.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in events] == ["snapshot_hashed"]
    assert events[0]["aggregate"] == expected
    assert events[0]["files"] == 2


def test_hash_snapshot_script_per_source_override(tmp_path: Path) -> None:
    raw = seed_snapshot(tmp_path)
    config = tmp_path / "snapshot.yaml"
    config.write_text("pilot_snapshot_date: '2026-08-25'\n", encoding="utf-8")
    hash_main(
        [
            "--raw-root",
            str(raw / "crossref"),
            "--config",
            str(config),
            "--snapshot-date",
            "2026-09-01",
            "--logs-dir",
            str(tmp_path / "logs"),
        ]
    )
    assert (raw / "crossref" / "manifest-2026-09-01.json").exists()
    assert load_aggregate(raw / "crossref", "2026-09-01") == (
        build_manifest(raw / "crossref", "2026-09-01").aggregate
    )
