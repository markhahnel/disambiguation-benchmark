"""LLM reply parsing: local models wrap JSON in fences and prose, propose IDs
outside the pool, and return junk confidences. All handled, none silently."""

import pytest

from disambig.llm import LlmError, build_user_prompt, extract_json_object, parse_reply
from disambig.models import Candidate, LlmAssessment


def pool() -> list[Candidate]:
    return [
        Candidate(ror_id="https://ror.org/052gg0110", name="University of Oxford"),
        Candidate(ror_id="https://ror.org/05cvf7v30", name="Institute of Physics"),
    ]


def test_extracts_plain_json() -> None:
    parsed = extract_json_object('{"assessment": "single", "proposals": []}')
    assert parsed["assessment"] == "single"


def test_extracts_fenced_json_with_prose() -> None:
    reply = 'Sure! Here is my answer:\n```json\n{"assessment": "none", "proposals": []}\n```\nDone.'
    assert extract_json_object(reply)["assessment"] == "none"


def test_no_json_raises() -> None:
    with pytest.raises(LlmError, match="No JSON object"):
        extract_json_object("I could not decide.")


def test_malformed_json_raises() -> None:
    with pytest.raises(LlmError, match="Malformed JSON"):
        extract_json_object('{"assessment": "single", "proposals": [}')


def test_proposal_annotates_pool_candidate() -> None:
    assessment, annotated = parse_reply(
        {
            "assessment": "single",
            "proposals": [
                {
                    "ror_id": "https://ror.org/052gg0110",
                    "confidence": 0.9,
                    "justification": "Exact name match, Oxford UK.",
                }
            ],
        },
        pool(),
    )
    assert assessment is LlmAssessment.SINGLE
    oxford = annotated[0]
    assert oxford.llm_proposed and oxford.llm_confidence == 0.9
    assert annotated[1].llm_proposed is False


def test_out_of_pool_ror_id_is_dropped() -> None:
    _, annotated = parse_reply(
        {
            "assessment": "single",
            "proposals": [{"ror_id": "https://ror.org/0invented0", "confidence": 1.0}],
        },
        pool(),
    )
    assert not any(candidate.llm_proposed for candidate in annotated)


def test_confidence_clamped_and_junk_tolerated() -> None:
    _, annotated = parse_reply(
        {
            "assessment": "multiple",
            "proposals": [
                {"ror_id": "https://ror.org/052gg0110", "confidence": 1.7},
                {"ror_id": "https://ror.org/05cvf7v30", "confidence": "high"},
            ],
        },
        pool(),
    )
    assert annotated[0].llm_confidence == 1.0
    assert annotated[1].llm_proposed and annotated[1].llm_confidence is None


def test_unknown_assessment_raises() -> None:
    with pytest.raises(LlmError, match="Unknown assessment"):
        parse_reply({"assessment": "maybe", "proposals": []}, pool())


def test_missing_proposals_raises() -> None:
    with pytest.raises(LlmError, match="no proposals list"):
        parse_reply({"assessment": "single"}, pool())


def test_prompt_numbers_pool_and_includes_context() -> None:
    prompt = build_user_prompt(
        "Dept. of Physics, Oxford", ["title · venue · 2024"], pool()
    )
    assert "1. https://ror.org/052gg0110" in prompt
    assert "2. https://ror.org/05cvf7v30" in prompt
    assert "title · venue · 2024" in prompt
