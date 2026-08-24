"""First-pass proposer against a local llama-server (OpenAI-compatible API).

The LLM proposes candidate RORs with a confidence and a short justification,
drawing only from the candidate pool the ROR API returned. It never invents
ROR IDs (proposals outside the pool are dropped and logged) and it never
decides: no LLM-only label enters the gold standard.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import structlog

from disambig.models import Candidate, LlmAssessment

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """\
You match raw scholarly affiliation strings to ROR organisation records.
You are given one affiliation string, optional publication context, and a
numbered candidate pool from the ROR API. Respond with JSON only, no prose:

{"assessment": "single" | "multiple" | "none" | "ambiguous",
 "proposals": [{"ror_id": "<id from the pool>",
                "confidence": <0.0-1.0>,
                "justification": "<one short sentence>"}]}

Rules: only use ror_id values from the candidate pool. "multiple" means the
string genuinely lists more than one institution; propose each. "none" means
no pool candidate is plausible. "ambiguous" means the evidence cannot decide
between candidates; propose the contenders with your reasoning.
"""


class LlmError(RuntimeError):
    """The llama-server call or its response parsing failed."""


def build_user_prompt(
    raw_affiliation: str, context_lines: list[str], pool: list[Candidate]
) -> str:
    lines = [f"Affiliation string: {raw_affiliation}"]
    if context_lines:
        lines.append("Publication context:")
        lines.extend(f"  {line}" for line in context_lines)
    lines.append("Candidate pool:")
    for index, candidate in enumerate(pool, start=1):
        alias_note = f" (aliases: {', '.join(candidate.aliases[:4])})" if candidate.aliases else ""
        place = ", ".join(part for part in (candidate.city, candidate.country) if part)
        lines.append(
            f"  {index}. {candidate.ror_id} {candidate.name}{alias_note}"
            f" [{place or 'location unknown'}; {', '.join(candidate.org_types) or 'type unknown'}]"
        )
    return "\n".join(lines)


def extract_json_object(text: str) -> dict[str, Any]:
    """Parse the model reply, tolerating code fences and surrounding prose."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    body = fenced.group(1) if fenced else text
    start = body.find("{")
    end = body.rfind("}")
    if start == -1 or end <= start:
        raise LlmError(f"No JSON object in LLM reply: {text!r:.200}")
    try:
        parsed = json.loads(body[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LlmError(f"Malformed JSON in LLM reply: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LlmError("LLM reply JSON is not an object")
    return parsed


def parse_reply(
    parsed: dict[str, Any], pool: list[Candidate]
) -> tuple[LlmAssessment, list[Candidate]]:
    """Apply proposals to the pool. Unknown ROR IDs are dropped and logged."""
    raw_assessment = parsed.get("assessment")
    if not isinstance(raw_assessment, str):
        raise LlmError(f"Unknown assessment value: {raw_assessment!r}")
    try:
        assessment = LlmAssessment(raw_assessment)
    except ValueError as exc:
        raise LlmError(f"Unknown assessment value: {raw_assessment!r}") from exc

    by_id = {candidate.ror_id: candidate for candidate in pool}
    proposals = parsed.get("proposals")
    if not isinstance(proposals, list):
        raise LlmError("LLM reply has no proposals list")
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        ror_id = proposal.get("ror_id")
        candidate = by_id.get(ror_id) if isinstance(ror_id, str) else None
        if candidate is None:
            log.warning("llm_proposed_ror_outside_pool", ror_id=ror_id)
            continue
        confidence = proposal.get("confidence")
        candidate.llm_proposed = True
        candidate.llm_confidence = (
            min(max(float(confidence), 0.0), 1.0)
            if isinstance(confidence, int | float)
            else None
        )
        justification = proposal.get("justification")
        candidate.llm_justification = (
            justification if isinstance(justification, str) and justification.strip() else None
        )
    return assessment, pool


class LlamaClient:
    def __init__(self, base_url: str, timeout_s: float = 120.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout_s)

    def propose(
        self, raw_affiliation: str, context_lines: list[str], pool: list[Candidate]
    ) -> tuple[LlmAssessment, list[Candidate], str]:
        """Returns (assessment, pool annotated in place, model name)."""
        user_prompt = build_user_prompt(raw_affiliation, context_lines, pool)
        request = {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
        }
        try:
            response = self._client.post(f"{self._base_url}/v1/chat/completions", json=request)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LlmError(f"llama-server request failed: {exc}") from exc
        payload = response.json()
        try:
            content = payload["choices"][0]["message"]["content"]
            model = str(payload.get("model", "unknown"))
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError(f"Unexpected llama-server response shape: {exc}") from exc
        assessment, annotated = parse_reply(extract_json_object(content), pool)
        return assessment, annotated, model

    def close(self) -> None:
        self._client.close()
