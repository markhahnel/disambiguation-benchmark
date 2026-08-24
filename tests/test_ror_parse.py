"""ROR record parsing against real recorded responses and real-world rot:
missing fields, nulls where lists are expected, CJK names, HTML entities."""

import json
from pathlib import Path
from typing import Any

import pytest

from disambig.ror import parse_ror_record

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def test_parses_real_oxford_affiliation_response() -> None:
    payload = load("ror_affiliation_oxford.json")
    top = payload["items"][0]
    candidate = parse_ror_record(top["organization"], match_score=top["score"])
    assert candidate.ror_id == "https://ror.org/052gg0110"
    assert candidate.name == "University of Oxford"
    assert candidate.country == "United Kingdom"
    assert candidate.match_score == 1.0
    assert "education" in candidate.org_types


def test_parses_real_cas_response_with_cjk_and_relationships() -> None:
    payload = load("ror_affiliation_cas.json")
    org = payload["items"][0]["organization"]
    candidate = parse_ror_record(org)
    assert candidate.name == "Institute of Physics"
    assert "中国科学院物理研究所" in candidate.aliases
    assert "IOP" in candidate.acronyms
    assert candidate.city == "Beijing"
    assert any(rel.rel_type == "parent" for rel in candidate.relationships) or any(
        rel.rel_type == "child" for rel in candidate.relationships
    )


def test_missing_id_is_an_error_not_a_silent_drop() -> None:
    with pytest.raises(ValueError, match="without an id"):
        parse_ror_record({"names": [{"value": "X", "types": ["ror_display"]}]})


def test_no_usable_name_is_an_error() -> None:
    with pytest.raises(ValueError, match="no usable name"):
        parse_ror_record({"id": "https://ror.org/000000000", "names": []})


def test_nulls_where_lists_expected() -> None:
    candidate = parse_ror_record(
        {
            "id": "https://ror.org/012345678",
            "names": [{"value": "Universidad de Prueba", "types": ["ror_display"], "lang": None}],
            "locations": None,
            "types": None,
            "relationships": None,
        }
    )
    assert candidate.name == "Universidad de Prueba"
    assert candidate.city is None
    assert candidate.org_types == []
    assert candidate.relationships == []


def test_missing_display_name_falls_back_to_alias() -> None:
    candidate = parse_ror_record(
        {
            "id": "https://ror.org/012345678",
            "names": [{"value": "Fallback Alias", "types": ["alias"]}],
        }
    )
    assert candidate.name == "Fallback Alias"


def test_html_entities_and_mojibake_survive_untouched() -> None:
    # Parsers must not "helpfully" mangle already-broken upstream text.
    dirty = "Universit&eacute; de MontrÃ©al"
    candidate = parse_ror_record(
        {
            "id": "https://ror.org/012345678",
            "names": [{"value": dirty, "types": ["ror_display"]}],
        }
    )
    assert candidate.name == dirty


def test_relationship_rows_with_missing_fields_are_skipped() -> None:
    candidate = parse_ror_record(
        {
            "id": "https://ror.org/012345678",
            "names": [{"value": "X", "types": ["ror_display"]}],
            "relationships": [
                {"type": "parent", "label": "Parent U", "id": "https://ror.org/0aaaaaaaa"},
                {"type": "child", "label": None, "id": "https://ror.org/0bbbbbbbb"},
                "not-a-dict",
            ],
        }
    )
    assert len(candidate.relationships) == 1
    assert candidate.relationships[0].label == "Parent U"
