"""Loader over a pinned ROR v2 data dump.

ROR changes under us: organisations are renamed, merged, split, and withdrawn
between fortnightly releases. Two things in this benchmark depend on knowing
exactly which release was used. The parent-child and any-relationship
matching rules (METHODS.md section 2) resolve hierarchy against the dump, and
the renamed/merged/split stratum is built from predecessor and successor
relationships, which exist only in the dump and not in a per-organisation API
response. So every run reads one pinned release, recorded in
config/ror_dump.yaml and verified by hash on load.

Memory: the v2.13 dump JSON is 318MB for 141,528 organisations, and the fully
parsed objects are larger again. Loading therefore streams the file once, keeping
only a byte-offset index, statuses, and relationship sets in memory. get()
seeks back to a single record and parses it on demand, so the raw JSON and the
parsed objects are never both resident.

Strictness: the file on disk is hash-verified against the pin before it is
indexed, so any structural surprise inside it (a relationship that is not an
object, an id that is not shaped like a ROR id, content after the closing
bracket) means the file is not the one that was pinned. Those are errors, not
rows to skip, because a silently dropped hierarchy edge is a silently wrong
match rate.
"""

from __future__ import annotations

import codecs
import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Self

import structlog
import yaml

from disambig.models import Candidate
from disambig.ror import parse_ror_record
from disambig.ror_ids import normalise_ror_id

log = structlog.get_logger(__name__)

RELATIONSHIP_TYPES: tuple[str, ...] = (
    "parent",
    "child",
    "related",
    "predecessor",
    "successor",
)
KNOWN_STATUSES = frozenset({"active", "inactive", "withdrawn"})

_READ_CHUNK_BYTES = 4 * 1024 * 1024
_HASH_BLOCK_BYTES = 1024 * 1024
_DECODER = json.JSONDecoder()
# Whitespace and the commas between top-level array elements. Every one of
# these is a single byte in UTF-8, which the offset arithmetic relies on.
_BETWEEN_RECORDS = frozenset(" \t\r\n,")
# After the closing bracket only whitespace may follow: a comma or a second
# array there is content, and content after the array is an error.
_WHITESPACE = frozenset(" \t\r\n")
_BOM = "﻿"


class RorDumpHashMismatch(RuntimeError):
    """The dump on disk is not the dump that was pinned."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_HASH_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _skip(buffer: str, pos: int, skippable: frozenset[str]) -> int:
    """How many characters at buffer[pos:] are in skippable."""
    skipped = 0
    while pos + skipped < len(buffer) and buffer[pos + skipped] in skippable:
        skipped += 1
    return skipped


@dataclass(frozen=True)
class RecordSpan:
    """One organisation record and where its bytes live in the dump file."""

    offset: int
    length: int
    record: dict[str, Any]


def iter_dump_records(path: Path) -> Iterator[RecordSpan]:
    """Stream the dump's top-level JSON array one record at a time.

    Byte offsets rather than character offsets, because get() seeks on the
    file in binary mode and the dump contains plenty of non-ASCII names.

    The whole file is read, including whatever follows the closing bracket:
    anything there other than whitespace is an error for the same reason an
    unclosed array is, since the file is then not the one that was indexed.
    """
    incremental = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    pos = 0
    byte_pos = 0
    started = False
    closed = False
    at_eof = False
    with path.open("rb") as handle:
        while not at_eof:
            chunk = handle.read(_READ_CHUNK_BYTES)
            at_eof = not chunk
            buffer = buffer[pos:] + incremental.decode(chunk, final=at_eof)
            pos = 0
            while True:
                if closed:
                    skipped = _skip(buffer, pos, _WHITESPACE)
                    pos += skipped
                    byte_pos += skipped
                    if pos < len(buffer):
                        raise ValueError(
                            f"{path} holds content after the closing bracket of the JSON "
                            f"array at byte {byte_pos} (found {buffer[pos]!r})"
                        )
                    break
                skipped = _skip(buffer, pos, _BETWEEN_RECORDS)
                pos += skipped
                byte_pos += skipped
                if pos >= len(buffer):
                    break
                char = buffer[pos]
                if not started:
                    if char == _BOM:
                        raise ValueError(
                            f"{path} starts with a UTF-8 byte order mark. The ROR dump is "
                            "written without one, so this file is not the pinned release."
                        )
                    if char != "[":
                        raise ValueError(
                            f"{path} does not start with a JSON array (found {char!r})"
                        )
                    started = True
                    pos += 1
                    byte_pos += 1
                    continue
                if char == "]":
                    closed = True
                    pos += 1
                    byte_pos += 1
                    continue
                try:
                    record, end = _DECODER.raw_decode(buffer, pos)
                except json.JSONDecodeError as exc:
                    # Either the record straddles the chunk boundary, or the
                    # file is genuinely malformed. Only EOF tells them apart.
                    if at_eof:
                        raise ValueError(
                            f"{path} ends before the JSON array is closed, or holds a "
                            f"malformed record: cannot decode the record at byte "
                            f"{byte_pos} ({exc.msg})"
                        ) from exc
                    break
                if not isinstance(record, dict):
                    raise ValueError(f"{path}: top-level array holds a {type(record).__name__}")
                length = len(buffer[pos:end].encode())
                yield RecordSpan(offset=byte_pos, length=length, record=record)
                pos = end
                byte_pos += length
    if not started:
        raise ValueError(f"{path} holds no JSON array")
    if not closed:
        raise ValueError(f"{path} ends before the JSON array is closed")


def require_v2_schema(record: dict[str, Any], source: str) -> None:
    """Fail loudly on a v1-schema dump file.

    The v1 records carry flat `name`/`aliases`/`addresses` keys; v2 carries
    `names` and `locations`, which is what parse_ror_record expects. Older
    releases shipped both schemas in one zip, so the wrong file is a live
    possibility rather than a theoretical one.

    Only admin.last_modified.schema_version is consulted. admin.created is a
    historical stamp: every organisation registered before ROR moved to v2
    says created under 1.0 in the v2 dump as well, so checking it would
    reject the real file.
    """
    admin = record.get("admin")
    if isinstance(admin, dict):
        entry = admin.get("last_modified")
        if isinstance(entry, dict):
            version = entry.get("schema_version")
            if isinstance(version, str) and not version.startswith("2"):
                raise ValueError(f"{source} declares ROR schema_version {version!r}, expected 2.x")
    if not isinstance(record.get("names"), list) or not isinstance(record.get("locations"), list):
        raise ValueError(
            f"{source} does not look like a ROR v2 dump: "
            "the first record has no names/locations lists"
        )


@dataclass(frozen=True)
class RelationshipSets:
    """The related ROR ids of one organisation, split by relationship type.

    Every id is in the canonical form normalise_ror_id produces, the same form
    the matcher puts gold labels and source assignments into, so membership
    tests against these sets never fail on formatting.
    """

    parent: frozenset[str] = frozenset()
    child: frozenset[str] = frozenset()
    related: frozenset[str] = frozenset()
    predecessor: frozenset[str] = frozenset()
    successor: frozenset[str] = frozenset()

    def all_ids(self) -> frozenset[str]:
        return self.parent | self.child | self.related | self.predecessor | self.successor


NO_RELATIONSHIPS = RelationshipSets()
_NO_IDS: frozenset[str] = frozenset()


def _relationship_sets(grouped: dict[str, set[str]]) -> RelationshipSets:
    """Share one empty frozenset: most organisations have edges of two types at
    most, and there are tens of thousands of them."""

    def freeze(rel_type: str) -> frozenset[str]:
        ids = grouped.get(rel_type)
        return frozenset(ids) if ids else _NO_IDS

    return RelationshipSets(
        parent=freeze("parent"),
        child=freeze("child"),
        related=freeze("related"),
        predecessor=freeze("predecessor"),
        successor=freeze("successor"),
    )


@dataclass(frozen=True)
class RorRecord:
    """A dump record: the Candidate view plus the fields the v2 API client drops.

    external_ids maps identifier scheme (as ROR names it: grid, isni,
    wikidata, fundref) to the preferred value, falling back to the first of
    `all` where ROR records no preferred one.

    Policy, stated once: any identifier scheme present in ROR external_ids is
    crosswalked to ROR through one shared index, RorDump.external_id_index(),
    built from the pinned dump and applied identically to every source's
    output at harvest time. No source gets its own crosswalk, and nothing in
    the evaluation path knows which scheme any source emits.
    """

    candidate: Candidate
    status: str
    external_ids: Mapping[str, str]

    @property
    def ror_id(self) -> str:
        return self.candidate.ror_id

    @property
    def name(self) -> str:
        return self.candidate.name


@dataclass(frozen=True)
class RorDumpPin:
    """config/ror_dump.yaml: which ROR release every number in the post used."""

    version: str
    zenodo_doi: str
    zenodo_concept_doi: str
    record_id: int
    publication_date: str
    filename: str
    size_bytes: int
    md5_zenodo: str
    sha256_zip: str
    v2_json_filename: str
    sha256_v2_json: str
    organisation_count: int
    downloaded_at: str
    source_url: str
    listing_url: str

    @classmethod
    def from_yaml(cls, path: Path) -> Self:
        loaded = yaml.safe_load(path.read_text())
        if not isinstance(loaded, dict):
            raise ValueError(f"{path} does not hold a YAML mapping")
        expected = {field.name for field in fields(cls)}
        missing = sorted(expected - set(loaded))
        if missing:
            raise ValueError(f"{path} is missing pin keys: {', '.join(missing)}")
        unexpected = sorted(set(loaded) - expected)
        if unexpected:
            raise ValueError(f"{path} holds unknown pin keys: {', '.join(unexpected)}")
        return cls(**loaded)

    def as_dict(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    def to_yaml(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(self.as_dict(), sort_keys=False, allow_unicode=True))

    def json_path(self, data_root: Path) -> Path:
        """Where pin_ror_dump.py put the extracted v2 JSON."""
        return data_root / "raw" / "ror" / self.version / self.v2_json_filename

    def zip_path(self, data_root: Path) -> Path:
        return data_root / "raw" / "ror" / self.version / self.filename


def _canonical_record_id(record: dict[str, Any], path: Path, offset: int) -> str:
    ror_id = record.get("id")
    if not isinstance(ror_id, str) or not ror_id.strip():
        raise ValueError(f"{path}: record at byte {offset} has no id")
    try:
        return normalise_ror_id(ror_id)
    except ValueError as exc:
        raise ValueError(
            f"{path}: record at byte {offset} has an id not shaped like a ROR id: {ror_id!r}"
        ) from exc


def _relationships_of(record: dict[str, Any], ror_id: str, path: Path) -> Iterator[tuple[str, str]]:
    """(type, canonical target id) for every relationship row on a record.

    Deliberately not reusing the Candidate.relationships list from ror.py: that
    one drops rows with a null label, and a label is cosmetic. A hierarchy edge
    with no label still moves a match from wrong to right under the parent-child
    rule. Anything structurally wrong raises, see the module docstring.
    """
    raw = record.get("relationships")
    if raw is None:
        return
    if not isinstance(raw, list):
        raise ValueError(
            f"{path}: ROR record {ror_id} has relationships of type {type(raw).__name__}, "
            "expected a list"
        )
    for relationship in raw:
        if not isinstance(relationship, dict):
            raise ValueError(
                f"{path}: ROR record {ror_id} has a relationship entry that is not an "
                f"object ({type(relationship).__name__}: {relationship!r:.80})"
            )
        rel_type = relationship.get("type")
        rel_id = relationship.get("id")
        if not isinstance(rel_type, str) or not rel_type.strip():
            raise ValueError(
                f"{path}: ROR record {ror_id} has a relationship with no type (target {rel_id!r})"
            )
        if not isinstance(rel_id, str):
            raise ValueError(
                f"{path}: ROR record {ror_id} has a {rel_type} relationship whose id is "
                f"not a string: {rel_id!r}"
            )
        try:
            target = normalise_ror_id(rel_id)
        except ValueError as exc:
            raise ValueError(
                f"{path}: ROR record {ror_id} has a {rel_type} relationship whose id is "
                f"not shaped like a ROR id: {rel_id!r}"
            ) from exc
        yield rel_type, target


def _external_id_entries(
    record: dict[str, Any], ror_id: str, source: str
) -> Iterator[tuple[str, str | None, list[str]]]:
    """(scheme, preferred, all) for every external_ids entry on a record.

    Values are whitespace-stripped and otherwise left as ROR spells them. No
    scheme gets its own normalisation here: whatever a source emits is looked
    up in the index exactly as ROR records it. Malformed shapes raise.
    """
    raw = record.get("external_ids")
    if raw is None:
        return
    if not isinstance(raw, list):
        raise ValueError(
            f"{source}: ROR record {ror_id} has external_ids of type {type(raw).__name__}, "
            "expected a list"
        )
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(
                f"{source}: ROR record {ror_id} has an external_ids entry that is not an "
                f"object ({type(entry).__name__}: {entry!r:.80})"
            )
        scheme = entry.get("type")
        if not isinstance(scheme, str) or not scheme.strip():
            raise ValueError(
                f"{source}: ROR record {ror_id} has an external_ids entry with no type "
                f"({entry!r:.80})"
            )
        preferred = entry.get("preferred")
        if preferred is not None and not isinstance(preferred, str):
            raise ValueError(
                f"{source}: ROR record {ror_id} has a non-string preferred {scheme} id "
                f"({preferred!r:.80})"
            )
        values = entry.get("all")
        if values is None:
            values = []
        if not isinstance(values, list):
            raise ValueError(
                f"{source}: ROR record {ror_id} has {scheme} ids of type "
                f"{type(values).__name__}, expected a list"
            )
        cleaned: list[str] = []
        for value in values:
            if not isinstance(value, str):
                raise ValueError(
                    f"{source}: ROR record {ror_id} has a non-string {scheme} id ({value!r:.80})"
                )
            if value.strip():
                cleaned.append(value.strip())
        preferred_clean = preferred.strip() if isinstance(preferred, str) else None
        yield scheme.strip(), preferred_clean or None, cleaned


def _external_ids(record: dict[str, Any], ror_id: str, source: str) -> dict[str, str]:
    external: dict[str, str] = {}
    for scheme, preferred, values in _external_id_entries(record, ror_id, source):
        if preferred is not None:
            external[scheme] = preferred
        elif values:
            external[scheme] = values[0]
    return external


class RorDump:
    """Random access over the extracted v2 JSON of one pinned ROR release."""

    def __init__(
        self,
        json_path: Path,
        offsets: dict[str, tuple[int, int]],
        statuses: dict[str, str],
        relationship_index: dict[str, RelationshipSets],
        status_counts: dict[str, int],
        relationship_counts: dict[str, int],
        pin: RorDumpPin | None = None,
    ) -> None:
        self.json_path = json_path
        self.pin = pin
        self._offsets = offsets
        self._statuses = statuses
        self._relationship_index = relationship_index
        self._status_counts = status_counts
        self._relationship_counts = relationship_counts
        self._handle: IO[bytes] | None = None
        self._external_id_index: dict[str, dict[str, list[str]]] | None = None

    @classmethod
    def load(cls, path: Path, pin: RorDumpPin | None = None, verify: bool = True) -> RorDump:
        """One streaming pass over the dump, building the in-memory indexes.

        A pin is verified by SHA-256 before anything is indexed, so a dump that
        has been swapped or truncated under us fails here rather than producing
        quietly different match rates. verify=False exists for tests over
        fixtures, never for an analysis run.

        Every ROR id, the record's own and every relationship target, is put
        through normalise_ror_id here, so the keys of this index and the ids
        the matcher looks up share one canonical form.
        """
        if pin is not None and verify:
            digest = sha256_file(path)
            if digest != pin.sha256_v2_json:
                raise RorDumpHashMismatch(
                    f"{path} has sha256 {digest}, but the pin for ROR {pin.version} "
                    f"records {pin.sha256_v2_json}. Re-run scripts/pin_ror_dump.py --refresh."
                )

        offsets: dict[str, tuple[int, int]] = {}
        statuses: dict[str, str] = {}
        relationship_index: dict[str, RelationshipSets] = {}
        status_counts: dict[str, int] = {}
        relationship_counts: dict[str, int] = {}
        interned_statuses: dict[str, str] = {}
        checked_schema = False

        for span in iter_dump_records(path):
            record = span.record
            if not checked_schema:
                require_v2_schema(record, str(path))
                checked_schema = True
            ror_id = _canonical_record_id(record, path, span.offset)
            if ror_id in offsets:
                raise ValueError(f"{path}: duplicate ROR id {ror_id}")
            offsets[ror_id] = (span.offset, span.length)

            status = record.get("status")
            if not isinstance(status, str) or not status.strip():
                raise ValueError(f"{path}: ROR record {ror_id} has no status")
            if status not in KNOWN_STATUSES:
                log.warning("ror_dump_unknown_status", ror_id=ror_id, status=status)
            statuses[ror_id] = interned_statuses.setdefault(status, status)
            status_counts[status] = status_counts.get(status, 0) + 1

            grouped: dict[str, set[str]] = {}
            for rel_type, target in _relationships_of(record, ror_id, path):
                relationship_counts[rel_type] = relationship_counts.get(rel_type, 0) + 1
                if rel_type not in RELATIONSHIP_TYPES:
                    log.warning(
                        "ror_dump_unknown_relationship_type",
                        ror_id=ror_id,
                        rel_type=rel_type,
                        target=target,
                    )
                    continue
                grouped.setdefault(rel_type, set()).add(target)
            if grouped:
                relationship_index[ror_id] = _relationship_sets(grouped)

        if not offsets:
            raise ValueError(f"{path} holds no organisation records")
        if pin is not None and len(offsets) != pin.organisation_count:
            raise ValueError(
                f"{path} holds {len(offsets)} organisations, but the pin for ROR "
                f"{pin.version} records {pin.organisation_count}"
            )
        log.info(
            "ror_dump_loaded",
            path=str(path),
            version=pin.version if pin is not None else None,
            organisations=len(offsets),
            status_counts=status_counts,
            relationship_counts=relationship_counts,
        )
        return cls(
            json_path=path,
            offsets=offsets,
            statuses=statuses,
            relationship_index=relationship_index,
            status_counts=status_counts,
            relationship_counts=relationship_counts,
            pin=pin,
        )

    @classmethod
    def from_pin(cls, pin_path: Path, data_root: Path) -> RorDump:
        """Load the release recorded in config/ror_dump.yaml. The analysis entry point."""
        pin = RorDumpPin.from_yaml(pin_path)
        json_path = pin.json_path(data_root)
        if not json_path.exists():
            raise FileNotFoundError(
                f"ROR {pin.version} is pinned in {pin_path} but {json_path} is absent. "
                "Run scripts/pin_ror_dump.py."
            )
        return cls.load(json_path, pin=pin)

    def __len__(self) -> int:
        return len(self._offsets)

    def __contains__(self, ror_id: str) -> bool:
        return ror_id in self._offsets

    def ror_ids(self) -> frozenset[str]:
        return frozenset(self._offsets)

    def get(self, ror_id: str) -> RorRecord:
        """Read one record back off disk and parse it. Raises KeyError if absent."""
        span = self._offsets.get(ror_id)
        if span is None:
            raise KeyError(f"{ror_id} is not in ROR dump {self.json_path.name}")
        if self._handle is None:
            self._handle = self.json_path.open("rb")
        offset, length = span
        self._handle.seek(offset)
        raw = self._handle.read(length)
        if len(raw) != length:
            raise RorDumpHashMismatch(
                f"{self.json_path} is shorter than its index: {ror_id} needs {length} "
                f"bytes at {offset}, got {len(raw)}"
            )
        record = json.loads(raw)
        if not isinstance(record, dict):
            raise ValueError(f"{self.json_path}: byte {offset} does not hold an object")
        candidate = parse_ror_record(record)
        if normalise_ror_id(candidate.ror_id) != ror_id:
            raise RorDumpHashMismatch(
                f"{self.json_path} byte {offset} holds {candidate.ror_id}, indexed as {ror_id}"
            )
        return RorRecord(
            candidate=candidate,
            status=self._statuses[ror_id],
            external_ids=_external_ids(record, ror_id, str(self.json_path)),
        )

    def status(self, ror_id: str) -> str:
        """active, inactive or withdrawn. Raises KeyError if absent."""
        status = self._statuses.get(ror_id)
        if status is None:
            raise KeyError(f"{ror_id} is not in ROR dump {self.json_path.name}")
        return status

    def relationships(self, ror_id: str) -> RelationshipSets:
        """Total over ids in the dump: an organisation with no edges gets empty sets."""
        if ror_id not in self._offsets:
            raise KeyError(f"{ror_id} is not in ROR dump {self.json_path.name}")
        return self._relationship_index.get(ror_id, NO_RELATIONSHIPS)

    def successors(self, ror_id: str) -> frozenset[str]:
        """Where a superseded organisation went. Empty for a live organisation."""
        return self.relationships(ror_id).successor

    def predecessors(self, ror_id: str) -> frozenset[str]:
        return self.relationships(ror_id).predecessor

    @property
    def relationship_index(self) -> Mapping[str, RelationshipSets]:
        """Sparse: only organisations with at least one relationship appear.

        Use relationships() for a lookup that is total over the dump.
        """
        return self._relationship_index

    @property
    def status_counts(self) -> Mapping[str, int]:
        return dict(self._status_counts)

    @property
    def relationship_counts(self) -> Mapping[str, int]:
        """Relationship rows per type, counted once per asserting organisation.

        ROR asserts hierarchy from both ends, so a parent row and its mirroring
        child row are two rows here, not one.
        """
        return dict(self._relationship_counts)

    def external_id_index(self) -> Mapping[str, Mapping[str, list[str]]]:
        """scheme -> external id -> the ROR ids that carry it, from the pinned dump.

        This is the one crosswalk every source's output goes through at
        harvest time, so an assignment made under any other scheme ROR knows
        about (grid, isni, wikidata, fundref) becomes a ROR id by the same
        table whichever source emitted it. Every value in an entry's `all`
        list is indexed as well as the preferred one, because a source may
        emit any of them.

        Built lazily with a second streaming pass over the file, since the
        load pass deliberately keeps nothing per record beyond offsets,
        status and edges. The value is a list because ROR does carry the same
        external id on more than one record (an ISNI shared by a university
        and its hospital, say); such an id is logged as a warning naming every
        ROR id it maps to, and all of them are kept, so the caller sees the
        ambiguity rather than a silently chosen winner. Nothing is skipped
        silently: a malformed entry raises.
        """
        if self._external_id_index is None:
            index: dict[str, dict[str, list[str]]] = {}
            source = str(self.json_path)
            for span in iter_dump_records(self.json_path):
                ror_id = _canonical_record_id(span.record, self.json_path, span.offset)
                for scheme, preferred, values in _external_id_entries(span.record, ror_id, source):
                    by_value = index.setdefault(scheme, {})
                    ids = list(values)
                    if preferred is not None and preferred not in ids:
                        ids.append(preferred)
                    for value in ids:
                        holders = by_value.setdefault(value, [])
                        if ror_id not in holders:
                            holders.append(ror_id)
            duplicates = 0
            for scheme, by_value in index.items():
                for value, holders in by_value.items():
                    if len(holders) > 1:
                        duplicates += 1
                        log.warning(
                            "ror_dump_external_id_shared",
                            scheme=scheme,
                            external_id=value,
                            ror_ids=list(holders),
                        )
            log.info(
                "ror_dump_external_id_index_built",
                path=source,
                schemes={scheme: len(by_value) for scheme, by_value in index.items()},
                shared_ids=duplicates,
            )
            self._external_id_index = index
        return self._external_id_index

    def superseded_ids(self) -> frozenset[str]:
        """Non-active organisations that name at least one successor."""
        return frozenset(
            ror_id
            for ror_id, status in self._statuses.items()
            if status != "active" and self.successors(ror_id)
        )

    def summary(self) -> dict[str, Any]:
        """Counts for the phase gate summary. Not a findings entry."""
        return {
            "version": self.pin.version if self.pin is not None else None,
            "publication_date": self.pin.publication_date if self.pin is not None else None,
            "organisations": len(self._offsets),
            "status_counts": dict(self._status_counts),
            "relationship_counts": dict(self._relationship_counts),
            "organisations_with_relationships": len(self._relationship_index),
            "superseded_with_successor": len(self.superseded_ids()),
        }

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
