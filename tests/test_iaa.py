"""Cohen's kappa golden tests, expected values computed by hand."""

import pytest

from disambig.iaa import cohens_kappa, outcome_key


def test_kappa_hand_computed_golden() -> None:
    # 10 pairs: (A,A) x4, (B,B) x3, (A,B) x2, (B,A) x1.
    # observed = 7/10. counts: a(A)=6, a(B)=4; b(A)=5, b(B)=5.
    # expected = 0.6*0.5 + 0.4*0.5 = 0.5. kappa = (0.7-0.5)/(1-0.5) = 0.4.
    pairs = [("A", "A")] * 4 + [("B", "B")] * 3 + [("A", "B")] * 2 + [("B", "A")]
    assert cohens_kappa(pairs) == pytest.approx(0.4)


def test_perfect_agreement_across_categories() -> None:
    assert cohens_kappa([("A", "A"), ("B", "B"), ("C", "C")]) == pytest.approx(1.0)


def test_single_shared_category_is_defined() -> None:
    assert cohens_kappa([("A", "A"), ("A", "A")]) == 1.0


def test_chance_level_agreement_is_zero() -> None:
    # Both annotators split 50/50 independently: observed 0.5 = expected 0.5.
    pairs = [("A", "A"), ("A", "B"), ("B", "A"), ("B", "B")]
    assert cohens_kappa(pairs) == pytest.approx(0.0)


def test_empty_input_raises() -> None:
    with pytest.raises(ValueError):
        cohens_kappa([])


def test_outcome_key_orders_ror_sets() -> None:
    assert outcome_key("corrected", ["b", "a"]) == outcome_key("accepted", ["a", "b"])
    assert outcome_key("ambiguous", []) == "ambiguous"
    assert outcome_key("no_ror", []) != outcome_key("ambiguous", [])
