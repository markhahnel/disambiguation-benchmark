"""Render the post drafts by substituting {{f.key}} placeholders from findings.json.

Enforces the findings contract (CLAUDE.md rule 0) structurally:

- A placeholder with no matching findings key fails the render with a
  non-zero exit naming the key. A findings key that no rendered draft
  references produces a warning.
- Anything that was plainly meant to be a placeholder and is not one
  ({{ f.key }}, {f.key}, {{{f.key}}}, {{key}}) fails as malformed, because a
  mistyped placeholder would otherwise ship a pair of braces to subscribers.
- Every digit typed into the draft fails the render. The check runs on the
  draft after removing the spans that legitimately carry digits without
  making a claim: placeholders, fenced code blocks, inline code spans,
  markdown link targets, autolinks in angle brackets, bare http(s) URLs,
  DOIs and the tokens in ALLOWED_TOKENS. Whatever digit is left is a number
  that no analysis script computed, which is the single most likely way an
  unsourced number reaches the post.
- The one escape is {{lit:TEXT}}, which renders as TEXT and is recorded in
  the claims register as a literal row explicitly marked as not a finding.
  Years, version strings and the like are allowed that way, and every one
  of them is visible in the published register.
- An empty draft fails rather than rendering nothing, and after
  substitution the rendered text may contain no "{{" at all: a survivor
  fails naming its line and column, which also catches a findings value that
  itself contains a placeholder.

The benchmark publishes as a two-part series (part one institutional
disambiguation, part two author disambiguation) from one shared
findings.json, so each post/draft*.md renders to its own
post/rendered<part>.md and post/claims<part>.md. The unreferenced-key
warning is computed across every draft rendered in the run: computed per
draft, every part-two key would warn while rendering part one.

Values are substituted exactly as recorded. Rounding belongs in the analysis
script that computed the finding, so the published number and the findings
entry can never disagree.

Diagnostics go through structlog under a run ID like every other run in this
repo, so the run that produced a given claims register can be found in logs/
afterwards. The console copy of each event lands on stderr.

Usage:
  uv run scripts/render_post.py
  uv run scripts/render_post.py --draft post/draft-part1.md
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import structlog

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from disambig.findings import Finding, load_findings  # noqa: E402
from disambig.logging_setup import configure_logging, new_run_id  # noqa: E402

log = structlog.get_logger(__name__)

PLACEHOLDER_RE = re.compile(r"\{\{f\.([a-z0-9_]+)\}\}")
# The literal escape: TEXT may not contain braces or a newline, and may not
# start or end with whitespace, so what renders is exactly what was marked.
LITERAL_RE = re.compile(r"\{\{lit:([^\s{}](?:[^{}\n]*[^\s{}])?)\}\}")
# Both substituted in one pass so their offsets in the rendered text, and
# therefore their order in the claims register, come out right.
_TOKEN_RE = re.compile(
    r"\{\{f\.(?P<key>[a-z0-9_]+)\}\}|\{\{lit:(?P<lit>[^\s{}](?:[^{}\n]*[^\s{}])?)\}\}"
)
# Anything that was plainly meant to be a placeholder: a run of braces
# around an f.key reference (which catches a dropped brace and a brace too
# many, since the span must then fail to be a well-formed placeholder), plus
# any other double-brace span. Whatever this matches and neither
# PLACEHOLDER_RE nor LITERAL_RE fully matches is a typo, and a typo has to
# fail the render instead of shipping a brace to subscribers.
ATTEMPT_RE = re.compile(r"\{+\s*f\.[a-z0-9_]+\s*\}+|\{\{[^{}]*\}\}")
_PARAGRAPH_BREAK_RE = re.compile(r"\n[ \t]*\n")
# A sentence ends at terminal punctuation followed by whitespace, which no
# decimal number does ("61.4" has no space after the point).
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s")
_MARKDOWN_LEAD_RE = re.compile(r"^(?:#{1,6}\s+|[-*+]\s+|\d+\.\s+|>\s*)+")
CLAIM_LIMIT = 240

# Tokens the digit scan skips. Deliberately tiny: each entry is a word that
# carries a digit without being a number, and the list is logged on every
# render so nobody can forget it exists. "F1" is the metric name.
ALLOWED_TOKENS: frozenset[str] = frozenset({"F1"})

# Spans removed from the draft before the digit scan, in the order applied.
# Fenced blocks go first because they can contain anything, including
# backticks; the closing fence must match the opening one.
_FENCED_CODE_RE = re.compile(
    r"^(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^(?P=fence)[ \t]*$", re.MULTILINE | re.DOTALL
)
_INLINE_CODE_RE = re.compile(r"(?P<ticks>`+)(.+?)(?P=ticks)(?!`)")
_AUTOLINK_RE = re.compile(r"<[A-Za-z][A-Za-z0-9+.-]*:[^\s<>]*>")
# The parenthesised target of [text](target), allowing one level of nested
# parentheses inside the URL and an optional quoted title.
_LINK_TARGET_RE = re.compile(r"\]\((?:[^()\n]|\([^()\n]*\))*\)")
_BARE_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s<>\"']+")
_DIGIT_TOKEN_RE = re.compile(r"\S*\d\S*")

LITERAL_KEY_CELL = "literal, not a finding"
NOT_APPLICABLE = "n/a"


@dataclass(frozen=True)
class Substitution:
    """One published claim: a value and the sentence carrying it.

    ``key`` is the findings key, or None for a {{lit:TEXT}} literal, which
    is not a finding and is marked as such in the claims register.
    """

    key: str | None
    value: str
    claim: str

    @property
    def is_literal(self) -> bool:
        return self.key is None


@dataclass(frozen=True)
class Position:
    """A line and column (both 1-based) and the text found there."""

    line: int
    column: int
    text: str


@dataclass(frozen=True)
class RenderResult:
    text: str
    substitutions: tuple[Substitution, ...]  # findings and literals, in document order
    keys_used: tuple[str, ...]  # unique, in order of first use
    missing: tuple[str, ...]
    malformed: tuple[str, ...]
    literal_digits: tuple[Position, ...]  # digits typed into the draft
    survivors: tuple[Position, ...]  # "{{" still present after substitution

    @property
    def literals(self) -> tuple[Substitution, ...]:
        return tuple(s for s in self.substitutions if s.is_literal)

    @property
    def finding_substitutions(self) -> tuple[Substitution, ...]:
        return tuple(s for s in self.substitutions if not s.is_literal)

    @property
    def failed(self) -> bool:
        return bool(self.missing or self.malformed or self.literal_digits or self.survivors)


def claim_text(text: str, offset: int, limit: int = CLAIM_LIMIT) -> str:
    """The sentence around ``offset``, flattened to sit in a table cell.

    Bounded by the enclosing paragraph first, then by sentence punctuation,
    because drafts are hard-wrapped: cutting at every newline would truncate
    claims mid-sentence, and not bounding by paragraph would run a heading
    into the prose beneath it.
    """
    paragraph_start = 0
    for match in _PARAGRAPH_BREAK_RE.finditer(text, 0, offset):
        paragraph_start = match.end()
    forward_break = _PARAGRAPH_BREAK_RE.search(text, offset)
    paragraph_end = forward_break.start() if forward_break else len(text)

    start = paragraph_start
    for match in _SENTENCE_END_RE.finditer(text, paragraph_start, offset):
        start = match.end()
    sentence_end = _SENTENCE_END_RE.search(text, offset, paragraph_end)
    end = sentence_end.start() if sentence_end else paragraph_end

    sentence = _MARKDOWN_LEAD_RE.sub("", text[start:end].strip())
    sentence = " ".join(sentence.split())
    if len(sentence) > limit:
        sentence = sentence[: limit - 3].rstrip() + "..."
    return sentence


def position_of(text: str, offset: int, found: str) -> Position:
    """1-based line and column of ``offset`` in ``text``."""
    line_start = text.rfind("\n", 0, offset) + 1
    return Position(
        line=text.count("\n", 0, offset) + 1, column=offset - line_start + 1, text=found
    )


def _blank(match: re.Match[str]) -> str:
    """Replace a span with spaces, keeping newlines so positions survive."""
    return "".join("\n" if char == "\n" else " " for char in match.group(0))


def _allowed_token_patterns(tokens: Iterable[str]) -> list[re.Pattern[str]]:
    # Whole-token matches only: "F1" must not exempt the "1" in "F10".
    return [re.compile(rf"(?<!\w){re.escape(token)}(?!\w)") for token in sorted(tokens)]


def literal_digits(
    draft: str, allowed_tokens: frozenset[str] = ALLOWED_TOKENS
) -> tuple[Position, ...]:
    """Every digit typed into the draft outside the spans that may carry one.

    Returns the whitespace-delimited token holding each digit, with its line
    and column in the draft, so the error names what to replace with a
    {{f.key}} placeholder or, where it genuinely is not a number, a
    {{lit:TEXT}} marker.
    """
    scanned = draft
    for pattern in (
        _FENCED_CODE_RE,
        _INLINE_CODE_RE,
        ATTEMPT_RE,  # well-formed placeholders and literals, and the malformed attempts
        _AUTOLINK_RE,
        _LINK_TARGET_RE,
        _BARE_URL_RE,
        _DOI_RE,
        *_allowed_token_patterns(allowed_tokens),
    ):
        scanned = pattern.sub(_blank, scanned)
    return tuple(
        position_of(scanned, match.start(), match.group(0))
        for match in _DIGIT_TOKEN_RE.finditer(scanned)
    )


def malformed_placeholders(draft: str) -> tuple[str, ...]:
    """Spans that were meant as placeholders but are not well formed."""
    return tuple(
        span
        for span in ATTEMPT_RE.findall(draft)
        if PLACEHOLDER_RE.fullmatch(span) is None and LITERAL_RE.fullmatch(span) is None
    )


def brace_survivors(text: str, explained: Iterable[str]) -> tuple[Position, ...]:
    """Every "{{" in the rendered text not already reported some other way.

    ``explained`` holds the spans that other checks have already failed on
    (missing placeholders left in place, malformed attempts), so one typo
    is reported once. Anything else that opens a double brace, including a
    placeholder arriving inside a findings value, is a survivor.
    """
    known = tuple(explained)
    survivors: list[Position] = []
    offset = text.find("{{")
    while offset != -1:
        if not any(text.startswith(span, offset) for span in known):
            end_of_line = text.find("\n", offset)
            snippet = text[offset : end_of_line if end_of_line != -1 else len(text)]
            survivors.append(position_of(text, offset, snippet[:60]))
        offset = text.find("{{", offset + 2)
    return tuple(survivors)


def render(draft: str, findings: dict[str, Finding]) -> RenderResult:
    """Substitute every {{f.key}} and {{lit:TEXT}}, recording what was substituted."""
    used: list[str] = []
    missing: list[str] = []
    placements: list[tuple[str | None, str, int]] = []  # key, value, offset in rendered text
    delta = 0

    def substitute(match: re.Match[str]) -> str:
        nonlocal delta
        original = match.group(0)
        literal = match.group("lit")
        if literal is not None:
            replacement = literal
            placements.append((None, replacement, match.start() + delta))
        else:
            key = match.group("key")
            if key not in findings:
                if key not in missing:
                    missing.append(key)
                return original  # left in place so the failure is visible in the diff
            replacement = str(findings[key].value)
            placements.append((key, replacement, match.start() + delta))
            if key not in used:
                used.append(key)
        delta += len(replacement) - len(original)
        return replacement

    malformed = malformed_placeholders(draft)
    digits = literal_digits(draft)
    text = _TOKEN_RE.sub(substitute, draft)
    explained = [*malformed, *(f"{{{{f.{key}}}}}" for key in missing)]
    substitutions = tuple(
        Substitution(key=key, value=value, claim=claim_text(text, offset))
        for key, value, offset in placements
    )
    return RenderResult(
        text=text,
        substitutions=substitutions,
        keys_used=tuple(used),
        missing=tuple(missing),
        malformed=malformed,
        literal_digits=digits,
        survivors=brace_survivors(text, explained),
    )


def _cell(value: str) -> str:
    """Collapse whitespace and escape pipes so a claim cannot break the table."""
    return " ".join(value.split()).replace("|", "\\|")


def claims_table(
    result: RenderResult,
    findings: dict[str, Finding],
    draft_name: str,
    findings_name: str,
) -> str:
    """The generated claims register: one row per substituted value.

    Literal rows carry the same columns with the key cell reading
    "literal, not a finding", n/a in every column a finding would fill, and
    "not computed; typed in <draft>" in place of a script, so a reader
    scanning the register cannot mistake one for a computed value.
    """
    lines = [
        f"# Claims register: {draft_name}",
        "",
        f"Generated by scripts/render_post.py from {findings_name}. One row per",
        f"findings value substituted into {draft_name}, in the order the claims",
        "appear. Do not edit by hand.",
        "",
        "| Claim | Findings key | Value | Unit | n | Computed by | Snapshot |",
        "|---|---|---|---|---|---|---|",
    ]
    for substitution in result.substitutions:
        if substitution.key is None:
            lines.append(
                f"| {_cell(substitution.claim)} | {LITERAL_KEY_CELL} |"
                f" {_cell(substitution.value)} | {NOT_APPLICABLE} | {NOT_APPLICABLE} |"
                f" not computed; typed in {_cell(draft_name)} | {NOT_APPLICABLE} |"
            )
            continue
        finding = findings[substitution.key]
        lines.append(
            f"| {_cell(substitution.claim)} | `{substitution.key}` |"
            f" {_cell(substitution.value)} | {_cell(finding.unit)} | {finding.n} |"
            f" `{_cell(finding.computed_by)}` | `{finding.source_snapshot[:12]}` |"
        )
    if result.literals:
        lines += [
            "",
            f"Rows marked '{LITERAL_KEY_CELL}' were typed into the draft through a",
            "{{lit:TEXT}} marker and were not computed by any script: years, version",
            "strings and the like. Each one is listed so it can be checked by eye.",
        ]
    releases = sorted(
        {
            str(findings[key].ror_dump_version)
            for key in result.keys_used
            if findings[key].ror_dump_version is not None
        }
    )
    without_release = [
        key for key in result.keys_used if findings[key].ror_dump_version is None
    ]
    if releases:
        lines += [
            "",
            "ROR data dump release cited by the findings above: "
            + ", ".join(f"`{release}`" for release in releases)
            + ".",
        ]
        if without_release:
            lines.append(
                "Findings with no ROR release (they do not depend on the registry): "
                + ", ".join(f"`{key}`" for key in sorted(set(without_release)))
                + "."
            )
    exempt = [
        (key, findings[key].scale_exempt_reason)
        for key in result.keys_used
        if findings[key].scale_exempt_reason is not None
    ]
    if exempt:
        lines += [
            "",
            "Scale guard exemptions (see src/disambig/findings.py):",
            "",
        ]
        lines += [f"- `{key}`: {_cell(str(reason))}" for key, reason in exempt]
    return "\n".join(lines) + "\n"


def output_names(draft_path: Path) -> tuple[str, str]:
    """(rendered name, claims name) for a draft: draft-part1.md renders to
    rendered-part1.md and claims-part1.md."""
    stem = draft_path.stem
    if stem != "draft" and not stem.startswith("draft-"):
        raise ValueError(
            f"draft filename must be draft.md or draft-<part>.md, got '{draft_path.name}'"
        )
    part = stem[len("draft") :]
    return f"rendered{part}.md", f"claims{part}.md"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--findings", type=Path, default=PROJECT_ROOT / "outputs" / "findings.json")
    parser.add_argument(
        "--draft",
        type=Path,
        action="append",
        help="draft to render; repeatable. Default: every post/draft*.md",
    )
    parser.add_argument("--post-dir", type=Path, default=PROJECT_ROOT / "post")
    parser.add_argument("--logs-dir", type=Path, default=PROJECT_ROOT / "logs")
    args = parser.parse_args(argv)

    run_id = new_run_id()
    configure_logging(args.logs_dir, run_id)

    post_dir: Path = args.post_dir
    drafts: list[Path] = sorted(args.draft) if args.draft else sorted(post_dir.glob("draft*.md"))
    if not drafts:
        log.error("no_drafts", post_dir=str(post_dir), expected="draft.md or draft-part1.md")
        raise SystemExit(1)

    findings = load_findings(args.findings)
    allowed = sorted(ALLOWED_TOKENS)
    log.info(
        "render_start",
        findings=str(args.findings),
        findings_keys=len(findings),
        drafts=[draft.name for draft in drafts],
    )
    # Logged on every render, so the exemption list is never forgotten.
    log.info("literal_scan_allowlist", allowed_tokens=allowed, defined_in="scripts/render_post.py")
    print(f"digit scan skips these tokens (ALLOWED_TOKENS): {', '.join(allowed)}")

    texts = {draft: draft.read_text(encoding="utf-8") for draft in drafts}
    results = {draft: render(text, findings) for draft, text in texts.items()}

    # Validate every draft before writing any output, so a broken part two
    # cannot leave a stale rendered part one behind.
    failed = False
    for draft, result in results.items():
        if not texts[draft].strip():
            log.error("draft_empty", draft=draft.name, reason="nothing to render is a failure")
            failed = True
        for key in result.missing:
            log.error(
                "placeholder_missing",
                draft=draft.name,
                key=key,
                placeholder=f"{{{{f.{key}}}}}",
                findings=str(args.findings),
            )
            failed = True
        for span in result.malformed:
            log.error(
                "placeholder_malformed",
                draft=draft.name,
                span=span,
                expected="{{f.snake_case_key}} or {{lit:TEXT}}",
            )
            failed = True
        for digit in result.literal_digits:
            log.error(
                "literal_digit",
                draft=draft.name,
                line=digit.line,
                column=digit.column,
                text=digit.text,
                fix="use a {{f.key}} placeholder, or {{lit:TEXT}} if it is not a number",
            )
            failed = True
        for survivor in result.survivors:
            log.error(
                "brace_survived_substitution",
                draft=draft.name,
                line=survivor.line,
                column=survivor.column,
                text=survivor.text,
            )
            failed = True
    if failed:
        log.error("render_aborted", reason="an uncomputed number does not get published")
        raise SystemExit(1)

    draft_names = [draft.name for draft in drafts]
    referenced = {key for result in results.values() for key in result.keys_used}
    for key in sorted(set(findings) - referenced):
        log.warning("finding_unreferenced", key=key, drafts=draft_names)
    for draft, result in results.items():
        if not result.keys_used:
            log.warning("draft_has_no_findings", draft=draft.name)

    post_dir.mkdir(parents=True, exist_ok=True)
    for draft, result in results.items():
        rendered_name, claims_name = output_names(draft)
        (post_dir / rendered_name).write_text(result.text, encoding="utf-8")
        (post_dir / claims_name).write_text(
            claims_table(result, findings, draft.name, args.findings.name), encoding="utf-8"
        )
        count = len(result.finding_substitutions)
        literal_count = len(result.literals)
        log.info(
            "draft_rendered",
            draft=draft.name,
            rendered=rendered_name,
            claims=claims_name,
            substitutions=count,
            distinct_keys=len(result.keys_used),
            literals=literal_count,
        )
        print(
            f"{draft.name}: {count} value{'' if count == 1 else 's'} substituted"
            f" ({len(result.keys_used)} distinct),"
            f" {literal_count} literal{'' if literal_count == 1 else 's'}"
            f" -> {rendered_name}, {claims_name}"
        )


if __name__ == "__main__":
    main()
