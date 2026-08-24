"""Draw the 50-item pilot sample (5 per stratum) for Benchmark A.

PILOT-ONLY SHORTCUT, FLAGGED: this sampler draws from OpenAlex because it is
the fastest frame to stand up. Several strata are routed using OpenAlex's own
institution resolution (country and type filters), which oversamples strings
OpenAlex could already resolve and would flatter OpenAlex if reused for the
real frame. The production frame (Phase 1) samples raw strings from
Crossref + PubMed and routes with string-level heuristics only. See
METHODS.md section 3 before trusting anything drawn here beyond the pilot.

The renamed_merged_split stratum uses a curated seed list of predecessor
names (recent French mergers dominate because they are the canonical recent
cases); Phase 1 builds this stratum from ROR dump predecessor/successor
relationships instead.

Usage:
  uv run scripts/sample_pilot.py --out data/interim/pilot_items.jsonl
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import structlog
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from disambig.env import load_dotenv, require_env  # noqa: E402
from disambig.httpcache import CachingClient  # noqa: E402
from disambig.logging_setup import configure_logging, new_run_id  # noqa: E402
from disambig.models import Item, PublicationContext  # noqa: E402
from disambig.signals import (  # noqa: E402
    dominant_non_latin_script,
    looks_multi_affiliation,
    token_signals,
)

log = structlog.get_logger(__name__)

OPENALEX = "https://api.openalex.org"
PILOT_PER_STRATUM = 5
SAMPLE_SIZE = 100  # works fetched per query before client-side routing
RANDOM_SEED = 20260824

UNDERREP_COUNTRIES = (
    "ug|tz|sn|ml|bf|bw|zm|mw|rw|bj|kz|uz|kg|tj|fj|pg|ws|to|sb|jm|tt|bb|gy|ht"
)
NON_LATIN_COUNTRIES = "cn|jp|kr|ru|eg|sa|th"

COLLIDING_DEPT_QUERIES = [
    "Institute of Physics",
    "School of Public Health",
    "Institute of Microbiology",
    "Department of Computer Science and Engineering",
    "Institute of Chemistry",
]
RENAMED_SEED_QUERIES = [
    "Universite Pierre et Marie Curie",
    "Paris Diderot",
    "Universite Paris Descartes",
    "Kazan State University",
    "University College of North Wales",
]


def latin_university(text: str) -> bool:
    signals = token_signals(text)
    return (
        dominant_non_latin_script(text) is None
        and not signals["hospital"]
        and any(tok in text.lower() for tok in ("university", "college"))
    )


@dataclass
class Strategy:
    stratum: str
    queries: list[str]  # OpenAlex /works query strings (joined filters etc.)
    predicate: Any  # str -> bool on the raw affiliation string
    sub_stratum_fn: Any = None  # optional str -> str | None


def strategies() -> list[Strategy]:
    def works_filter(filt: str) -> str:
        return (
            f"{OPENALEX}/works?filter={filt}"
            f"&sample={SAMPLE_SIZE}&seed={RANDOM_SEED}&per-page={SAMPLE_SIZE}"
        )

    def raw_search(query: str) -> str:
        return works_filter(f"raw_affiliation_strings.search:{quote(query)}")

    return [
        Strategy(
            "anglophone_university",
            [works_filter("authorships.institutions.country_code:us|gb|ca|au|nz|ie,"
                          "authorships.institutions.type:education")],
            latin_university,
        ),
        Strategy(
            "non_latin_script",
            [works_filter(f"authorships.institutions.country_code:{NON_LATIN_COUNTRIES}")],
            lambda s: dominant_non_latin_script(s) is not None,
            sub_stratum_fn=dominant_non_latin_script,
        ),
        Strategy(
            "transliterated",
            [works_filter(f"authorships.institutions.country_code:{NON_LATIN_COUNTRIES}")],
            lambda s: dominant_non_latin_script(s) is None and len(s) > 20,
        ),
        Strategy(
            "hospital_medical",
            [works_filter("authorships.institutions.type:healthcare")],
            lambda s: token_signals(s)["hospital"],
        ),
        Strategy(
            "government_lab",
            [works_filter("authorships.institutions.type:government|facility")],
            lambda s: dominant_non_latin_script(s) is None or True,
        ),
        Strategy(
            "company",
            [works_filter("authorships.institutions.type:company")],
            lambda s: len(s) > 5,
        ),
        Strategy(
            "multi_affiliation",
            [works_filter("authorships.institutions.country_code:us|de|cn|br|in|jp")],
            looks_multi_affiliation,
        ),
        Strategy(
            "colliding_department",
            [raw_search(q) for q in COLLIDING_DEPT_QUERIES],
            lambda s: len(s) > 10,
        ),
        Strategy(
            "renamed_merged_split",
            [raw_search(q) for q in RENAMED_SEED_QUERIES],
            lambda s: len(s) > 10,
        ),
        Strategy(
            "underrepresented_small",
            [works_filter(f"authorships.institutions.country_code:{UNDERREP_COUNTRIES}")],
            lambda s: len(s) > 5,
        ),
    ]


def pin_snapshot_date() -> str:
    """Snapshot date is set once per project run and pinned in config."""
    snapshot_path = PROJECT_ROOT / "config" / "snapshot.yaml"
    config: dict[str, Any] = {}
    if snapshot_path.exists():
        config = yaml.safe_load(snapshot_path.read_text()) or {}
    if not config.get("pilot_snapshot_date"):
        from datetime import UTC, datetime

        config["pilot_snapshot_date"] = datetime.now(UTC).strftime("%Y-%m-%d")
        config["pilot_random_seed"] = RANDOM_SEED
        snapshot_path.write_text(yaml.safe_dump(config, sort_keys=False))
        log.info("snapshot_date_pinned", date=config["pilot_snapshot_date"])
    return str(config["pilot_snapshot_date"])


def extract_instances(work: dict[str, Any]) -> list[tuple[str, PublicationContext]]:
    """(raw string, context) per author, defensively parsed."""
    instances: list[tuple[str, PublicationContext]] = []
    authorships = work.get("authorships")
    if not isinstance(authorships, list):
        return instances
    all_raw: list[str] = []
    for authorship in authorships:
        if isinstance(authorship, dict):
            for raw in authorship.get("raw_affiliation_strings") or []:
                if isinstance(raw, str) and raw.strip():
                    all_raw.append(raw.strip())
    venue = None
    location = work.get("primary_location")
    if isinstance(location, dict):
        source = location.get("source")
        if isinstance(source, dict):
            venue = source.get("display_name")
    for authorship in authorships:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author")
        author_name = author.get("display_name") if isinstance(author, dict) else None
        for raw in authorship.get("raw_affiliation_strings") or []:
            if not isinstance(raw, str) or not raw.strip():
                continue
            raw = raw.strip()
            context = PublicationContext(
                title=work.get("title"),
                venue=venue if isinstance(venue, str) else None,
                year=work.get("publication_year")
                if isinstance(work.get("publication_year"), int)
                else None,
                doi=work.get("doi"),
                author_name=author_name if isinstance(author_name, str) else None,
                coauthor_affiliations=[other for other in all_raw if other != raw][:4],
            )
            instances.append((raw, context))
    return instances


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=PROJECT_ROOT / "data" / "interim" / "pilot_items.jsonl"
    )
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    run_id = new_run_id()
    configure_logging(PROJECT_ROOT / "logs", run_id)
    load_dotenv(PROJECT_ROOT / "config" / ".env")
    mailto = require_env("OPENALEX_MAILTO", "OpenAlex polite pool contact email")
    snapshot_date = pin_snapshot_date()

    raw_dir = PROJECT_ROOT / "data" / "raw" / "openalex" / snapshot_date
    raw_dir.mkdir(parents=True, exist_ok=True)
    http = CachingClient(
        PROJECT_ROOT / "data" / "interim" / "http_cache.sqlite",
        f"openresearch.wtf disambiguation-benchmark/0.1 (mailto:{mailto})",
    )
    rng = random.Random(RANDOM_SEED)
    seen_strings: set[str] = set()
    items: list[Item] = []

    for strategy in strategies():
        picked = 0
        candidates: list[Item] = []
        for query_url in strategy.queries:
            url = f"{query_url}&mailto={quote(mailto)}"
            response = http.get(url, refresh=args.refresh)
            if response.status != 200:
                raise RuntimeError(f"OpenAlex query failed ({response.status}): {url}")
            query_hash = hashlib.sha256(url.encode()).hexdigest()[:8]
            snapshot_file = raw_dir / f"pilot_{strategy.stratum}_{query_hash}.jsonl.gz"
            with gzip.open(snapshot_file, "wt") as snap:
                snap.write(response.body.decode())
            payload = response.json()
            works = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(works, list):
                raise RuntimeError(f"Unexpected OpenAlex payload for {url}")
            for work in works:
                if not isinstance(work, dict):
                    continue
                work_instances = extract_instances(work)
                rng.shuffle(work_instances)
                for raw, context in work_instances:
                    if raw in seen_strings or not strategy.predicate(raw):
                        continue
                    item_id = hashlib.sha256(
                        f"{context.doi}|{raw}".encode()
                    ).hexdigest()[:16]
                    sub = strategy.sub_stratum_fn(raw) if strategy.sub_stratum_fn else None
                    candidates.append(
                        Item(
                            item_id=item_id,
                            raw_affiliation=raw,
                            stratum=strategy.stratum,
                            sub_stratum=sub,
                            source_frame=f"openalex-pilot-{snapshot_date}",
                            context=context,
                        )
                    )
                    seen_strings.add(raw)
                    break  # at most one instance per work, to avoid clustering
        rng.shuffle(candidates)
        chosen = candidates[:PILOT_PER_STRATUM]
        picked = len(chosen)
        items.extend(chosen)
        log.info(
            "stratum_sampled",
            stratum=strategy.stratum,
            eligible=len(candidates),
            picked=picked,
            shortfall=PILOT_PER_STRATUM - picked,
        )
        if picked < PILOT_PER_STRATUM:
            log.warning("stratum_shortfall", stratum=strategy.stratum, picked=picked)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as out:
        for item in items:
            out.write(item.model_dump_json() + "\n")
    log.info("pilot_sample_written", items=len(items), out=str(args.out))
    print(json.dumps({"items": len(items), "out": str(args.out)}))
    http.close()


if __name__ == "__main__":
    main()
