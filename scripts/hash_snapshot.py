"""Hash a raw data snapshot and write its manifest (CLAUDE.md rule 1).

Prints the aggregate hash, which is the value analysis scripts cite as
``source_snapshot`` in every findings and provenance entry. Hashing is
idempotent: existing manifests are excluded from the digest, so rerunning
on unchanged data reproduces the same aggregate.

The snapshot date is read from config/snapshot.yaml rather than the clock,
because it is pinned once per project run. ``snapshot_date`` wins if it is
set; the pilot pins ``pilot_snapshot_date`` instead.

The default root is the whole of data/raw, which includes the pinned ROR
data dump under data/raw/ror/, so the aggregate changes if the ROR release
does. A per-source root gives a per-source aggregate when one is wanted.

Usage:
  uv run scripts/hash_snapshot.py
  uv run scripts/hash_snapshot.py --raw-root data/raw/<source> --snapshot-date <YYYY-MM-DD>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import structlog
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from disambig.logging_setup import configure_logging, new_run_id  # noqa: E402
from disambig.snapshot import build_manifest, write_manifest  # noqa: E402

log = structlog.get_logger(__name__)

DATE_KEYS = ("snapshot_date", "pilot_snapshot_date")


def snapshot_date_from_config(config_path: Path) -> str:
    if not config_path.exists():
        raise FileNotFoundError(f"{config_path} does not exist; the snapshot date is pinned there")
    config: Any = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError(f"{config_path}: top level must be a mapping")
    for key in DATE_KEYS:
        value = config.get(key)
        if value:
            return str(value)
    raise ValueError(f"{config_path}: no snapshot date; set one of {', '.join(DATE_KEYS)}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, default=PROJECT_ROOT / "data" / "raw")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "snapshot.yaml")
    parser.add_argument(
        "--snapshot-date", help="override the date pinned in config, for a per-source manifest"
    )
    parser.add_argument("--logs-dir", type=Path, default=PROJECT_ROOT / "logs")
    args = parser.parse_args(argv)

    run_id = new_run_id()
    configure_logging(args.logs_dir, run_id)

    snapshot_date = args.snapshot_date or snapshot_date_from_config(args.config)
    manifest = build_manifest(args.raw_root, snapshot_date)
    out = write_manifest(manifest, args.raw_root)
    log.info(
        "snapshot_hashed",
        raw_root=str(args.raw_root),
        snapshot_date=snapshot_date,
        files=len(manifest.files),
        aggregate=manifest.aggregate,
        manifest=str(out),
    )
    print(f"{len(manifest.files)} files hashed under {args.raw_root}")
    print(f"aggregate {manifest.aggregate}")
    print(f"manifest  {out}")


if __name__ == "__main__":
    main()
