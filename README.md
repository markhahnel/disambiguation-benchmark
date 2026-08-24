# Disambiguation in the open

An open, reusable benchmark for author and institutional disambiguation
across Dimensions, OpenAlex, Crossref, and PubMed. Code, gold standard,
evaluation harness, and results, all public and rerunnable.

**Status: Phase 0** (scope, strata design, gold standard tooling). No
harvesting or evaluation has run yet. See `METHODS.md` for the design and
`LIMITATIONS.md` for where it can be wrong.

## Setup

Requires [uv](https://docs.astral.sh/uv/). Python 3.12+ is resolved and
installed by uv automatically.

```
uv sync
uv run pytest        # 54 tests, no network
uv run ruff check src scripts tests
uv run mypy && uv run mypy scripts/
```

Copy `config/env.example` to `config/.env` and fill in values (or export
them). Every script fails fast, naming the missing variable. Phase 0 needs
only `OPENALEX_MAILTO` (pilot sampling) and `LLAMA_SERVER_URL` (proposals).

## Phase 0 workflow

### 1. Try the review UI on demo data (no credentials needed)

```
uv run scripts/review_ui.py --items data/demo/demo_items.jsonl \
    --proposals data/demo/demo_proposals.jsonl \
    --db data/demo/demo_review.sqlite
```

Open `http://127.0.0.1:8377/?annotator=yourname`. Keys: `1`-`9` toggle
candidates (multi-select for multi-affiliation strings), `Enter` saves,
`a` accepts the LLM set, `x` ambiguous, `n` no ROR exists, `s` skip,
`c` search ROR directly, `j` reveal LLM reasoning (recorded), `,` note,
`z` undo, `?` help. Demo items are synthetic and banner-flagged; their
candidate pools are real ROR API output.

### 2. Draw the pilot sample (50 items, 5 per stratum)

```
uv run scripts/sample_pilot.py
```

Needs `OPENALEX_MAILTO`. Pins the snapshot date in `config/snapshot.yaml`
on first run, snapshots raw responses to `data/raw/openalex/<date>/`, and
writes `data/interim/pilot_items.jsonl`. Read the frame caveat at the top
of the script and in `METHODS.md` section 2 before trusting it for
anything beyond the pilot. Runtime: about a minute. Disk: a few MB.

### 3. Generate first-pass proposals

```
uv run scripts/propose_candidates.py \
    --items data/interim/pilot_items.jsonl \
    --out data/interim/pilot_proposals.jsonl
```

Needs llama-server running (`LLAMA_SERVER_URL`, default
`http://127.0.0.1:8080`). Pass `--no-llm` for ROR-matcher pools only. All
HTTP is cached in SQLite; reruns hit the network zero times unless
`--refresh` is passed.

### 4. Label

```
uv run scripts/review_ui.py --items data/interim/pilot_items.jsonl \
    --proposals data/interim/pilot_proposals.jsonl
```

Labels land in `data/interim/review.sqlite` (append-only; undo supersedes
rather than deletes). Loading is idempotent, so relaunching never loses
work. Per-annotator queues are independent, which is how the second
annotator labels the IAA subsample blind.

## Layout

Follows the house layout in the repo-level `CLAUDE.md`: `src/disambig/` is
the tested library, `scripts/` are entrypoints, `config/` holds strata and
snapshot pins, `data/raw/` is immutable snapshots, `tests/fixtures/` are
recorded real API responses replayed offline.

## Manual steps

- `config/.env` values must be provided by a human.
- llama-server must be started separately on the Mac Studio.
- Phase gates: each phase ends with a written summary and stops for review
  before the next begins.
