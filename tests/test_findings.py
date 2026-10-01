"""The findings contract: schema, merge behaviour, and the scale guard.

Values here are deliberately synthetic. Nothing in this file is a result.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from disambig.findings import (
    Finding,
    FindingsError,
    load_findings,
    normalise_unit,
    record_findings,
    unit_family,
)


def make(
    value: float | int | str = 42.0,
    unit: str = "things",
    n: int = 10,
    scale_exempt_reason: str | None = None,
    computed_at: str = "2026-09-08T00:00:00+00:00",
    ror_dump_version: str | None = None,
) -> Finding:
    return Finding(
        value=value,
        unit=unit,
        n=n,
        computed_by="scripts/analyse_example.py",
        computed_at=computed_at,
        source_snapshot="0123456789abcdef0123",
        scale_exempt_reason=scale_exempt_reason,
        ror_dump_version=ror_dump_version,
    )


def precision(true_positives: int, assigned: int) -> float:
    """The shape of the analysis-script bug that produces a NaN: a stratum
    with no assignments has a zero denominator, and the script falls through
    to a not-a-number rather than declaring the cell not measurable."""
    return true_positives / assigned if assigned else math.nan


def test_roundtrip_and_merge(tmp_path: Path) -> None:
    path = tmp_path / "findings.json"
    record_findings(path, {"first_key": make(1)})
    record_findings(path, {"second_key": make(2), "first_key": make(3)})
    loaded = load_findings(path)
    assert loaded["first_key"].value == 3  # same-key entries are replaced
    assert loaded["second_key"].value == 2
    assert list(loaded) == ["first_key", "second_key"]  # sorted, so diffs stay readable


def test_missing_file_is_empty_not_an_error(tmp_path: Path) -> None:
    assert load_findings(tmp_path / "nothing.json") == {}


def test_invalid_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(FindingsError, match="snake_case"):
        record_findings(tmp_path / "f.json", {"Bad-Key": make()})


def test_empty_unit_rejected(tmp_path: Path) -> None:
    with pytest.raises(FindingsError, match="empty unit"):
        record_findings(tmp_path / "f.json", {"key": make(unit=" ")})


def test_negative_n_rejected(tmp_path: Path) -> None:
    with pytest.raises(FindingsError, match="negative"):
        record_findings(tmp_path / "f.json", {"key": make(n=-1)})


def test_boolean_value_rejected(tmp_path: Path) -> None:
    # bool is a subclass of int, so this would otherwise publish "True".
    bad = Finding(
        value=True,
        unit="flag",
        n=1,
        computed_by="s",
        computed_at="2026-09-08T00:00:00+00:00",
        source_snapshot="h",
    )
    with pytest.raises(FindingsError, match="type bool"):
        record_findings(tmp_path / "f.json", {"key": bad})


# --- non-finite values -----------------------------------------------------


def test_zero_over_zero_nan_is_refused_before_it_reaches_disk(tmp_path: Path) -> None:
    path = tmp_path / "findings.json"
    value = precision(0, 0)
    assert math.isnan(value)  # the bug really produced a NaN
    with pytest.raises(FindingsError, match=r"'example_precision_pct'.*not a finite number"):
        record_findings(path, {"example_precision_pct": make(value, unit="per cent")})
    assert not path.exists()


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_float_rejected_with_any_unit(tmp_path: Path, value: float) -> None:
    # No escape hatch: an exemption reason does not make an infinity a number.
    with pytest.raises(FindingsError, match="not a finite number"):
        record_findings(
            tmp_path / "f.json", {"key": make(value, unit="things", scale_exempt_reason="no")}
        )


def test_non_finite_value_does_not_clobber_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "findings.json"
    record_findings(path, {"good_key": make(7)})
    before = path.read_text(encoding="utf-8")
    with pytest.raises(FindingsError, match="not a finite number"):
        record_findings(path, {"good_key": make(7), "bad_key": make(math.nan)})
    assert path.read_text(encoding="utf-8") == before


def test_serialisation_refuses_nan_even_if_validation_is_bypassed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Second line of defence: with validate() out of the way, json.dumps with
    # allow_nan=False still refuses to write the token NaN, and nothing lands.
    monkeypatch.setattr(Finding, "validate", lambda self, key: None)
    path = tmp_path / "findings.json"
    with pytest.raises(ValueError, match="Out of range float"):
        record_findings(path, {"key": make(math.nan)})
    assert not path.exists()


def test_hand_edited_file_with_nan_token_rejected_on_load(tmp_path: Path) -> None:
    # Python's json module parses the non-standard token NaN; the loader
    # must still refuse the entry rather than carry a NaN into the render.
    path = tmp_path / "findings.json"
    record_findings(path, {"key": make(7)})
    text = path.read_text(encoding="utf-8").replace('"value": 7', '"value": NaN')
    path.write_text(text, encoding="utf-8")
    with pytest.raises(FindingsError, match=r"'key'.*not a finite number"):
        load_findings(path)


# --- timestamps and hand-edited files ---------------------------------------


@pytest.mark.parametrize("computed_at", ["yesterday", "08/09/2026", "2026-13-40T00:00:00"])
def test_non_iso_timestamp_rejected(tmp_path: Path, computed_at: str) -> None:
    with pytest.raises(FindingsError, match="ISO 8601"):
        record_findings(tmp_path / "f.json", {"key": make(computed_at=computed_at)})


@pytest.mark.parametrize(
    "computed_at", ["2026-09-08T00:00:00+00:00", "2026-09-08T00:00:00Z", "2026-09-08"]
)
def test_iso_timestamp_forms_accepted(tmp_path: Path, computed_at: str) -> None:
    record_findings(tmp_path / "f.json", {"key": make(computed_at=computed_at)})
    assert load_findings(tmp_path / "f.json")["key"].computed_at == computed_at


def test_malformed_file_rejected(tmp_path: Path) -> None:
    path = tmp_path / "findings.json"
    path.write_text('["not", "an", "object"]', encoding="utf-8")
    with pytest.raises(FindingsError, match="top level"):
        load_findings(path)


def test_hand_edited_entry_missing_field_rejected(tmp_path: Path) -> None:
    path = tmp_path / "findings.json"
    path.write_text('{"key": {"value": 1, "unit": "things"}}', encoding="utf-8")
    with pytest.raises(FindingsError, match="missing n, computed_by"):
        load_findings(path)


def test_hand_edited_entry_with_unexpected_field_rejected(tmp_path: Path) -> None:
    path = tmp_path / "findings.json"
    record_findings(path, {"key": make(7)})
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["key"]["rounded_to"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FindingsError, match="unexpected fields rounded_to"):
        load_findings(path)


@pytest.mark.parametrize(
    ("field", "bad"),
    [("unit", 5), ("computed_by", None), ("computed_at", 20260908), ("source_snapshot", ["h"])],
)
def test_hand_edited_entry_with_non_string_field_rejected(
    tmp_path: Path, field: str, bad: object
) -> None:
    # A contract violation must read as one, not as an AttributeError.
    path = tmp_path / "findings.json"
    record_findings(path, {"key": make(7)})
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["key"][field] = bad
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FindingsError, match=f"non-string {field}"):
        load_findings(path)


def test_hand_edited_entry_with_non_integer_n_rejected(tmp_path: Path) -> None:
    path = tmp_path / "findings.json"
    record_findings(path, {"key": make(7)})
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["key"]["n"] = "10"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FindingsError, match="non-integer n"):
        load_findings(path)


# --- the scale guard and its unit families ----------------------------------


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("per cent", "per cent"),
        ("Per-Cent", "per cent"),
        ("per_cent", "per cent"),
        ("per   cent", "per cent"),
        ("pct.", "pct"),
        (" % of instances ", "% of instances"),
        ("percentage_points", "percentage points"),
        ("PROPORTION", "proportion"),
        ("instances/author", "instances author"),
    ],
)
def test_normalise_unit(unit: str, expected: str) -> None:
    assert normalise_unit(unit) == expected


# Hand-checked classification table: the unit string as an analyst might
# type it, and the scale family the guard must read from it.
UNIT_FAMILY_GOLDEN: list[tuple[str, str | None]] = [
    ("%", "percent"),
    ("per cent", "percent"),  # the house spelling (CLAUDE.md section 5)
    ("Per Cent", "percent"),
    ("per-cent", "percent"),
    ("percent", "percent"),
    ("percentage", "percent"),
    ("% of instances", "percent"),
    ("percent of works", "percent"),
    ("percentage of items", "percent"),
    ("pct", "percent"),
    ("pct.", "percent"),
    ("percentage point", "percent"),
    ("percentage points", "percent"),
    ("percentage_point", "percent"),
    ("percentage pts", "percent"),
    ("pp", "percent"),
    ("PP", "percent"),
    ("proportion", "proportion"),
    ("Proportions", "proportion"),
    ("fraction", "proportion"),
    ("fraction of instances", "proportion"),
    ("ratio", None),  # a ratio above one is ordinary
    ("count", None),
    ("instances", None),
    ("kappa", None),
    ("seconds", None),
    ("rate", None),  # reported on both scales, so the unit alone fixes neither
    ("ppm", None),  # parts per million is not "pp", and equality is required
    ("things", None),
]


@pytest.mark.parametrize(("unit", "family"), UNIT_FAMILY_GOLDEN)
def test_unit_family_golden(unit: str, family: str | None) -> None:
    assert unit_family(unit) == family


@pytest.mark.parametrize("unit", ["%", "pct", "percent", "percentage", "percentage points", "pp"])
@pytest.mark.parametrize("value", [0.42, 1.0, -0.42])
def test_proportion_labelled_as_percentage_rejected(
    tmp_path: Path, unit: str, value: float
) -> None:
    with pytest.raises(FindingsError, match="reads as a proportion"):
        record_findings(tmp_path / "f.json", {"key": make(value, unit=unit)})


@pytest.mark.parametrize(
    "unit",
    [
        "per cent",  # the house spelling, previously unguarded
        "Per Cent",
        "% of instances",
        "percent of works",
        "percentage of items",
        "pct.",
        "percentage points",
        "percentage-points",
    ],
)
def test_percent_synonyms_are_guarded(tmp_path: Path, unit: str) -> None:
    with pytest.raises(FindingsError, match="reads as a proportion"):
        record_findings(tmp_path / "f.json", {"key": make(0.42, unit=unit)})


@pytest.mark.parametrize("unit", ["ratio", "count", "rate", "kappa", "ppm"])
def test_units_that_fix_no_scale_stay_unguarded(tmp_path: Path, unit: str) -> None:
    for value in (0.42, 2.5):
        record_findings(tmp_path / "f.json", {"key": make(value, unit=unit)})
        assert load_findings(tmp_path / "f.json")["key"].value == value


@pytest.mark.parametrize("unit", ["proportion", "fraction", "Proportions", "fraction of works"])
@pytest.mark.parametrize("value", [42.0, 1.5, -1.5])
def test_percentage_labelled_as_proportion_rejected(
    tmp_path: Path, unit: str, value: float
) -> None:
    with pytest.raises(FindingsError, match="exceeds"):
        record_findings(tmp_path / "f.json", {"key": make(value, unit=unit)})


@pytest.mark.parametrize(
    ("value", "unit"),
    [
        (42.0, "percent"),  # an ordinary percentage
        (42.0, "per cent"),
        (0, "percent"),  # zero is the same number either way
        (0, "per cent"),
        (100.0, "%"),
        (0.42, "proportion"),
        (1.0, "proportion"),
        (0, "fraction"),
        (-0.42, "proportion"),  # a negative change in a proportion is in range
        (2.5, "ratio"),  # a ratio above one is ordinary, so it is not guarded
        (12, "instances"),
        (0.5, "kappa"),
    ],
)
def test_values_consistent_with_their_unit_pass(
    tmp_path: Path, value: float | int, unit: str
) -> None:
    record_findings(tmp_path / "f.json", {"key": make(value, unit=unit)})
    assert load_findings(tmp_path / "f.json")["key"].value == value


def test_string_value_is_not_scale_checked(tmp_path: Path) -> None:
    record_findings(tmp_path / "f.json", {"key": make("not measurable", unit="percent")})
    assert load_findings(tmp_path / "f.json")["key"].value == "not measurable"


def test_scale_exemption_allows_a_genuine_sub_one_percentage(tmp_path: Path) -> None:
    path = tmp_path / "findings.json"
    record_findings(
        path,
        {
            "key": make(
                0.42,
                unit="per cent",
                scale_exempt_reason="checked against the count: a fraction of one per cent",
            )
        },
    )
    loaded = load_findings(path)
    assert loaded["key"].value == 0.42
    assert loaded["key"].scale_exempt_reason is not None


def test_empty_scale_exemption_rejected(tmp_path: Path) -> None:
    with pytest.raises(FindingsError, match="empty scale_exempt_reason"):
        record_findings(
            tmp_path / "f.json", {"key": make(0.42, unit="percent", scale_exempt_reason="  ")}
        )


def test_hand_edited_non_string_scale_exemption_rejected(tmp_path: Path) -> None:
    path = tmp_path / "findings.json"
    record_findings(path, {"key": make(7)})
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["key"]["scale_exempt_reason"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FindingsError, match="non-string scale_exempt_reason"):
        load_findings(path)


# --- the ROR data dump release ----------------------------------------------


def test_ror_dump_version_is_stored_and_loaded_back(tmp_path: Path) -> None:
    # The institutional benchmark sets it on every finding (METHODS.md
    # section 6, LIMITATIONS.md section 7).
    path = tmp_path / "findings.json"
    record_findings(path, {"key": make(7, ror_dump_version="v9.99")})
    assert load_findings(path)["key"].ror_dump_version == "v9.99"
    assert json.loads(path.read_text(encoding="utf-8"))["key"]["ror_dump_version"] == "v9.99"


def test_ror_dump_version_is_optional(tmp_path: Path) -> None:
    # The author benchmark does not use ROR and leaves it unset.
    path = tmp_path / "findings.json"
    record_findings(path, {"key": make(7)})
    assert load_findings(path)["key"].ror_dump_version is None


def test_empty_ror_dump_version_rejected(tmp_path: Path) -> None:
    with pytest.raises(FindingsError, match="empty ror_dump_version"):
        record_findings(tmp_path / "f.json", {"key": make(7, ror_dump_version=" ")})


def test_hand_edited_non_string_ror_dump_version_rejected(tmp_path: Path) -> None:
    path = tmp_path / "findings.json"
    record_findings(path, {"key": make(7, ror_dump_version="v9.99")})
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["key"]["ror_dump_version"] = 9.99
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FindingsError, match="non-string ror_dump_version"):
        load_findings(path)


def test_hand_edited_file_without_the_optional_fields_still_loads(tmp_path: Path) -> None:
    # Files written before the optional fields existed carry only the six
    # required fields and must keep loading.
    path = tmp_path / "findings.json"
    record_findings(path, {"key": make(7)})
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["key"]["scale_exempt_reason"]
    del payload["key"]["ror_dump_version"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_findings(path)["key"]
    assert loaded.value == 7
    assert loaded.ror_dump_version is None
    assert loaded.scale_exempt_reason is None
