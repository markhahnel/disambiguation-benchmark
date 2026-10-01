"""One canonical form for ROR identifiers, applied to every side of every comparison.

Lives in its own module so the dump loader (relationship targets), the gold
standard (annotator selections) and the matcher (source assignments) all
normalise through the same function. If any one of them skipped it, an id
would silently fail to match on formatting alone, which is not a
disambiguation error and must never be scored as one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

ROR_ID_PREFIX = "https://ror.org/"
# ROR ids are a leading zero, six Crockford base32 characters (no i, l, o or
# u), then a two-digit checksum. The checksum is not verified here: a malformed
# id is a pipeline bug and is rejected on shape, a well-formed id that ROR
# never issued is reported as NOT_IN_DUMP by the matcher.
_ROR_ID_BODY = re.compile(r"^0[a-hj-km-np-tv-z0-9]{6}[0-9]{2}$")
_ROR_ID_PREFIXES = ("https://ror.org/", "http://ror.org/", "ror.org/")


def normalise_ror_id(value: str) -> str:
    """Canonical `https://ror.org/<id>` form, applied identically to every input.

    Sources differ in whether they emit the full URL or the bare id, and that
    difference is formatting, not disambiguation. Anything not shaped like a
    ROR id is a pipeline bug upstream and is rejected rather than scored.
    """
    if not isinstance(value, str):
        raise ValueError(f"ROR id must be a string, got {type(value).__name__}")
    body = value.strip()
    for prefix in _ROR_ID_PREFIXES:
        if body.lower().startswith(prefix):
            body = body[len(prefix) :]
            break
    body = body.lower()
    if not _ROR_ID_BODY.match(body):
        raise ValueError(f"{value!r} is not shaped like a ROR id")
    return ROR_ID_PREFIX + body


def normalise_ror_ids(values: Iterable[str]) -> frozenset[str]:
    return frozenset(normalise_ror_id(value) for value in values)
