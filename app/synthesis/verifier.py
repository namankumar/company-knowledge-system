"""
Deterministic verification: every cited source ref must exist in the context
pack that produced the output. No LLM involved — this is the hard gate behind
"0 hallucinated source refs".
"""
import structlog

logger = structlog.get_logger()
REF_RE = r"\[ref:([A-Za-z][A-Za-z0-9_]*:[A-Za-z0-9_.:-]+)\]"
# Near-miss citation shapes weaker models emit instead of the literal
# [ref:kind:id]: a bare [kind:id] (missing the ref: prefix) or a markdown link
# [label](url). normalize_citations repairs both into canonical refs when — and
# only when — the target resolves to a real pack item.
import re as _re

_BARE_REF_RE = _re.compile(r"\[([A-Za-z][A-Za-z0-9_]*:[A-Za-z0-9_.:-]+)\]")
_MD_LINK_RE = _re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
_NAKED_URL_RE = _re.compile(r"https?://[^\s,)\]]+")
_BACKTICK_RE = _re.compile(r"`([A-Za-z0-9][A-Za-z0-9_.:-]{3,})`")


def normalize_citations(text: str, pack_items: list[dict]) -> str:
    """Rewrite near-miss GLM citations into canonical [ref:kind:id].

    Four repairs, all gated on the target existing in the pack, so this can never
    manufacture a hallucinated ref:
      - markdown [label](url) -> [ref:kind:id]  when url matches a pack source_url
      - naked url             -> [ref:kind:id]  when url matches a pack source_url
      - bare [kind:id]        -> [ref:kind:id]  when kind:id is a pack ref
      - backtick `kind:id` -> [ref:kind:id] when it resolves to a pack ref

    Already-canonical [ref:...] citations are left untouched. Note: this recovers
    malformed citations weaker models emit; it cannot supply a citation the model
    omitted entirely (an uncited synthesized claim stays uncited).
    """
    if not text or not pack_items:
        return text
    valid_keys = {
        f"{i['kind']}:{i['id']}"
        for i in pack_items
        if i.get("kind") and i.get("id") is not None
    }
    url_to_key: dict[str, str] = {}
    for i in pack_items:
        if not (i.get("kind") and i.get("id") is not None):
            continue
        key = f"{i['kind']}:{i['id']}"
        url = i.get("source_url")
        if url:
            url_to_key.setdefault(url, key)

    def _md(m: "_re.Match") -> str:
        key = url_to_key.get(m.group(1))
        return f"[ref:{key}]" if key else m.group(0)

    def _naked(m: "_re.Match") -> str:
        key = url_to_key.get(m.group(0))
        return f"[ref:{key}]" if key else m.group(0)

    def _bare(m: "_re.Match") -> str:
        key = m.group(1)
        if key.startswith("ref:"):
            return m.group(0)
        return f"[ref:{key}]" if key in valid_keys else m.group(0)

    def _backtick(m: "_re.Match") -> str:
        token = m.group(1)
        if ":" in token and token in valid_keys:
            return f"[ref:{token}]"
        return m.group(0)

    def _naked_in_parens(m: "_re.Match") -> str:
        # Only treat a naked URL as a citation when it sits inside parentheses
        # (the position GLM uses). A mid-sentence prose URL is left untouched.
        return "(" + _NAKED_URL_RE.sub(_naked, m.group(1)) + ")"

    text = _MD_LINK_RE.sub(_md, text)  # must run before the naked-url pass
    text = _re.sub(r"\(([^()]*https?://[^()]*)\)", _naked_in_parens, text)
    text = _BARE_REF_RE.sub(_bare, text)
    text = _BACKTICK_RE.sub(_backtick, text)
    return text


def verify_evidence(evidence: list[dict], pack_items: list[dict]) -> dict:
    """
    evidence: [{"ref": "kind:id", "quote": "..."}]
    Returns {valid, verified: [...], flagged: [...], invalid_refs: [...],
    unmatched_quotes: [...]}.

    Hard gate: a ref that does not exist in the pack (hallucinated source)
    invalidates the output. Advisory: a quote that does not appear verbatim in
    the referenced item does not invalidate — LLMs paraphrase, and the operator
    approval gate judges quality — but it goes in `flagged`, never `verified`:
    only items whose quote actually matched may carry that name downstream.
    Every returned item is annotated with quote_verified so storage and the
    console can label it honestly.
    """
    by_ref = {f"{i['kind']}:{i['id']}": i for i in pack_items}
    verified, flagged, invalid_refs, unmatched_quotes = [], [], [], []

    for item in evidence or []:
        ref = (item.get("ref") or "").strip()
        if ref not in by_ref:
            invalid_refs.append(ref or "(empty)")
            continue
        quote = (item.get("quote") or "").strip()
        hay = _normalized_text(by_ref[ref]["text"])
        needle = _normalized_text(quote)
        source_item = by_ref[ref]
        annotated = {**item, "source_url": source_item.get("source_url")}
        for key in (
            "visibility",
            "allowed_principals",
            "acl_state",
            "acl_freshness_state",
            "acl_basis",
            "acl_confidence",
            "source_title",
            "source_type",
            "source_id",
            "source_external_id",
            "source_identity_hints",
            "source_observed_at",
        ):
            if key in source_item:
                annotated[key] = source_item.get(key)
        if not needle or needle not in hay:
            unmatched_quotes.append(ref)
            flagged.append({**annotated, "quote_verified": False})
        else:
            verified.append({**annotated, "quote_verified": True})

    return {
        "valid": not invalid_refs and (len(verified) + len(flagged)) > 0,
        "verified": verified,
        "flagged": flagged,
        "invalid_refs": invalid_refs,
        "unmatched_quotes": unmatched_quotes,
    }


def verify_inline_citations(
    text: str,
    pack_items: list[dict],
    *,
    allow_uncited_sections: set[str] | None = None,
    require_relevance: bool = False,
) -> dict:
    """For chat/brief prose with [ref:kind:id] citations inline."""
    import re

    refs = re.findall(REF_RE, text)
    by_ref = {f"{i['kind']}:{i['id']}": i for i in pack_items}
    valid_refs = set(by_ref)
    invalid = [r for r in refs if r not in valid_refs]
    unsupported_claims = []
    uncited_claims = []
    current_section = ""
    allowed = allow_uncited_sections or set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#"):
            current_section = line.lstrip("#").strip().lower()
            continue
        if not line or current_section in allowed or _is_nonclaim(line):
            continue
        line_refs = re.findall(REF_RE, line)
        if line_refs:
            if require_relevance and not _line_supported_by_refs(line, line_refs, by_ref):
                unsupported_claims.append(line[:240])
            continue
        uncited_claims.append(line[:240])
    return {
        # Hard gate: hallucinated refs always fail. When callers request
        # relevance, every claim line must either cite a supporting source or be
        # a narrow nonclaim/confidence-gap line.
        "valid": not invalid and (not require_relevance or not (unsupported_claims or uncited_claims)),
        "cited": refs,
        "invalid_refs": invalid,
        "uncited_claims": uncited_claims,
        "unsupported_claims": unsupported_claims,
    }


def verify_block_citations(
    text: str,
    pack_items: list[dict],
    *,
    allow_uncited_sections: set[str] | None = None,
    require_relevance: bool = False,
) -> dict:
    """Like verify_inline_citations, but grades per BLOCK (a blank-line- or
    header-delimited group of lines) instead of per line.

    Weaker router models cite inconsistently within a workstream — e.g. citing
    "What changed" but leaving a sibling "Focus today" bullet bare under the
    same "## Workstream" header. Per-line grading fails that bullet even
    though the workstream as a whole is source-backed. Block grading requires
    at least one relevant citation covering the block's substantive lines; a
    block with zero citations still fails (a purely-synthesized block is not
    let through for free).
    """
    import re

    refs = re.findall(REF_RE, text)
    by_ref = {f"{i['kind']}:{i['id']}": i for i in pack_items}
    valid_refs = set(by_ref)
    invalid = [r for r in refs if r not in valid_refs]

    allowed = allow_uncited_sections or set()
    uncited_claims: list[str] = []
    unsupported_claims: list[str] = []
    current_section = ""
    block: list[str] = []

    def flush() -> None:
        if not block:
            return
        substantive = [line for line in block if not _is_nonclaim(line)]
        if substantive and current_section not in allowed:
            block_text = "\n".join(block)
            block_refs = re.findall(REF_RE, block_text)
            if not block_refs:
                uncited_claims.append(block_text[:240])
            elif require_relevance and not _line_supported_by_refs(block_text, block_refs, by_ref):
                unsupported_claims.append(block_text[:240])
        block.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#"):
            flush()
            current_section = line.lstrip("#").strip().lower()
            continue
        if not line:
            flush()
            continue
        block.append(line)
    flush()

    return {
        "valid": not invalid and (not require_relevance or not (unsupported_claims or uncited_claims)),
        "cited": refs,
        "invalid_refs": invalid,
        "uncited_claims": uncited_claims,
        "unsupported_claims": unsupported_claims,
    }


def _normalized_text(value: str) -> str:
    return " ".join((value or "").lower().split())


_SOURCES_LABEL_PREFIXES = ("sources used:", "sources:", "brief built from:")


def _is_nonclaim(line: str) -> bool:
    stripped = line.lstrip("-* ").strip().lower()
    for prefix in _SOURCES_LABEL_PREFIXES:
        if stripped == prefix or stripped.startswith(prefix):
            return _is_sources_line_nonclaim(stripped, prefix)
    if stripped.startswith("missing:"):
        return _is_missing_line_nonclaim(stripped)
    stripped_without_label = _strip_markdown_label(stripped)
    exact_nonclaims = {
        "approval boundary: no approval-path source was retrieved; keep this read-only until michelle approves.",
        "immediate recommended actions from the alerts:",
        "recommended actions:",
        "recommended next actions:",
        "why now: the sources do not include priority or due-date evidence.",
        "execution steps: the sources do not include enough detail to recommend execution steps",
        "draft artifact: not enough evidence to recommend an artifact shape; ask michelle what format she wants.",
        "draft artifact: the sources do not include enough detail to recommend a draft artifact.",
        "i couldn't verify this from the provided sources.",
        "i could not verify this from the provided sources.",
    }
    if stripped in exact_nonclaims or stripped_without_label in exact_nonclaims:
        return True
    stripped = stripped_without_label
    # The brief template emits these empty-slot lines with a trailing period
    # (e.g. "No action recommended."); tolerate it so the model's own boilerplate
    # is not scored as an uncited claim.
    # Slice 2.2's unchanged-story line is the one empty-slot phrase the prompt
    # asks the model to cite (the prior_brief ref), so strip a trailing
    # citation before the boilerplate match (the other empty-slot phrases
    # above never carry one).
    stripped_uncited = _re.sub(r"\s*" + REF_RE + r"\s*$", "", stripped).strip()
    if stripped_uncited.rstrip(" .") in {
        "no risk evidence found", "no decision evidence found", "no action recommended",
        # Slice 2.2: the fixed phrase for a story the model marks "unchanged"
        # since yesterday's brief (app.synthesis.brief SYSTEM prompt requires
        # this exact text). Generic by design, so it carries no vocabulary
        # that would overlap with the cited prior_brief text either way;
        # treat it as boilerplate like the other empty-slot lines above.
        "no change since yesterday",
    }:
        return True
    if stripped.startswith("no ") and _is_no_evidence_boilerplate(stripped):
        return True
    return False


def _strip_markdown_label(stripped: str) -> str:
    """Drop bullet labels like "**Blocked:** " before nonclaim checks."""
    import re

    return re.sub(r"^(?:\*\*)?[a-z][a-z0-9 /_-]{1,40}(?:\*\*)?:(?:\*\*)?\s+", "", stripped)


def _is_sources_line_nonclaim(stripped: str, prefix: str) -> bool:
    # Coverage's source-list line ("Sources used:" / "Sources:" / "Brief built
    # from:") is meta-commentary about the brief itself, not a claim, so it
    # doesn't need its own citation IF it's a clean list with nothing else
    # riding along. Any second sentence (e.g. a "Gaps: ..." compound) or an
    # action-marker word still requires citation — this must not become a way
    # to smuggle a real claim past the gate under a Coverage label.
    value = stripped.removeprefix(prefix).strip().rstrip(".")
    if not value:
        return True
    if ". " in value or " but " in f" {value} ":
        return False
    action_markers = (
        " approved ",
        " assigned ",
        " changed ",
        " emailed ",
        " launched ",
        " notified ",
        " resolved ",
        " today ",
        " yesterday ",
        " tomorrow ",
    )
    return not any(marker in f" {value} " for marker in action_markers)


def _no_action_markers(stripped: str) -> bool:
    # Wrap on a punctuation-trimmed copy so a trailing "…signed." still matches the
    # " signed " marker and keeps the line counted as a substantive claim.
    wrapped = f" {stripped.strip('.!,;: ')} "
    return not any(
        marker in wrapped
        for marker in (" approved ", " assigned ", " changed ", " emailed ", " launched ", " notified ", " signed ", " resolved ", " contacted ")
    )


# The only tail allowed after "no [adj] activity/changes/updates" for it to count
# as an epistemic gap rather than a claim: an optional gap verb and/or a
# date/time qualifier. The qualifier object is bounded to a DATE/TIME shape (not
# free prose) so a substantive clause ("for the payment outage affecting
# customers") cannot ride through, and no comma is allowed so a comma-joined
# second clause cannot be smuggled in.
_GAP_DATE = r"(?:\d{4}-\d{2}-\d{2}|today|yesterday|tomorrow|this (?:week|sprint|cycle)|q[1-4]|[a-z]+ \d{1,2})"
_GAP_TAIL_RE = _re.compile(
    r"^(?:found|recorded|reported|logged|noted|observed|to report)?\s*"
    rf"(?:(?:for|as of|on or before) {_GAP_DATE}|today|yet|this (?:week|sprint|cycle))?$"
)
# A predicate verb means the "no ..." line is asserting something, not reporting a gap.
_CLAIM_VERB_RE = _re.compile(r"\b(?:was|were|is|are|been|being|has|have|had|made|did|will|remains?|stayed)\b")


def _is_no_evidence_boilerplate(stripped: str) -> bool:
    # Absence-of-coverage lines the brief legitimately emits when a workstream or
    # date has nothing to report: "No evidence found", "No activity recorded for
    # 2026-06-19", "No changes for today", "No updates".
    if " evidence found" in stripped or " evidence beyond" in stripped:
        return _no_action_markers(stripped)
    # Narrow absence-of-activity/changes/updates gap. Reject compound lines (they
    # smuggle a real claim after the gap) and any line carrying a predicate verb.
    m = _re.match(r"no (?:new |material |recent )?(?:activity|changes?|updates?)\b\s*(.*)$", stripped)
    if not m:
        return False
    if "; " in stripped or ". " in stripped:
        return False
    tail = m.group(1).strip(" .")
    if tail and not _GAP_TAIL_RE.match(tail):
        return False
    if _CLAIM_VERB_RE.search(stripped):
        return False
    return _no_action_markers(stripped)


def _is_missing_line_nonclaim(stripped: str) -> bool:
    value = stripped.removeprefix("missing:").strip().rstrip(".")
    if not value:
        return True
    if ". " in value or " but " in f" {value} ":
        return False
    if value in {"nothing", "nothing material", "additional connected sources may improve this answer"}:
        return True

    action_markers = (
        " approved ",
        " authorized ",
        " asked ",
        " assigned ",
        " changed ",
        " contact ",
        " contacted ",
        " customers ",
        " customer ",
        " emailed ",
        " email ",
        " launch today ",
        " launched ",
        " rollout ",
        " signed ",
        " signoff ",
        " signed off ",
        " notify ",
        " notified ",
        " mark ",
        " resolved ",
        " today ",
        " yesterday ",
        " tomorrow ",
    )
    gap_markers = (
        " missing",
        "not connected",
        "not enough",
        "not retrieved",
        "not available",
        "unavailable",
        "unknown",
        "cannot verify",
        "could not verify",
        "connection",
        "evidence",
        "verify",
        "source",
        "sources",
    )

    for segment in value.split(";"):
        part = segment.strip()
        if not part:
            continue
        wrapped = f" {part} "
        if any(marker in part for marker in ("no source shows", "no public source shows", "cannot be verified", "could not verify", "not connected")):
            continue
        if any(marker in wrapped for marker in action_markers):
            return False
        if any(marker in part for marker in gap_markers):
            continue
        words = part.split()
        # Allow short noun-phrase gaps like "launch owner" or "current project update".
        if len(words) <= 4 and not any(word.endswith(("ed", "ing")) for word in words):
            continue
        return False
    return True


_STOPWORDS = {
    "about", "after", "again", "also", "because", "before", "brief", "could",
    "focus", "from", "have", "line", "material", "needs", "next", "nothing",
    "provided", "should", "source", "sources", "that", "their", "there",
    "today", "what", "when", "where", "which", "with", "work", "would",
}


def _meaningful_tokens(text: str) -> set[str]:
    import re

    clean = re.sub(REF_RE, " ", text.lower())
    tokens = set()
    for token in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", clean):
        if len(token) < 4 or token in _STOPWORDS:
            continue
        tokens.add(token)
    return tokens


# Slice 2.2: a `prior_brief` pack item embeds an entire previous brief (every
# workstream, not one evidence item), so its token vocabulary is broad enough
# that a fabricated claim stitched from real words across unrelated stories
# in yesterday's brief can satisfy the overlap threshold below without being
# true. Prior_brief-only support (no other ref on the same claim) is
# legitimate for the short day-over-day continuity line slice 2.2 renders
# ("Carried over...", "No change since yesterday...") but must not be enough
# to back a longer, substantive claim with no fresh evidence behind it.
_PRIOR_BRIEF_ONLY_TOKEN_CEILING = 8


def _line_supported_by_refs(line: str, refs: list[str], by_ref: dict[str, dict]) -> bool:
    import math

    claim_tokens = _meaningful_tokens(line)
    if not claim_tokens:
        return True
    source_tokens: set[str] = set()
    fresh_ref_seen = False
    for ref in refs:
        item = by_ref.get(ref)
        if not item:
            continue
        if not ref.startswith("prior_brief:"):
            fresh_ref_seen = True
        source_tokens |= _meaningful_tokens(item.get("text") or "")
    overlap = claim_tokens & source_tokens
    required = min(len(claim_tokens), max(2, math.ceil(len(claim_tokens) * 0.45)))
    if len(overlap) < required:
        return False
    if refs and not fresh_ref_seen and len(claim_tokens) > _PRIOR_BRIEF_ONLY_TOKEN_CEILING:
        return False
    return True
