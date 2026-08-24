"""Heuristic signals used to route sampled affiliation strings into strata.

These heuristics decide only which stratum a string is *sampled into*; they
never touch scoring or evaluation. Misrouted strings are corrected at
adjudication time (the review UI records the stratum with the item, and the
frozen gold standard carries the verified stratum).
"""

from __future__ import annotations

import re
import unicodedata

_SCRIPT_RANGES: list[tuple[str, tuple[tuple[int, int], ...]]] = [
    ("chinese", ((0x4E00, 0x9FFF), (0x3400, 0x4DBF))),
    ("japanese", ((0x3040, 0x309F), (0x30A0, 0x30FF))),  # kana; kanji counts as chinese
    ("korean", ((0xAC00, 0xD7AF), (0x1100, 0x11FF))),
    ("russian_cyrillic", ((0x0400, 0x04FF),)),
    ("arabic", ((0x0600, 0x06FF), (0x0750, 0x077F))),
    ("thai", ((0x0E00, 0x0E7F),)),
]

# CJK tokens sit outside the \b group: word boundaries never fire between
# CJK characters, so 病院 inside 附属病院 would otherwise never match.
_HOSPITAL_TOKENS = re.compile(
    r"\b(hospital|hospitals|clinic|clinique|klinik|klinikum|infirmary|medical cent(?:er|re)|"
    r"health system|nhs trust|hôpital|hopital|ospedale|sjukhus|ziekenhuis)\b"
    r"|병원|病院|医院",
    re.IGNORECASE,
)
_GOVERNMENT_TOKENS = re.compile(
    r"\b(national (?:institute|laborator|research)|ministry|research council|cnrs|csic|"
    r"max.planck|helmholtz|fraunhofer|academy of sciences|national academy|"
    r"government|federal (?:institute|agency))\b|中国科学院",
    re.IGNORECASE,
)
_COMPANY_TOKENS = re.compile(
    r"\b(inc\.?|ltd\.?|llc|gmbh|s\.?a\.?s\.?|b\.?v\.?|pty|corp\.?|corporation|"
    r"pharmaceuticals?|biotech|technologies|co\.,? ltd)\b",
    re.IGNORECASE,
)


def dominant_non_latin_script(text: str) -> str | None:
    """Name of the dominant non-Latin script, if non-Latin letters are the
    majority of the string's letters."""
    letters = [char for char in text if unicodedata.category(char).startswith("L")]
    if not letters:
        return None
    counts: dict[str, int] = {}
    latin = 0
    for char in letters:
        code = ord(char)
        matched = False
        for script, ranges in _SCRIPT_RANGES:
            if any(low <= code <= high for low, high in ranges):
                counts[script] = counts.get(script, 0) + 1
                matched = True
                break
        if not matched and code < 0x0250:
            latin += 1
    if not counts:
        return None
    # Japanese text is mostly kanji (which the ranges count as chinese) with
    # some kana; any kana alongside CJK ideographs means Japanese.
    if counts.get("japanese") and counts.get("chinese"):
        counts["japanese"] += counts.pop("chinese")
    script, _ = max(counts.items(), key=lambda pair: pair[1])
    total_non_latin = sum(counts.values())
    return script if total_non_latin > latin else None


def looks_multi_affiliation(text: str) -> bool:
    """More than one institution in a single field, detected via repeated
    top-level institution tokens or explicit numbering."""
    if re.match(r"^\s*1[\.\)]", text) and re.search(r"[;\n]\s*2[\.\)]", text):
        return True
    institution_tokens = re.findall(
        r"\b(university|université|universität|universidad|universidade|college|"
        r"institute|instituto|institut|hospital|academy)\b",
        text,
        re.IGNORECASE,
    )
    separators = len(re.findall(r";", text))
    return len(institution_tokens) >= 2 and separators >= 1


def token_signals(text: str) -> dict[str, bool]:
    return {
        "hospital": bool(_HOSPITAL_TOKENS.search(text)),
        "government": bool(_GOVERNMENT_TOKENS.search(text)),
        "company": bool(_COMPANY_TOKENS.search(text)),
        "multi": looks_multi_affiliation(text),
    }
