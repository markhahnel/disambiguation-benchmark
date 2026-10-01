"""Launch the adjudication UI.

Loads items (and proposals, if present) into the label database, then serves
the keyboard-driven review interface. Loading is idempotent: already-loaded
item_ids are untouched, so relaunching never loses labels.

If config/ror_dump.yaml exists, the pinned ROR release it names is loaded
(and hash-verified) and every label is checked against it: a selected id the
release does not contain is refused, a selected id that is not active in it
(superseded or withdrawn) is refused naming its active successor, with
succession read from either end of the edge exactly as the scorer reads it,
and ROR search results the release does not contain are hidden, so the frozen gold standard
can never cite an organisation the scorer cannot see or a record the scorer
would reject as not active. Records that are not active stay visible in
search, so a historical name can still be looked up and its successor read
off the candidate card. Without the pin the UI runs unchecked and says so at
startup; that is only acceptable for the demo data.

Usage:
  uv run scripts/review_ui.py --items data/interim/pilot_items.jsonl \
      --proposals data/interim/pilot_proposals.jsonl \
      [--db data/interim/review.sqlite] [--port 8377]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import structlog
import uvicorn
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from disambig.httpcache import CachingClient  # noqa: E402
from disambig.logging_setup import configure_logging, new_run_id  # noqa: E402
from disambig.models import Item, Proposal  # noqa: E402
from disambig.review_app import create_app  # noqa: E402
from disambig.ror import RorClient  # noqa: E402
from disambig.ror_dump import RorDump, RorDumpPin  # noqa: E402
from disambig.store import ReviewStore  # noqa: E402

log = structlog.get_logger(__name__)

USER_AGENT = (
    "openresearch.wtf disambiguation-benchmark/0.1 "
    "(mailto:m.hahnel@digital-science.com)"
)

PIN_PATH = PROJECT_ROOT / "config" / "ror_dump.yaml"
DATA_ROOT = PROJECT_ROOT / "data"


def read_jsonl[ModelT: BaseModel](path: Path, model: type[ModelT]) -> list[ModelT]:
    return [
        model.model_validate_json(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def load_ror_release(pin_path: Path, data_root: Path) -> RorDump | None:
    """The pinned ROR release every gold id has to belong to, or None without a pin.

    Loading goes through RorDump.from_pin, so the dump on disk is hash-verified
    against the pin before a single label is checked against it. A pin that
    names a dump which is absent or does not match is an error here, not a
    silent fall back to unchecked labelling.
    """
    if not pin_path.exists():
        log.warning("review_ui_no_ror_pin", pin=str(pin_path))
        return None
    try:
        dump = RorDump.from_pin(pin_path, data_root)
    except FileNotFoundError as exc:
        # A committed pin with no dump on disk is the fresh-clone case. Loud,
        # with the one command that fixes it, rather than a traceback or a
        # silent fall back to unchecked labelling.
        raise SystemExit(
            f"{exc}\nThe pinned release is gitignored; fetch it with:\n"
            "  uv run scripts/pin_ror_dump.py\n(about 40MB from Zenodo, no credentials)."
        ) from exc
    if dump.pin is None:
        raise RuntimeError(f"RorDump.from_pin returned a dump without its pin for {pin_path}")
    log.info(
        "review_ui_ror_release_loaded",
        version=dump.pin.version,
        publication_date=dump.pin.publication_date,
        pin=str(pin_path),
    )
    return dump


def startup_banner(port: int, pin: RorDumpPin | None) -> str:
    """What the console shows when the server is up, including the release check."""
    lines = [f"Review UI: http://127.0.0.1:{port}/?annotator=mark"]
    if pin is None:
        lines.append(
            f"WARNING: {PIN_PATH.relative_to(PROJECT_ROOT)} is absent, so labels are not "
            "being checked against a pinned ROR release."
        )
    else:
        lines.append(
            f"Labels are checked against pinned ROR release {pin.version} "
            f"({pin.publication_date}): ids outside it are refused, and so are ids that are "
            "not active in it (a gold label names the active successor instead, with "
            "succession read from either end of the edge as the scorer reads it)."
        )
    return "\n".join(f"  {line}" for line in lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=Path, required=True)
    parser.add_argument("--proposals", type=Path, default=None)
    parser.add_argument(
        "--db", type=Path, default=PROJECT_ROOT / "data" / "interim" / "review.sqlite"
    )
    parser.add_argument("--port", type=int, default=8377)
    args = parser.parse_args()

    run_id = new_run_id()
    configure_logging(PROJECT_ROOT / "logs", run_id)

    items: list[Item] = read_jsonl(args.items, Item)
    proposals: list[Proposal] = (
        read_jsonl(args.proposals, Proposal) if args.proposals else []
    )
    store = ReviewStore(args.db)
    inserted = store.load_items(items, proposals)
    release = load_ror_release(PIN_PATH, DATA_ROOT)
    log.info(
        "review_ui_start",
        items_in_file=len(items),
        newly_loaded=inserted,
        proposals=len(proposals),
        db=str(args.db),
        port=args.port,
        ror_release=release.pin.version if release is not None and release.pin else None,
    )

    http = CachingClient(PROJECT_ROOT / "data" / "interim" / "http_cache.sqlite", USER_AGENT)
    app = create_app(store, ror_searcher=RorClient(http), ror_release=release)
    print(f"\n{startup_banner(args.port, release.pin if release is not None else None)}\n")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
