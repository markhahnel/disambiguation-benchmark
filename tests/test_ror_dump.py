"""ROR dump loader against a committed sample of real v2.13 records.

tests/fixtures/ror_dump_sample.json holds 14 records lifted unmodified from
the pinned release (v2.13, 2026-09-22): each is the exact byte slice of that
record in the dump, so the file carries the dump's own \\uXXXX escaping of
non-ASCII names rather than a re-serialisation. Each one is there for a
reason:

  052gg0110  University of Oxford ............. plain university, 19 children
  03h2bh287  Oxford University Hospitals NHS Trust ... the hospital's parent
  0080acb59  John Radcliffe Hospital .......... hospital, child of two trusts
  05cvf7v30  Institute of Physics (CAS) ....... institute with a parent, and
                                               Chinese names. Its parent, CAS,
                                               is deliberately not in the
                                               sample, so the sparse-dump
                                               lookup path is exercised.
  00w0f8567  Kyushu Tokai University .......... inactive, one successor, and
                                               Japanese names plus a
                                               romanisation
  01p7qe739  Tokai University ................. that successor, so the merge
                                               resolves inside the sample
  03jzzxg14  University Hospitals Bristol and Weston NHS Foundation Trust ...
                                               inactive with two predecessors
                                               AND a successor: a merged trust
                                               that was itself merged again
  054vvq170  Bristol NHS Foundation Trust ..... its successor, naming it back
                                               as a predecessor, 12 children
  03bdvdc06  Public Library of Science ........ withdrawn, with a successor
  008zgvp64  Public Library of Science ........ that successor. Two live
                                               records share this display name
  030a08k25  Jishan County People's Hospital ... every optional field null or
                                               empty: no established date, no
                                               external ids, links, domains or
                                               relationships
  02e16g702  Hokkaido University .............. related-only relationships
  04dstfg97  Sholokhov Moscow State University for Humanities ... inactive,
                                               Cyrillic names, one successor
  028ydjm20  Argosy University ................ inactive with no successor at
                                               all: superseded and nowhere to
                                               go

Expected values below are hand-checked against those records, so the file is
the golden file for the loader as well as its input.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import structlog
from structlog.testing import capture_logs

from disambig import ror_dump as ror_dump_module
from disambig.ror_dump import (
    NO_RELATIONSHIPS,
    RorDump,
    RorDumpHashMismatch,
    RorDumpPin,
    iter_dump_records,
    require_v2_schema,
    sha256_file,
)

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "ror_dump_sample.json"

OXFORD = "https://ror.org/052gg0110"
OUH_TRUST = "https://ror.org/03h2bh287"
JOHN_RADCLIFFE = "https://ror.org/0080acb59"
CAS_INSTITUTE_OF_PHYSICS = "https://ror.org/05cvf7v30"
CAS = "https://ror.org/034t30j35"
KYUSHU_TOKAI = "https://ror.org/00w0f8567"
TOKAI = "https://ror.org/01p7qe739"
UHBW = "https://ror.org/03jzzxg14"
BRISTOL_FT = "https://ror.org/054vvq170"
PLOS_WITHDRAWN = "https://ror.org/03bdvdc06"
PLOS_ACTIVE = "https://ror.org/008zgvp64"
JISHAN = "https://ror.org/030a08k25"
HOKKAIDO = "https://ror.org/02e16g702"
SHOLOKHOV = "https://ror.org/04dstfg97"
ARGOSY = "https://ror.org/028ydjm20"


def sample_pin(**overrides: object) -> RorDumpPin:
    """A pin describing the fixture, so hash verification can be tested."""
    pin = RorDumpPin(
        version="v2.13",
        zenodo_doi="10.5281/zenodo.22902037",
        zenodo_concept_doi="10.5281/zenodo.6347574",
        record_id=22902037,
        publication_date="2026-09-22",
        filename="v2.13-2026-09-22-ror-data.zip",
        size_bytes=SAMPLE.stat().st_size,
        md5_zenodo="0" * 32,
        sha256_zip="0" * 64,
        v2_json_filename=SAMPLE.name,
        sha256_v2_json=sha256_file(SAMPLE),
        organisation_count=14,
        downloaded_at="2026-10-01T00:00:00+00:00",
        source_url="https://zenodo.org/api/records/22902037/files/"
        "v2.13-2026-09-22-ror-data.zip/content",
        listing_url="https://zenodo.org/api/records?q=parent.id:6347574",
    )
    return replace(pin, **overrides) if overrides else pin


@pytest.fixture
def dump() -> RorDump:
    with RorDump.load(SAMPLE, verify=False) as loaded:
        yield loaded


def test_streams_every_record_with_usable_byte_spans() -> None:
    raw = SAMPLE.read_bytes()
    spans = list(iter_dump_records(SAMPLE))
    assert len(spans) == 14
    for span in spans:
        slice_ = raw[span.offset : span.offset + span.length]
        assert json.loads(slice_)["id"] == span.record["id"]


def test_streams_compact_json_too(tmp_path: Path) -> None:
    # The dump is pretty-printed with four-space indent today; nothing in the
    # reader may depend on that.
    records = [span.record for span in iter_dump_records(SAMPLE)]
    compact = tmp_path / "compact.json"
    compact.write_text(json.dumps(records, ensure_ascii=False, separators=(",", ":")))
    assert [span.record["id"] for span in iter_dump_records(compact)] == [
        record["id"] for record in records
    ]


def test_streams_records_split_across_read_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A 64-byte read chunk puts every record boundary mid-buffer, which is the
    # case that silently truncated an earlier draft of the reader.
    monkeypatch.setattr("disambig.ror_dump._READ_CHUNK_BYTES", 64)
    assert len(list(iter_dump_records(SAMPLE))) == 14


def test_loads_the_expected_organisation_count(dump: RorDump) -> None:
    assert len(dump) == 14
    assert OXFORD in dump
    assert CAS not in dump


def test_status_counts_are_hand_checked(dump: RorDump) -> None:
    assert dump.status_counts == {"active": 9, "inactive": 4, "withdrawn": 1}


def test_relationship_counts_are_hand_checked(dump: RorDump) -> None:
    # 19 (Oxford) + 8 (OUH Trust) + 3 (Institute of Physics) + 1 (UHBW)
    # + 12 (Bristol NHS Foundation Trust) children.
    assert dump.relationship_counts == {
        "child": 43,
        "related": 14,
        "parent": 3,
        "successor": 4,
        "predecessor": 4,
    }


def test_get_reuses_the_ror_parser(dump: RorDump) -> None:
    record = dump.get(OXFORD)
    assert record.name == "University of Oxford"
    assert record.status == "active"
    assert record.candidate.city == "Oxford"
    assert record.candidate.country == "United Kingdom"
    assert record.candidate.org_types == ["education", "funder"]
    assert "Oxford University" in record.candidate.aliases
    assert record.external_ids["grid"] == "grid.4991.5"


def test_get_falls_back_to_the_first_of_all_when_no_preferred_id(dump: RorDump) -> None:
    # Oxford University Hospitals records a fundref id with preferred: null.
    assert dump.get(OUH_TRUST).external_ids["fundref"] == "501100006149"


def test_get_on_an_unknown_id_raises(dump: RorDump) -> None:
    with pytest.raises(KeyError, match="034t30j35"):
        dump.get(CAS)


def test_hospital_is_a_child_of_its_trust(dump: RorDump) -> None:
    relationships = dump.relationships(JOHN_RADCLIFFE)
    assert OUH_TRUST in relationships.parent
    assert len(relationships.parent) == 2
    assert OXFORD in relationships.related
    assert JOHN_RADCLIFFE in dump.relationships(OUH_TRUST).child


def test_institute_has_a_parent_outside_the_sample(dump: RorDump) -> None:
    relationships = dump.relationships(CAS_INSTITUTE_OF_PHYSICS)
    assert relationships.parent == frozenset({CAS})
    assert len(relationships.child) == 3
    assert CAS not in dump


def test_non_latin_names_survive_the_round_trip(dump: RorDump) -> None:
    physics = dump.get(CAS_INSTITUTE_OF_PHYSICS)
    assert physics.name == "Institute of Physics"
    assert "中国科学院物理研究所" in physics.candidate.aliases
    assert physics.candidate.acronyms == ["IOP"]
    assert "九州東海大学" in dump.get(KYUSHU_TOKAI).candidate.aliases
    assert "北海道大学" in dump.get(HOKKAIDO).candidate.aliases
    sholokhov = dump.get(SHOLOKHOV)
    # The initials are Cyrillic, exactly as the record spells them; ruff reads
    # a lone Cyrillic capital as a Latin look-alike, hence the suppression.
    assert (
        "Московский государственный гуманитарный университет "
        "имени М. А. Шолохова" in sholokhov.candidate.aliases  # noqa: RUF001
    )


def test_renamed_institution_resolves_to_its_successor(dump: RorDump) -> None:
    assert dump.status(KYUSHU_TOKAI) == "inactive"
    assert dump.successors(KYUSHU_TOKAI) == frozenset({TOKAI})
    assert dump.get(TOKAI).name == "Tokai University"
    assert dump.status(TOKAI) == "active"


def test_merged_trust_carries_predecessors_and_a_successor(dump: RorDump) -> None:
    relationships = dump.relationships(UHBW)
    assert len(relationships.predecessor) == 2
    assert relationships.successor == frozenset({BRISTOL_FT})
    assert dump.predecessors(UHBW) == relationships.predecessor
    # ROR asserts the merge from both ends here, which it does not always do.
    assert UHBW in dump.relationships(BRISTOL_FT).predecessor


def test_withdrawn_record_still_points_somewhere(dump: RorDump) -> None:
    assert dump.status(PLOS_WITHDRAWN) == "withdrawn"
    assert dump.successors(PLOS_WITHDRAWN) == frozenset({PLOS_ACTIVE})
    assert dump.status(PLOS_ACTIVE) == "active"
    # Both records display the same name, which is why matching cannot lean on
    # display names alone.
    assert dump.get(PLOS_WITHDRAWN).name == dump.get(PLOS_ACTIVE).name


def test_superseded_ids_exclude_a_dead_end(dump: RorDump) -> None:
    assert dump.status(ARGOSY) == "inactive"
    assert dump.successors(ARGOSY) == frozenset()
    assert dump.superseded_ids() == frozenset({KYUSHU_TOKAI, UHBW, PLOS_WITHDRAWN, SHOLOKHOV})


def test_record_with_every_optional_field_empty(dump: RorDump) -> None:
    record = dump.get(JISHAN)
    assert record.name == "Jishan County People's Hospital"
    assert record.external_ids == {}
    assert record.candidate.relationships == []
    assert record.candidate.city == "Yuncheng"
    assert dump.relationships(JISHAN) is NO_RELATIONSHIPS
    assert dump.successors(JISHAN) == frozenset()


def test_relationship_index_is_sparse_but_lookups_are_total(dump: RorDump) -> None:
    assert JISHAN not in dump.relationship_index
    assert PLOS_ACTIVE not in dump.relationship_index
    assert len(dump.relationship_index) == 11
    assert dump.relationships(PLOS_ACTIVE).all_ids() == frozenset()


def test_relationships_and_status_reject_unknown_ids(dump: RorDump) -> None:
    with pytest.raises(KeyError, match="034t30j35"):
        dump.relationships(CAS)
    with pytest.raises(KeyError, match="034t30j35"):
        dump.status(CAS)


def test_summary_reports_the_pinned_version(dump: RorDump) -> None:
    assert dump.summary()["version"] is None
    with RorDump.load(SAMPLE, pin=sample_pin()) as pinned:
        summary = pinned.summary()
    assert summary["version"] == "v2.13"
    assert summary["publication_date"] == "2026-09-22"
    assert summary["organisations"] == 14
    assert summary["organisations_with_relationships"] == 11
    assert summary["superseded_with_successor"] == 4


def test_matching_pin_loads_and_carries_its_metadata() -> None:
    pin = sample_pin()
    with RorDump.load(SAMPLE, pin=pin) as loaded:
        assert loaded.pin is not None
        assert loaded.pin.version == "v2.13"
        assert loaded.pin.zenodo_doi == "10.5281/zenodo.22902037"


def test_hash_mismatch_raises() -> None:
    stale = sample_pin(sha256_v2_json="f" * 64)
    with pytest.raises(RorDumpHashMismatch, match=r"Re-run scripts/pin_ror_dump\.py"):
        RorDump.load(SAMPLE, pin=stale)


def test_organisation_count_mismatch_raises() -> None:
    wrong = sample_pin(organisation_count=999)
    with pytest.raises(ValueError, match="holds 14 organisations"):
        RorDump.load(SAMPLE, pin=wrong)


def test_pin_round_trips_through_yaml(tmp_path: Path) -> None:
    pin_path = tmp_path / "ror_dump.yaml"
    sample_pin().to_yaml(pin_path)
    assert RorDumpPin.from_yaml(pin_path) == sample_pin()


def test_pin_missing_keys_are_named(tmp_path: Path) -> None:
    pin_path = tmp_path / "ror_dump.yaml"
    pin_path.write_text("version: v2.12\n")
    with pytest.raises(ValueError, match=r"missing pin keys: .*sha256_zip"):
        RorDumpPin.from_yaml(pin_path)


def test_from_pin_names_the_absent_file(tmp_path: Path) -> None:
    pin_path = tmp_path / "ror_dump.yaml"
    sample_pin().to_yaml(pin_path)
    with pytest.raises(FileNotFoundError, match=r"pin_ror_dump\.py"):
        RorDump.from_pin(pin_path, tmp_path / "data")


def test_from_pin_loads_a_dump_laid_out_as_the_script_leaves_it(tmp_path: Path) -> None:
    pin = sample_pin()
    pin_path = tmp_path / "ror_dump.yaml"
    pin.to_yaml(pin_path)
    json_path = pin.json_path(tmp_path / "data")
    json_path.parent.mkdir(parents=True)
    json_path.write_bytes(SAMPLE.read_bytes())
    with RorDump.from_pin(pin_path, tmp_path / "data") as loaded:
        assert len(loaded) == 14


def test_v1_schema_file_is_rejected(tmp_path: Path) -> None:
    v1 = tmp_path / "v1.json"
    v1.write_text(
        json.dumps(
            [
                {
                    "id": "https://ror.org/052gg0110",
                    "name": "University of Oxford",
                    "aliases": ["Oxford University"],
                    "addresses": [{"city": "Oxford"}],
                    "status": "active",
                    "admin": {"last_modified": {"schema_version": "1.0"}},
                }
            ]
        )
    )
    with pytest.raises(ValueError, match="schema_version"):
        RorDump.load(v1)


def test_v1_schema_without_admin_block_is_still_rejected() -> None:
    with pytest.raises(ValueError, match="ROR v2 dump"):
        require_v2_schema({"id": "https://ror.org/052gg0110", "name": "X"}, "old.json")


def test_v1_created_stamp_on_a_v2_record_is_accepted() -> None:
    # Most organisations in the v2 dump were registered under v1 and keep a
    # created stamp of 1.0 for good; only last_modified says which schema the
    # file is in. Checking created rejected the real dump once already.
    require_v2_schema(
        {
            "id": "https://ror.org/052gg0110",
            "admin": {
                "created": {"date": "2018-11-14", "schema_version": "1.0"},
                "last_modified": {"date": "2026-09-22", "schema_version": "2.1"},
            },
            "names": [],
            "locations": [],
        },
        "dump.json",
    )


def test_record_without_a_status_is_an_error_not_a_default(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    records = [span.record for span in iter_dump_records(SAMPLE)]
    del records[0]["status"]
    broken.write_text(json.dumps(records, ensure_ascii=False))
    with pytest.raises(ValueError, match="has no status"):
        RorDump.load(broken)


def test_duplicate_ror_id_is_an_error(tmp_path: Path) -> None:
    duplicated = tmp_path / "duplicated.json"
    records = [span.record for span in iter_dump_records(SAMPLE)]
    duplicated.write_text(json.dumps([records[0], records[0]], ensure_ascii=False))
    with pytest.raises(ValueError, match="duplicate ROR id"):
        RorDump.load(duplicated)


def test_relationship_with_a_null_label_still_indexes(tmp_path: Path) -> None:
    # ror.py drops these rows because a Candidate needs a label. A hierarchy
    # edge with no label still decides a parent-child match, so the index keeps
    # it.
    unlabelled = tmp_path / "unlabelled.json"
    records = [span.record for span in iter_dump_records(SAMPLE)]
    hospital = next(record for record in records if record["id"] == JOHN_RADCLIFFE)
    for relationship in hospital["relationships"]:
        relationship["label"] = None
    unlabelled.write_text(json.dumps(records, ensure_ascii=False))
    with RorDump.load(unlabelled) as loaded:
        assert OUH_TRUST in loaded.relationships(JOHN_RADCLIFFE).parent
        assert loaded.get(JOHN_RADCLIFFE).candidate.relationships == []


def test_unclosed_array_is_an_error(tmp_path: Path) -> None:
    truncated = tmp_path / "truncated.json"
    truncated.write_text(SAMPLE.read_text()[: SAMPLE.stat().st_size // 2])
    with pytest.raises(ValueError, match="ends before the JSON array is closed"):
        list(iter_dump_records(truncated))


def test_a_json_object_instead_of_an_array_is_an_error(tmp_path: Path) -> None:
    not_an_array = tmp_path / "object.json"
    not_an_array.write_text('{"id": "https://ror.org/052gg0110"}')
    with pytest.raises(ValueError, match="does not start with a JSON array"):
        list(iter_dump_records(not_an_array))


def test_empty_dump_is_an_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text("[]")
    with pytest.raises(ValueError, match="no organisation records"):
        RorDump.load(empty)


# --- the reader reads the whole file -----------------------------------------


def test_content_after_the_closing_bracket_is_an_error(tmp_path: Path) -> None:
    # Same reasoning as the unclosed array: a second array, or anything else,
    # after the one we indexed means this is not the file that was pinned.
    trailing = tmp_path / "trailing.json"
    trailing.write_bytes(SAMPLE.read_bytes() + b"\n[]")
    with pytest.raises(
        ValueError, match=r"content after the closing bracket .* at byte \d+ \(found '\['\)"
    ):
        list(iter_dump_records(trailing))


def test_a_comma_after_the_closing_bracket_is_content(tmp_path: Path) -> None:
    trailing = tmp_path / "comma.json"
    trailing.write_bytes(SAMPLE.read_bytes() + b",")
    with pytest.raises(ValueError, match="content after the closing bracket"):
        list(iter_dump_records(trailing))


def test_whitespace_after_the_closing_bracket_is_fine(tmp_path: Path) -> None:
    padded = tmp_path / "padded.json"
    padded.write_bytes(SAMPLE.read_bytes() + b"\n \t\r\n")
    assert len(list(iter_dump_records(padded))) == 14


def test_trailing_content_is_caught_across_read_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("disambig.ror_dump._READ_CHUNK_BYTES", 64)
    trailing = tmp_path / "trailing.json"
    trailing.write_bytes(SAMPLE.read_bytes() + b"\n" * 200 + b"x")
    with pytest.raises(ValueError, match="content after the closing bracket"):
        list(iter_dump_records(trailing))


def test_utf8_bom_gets_a_clear_message(tmp_path: Path) -> None:
    with_bom = tmp_path / "bom.json"
    with_bom.write_bytes(b"\xef\xbb\xbf" + SAMPLE.read_bytes())
    with pytest.raises(ValueError, match="starts with a UTF-8 byte order mark"):
        list(iter_dump_records(with_bom))


# --- ids are canonical on both sides of every comparison ---------------------


def write_dump(
    tmp_path: Path, mutate: Callable[[list[dict[str, Any]]], None], name: str = "modified.json"
) -> Path:
    """The fixture records, altered by mutate, written out as a dump file."""
    records = [span.record for span in iter_dump_records(SAMPLE)]
    mutate(records)
    path = tmp_path / name
    path.write_text(json.dumps(records, ensure_ascii=False))
    return path


def record_for(records: list[dict[str, Any]], ror_id: str) -> dict[str, Any]:
    return next(record for record in records if record["id"] == ror_id)


def test_relationship_target_ids_are_normalised_at_load(tmp_path: Path) -> None:
    # Padding, upper case and an upper-case scheme on the edge; the lookup
    # side (the matcher) normalises, so the dump side must too or the edge
    # silently never matches.
    def mutate(records: list[dict[str, Any]]) -> None:
        for relationship in record_for(records, JOHN_RADCLIFFE)["relationships"]:
            if relationship["id"] == OUH_TRUST:
                relationship["id"] = "  HTTPS://ROR.ORG/03H2BH287 "

    with RorDump.load(write_dump(tmp_path, mutate)) as loaded:
        assert OUH_TRUST in loaded.relationships(JOHN_RADCLIFFE).parent
        assert "  HTTPS://ROR.ORG/03H2BH287 " not in loaded.relationships(JOHN_RADCLIFFE).parent
        assert loaded.relationship_counts == RorDump.load(SAMPLE, verify=False).relationship_counts


def test_malformed_relationship_id_names_the_organisation_and_the_id(tmp_path: Path) -> None:
    def mutate(records: list[dict[str, Any]]) -> None:
        record_for(records, JOHN_RADCLIFFE)["relationships"][0]["id"] = "grid.4991.5"

    with pytest.raises(ValueError, match=r"0080acb59 has a \w+ relationship .* 'grid\.4991\.5'"):
        RorDump.load(write_dump(tmp_path, mutate))


def test_null_relationship_id_names_the_organisation(tmp_path: Path) -> None:
    def mutate(records: list[dict[str, Any]]) -> None:
        record_for(records, JOHN_RADCLIFFE)["relationships"][0]["id"] = None

    with pytest.raises(
        ValueError, match=r"0080acb59 has a \w+ relationship whose id is not a string: None"
    ):
        RorDump.load(write_dump(tmp_path, mutate))


def test_relationship_without_a_type_is_an_error(tmp_path: Path) -> None:
    def mutate(records: list[dict[str, Any]]) -> None:
        record_for(records, JOHN_RADCLIFFE)["relationships"][0]["type"] = None

    with pytest.raises(ValueError, match="0080acb59 has a relationship with no type"):
        RorDump.load(write_dump(tmp_path, mutate))


def test_relationship_entry_that_is_not_an_object_is_an_error(tmp_path: Path) -> None:
    # Previously skipped with a silent continue. A dropped hierarchy edge moves
    # a parent-child outcome from correct to wrong, so it is an error.
    def mutate(records: list[dict[str, Any]]) -> None:
        record_for(records, JOHN_RADCLIFFE)["relationships"].append(OUH_TRUST)

    with pytest.raises(
        ValueError, match="0080acb59 has a relationship entry that is not an object"
    ):
        RorDump.load(write_dump(tmp_path, mutate))


def test_relationships_that_are_not_a_list_is_an_error(tmp_path: Path) -> None:
    def mutate(records: list[dict[str, Any]]) -> None:
        record_for(records, JOHN_RADCLIFFE)["relationships"] = {"type": "parent", "id": OUH_TRUST}

    with pytest.raises(ValueError, match="0080acb59 has relationships of type dict"):
        RorDump.load(write_dump(tmp_path, mutate))


def test_null_relationships_means_no_edges(tmp_path: Path) -> None:
    def mutate(records: list[dict[str, Any]]) -> None:
        record_for(records, JOHN_RADCLIFFE)["relationships"] = None

    with RorDump.load(write_dump(tmp_path, mutate)) as loaded:
        assert loaded.relationships(JOHN_RADCLIFFE) is NO_RELATIONSHIPS


def test_record_ids_are_normalised_at_load(tmp_path: Path) -> None:
    def mutate(records: list[dict[str, Any]]) -> None:
        record_for(records, OXFORD)["id"] = "https://ror.org/052GG0110"

    with RorDump.load(write_dump(tmp_path, mutate)) as loaded:
        assert OXFORD in loaded
        assert "https://ror.org/052GG0110" not in loaded
        assert loaded.status(OXFORD) == "active"
        assert loaded.get(OXFORD).name == "University of Oxford"


def test_record_id_not_shaped_like_a_ror_id_is_an_error(tmp_path: Path) -> None:
    def mutate(records: list[dict[str, Any]]) -> None:
        record_for(records, OXFORD)["id"] = "grid.4991.5"

    with pytest.raises(ValueError, match=r"has an id not shaped like a ROR id: 'grid\.4991\.5'"):
        RorDump.load(write_dump(tmp_path, mutate))


# --- the shared external id crosswalk ------------------------------------------


def test_external_id_index_is_hand_checked(dump: RorDump) -> None:
    index = dump.external_id_index()
    assert set(index) == {"grid", "isni", "wikidata", "fundref"}
    # Counted by hand from the fixture records: ten of the fourteen carry a
    # GRID id, nine an ISNI, eleven carry Wikidata ids (Oxford six of them),
    # and four carry FundRef ids (Oxford 53, Tokai 2, OUH Trust and Hokkaido
    # one each).
    assert len(index["grid"]) == 10
    assert len(index["isni"]) == 9
    assert len(index["wikidata"]) == 16
    assert len(index["fundref"]) == 57
    assert index["grid"]["grid.4991.5"] == [OXFORD]
    assert index["isni"]["0000 0004 1936 8948"] == [OXFORD]
    # Preferred is null on this one, so it is reached through `all`.
    assert index["fundref"]["501100006149"] == [OUH_TRUST]
    # Non-preferred members of `all` are indexed too: a source may emit any.
    assert index["wikidata"]["Q1095537"] == [OXFORD]
    assert index["fundref"]["501100010656"] == [TOKAI]
    # Withdrawn and inactive records are in the crosswalk as well; the status
    # is the matcher's business, not the index's.
    assert index["grid"]["grid.466955.d"] == [PLOS_WITHDRAWN]
    assert index["grid"]["grid.411243.1"] == [KYUSHU_TOKAI]
    assert "grid.999999.9" not in index["grid"]
    assert all(len(holders) == 1 for by_value in index.values() for holders in by_value.values())


def test_external_id_index_is_built_once(dump: RorDump) -> None:
    assert dump.external_id_index() is dump.external_id_index()


def test_shared_external_id_keeps_every_ror_id_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def mutate(records: list[dict[str, Any]]) -> None:
        for entry in record_for(records, OUH_TRUST)["external_ids"]:
            if entry["type"] == "grid":
                entry["all"].append("grid.4991.5")

    with RorDump.load(write_dump(tmp_path, mutate)) as loaded, capture_logs() as events:
        # A fresh logger, because another test may have cached the module's
        # one under a non-capturing configuration.
        monkeypatch.setattr(ror_dump_module, "log", structlog.get_logger("test_ror_dump"))
        index = loaded.external_id_index()
    assert index["grid"]["grid.4991.5"] == [OXFORD, OUH_TRUST]
    assert index["grid"]["grid.410556.3"] == [OUH_TRUST]
    shared = [event for event in events if event["event"] == "ror_dump_external_id_shared"]
    assert len(shared) == 1
    assert shared[0]["scheme"] == "grid"
    assert shared[0]["external_id"] == "grid.4991.5"
    assert shared[0]["ror_ids"] == [OXFORD, OUH_TRUST]
    built = [event for event in events if event["event"] == "ror_dump_external_id_index_built"]
    assert built[0]["shared_ids"] == 1


def test_external_id_entry_that_is_not_an_object_is_an_error(tmp_path: Path) -> None:
    def mutate(records: list[dict[str, Any]]) -> None:
        record_for(records, OXFORD)["external_ids"].append("grid.4991.5")

    with RorDump.load(write_dump(tmp_path, mutate)) as loaded:
        with pytest.raises(ValueError, match="052gg0110 has an external_ids entry that is not"):
            loaded.external_id_index()
        with pytest.raises(ValueError, match="052gg0110 has an external_ids entry that is not"):
            loaded.get(OXFORD)


def test_external_id_entry_without_a_type_is_an_error(tmp_path: Path) -> None:
    def mutate(records: list[dict[str, Any]]) -> None:
        del record_for(records, OXFORD)["external_ids"][0]["type"]

    with (
        RorDump.load(write_dump(tmp_path, mutate)) as loaded,
        pytest.raises(ValueError, match="052gg0110 has an external_ids entry with no type"),
    ):
        loaded.external_id_index()


def test_non_string_external_id_value_is_an_error(tmp_path: Path) -> None:
    def mutate(records: list[dict[str, Any]]) -> None:
        for entry in record_for(records, OXFORD)["external_ids"]:
            if entry["type"] == "grid":
                entry["all"] = [4991]

    with (
        RorDump.load(write_dump(tmp_path, mutate)) as loaded,
        pytest.raises(ValueError, match="052gg0110 has a non-string grid id"),
    ):
        loaded.get(OXFORD)


def test_external_ids_that_are_not_a_list_is_an_error(tmp_path: Path) -> None:
    def mutate(records: list[dict[str, Any]]) -> None:
        record_for(records, OXFORD)["external_ids"] = {"type": "grid", "all": ["grid.4991.5"]}

    with (
        RorDump.load(write_dump(tmp_path, mutate)) as loaded,
        pytest.raises(ValueError, match="052gg0110 has external_ids of type dict"),
    ):
        loaded.get(OXFORD)


def test_external_id_values_are_stripped_not_otherwise_changed(tmp_path: Path) -> None:
    def mutate(records: list[dict[str, Any]]) -> None:
        for entry in record_for(records, OXFORD)["external_ids"]:
            if entry["type"] == "grid":
                entry["all"] = [" grid.4991.5 "]
                entry["preferred"] = " grid.4991.5 "

    with RorDump.load(write_dump(tmp_path, mutate)) as loaded:
        assert loaded.get(OXFORD).external_ids["grid"] == "grid.4991.5"
        assert loaded.external_id_index()["grid"]["grid.4991.5"] == [OXFORD]
        assert " grid.4991.5 " not in loaded.external_id_index()["grid"]
