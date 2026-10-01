# Methods

Status: **Phase 0 draft, pending gate review.** Nothing here is frozen. This
document is written to be read by someone who wants to disagree with us, and
Phase 0 exists so the first person to disagree is the project owner, before
any labelling at scale.

## 1. What is being benchmarked

Two related but separate benchmarks across four sources: Dimensions,
OpenAlex, Crossref, and PubMed.

- **Benchmark A, institutional disambiguation**: given a work-author
  affiliation, does the source assign the correct ROR identifier(s)?
- **Benchmark B, author disambiguation**: given a set of works asserted by a
  researcher on their ORCID record, does the source cluster them into one
  author entity, and does that entity contain works that are not theirs?

## 2. Benchmark A: unit of evaluation

The unit is the **work-author-affiliation instance**: one author on one work
with one affiliation field. The gold label is the set of ROR IDs for the
institution(s) that author actually listed on that work, adjudicated by a
human from the raw affiliation string plus publication context.

Why instance-centric rather than string-centric: the four sources hold
different raw strings for the same instance (publisher-deposited, PubMed's
`AffiliationInfo`, or their own normalisation). Scoring each source on the
string it happens to display would make the samples incomparable. Scoring
each source on the same instance, against a human judgement of what the
author actually listed, keeps one evaluation path for all sources. The raw
string as each source holds it is recorded at harvest for error analysis.

Accuracy is only computed on instances where the work is present in all four
sources (the intersection frame). Corpus coverage, whether the work is there
at all, is a different failure and is reported separately per source against
the full frame. This choice removes corpus-size differences from the
disambiguation comparison; it also means the benchmark says nothing about
corpus coverage as such, and we say so in the post.

### Matching rules

A large share of apparent errors are hierarchy choices, not errors: a
hospital that is a ROR child of a university, an institute inside a national
research council. Every result is therefore computed and published under
three rules, with no primary rule declared. Hierarchy edges are **one hop**,
by definition, over the relationship edges in the pinned ROR release:

1. **Exact**: assigned ROR ID equals gold ROR ID.
2. **Parent-child**: exact, or one direct parent or child edge joins the
   assigned ID and the gold ID, in either direction.
3. **Any-relationship**: exact, or one direct parent, child or related edge
   joins them in either direction, or one ID is a predecessor or successor
   of the other along the ROR succession chain, of any length.

Succession is the one place a chain is followed: a sequence of renames and
mergers is a single institution's identity over time, the edges are directed
and short, and nothing degenerate can happen. Hierarchy and "related" edges
are never chained.

Transitive closure is deliberately not a headline rule. Closure over ROR's
"related" edges is degenerate: it would eventually connect most of a national
academy's estate and make the most permissive rule meaningless, while
quietly flattering whichever source assigns parent organisations. Ancestry
distance (shortest monotone path over parent and child edges only, to a
maximum depth) is computed as a separate sensitivity diagnostic instead. ROR
does not assert every edge from both ends, so every rule consults the edges
recorded on both the assigned and the gold record.

The ranking of sources can flip between rules. That sensitivity is itself a
published figure, not a footnote.

**Identifiers.** Every ROR ID on every side of every comparison (gold label,
source assignment, relationship target) passes through one normaliser
(`src/disambig/ror_ids.py`). Formatting differences are not disambiguation
errors. Any other identifier scheme a source emits (GRID, ISNI, Wikidata,
FundRef) is crosswalked to ROR through one shared index built from the pinned
dump's `external_ids` and applied identically to every source at harvest.
Where one external identifier maps to more than one ROR record (the pinned
release has several hundred such pairs, mostly ISNI and Wikidata), the
crosswalk yields no assignment, for every source alike, and the count of
such cases is published. There is no per-source normalisation step anywhere.

**Outcomes.** For each item and each rule the scorer records one outcome:
`correct`, `collapsed` (every assigned ID grounded, but the gold set is
larger: the multi-affiliation collapse), `over_assigned`, `stale` (no
assigned ID grounded, and every assigned ID is a predecessor, through any
length of successor chain, of a gold ID: a renamed or merged institution
reported under its old identifier), `partial`, `wrong`, `no_assignment`,
`gold_ambiguous`, and `gold_no_ror`. The last three carry no element counts:
an abstention is a **coverage** failure and is reported alongside accuracy,
never folded into recall, and ambiguous or no-ROR gold items are reported as
their own rates. The pairwise relation between each assigned and gold ID is
recorded independently of the rule, so the error taxonomy is tabulated once,
not once per rule.

**Pending gate decisions**, implemented one way for now and listed here so
they are not mistaken for settled design: whether rule 3 should exclude
predecessor and successor edges so that stale IDs stay a visible error under
all three rules (currently they count as hits under rule 3); whether an
assignment made against a `no_ror` gold item counts as a precision failure
(currently reported as its own rate only); whether `over_assigned` counts as
a hit under the hierarchy rules (currently it does not, and the extra IDs
count as false positives); and the reporting depth of the ancestry
diagnostic.

### Sampling frame and strata

Target n = 2,000, stratified deliberately towards hard cases (ten strata,
roughly equal cells, defined with inclusion criteria and sub-quotas in
`config/strata.yaml`). A proportional sample would be dominated by easy
Anglophone universities and would tell us nothing.

**Pilot frame (50 items, 5 per stratum)**: drawn from OpenAlex via
`scripts/sample_pilot.py` because it is the fastest frame to stand up.
Several strata are routed using OpenAlex's own institution resolution
(country and type filters). This oversamples strings OpenAlex could already
resolve and would flatter OpenAlex if kept; it is acceptable only for
piloting the UI and the strata definitions, and it is flagged in
LIMITATIONS.md.

**Production frame (Phase 1, pending gate approval)**: instances sampled
from Crossref and PubMed raw strings, routed into strata by string-level
heuristics only (script detection, token lists, structure detection in
`src/disambig/signals.py`), never by any evaluated source's resolution
output. The renamed/merged/split stratum is built from predecessor and
successor relationships in the pinned ROR data dump. Routing heuristics
decide only what gets sampled into a stratum; the verified stratum is
confirmed at adjudication and travels with the frozen gold standard.

### Labelling protocol

Two-stage, and the stages have different powers:

1. **Proposal (machine)**: a local LLM (llama-server) sees the raw string,
   publication context, and a candidate pool from the ROR affiliation
   matcher, and proposes candidate RORs with a confidence and a one-line
   justification. Proposals from outside the candidate pool are discarded
   and logged; the model cannot introduce a ROR ID the matcher did not
   return (the human can, via direct ROR search). A per-item LLM failure
   leaves matcher output only; it never blocks adjudication.
2. **Adjudication (human)**: every single item is adjudicated in the review
   UI. No LLM-only label enters the gold standard.

Review UI design choices that matter methodologically:

- Candidates are ordered by ROR matcher score, never by LLM confidence.
- LLM justifications are collapsed until the annotator asks for them, and
  the reveal is recorded per label, so anchoring on the first pass is
  measurable rather than assumed away.
- `accepted` versus `corrected` is derived server-side by comparing the
  selected ROR set with the LLM-proposed set.
- `ambiguous` and `no_ror` are first-class labels. An ambiguous rate per
  stratum is a finding; forcing a choice would manufacture false certainty.
- Multi-affiliation items take a ROR set, not a single ID.
- Labelling time per item is recorded; the time distribution across strata
  is itself a finding about which cases are genuinely hard.
- Every label event is kept; corrections supersede rather than overwrite.

### Inter-annotator agreement

A second annotator labels a 200-item subsample (20 per stratum) blind to the
first annotator's labels (per-annotator queues are independent by
construction). Agreement is reported as Cohen's kappa on the full outcome:
decision class plus exact ROR set (`src/disambig/iaa.py`). Disagreements are
adjudicated by discussion and recorded; items that stay contested are
labelled ambiguous. The second annotator should be someone with scholarly
metadata experience and no Digital Science affiliation, for the obvious
reason.

### Freezing

The gold standard is frozen and SHA-256 hashed before any evaluated source
is queried for its assignments. No label is revisited after seeing which
source it favours. The freeze hash is recorded in `outputs/provenance.json`
and published.

## 3. Benchmark B: author disambiguation

Ground truth is author-asserted ORCID records, with the caveats stated in
LIMITATIONS.md. Inclusion: records with at least 10 works whose ORCID
`source` is the record holder themselves. Works added by any member client
(publisher integrations, Crossref Metadata Search, DataCite, and every
source under evaluation) are excluded, because a record populated by
OpenAlex cannot score OpenAlex. The exact filter expression and its
inclusion/exclusion counts will be published alongside the harvest.

For each source: do the researcher's self-asserted works map to one author
entity, and does that entity contain works that are not theirs?

Metrics, all reported, none averaged into a single headline: B-cubed
precision/recall/F1, pairwise precision/recall/F1, cluster purity, and a
separate splitting-versus-merging breakdown, because those two errors have
opposite consequences for downstream users and a single F1 hides which one
is happening.

Stratification (design in `config/strata.yaml`): quota cells on name
commonality (five buckets from the empirical name-frequency distribution,
edges frozen with the gold standard) crossed with mobility (distinct
institutions in ORCID employment history, three buckets), 15 cells of 70.
Script, career stage (first publication year), and discipline are recorded
covariates with minimum floors rather than quota dimensions; a full five-way
cross would need cells too small to carry a confidence interval.

## 4. Structural fairness

I work for Digital Science and Dimensions is our product, so the harness is
built to be structurally incapable of favouring any source:

- One evaluation path. No per-source branches, normalisation, or
  tie-breaking. Handling needed by one source is applied to all or not at
  all.
- Sources are loaded as `SOURCE_A` through `SOURCE_D` from a mapping file
  during evaluation; real names join only at the reporting step. This is
  slightly theatrical. We do it anyway, and we say so here.
- The report generator emits the losses table first: every category where
  Dimensions is not the best source, produced automatically with no manual
  step that could drop rows. It is published whatever it says.
- Gold standard construction precedes any source query, and the frozen gold
  standard is hashed before evaluation.

## 5. Statistical reporting

Precision, recall, and F1 per source per stratum with bootstrapped
confidence intervals (BCa, 10,000 resamples, resampling instances within
stratum). Some cells will have small n and wide intervals; they are shown
wide. Coverage (any assignment at all) is always reported next to accuracy,
because a source that assigns nothing is 100% precise and useless.

## 6. What would change the answer

- A different ROR release: renamed and merged institutions move under you.
  The ROR data dump is pinned to one Zenodo release in `config/ror_dump.yaml`
  (version, DOI, SHA-256 of the archive and of the extracted JSON), that
  directory is folded into the snapshot hash every finding cites, and every
  institutional-benchmark finding carries the release version explicitly.
  Gold labels can only be assigned to IDs present in the pinned release: the
  review UI refuses anything else, so a gold standard can never reference an
  organisation the scorer cannot see.
- The hierarchy matching rule: which is why all three are published.
- The intersection frame: excluding works missing from any source shrinks
  and skews the frame towards well-covered literature. Coverage against the
  full frame is reported to keep this visible.
- The ORCID self-assertion filter: stricter filters shrink n and skew
  towards diligent curators; looser ones let evaluated sources leak into the
  ground truth. Ours is published as an exact expression.
