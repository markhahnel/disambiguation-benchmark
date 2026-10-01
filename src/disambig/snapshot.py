"""Snapshot hashing (CLAUDE.md rule 1).

Every file under a raw snapshot root gets a SHA-256; the aggregate hash
over the sorted per-file digest lines is the ``source_snapshot`` value
that appears in every findings and provenance entry. Analysis reads
snapshots, never live APIs, so the aggregate is what makes a number
reproducible: rerun the analysis against the same aggregate and you must
get the same finding.

The root is a parameter rather than a constant because this project
harvests four sources into ``data/raw/<source>/<snapshot_date>/`` and
will want both a whole-snapshot aggregate and per-source aggregates.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MANIFEST_PREFIX = "manifest-"


@dataclass(frozen=True)
class SnapshotManifest:
    snapshot_date: str
    files: dict[str, str]  # path relative to the raw root -> sha256
    aggregate: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(raw_root: Path, snapshot_date: str) -> SnapshotManifest:
    if not _DATE_RE.match(snapshot_date):
        raise ValueError(f"snapshot_date must be YYYY-MM-DD, got '{snapshot_date}'")
    files: dict[str, str] = {}
    for path in sorted(raw_root.rglob("*")):
        # Manifests are excluded so that hashing twice is idempotent.
        if path.is_file() and not path.name.startswith(_MANIFEST_PREFIX):
            files[path.relative_to(raw_root).as_posix()] = sha256_file(path)
    if not files:
        raise FileNotFoundError(f"no files to hash under {raw_root}")
    aggregate = hashlib.sha256(
        "".join(f"{rel}:{digest}\n" for rel, digest in sorted(files.items())).encode()
    ).hexdigest()
    return SnapshotManifest(snapshot_date=snapshot_date, files=files, aggregate=aggregate)


def write_manifest(manifest: SnapshotManifest, raw_root: Path) -> Path:
    out = raw_root / f"{_MANIFEST_PREFIX}{manifest.snapshot_date}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "snapshot_date": manifest.snapshot_date,
                "aggregate": manifest.aggregate,
                "files": dict(sorted(manifest.files.items())),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return out


def load_aggregate(raw_root: Path, snapshot_date: str) -> str:
    """The aggregate hash an analysis script cites as source_snapshot."""
    manifest_path = raw_root / f"{_MANIFEST_PREFIX}{snapshot_date}.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{manifest_path}: top level must be an object")
    aggregate = data.get("aggregate")
    if not isinstance(aggregate, str) or not aggregate:
        raise ValueError(f"{manifest_path}: missing aggregate hash")
    return aggregate
