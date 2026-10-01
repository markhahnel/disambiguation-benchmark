# Disambiguation in the open

An open, reusable benchmark for author and institutional disambiguation
across Dimensions, OpenAlex, Crossref, and PubMed. Code, gold standard,
evaluation harness, and results, all public and rerunnable.

**Status: Phase 0 complete, groundwork for Phase 1 built.** The strata
design, the adjudication UI, a 50-item pilot sample, the pinned ROR
release, the three matching rules, the findings contract, and the figure
toolchain exist and are tested. No gold labels exist yet, no evaluated
source has been harvested, and no finding has been computed. See
`METHODS.md` for the design and `LIMITATIONS.md` for where it can be wrong.
The results will be published as a two-part series: part 1 institutional,
part 2 author disambiguation.

## Reproduce from a cold start

Requires [uv](https://docs.astral.sh/uv/); it resolves and installs the
pinned Python for you. PNG figure export needs `rsvg-convert`
(`brew install librsvg` on macOS).

```
uv sync
uv run scripts/pin_ror_dump.py
uv run pytest
```

- `pin_ror_dump.py` downloads the ROR data dump release recorded in
  `config/ror_dump.yaml` from Zenodo (about 40MB, 20 seconds), verifies its
  hashes, and extracts it under `data/raw/ror/` (about 320MB, gitignored).
  Everything that scores against ROR, including the review UI, refuses to
  run without it. A second run is a no-op. No credentials needed.
- `pytest` runs the whole suite offline against recorded fixtures (630
  tests, under five seconds). Nothing in the tests touches the network.
- Lint and types: `uv run ruff check src scripts tests`, `uv run mypy`, and
  `uv run mypy scripts/`. All four checks pass at every commit.

Secrets live in named environment variables. Copy `config/env.example` to
`config/.env` (gitignored) and fill in values, or export them. Every script
fails fast and names the missing variable. Phase 0 needed only
`OPENALEX_MAILTO`; `LLAMA_SERVER_URL` is optional (see step 3 below).

## Phase 0 workflow

### 1. Try the review UI on demo data

```
uv run scripts/review_ui.py --items data/demo/demo_items.jsonl \
    --proposals data/demo/demo_proposals.jsonl \
    --db data/demo/demo_review.sqlite
```

Open `http://127.0.0.1:8377/?annotator=yourname`. Keys: `1`-`9` toggle
candidates (multi-select for multi-affiliation strings), `Enter` saves,
`a` accepts the LLM set, `x` ambiguous, `n` no ROR exists, `s` skip,
`c` searches ROR directly to correct, `j` reveals LLM reasoning (recorded),
`,` note, `z` undo, `?` help. Demo items are synthetic and banner-flagged;
their candidate pools are real ROR API output. With the pinned release
present, every saved label is checked against it and an ID outside the
release is refused with a message naming it; ROR search results outside
the release are hidden and logged.

### 2. Draw the pilot sample (50 items, 5 per stratum)

```
uv run scripts/sample_pilot.py
```

Needs `OPENALEX_MAILTO`. Pins the snapshot date in `config/snapshot.yaml`
on first run, snapshots raw responses to `data/raw/openalex/<date>/`, and
writes `data/interim/pilot_items.jsonl`. Read the frame caveat at the top
of the script and in `METHODS.md` section 2: the pilot frame leans on
OpenAlex's own resolution and is not the production frame.

### 3. Generate first-pass proposals

```
uv run scripts/propose_candidates.py \
    --items data/interim/pilot_items.jsonl \
    --out data/interim/pilot_proposals.jsonl --no-llm
```

`--no-llm` gives candidate pools straight from the ROR affiliation matcher,
which is how the pilot was run. Without it, the script needs a llama-server
at `LLAMA_SERVER_URL` and adds LLM proposals that the annotator can accept
or correct; the LLM never decides. All HTTP is cached in SQLite; reruns hit
the network zero times unless `--refresh` is passed.

### 4. Label

```
uv run scripts/review_ui.py --items data/interim/pilot_items.jsonl \
    --proposals data/interim/pilot_proposals.jsonl
```

Labels land in `data/interim/review.sqlite`, append-only; undo supersedes
rather than deletes. Loading is idempotent, so relaunching never loses
work. Per-annotator queues are independent, which is how the second
annotator labels the agreement subsample blind. Labelling time per item is
recorded.

## The findings contract (how a number reaches the post)

No number is typed into a draft. Analysis scripts record values through
`src/disambig/findings.py` into `outputs/findings.json`, each with its
unit, n, generating script, timestamp, snapshot hash, and (for the
institutional benchmark) the ROR release. `scripts/hash_snapshot.py`
produces the snapshot hash. `scripts/render_post.py` substitutes
`{{f.key}}` placeholders in `post/draft-part1.md` and `post/draft-part2.md`
from one shared findings file, fails on a missing key, fails on any digit
in the draft that is not a placeholder (the only escape is an explicit
`{{lit:TEXT}}` marker, and every one is listed in the register), and
generates `post/claims-partN.md`: one row per published value, with the
ROR release cited as a footnote. `outputs/provenance.json` records the
script, snapshot, filter, and row counts behind every figure and table.

## Figures

`src/disambig/figures.py` and `viz/` hold the toolchain: one colourblind-safe
palette (`viz/palette.js`), a self-contained figure template, SVG and 2x
PNG export in light and dark, per-figure CSV, alt text, and two guards (size
budget; no reference to anything that would be fetched at render time).
Statistical figures vendor Observable Plot inline: its UMD bundle, D3
included, measured 209KB raw and 69KB gzipped at v0.6.17, comfortably inside
the 500KB per-figure budget, so no figure ever loads a library from a CDN.
Bespoke figures (the hard-cases gallery, the error Sankey) are hand-written
SVG and vanilla JS.
`uv run scripts/figure_selftest.py` exercises the whole path on obviously
synthetic input and writes to `outputs/figures/_selftest/`, which is
gitignored and never deployed. Directories under `outputs/figures/` whose
names start with an underscore are never published.

## Layout

Follows the house layout in the repo-level `CLAUDE.md`: `src/disambig/` is
the tested library, `scripts/` are entrypoints, `config/` holds strata,
the snapshot date, and the ROR pin, `data/raw/` is immutable snapshots,
`tests/fixtures/` are recorded real API responses replayed offline, `viz/`
is the shared figure code, `outputs/` is findings, provenance, figures and
tables, and `post/` is the drafts and generated claims registers.

## Manual steps

- `config/.env` values must be provided by a human.
- llama-server, if used, must be started separately.
- **Ghost Code Injection, Site Footer**: the iframe height listener in
  `viz/ghost-embed-listener.js` must be pasted into Ghost settings by hand
  once (the file's header comment is the copy-paste block). Until it is in
  place, figures render at their fallback height.
- Phase gates: each phase ends with a written summary and stops for review
  before the next begins.
