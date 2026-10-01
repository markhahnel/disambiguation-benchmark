"""Provenance for every figure and table (CLAUDE.md rule 0.4).

``outputs/provenance.json`` records, per artefact: the generating script,
the input snapshot hash, the query or filter applied, row counts in and
out, the run ID, and the run timestamp.

This project also records non-figure artefacts here. METHODS.md commits
to freezing and hashing the gold standard before any evaluated source is
queried for its assignments, and that hash needs somewhere auditable to
live: the freeze is an artefact with a script, a row count and a
timestamp like any other.

Validation here mirrors the findings contract: every field is checked for
its type, not just for emptiness, and a file read back from disk is
validated entry by entry. A provenance row that records the text "None"
as its script, because None was stringified on the way in, defeats the
purpose of the file.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any

_TEXT_FIELDS = ("script", "source_snapshot", "query", "run_id", "run_at")
_COUNT_FIELDS = ("rows_in", "rows_out")


class ProvenanceError(ValueError):
    """A provenance entry violated the contract."""


@dataclass(frozen=True)
class ProvenanceEntry:
    script: str
    source_snapshot: str
    query: str
    rows_in: int
    rows_out: int
    run_id: str
    run_at: str

    def validate(self, artefact: str) -> None:
        if not isinstance(artefact, str) or not artefact.strip():
            raise ProvenanceError("provenance artefact name is empty")
        # Type checks come first because a hand-edited file can hold anything,
        # and None must be refused as None, never recorded as the text "None".
        for field in _TEXT_FIELDS:
            text = getattr(self, field)
            if not isinstance(text, str):
                raise ProvenanceError(f"provenance for '{artefact}' has non-string {field}")
            if not text.strip():
                # query is required rather than optional: an empty query field
                # is indistinguishable from having forgotten to record one.
                hint = " (use 'all rows' if no filter was applied)" if field == "query" else ""
                raise ProvenanceError(f"provenance for '{artefact}' has empty {field}{hint}")
        try:
            datetime.fromisoformat(self.run_at)
        except ValueError as exc:
            raise ProvenanceError(
                f"provenance for '{artefact}' has run_at '{self.run_at}',"
                " which is not an ISO 8601 timestamp"
            ) from exc
        for field in _COUNT_FIELDS:
            count = getattr(self, field)
            # bool is a subclass of int; a row count of True is a bug, not one row.
            if isinstance(count, bool) or not isinstance(count, int):
                raise ProvenanceError(f"provenance for '{artefact}' has non-integer {field}")
            if count < 0:
                raise ProvenanceError(f"provenance for '{artefact}' has negative {field}")
        # There is deliberately no rows_out <= rows_in check. A filter never
        # adds rows, but a pivot does: a per-stratum-by-source table has one
        # row in per instance and one row out per (stratum, source) cell, and
        # a bootstrap summary emits one row per resample statistic. Both
        # legitimately report more rows out than in, so the inequality is not
        # a contract and an auditor should not expect one.


def _entry_from_payload(path: Path, artefact: str, payload: Any) -> ProvenanceEntry:
    if not isinstance(payload, dict):
        raise ProvenanceError(f"{path}: entry '{artefact}' must be an object")
    expected = [field.name for field in fields(ProvenanceEntry)]
    missing = [field for field in expected if field not in payload]
    if missing:
        raise ProvenanceError(f"{path}: entry '{artefact}' is missing {', '.join(missing)}")
    unexpected = sorted(set(payload) - set(expected))
    if unexpected:
        raise ProvenanceError(
            f"{path}: entry '{artefact}' has unexpected fields {', '.join(unexpected)}"
        )
    entry = ProvenanceEntry(**payload)
    entry.validate(artefact)
    return entry


def load_provenance(path: Path) -> dict[str, ProvenanceEntry]:
    """Read provenance.json back, validating every entry on the way in."""
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ProvenanceError(f"{path}: top level must be an object")
    return {
        artefact: _entry_from_payload(path, artefact, payload)
        for artefact, payload in loaded.items()
    }


def record_provenance(path: Path, artefact: str, entry: ProvenanceEntry) -> None:
    """Merge one artefact's provenance into provenance.json, replacing its row."""
    entry.validate(artefact)
    existing = load_provenance(path)
    existing[artefact] = entry
    payload = {name: asdict(existing[name]) for name in sorted(existing)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
