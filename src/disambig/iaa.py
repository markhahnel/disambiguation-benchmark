"""Inter-annotator agreement: Cohen's kappa on the double-labelled subsample.

Agreement is computed on the label outcome: the sorted ROR set for resolved
items, or the categorical decision for ambiguous / no_ror. Two annotators
agree only if both the decision class and the ROR set match exactly.
"""

from __future__ import annotations

from collections import Counter


def outcome_key(decision: str, ror_ids: list[str]) -> str:
    if decision in {"ambiguous", "no_ror"}:
        return decision
    return "ror:" + "|".join(sorted(ror_ids))


def cohens_kappa(pairs: list[tuple[str, str]]) -> float:
    """Kappa over (annotator_a_outcome, annotator_b_outcome) pairs."""
    if not pairs:
        raise ValueError("Cannot compute kappa on zero pairs")
    n = len(pairs)
    observed = sum(1 for a, b in pairs if a == b) / n
    counts_a = Counter(a for a, _ in pairs)
    counts_b = Counter(b for _, b in pairs)
    expected = sum(
        (counts_a[category] / n) * (counts_b[category] / n)
        for category in set(counts_a) | set(counts_b)
    )
    if expected == 1.0:
        # Both annotators used a single identical category throughout.
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1 - expected)
