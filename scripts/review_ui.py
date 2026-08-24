"""Launch the adjudication UI.

Loads items (and proposals, if present) into the label database, then serves
the keyboard-driven review interface. Loading is idempotent: already-loaded
item_ids are untouched, so relaunching never loses labels.

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
from disambig.store import ReviewStore  # noqa: E402

log = structlog.get_logger(__name__)

USER_AGENT = (
    "openresearch.wtf disambiguation-benchmark/0.1 "
    "(mailto:m.hahnel@digital-science.com)"
)


def read_jsonl[ModelT: BaseModel](path: Path, model: type[ModelT]) -> list[ModelT]:
    return [
        model.model_validate_json(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


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
    log.info(
        "review_ui_start",
        items_in_file=len(items),
        newly_loaded=inserted,
        proposals=len(proposals),
        db=str(args.db),
        port=args.port,
    )

    http = CachingClient(PROJECT_ROOT / "data" / "interim" / "http_cache.sqlite", USER_AGENT)
    app = create_app(store, ror_searcher=RorClient(http))
    print(f"\n  Review UI: http://127.0.0.1:{args.port}/?annotator=mark\n")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
