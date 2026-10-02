"""Remove personal email addresses from affiliation strings before publication.

Publisher metadata routinely appends "Electronic address: name@example.org"
to the corresponding author's affiliation, and that string is the raw input
this benchmark publishes. The address is personal data of a third party and
carries no disambiguation signal, so its local part is replaced. The domain
is kept: "@kuleuven.be" is an institutional cue that every evaluated source
also saw, and removing it would change the task.

One rule, applied identically to every item before labelling; the raw
snapshot on disk retains the original string. Obfuscated forms ("name at
domain dot org") are not detected and are not claimed to be.
"""

from __future__ import annotations

import re

REDACTED_LOCAL_PART = "redacted"

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")


def redact_emails(text: str) -> tuple[str, int]:
    """Return the text with each email's local part replaced, and the count."""
    redacted, count = _EMAIL.subn(lambda m: f"{REDACTED_LOCAL_PART}@{m.group(1)}", text)
    return redacted, count


def redact_many(texts: list[str]) -> tuple[list[str], int]:
    total = 0
    out: list[str] = []
    for text in texts:
        redacted, count = redact_emails(text)
        out.append(redacted)
        total += count
    return out, total
