"""The findings contract (CLAUDE.md rule 0).

``outputs/findings.json`` is a flat key/value map of computed values.
Only analysis scripts write it, through :func:`record_findings`; every
entry carries value, unit, n, computed_by, computed_at and
source_snapshot, so every number in the published post traces back to
the code and the data snapshot that produced it.

Beyond that shared contract this project adds a scale guard, because the
benchmark reports the same quantities as proportions in some places and
as percentages in others (precision, recall, coverage and the ambiguous
rate, each per stratum, per matching rule and per source). A proportion
published as a percentage is a silent factor-of-a-hundred error that no
reader can catch, so an entry whose value contradicts its own unit is
refused here rather than discovered in the post.

The guard classifies units by family rather than by an exact spelling,
because the unit field is free text written by an analyst in a hurry:
"per cent" (the house spelling), "% of instances", "pct." and
"percentage points" all assert the value has already been multiplied by
a hundred, and all of them are guarded. See :func:`unit_family`.

Sub-one percentages are real, so the guard has an explicit and recorded
escape hatch rather than no escape hatch at all: set
``scale_exempt_reason`` and the reason travels with the entry into
``post/claims.md``, where a reviewer can see it.

A value that is not a finite number is refused outright and has no
escape hatch. The realistic way one arrives is a zero denominator in an
analysis script (precision on a stratum with no assignments, kappa on an
empty cell), and rule 0 says an uncomputable number is not written down,
it is written up as not measurable. NaN would also pass every comparison
in the scale guard, so the check runs before it.

Each finding may name the ROR data dump release it was computed against
in ``ror_dump_version``. METHODS.md and LIMITATIONS.md promise that the
release is recorded in every findings entry of the institutional
benchmark; the author benchmark does not use ROR and leaves it unset.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

_KEY_RE = re.compile(r"^[a-z0-9_]+$")

_REQUIRED_FIELDS = ("value", "unit", "n", "computed_by", "computed_at", "source_snapshot")
_OPTIONAL_FIELDS = ("scale_exempt_reason", "ror_dump_version")
_TEXT_FIELDS = ("unit", "computed_by", "computed_at", "source_snapshot")

# Everything in a unit string that is not a letter, a digit or the percent
# sign is a separator: hyphens, underscores, full stops, slashes and runs of
# whitespace all collapse to one space, so "Per-Cent", "per_cent" and
# "per  cent" normalise to the same token.
_UNIT_SEPARATOR_RE = re.compile(r"(?:[^\w%]|_)+")

# Markers that assert the value has been multiplied by a hundred already.
# "percentage point" is covered by "percent" as a substring; it is listed
# anyway so the family's membership can be read off this tuple without
# working out substring overlaps. Percentage points are guarded because the
# same confusion applies to a difference between two percentages, and a
# genuine sub-one-point difference can take the escape hatch. "pp" is
# matched on equality only, so it cannot fire inside another word.
_PERCENT_MARKERS = ("percent", "per cent", "pct", "%", "percentage point")
_PERCENT_EXACT = frozenset({"pp"})
# Markers that assert the value lies within [0, 1]. "ratio" is deliberately
# absent: a ratio above one is ordinary. "rate" is absent too: the project
# reports rates on both scales, so the unit alone does not fix the scale.
_PROPORTION_MARKERS = ("proportion", "fraction")

UnitFamily = Literal["percent", "proportion"]


class FindingsError(ValueError):
    """A findings entry violated the contract."""


def normalise_unit(unit: str) -> str:
    """Casefold and collapse whitespace and punctuation to single spaces."""
    return _UNIT_SEPARATOR_RE.sub(" ", unit.casefold()).strip()


def unit_family(unit: str) -> UnitFamily | None:
    """Which scale a unit string asserts, or None if it asserts neither.

    "percent" if the normalised unit contains "percent", "per cent", "pct",
    "%" or "percentage point", or equals "pp"; "proportion" if it contains
    "proportion" or "fraction". Anything else (counts, ratios, kappa,
    seconds) is unguarded, because the unit does not fix the scale.
    """
    normalised = normalise_unit(unit)
    if normalised in _PERCENT_EXACT or any(marker in normalised for marker in _PERCENT_MARKERS):
        return "percent"
    if any(marker in normalised for marker in _PROPORTION_MARKERS):
        return "proportion"
    return None


@dataclass(frozen=True)
class Finding:
    value: float | int | str
    unit: str
    n: int
    computed_by: str
    computed_at: str
    source_snapshot: str
    scale_exempt_reason: str | None = None
    ror_dump_version: str | None = None

    def validate(self, key: str) -> None:
        if not _KEY_RE.match(key):
            raise FindingsError(f"findings key '{key}' must be snake_case ascii")
        # bool is a subclass of int, and a finding rendering as "True" in the
        # post is a mistake every time.
        if isinstance(self.value, bool) or not isinstance(self.value, int | float | str):
            raise FindingsError(
                f"findings key '{key}' has value of type {type(self.value).__name__};"
                " findings values are numbers or strings"
            )
        # Before the scale guard, which NaN would pass: every comparison with
        # NaN is False. Before serialisation too, so the token never reaches
        # disk; record_findings refuses it a second time with allow_nan=False.
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise FindingsError(
                f"findings key '{key}' has value {self.value!r}, which is not a finite"
                " number. An uncomputable number (a zero denominator, say) is not"
                " recorded; the post says 'not measurable with the current data' instead"
            )
        # Type checks come first because a hand-edited file can hold anything,
        # and a contract violation should read as one, not as an AttributeError.
        for field in _TEXT_FIELDS:
            text = getattr(self, field)
            if not isinstance(text, str):
                raise FindingsError(f"findings key '{key}' has non-string {field}")
            if not text.strip():
                raise FindingsError(f"findings key '{key}' has empty {field}")
        if isinstance(self.n, bool) or not isinstance(self.n, int):
            raise FindingsError(f"findings key '{key}' has non-integer n")
        if self.n < 0:
            raise FindingsError(f"findings key '{key}' has negative n")
        try:
            datetime.fromisoformat(self.computed_at)
        except ValueError as exc:
            raise FindingsError(
                f"findings key '{key}' has computed_at '{self.computed_at}',"
                " which is not an ISO 8601 timestamp"
            ) from exc
        for field in _OPTIONAL_FIELDS:
            self._validate_optional_text(key, field)
        self._validate_scale(key)

    def _validate_optional_text(self, key: str, field: str) -> None:
        """An optional text field is either absent (None) or non-empty text."""
        text = getattr(self, field)
        if text is None:
            return
        if not isinstance(text, str):
            raise FindingsError(f"findings key '{key}' has non-string {field}")
        if not text.strip():
            raise FindingsError(f"findings key '{key}' has an empty {field}")

    def _validate_scale(self, key: str) -> None:
        """Refuse values that contradict their own unit (see module docstring)."""
        if self.scale_exempt_reason is not None:
            return
        if isinstance(self.value, bool) or not isinstance(self.value, int | float):
            return
        family = unit_family(self.unit)
        magnitude = abs(float(self.value))
        # Zero is excluded: it is the same number on either scale, so no
        # silent error is possible and a genuine zero per cent must record.
        if family == "percent" and 0 < magnitude <= 1:
            raise FindingsError(
                f"findings key '{key}' has unit '{self.unit}' but value {self.value} is in"
                " [0, 1], which reads as a proportion mislabelled as a percentage."
                " Multiply by a hundred, change the unit, or set scale_exempt_reason"
                " to record why this really is a sub-one percentage"
            )
        if family == "proportion" and magnitude > 1:
            raise FindingsError(
                f"findings key '{key}' has unit '{self.unit}' but value {self.value} exceeds"
                " 1, which a proportion cannot. Divide by a hundred, change the unit to a"
                " percentage, or set scale_exempt_reason"
            )


def _finding_from_entry(path: Path, key: str, entry: dict[str, Any]) -> Finding:
    missing = [field for field in _REQUIRED_FIELDS if field not in entry]
    if missing:
        raise FindingsError(f"{path}: entry '{key}' is missing {', '.join(missing)}")
    unexpected = sorted(set(entry) - set(_REQUIRED_FIELDS) - set(_OPTIONAL_FIELDS))
    if unexpected:
        raise FindingsError(f"{path}: entry '{key}' has unexpected fields {', '.join(unexpected)}")
    finding = Finding(**entry)
    finding.validate(key)
    return finding


def load_findings(path: Path) -> dict[str, Finding]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise FindingsError(f"{path}: top level must be an object")
    out: dict[str, Finding] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            raise FindingsError(f"{path}: entry '{key}' must be an object")
        out[key] = _finding_from_entry(path, key, entry)
    return out


def record_findings(path: Path, entries: dict[str, Finding]) -> None:
    """Merge new entries into findings.json, replacing same-key entries.

    Serialises with ``allow_nan=False`` as a second line of defence behind
    :meth:`Finding.validate`: the tokens NaN and Infinity are not JSON, and a
    findings file a strict reader cannot parse is not a published artefact.
    """
    for key, finding in entries.items():
        finding.validate(key)
    existing = load_findings(path)
    existing.update(entries)
    payload = {key: asdict(existing[key]) for key in sorted(existing)}
    serialised = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialised + "\n", encoding="utf-8")
