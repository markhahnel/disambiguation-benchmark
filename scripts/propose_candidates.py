"""First-pass proposals: ROR candidate pool + local LLM suggestions.

Reads items JSONL, writes proposals JSONL. The LLM proposes and never
decides; a per-item LLM failure is recorded on the proposal (the annotator
sees ROR matcher output only for that item) rather than aborting the run.

Usage:
  uv run scripts/propose_candidates.py --items data/interim/pilot_items.jsonl \
      --out data/interim/pilot_proposals.jsonl [--no-llm] [--refresh]
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

import structlog

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from disambig.env import load_dotenv, require_env  # noqa: E402
from disambig.httpcache import CachingClient  # noqa: E402
from disambig.llm import LlamaClient, LlmError  # noqa: E402
from disambig.logging_setup import configure_logging, new_run_id  # noqa: E402
from disambig.models import Item, Proposal  # noqa: E402
from disambig.ror import RorClient  # noqa: E402

log = structlog.get_logger(__name__)

USER_AGENT = (
    "openresearch.wtf disambiguation-benchmark/0.1 "
    "(mailto:m.hahnel@digital-science.com)"
)
POOL_SIZE = 8


def context_lines(item: Item) -> list[str]:
    if item.context is None:
        return []
    ctx = item.context
    lines = []
    header = " · ".join(
        str(part) for part in (ctx.author_name, ctx.title, ctx.venue, ctx.year) if part
    )
    if header:
        lines.append(header)
    if ctx.doi:
        lines.append(f"doi: {ctx.doi}")
    lines.extend(f"co-author affiliation: {aff}" for aff in ctx.coauthor_affiliations[:3])
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--no-llm", action="store_true", help="ROR matcher pool only")
    parser.add_argument("--refresh", action="store_true", help="bypass the HTTP cache")
    args = parser.parse_args()

    run_id = new_run_id()
    configure_logging(PROJECT_ROOT / "logs", run_id)
    load_dotenv(PROJECT_ROOT / "config" / ".env")

    llama: LlamaClient | None = None
    if not args.no_llm:
        llama = LlamaClient(require_env("LLAMA_SERVER_URL", "local llama-server for proposals"))

    http = CachingClient(PROJECT_ROOT / "data" / "interim" / "http_cache.sqlite", USER_AGENT)
    ror = RorClient(http)

    items = [
        Item.model_validate_json(line)
        for line in args.items.read_text().splitlines()
        if line.strip()
    ]
    log.info("propose_start", items=len(items), llm=not args.no_llm)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    llm_failures = 0
    with args.out.open("w") as out:
        for item in items:
            pool = ror.match_affiliation(item.raw_affiliation, refresh=args.refresh)[:POOL_SIZE]
            assessment = None
            model = None
            llm_error = None
            if llama is not None and pool:
                try:
                    assessment, pool, model = llama.propose(
                        item.raw_affiliation, context_lines(item), pool
                    )
                except LlmError as exc:
                    llm_failures += 1
                    llm_error = str(exc)
                    log.error("llm_proposal_failed", item_id=item.item_id, error=llm_error)
            proposal = Proposal(
                item_id=item.item_id,
                candidates=pool,
                llm_assessment=assessment,
                llm_model=model,
                llm_error=llm_error,
                run_id=run_id,
                created_at=datetime.now(UTC).isoformat(),
            )
            out.write(proposal.model_dump_json() + "\n")
            log.info(
                "proposal_written",
                item_id=item.item_id,
                pool=len(pool),
                assessment=str(assessment) if assessment else None,
            )
    log.info("propose_done", items=len(items), llm_failures=llm_failures, out=str(args.out))
    http.close()
    if llama is not None:
        llama.close()


if __name__ == "__main__":
    main()
