"""ROR v2 API client: candidate pool for labelling, canonical org records.

Defensive parsing throughout: scholarly APIs return nulls where lists are
expected, missing fields, and mixed scripts. A record that cannot be parsed
at all is an explicit error, never a silently dropped candidate.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import structlog

from disambig.httpcache import CachingClient
from disambig.models import Candidate, Relationship

log = structlog.get_logger(__name__)

ROR_API_BASE = "https://api.ror.org/v2"


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def parse_ror_record(record: dict[str, Any], match_score: float | None = None) -> Candidate:
    """Convert a ROR v2 organization record into a Candidate."""
    ror_id = _as_str(record.get("id"))
    if ror_id is None:
        raise ValueError(f"ROR record without an id: {record!r:.200}")

    display_name: str | None = None
    aliases: list[str] = []
    acronyms: list[str] = []
    for name in _as_list(record.get("names")):
        if not isinstance(name, dict):
            continue
        value = _as_str(name.get("value"))
        if value is None:
            continue
        types = {t for t in _as_list(name.get("types")) if isinstance(t, str)}
        if "ror_display" in types:
            display_name = value
        elif "acronym" in types:
            acronyms.append(value)
        else:
            aliases.append(value)
    if display_name is None:
        # Malformed but salvageable: fall back to any name rather than dropping
        # the candidate, and log it so the snapshot issue is visible.
        fallback = aliases + acronyms
        if not fallback:
            raise ValueError(f"ROR record {ror_id} has no usable name")
        display_name = fallback[0]
        log.warning("ror_record_missing_display_name", ror_id=ror_id, used=display_name)

    city: str | None = None
    country: str | None = None
    locations = _as_list(record.get("locations"))
    if locations and isinstance(locations[0], dict):
        details = locations[0].get("geonames_details")
        if isinstance(details, dict):
            city = _as_str(details.get("name"))
            country = _as_str(details.get("country_name")) or _as_str(
                details.get("country_code")
            )

    relationships = [
        Relationship(rel_type=rel_type, label=label, ror_id=rel_id)
        for rel in _as_list(record.get("relationships"))
        if isinstance(rel, dict)
        and (rel_type := _as_str(rel.get("type"))) is not None
        and (label := _as_str(rel.get("label"))) is not None
        and (rel_id := _as_str(rel.get("id"))) is not None
    ]

    return Candidate(
        ror_id=ror_id,
        name=display_name,
        aliases=aliases,
        acronyms=acronyms,
        city=city,
        country=country,
        org_types=[t for t in _as_list(record.get("types")) if isinstance(t, str)],
        relationships=relationships,
        match_score=match_score,
    )


class RorClient:
    def __init__(self, http: CachingClient) -> None:
        self._http = http

    def match_affiliation(self, affiliation: str, refresh: bool = False) -> list[Candidate]:
        """Candidate pool via the ROR affiliation matcher."""
        url = f"{ROR_API_BASE}/organizations?affiliation={quote(affiliation)}"
        response = self._http.get(url, refresh=refresh)
        if response.status != 200:
            raise RuntimeError(f"ROR affiliation match failed ({response.status}) for {url}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Unexpected ROR payload type for {url}")
        candidates: list[Candidate] = []
        for entry in _as_list(payload.get("items")):
            if not isinstance(entry, dict):
                continue
            organization = entry.get("organization")
            if not isinstance(organization, dict):
                log.warning("ror_match_item_without_organization", url=url)
                continue
            score = entry.get("score")
            match_score = float(score) if isinstance(score, int | float) else None
            candidates.append(parse_ror_record(organization, match_score=match_score))
        return candidates

    def search(self, query: str, refresh: bool = False) -> list[Candidate]:
        """Quick-search fallback used by the review UI's correction flow."""
        url = f"{ROR_API_BASE}/organizations?query={quote(query)}"
        response = self._http.get(url, refresh=refresh)
        if response.status != 200:
            raise RuntimeError(f"ROR search failed ({response.status}) for {url}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Unexpected ROR payload type for {url}")
        return [
            parse_ror_record(record)
            for record in _as_list(payload.get("items"))
            if isinstance(record, dict)
        ]
