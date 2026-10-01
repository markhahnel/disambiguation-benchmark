# Limitations

Ranked by how much each could move the headline result. Status: Phase 0
draft; this list grows as the project does, it never shrinks.

## 1. ORCID as ground truth is biased towards record curators

Benchmark B's ground truth over-represents researchers who curate their own
ORCID records: skewing Anglophone, mid-career, well-resourced, and towards
fields with strong ORCID uptake. The self-assertion filter (required to
avoid circularity) makes this worse, because it selects the most diligent
curators. Direction of bias: it flatters every source on exactly the
easy-to-disambiguate population, and under-tests the populations where
disambiguation fails. The stratification by name commonality and mobility
recovers some of this; it cannot recover researchers who are absent
entirely. Rough size: unknown until we harvest; the filter's
inclusion/exclusion counts will be published so readers can judge.

## 2. The pilot sampling frame leans on OpenAlex's own resolution

The 50-item pilot routes several strata using OpenAlex country and type
filters, which are outputs of the very disambiguation OpenAlex is scored on.
Kept at scale, this would flatter OpenAlex materially. It is pilot-only; the
production frame samples Crossref and PubMed raw strings routed by
string-level heuristics. If the production frame is not approved, this moves
to the top of the list.

## 3. The intersection frame narrows what "accuracy" means

Accuracy is computed only on works present in all four sources. That removes
corpus coverage differences from the comparison (deliberately), but it also
means accuracy is measured on the best-covered slice of the literature,
which is exactly where disambiguation is easiest. The coverage-versus-
accuracy split keeps this visible; it does not remove it.

## 4. LLM anchoring in gold standard construction

The first-pass LLM proposals could anchor the human adjudicator towards the
machine's choice, and the machine's errors could become gold errors. The
mitigations (matcher-score ordering, collapsed justifications with recorded
reveals, every item human-adjudicated, IAA subsample labelled independently)
measure and bound this rather than eliminate it. The kappa on the 200-item
subsample is the check; if it comes back low, the gold standard does not
ship until we understand why.

## 5. The ROR affiliation matcher shapes the candidate pool

The candidate pool the annotator sees comes from the ROR affiliation
matcher. Where the matcher misses the true institution entirely (observed
already on a CJK demo string, where adding a city and postcode to an
otherwise exact institutional name changed the returned candidates), the
annotator must find it by manual search, which is slower and could depress
recall of correct labels in exactly the hard strata. Labelling time and
manual-search usage are recorded per item so this effect is measurable.

## 6. A largely single-annotator gold standard

One person adjudicates every item; the second annotator covers a 200-item
subsample. Kappa quantifies agreement on that subsample, but systematic
idiosyncrasies of the primary annotator on the other 1,800 items remain
possible, and the annotator is employed by the maker of one evaluated
source. The blinding protocol does not apply to labelling (the annotator
sees raw strings, not source outputs, so there is nothing to unblind), but
the perception issue is real and is why the gold standard ships as a public,
correctable artefact.

## 7. ROR version drift

Institutions are renamed, merged, split, and added between ROR releases. All
matching runs against one pinned ROR data dump release, recorded with its
DOI and hashes in `config/ror_dump.yaml` and carried on every institutional
finding. Results under a different ROR release will differ, most for the
renamed/merged/split stratum, which is partly the point of having that
stratum. A consequence of pinning: an organisation added to ROR after the
pinned release date cannot be a gold label at all (the review UI refuses
IDs outside the release), so such instances can only be labelled `no_ror`
or `ambiguous`. That slightly under-states what the live registry could
resolve, identically for every source.

## 8. Stratum routing heuristics are imperfect

Script detection, token lists, and multi-affiliation detection route
sampling; each has known failure modes (for example, Japanese text written
without kana classifies as Chinese, and unusual multi-affiliation formats
escape detection). Routing errors are corrected at adjudication (the
verified stratum travels with the label), so they cost sampling efficiency,
not gold standard correctness. Residual effect: strata may under-fill their
targets, which is reported, never silently backfilled.
