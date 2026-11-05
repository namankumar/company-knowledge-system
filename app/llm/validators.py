"""Deterministic LLM output validators."""
from dataclasses import dataclass, field
import re
from typing import Any

from app.llm.failures import FailureReason
from app.synthesis.verifier import (
    normalize_citations,
    verify_evidence,
    verify_inline_citations,
)


@dataclass(frozen=True)
class ValidatorResult:
    valid: bool
    reason: FailureReason | None = None
    details: dict[str, Any] = field(default_factory=dict)


_TRIAGE_DECISIONS = {"ignore", "extract", "extract_and_synthesize"}
_DRIFT_DECISIONS = {"alert", "suppress", "open_question"}
_SEVERITIES = {"high", "medium", "low"}
_WORKSTREAM_DIGEST_REF_RE = re.compile(r"\b[a-z][a-z0-9_]*:[A-Za-z0-9-]{8,}\b")
_WORKSTREAM_DIGEST_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]+")
_WORKSTREAM_DIGEST_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "by",
    "due",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
    "workstream",
    "project",
    "initiative",
    "commitment",
    "status",
    "detail",
    "summary",
    "evidence",
    "owner",
}


def validate_task_output(
    task: str,
    *,
    parsed: Any = None,
    text: str = "",
    pack_items: list[dict] | None = None,
    prompt_has_content: bool = False,
    expected_shape: dict[str, type] | None = None,
) -> ValidatorResult:
    if task == "triage":
        return validate_triage(parsed)
    if task == "extraction":
        return validate_extraction(parsed, prompt_has_content=prompt_has_content)
    if task == "drift":
        return validate_drift(parsed, pack_items=pack_items or [])
    if task == "brief_convergence":
        return validate_brief_convergence(parsed, pack_items=pack_items or [])
    if task == "brief":
        return validate_brief_structured(parsed, pack_items=pack_items or [])
    if task == "brief_weekly":
        return validate_brief_weekly(parsed, pack_items=pack_items or [])
    if task == "plan_actual":
        return validate_plan_actual(parsed, pack_items=pack_items or [])
    if task == "personalization_seed":
        return validate_personalization_seed(parsed, pack_items=pack_items or [])
    if task == "person_context_seed":
        return validate_person_context_seed(parsed, pack_items=pack_items or [])
    if task == "person_context_update":
        return validate_person_context_update(parsed, pack_items=pack_items or [])
    if task == "tenant_context_seed":
        return validate_tenant_context_seed(parsed, pack_items=pack_items or [])
    if task == "tenant_context_update":
        return validate_tenant_context_update(parsed, pack_items=pack_items or [])
    if task == "comms_draft":
        return validate_comms_draft(parsed, pack_items=pack_items or [])
    if task == "chat":
        return validate_chat(text, pack_items=pack_items or [])
    if task == "workstream_digest_draft":
        return validate_workstream_digest_draft(parsed, pack_items=pack_items or [])
    if task == "session_state_maintain":
        return validate_session_state_draft(parsed, valid_source_refs=_pack_refs(pack_items or []))
    if task == "repair":
        return validate_repair(parsed, expected_shape=expected_shape or {})
    if task == "workspace":
        return validate_workspace(parsed)
    return ValidatorResult(False, "schema_invalid", {"error": f"unknown task: {task}"})


def _unwrap_workstream_digest_candidate(parsed: Any) -> dict | None:
    if not isinstance(parsed, dict):
        return None
    nested = parsed.get("workstream_digest")
    if isinstance(nested, dict):
        return nested
    return parsed


def _clean_digest_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _first_digest_text(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        text = _clean_digest_text(mapping.get(key))
        if text:
            return text
    return ""


def _tokenize_digest_text(text: str) -> set[str]:
    tokens = set()
    for token in _WORKSTREAM_DIGEST_TOKEN_RE.findall(text.lower()):
        token = token.strip("-")
        if not token or token in _WORKSTREAM_DIGEST_STOPWORDS or token.isdigit():
            continue
        tokens.add(token)
    return tokens


def _flatten_digest_text(value: Any) -> str:
    parts: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"evidence_refs", "sources", "cited_sources", "ref", "source_ref"}:
                continue
            if isinstance(item, (str, int, float)) and not isinstance(item, bool):
                cleaned = _clean_digest_text(item)
                if cleaned:
                    parts.append(cleaned)
            elif isinstance(item, dict):
                parts.append(_flatten_digest_text(item))
            elif isinstance(item, list):
                for sub_item in item:
                    parts.append(_flatten_digest_text(sub_item))
    elif isinstance(value, list):
        for item in value:
            parts.append(_flatten_digest_text(item))
    else:
        cleaned = _clean_digest_text(value)
        if cleaned:
            parts.append(cleaned)
    return " ".join(part for part in parts if part).strip()


def _render_workstream_digest_summary(candidate: dict) -> str:
    direct_summary = _first_digest_text(candidate, "summary_text", "summary")
    if direct_summary:
        return direct_summary

    project = candidate.get("project") if isinstance(candidate.get("project"), dict) else {}
    title = _first_digest_text(candidate, "title", "name", "workstream") or _first_digest_text(project, "name", "title")
    workstream_id = _first_digest_text(candidate, "workstream_id", "id") or _first_digest_text(project, "id")
    owner = _first_digest_text(candidate, "owner") or _first_digest_text(project, "owner")
    due_date = _first_digest_text(candidate, "due_date") or _first_digest_text(project, "due_date")
    status_detail = (
        _first_digest_text(candidate, "status_detail", "current_status", "overall_status")
        or _first_digest_text(project, "status_detail", "current_status", "overall_status")
    )
    milestone = _first_digest_text(candidate, "milestone")

    headline_bits = [title or "Workstream digest"]
    if workstream_id and workstream_id != title:
        headline_bits[-1] = f"{headline_bits[-1]} ({workstream_id})"
    if owner:
        headline_bits.append(f"is owned by {owner}")
    if due_date:
        headline_bits.append(f"due {due_date}")
    if status_detail:
        headline_bits.append(f"and currently {status_detail}")
    summary_parts = [", ".join(headline_bits).strip().rstrip(".") + "."]
    if milestone:
        summary_parts.append(f"Milestone: {milestone}.")

    list_fields = (
        ("commitments", ("description", "owner", "due_date", "status_detail")),
        ("dependencies", ("entity", "depends_on_milestone", "milestone_due_date")),
        ("open_issues", ("type", "description", "impact", "recommended_action")),
        ("risks", ("description",)),
        ("facts", ("description", "detail")),
        ("next_steps", ("description", "action", "deadline", "detail")),
        ("sources", ("content", "description", "detail")),
    )
    for field_name, item_fields in list_fields:
        items = candidate.get(field_name)
        if not isinstance(items, list) or not items:
            continue
        rendered_items: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                text = _clean_digest_text(item)
                if text:
                    rendered_items.append(text)
                continue
            bits = [(_clean_digest_text(item.get(field)) for field in item_fields)]
            flat_bits = [bit for bit in bits[0] if bit]
            if flat_bits:
                rendered_items.append(", ".join(flat_bits))
        if rendered_items:
            summary_parts.append(f"{field_name.replace('_', ' ').capitalize()}: " + "; ".join(rendered_items) + ".")

    candidate_text = _flatten_digest_text(candidate)
    if candidate_text and candidate_text not in " ".join(summary_parts):
        summary_parts.append(candidate_text[:240].rstrip())

    summary_text = " ".join(part for part in summary_parts if part).strip()
    return summary_text


def _extract_workstream_digest_refs(candidate: dict) -> list[str]:
    refs: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            refs.update(_WORKSTREAM_DIGEST_REF_RE.findall(value))
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"ref", "source_ref", "cited_ref"} and isinstance(item, str):
                    refs.update(_WORKSTREAM_DIGEST_REF_RE.findall(item))
                else:
                    walk(item)
            return
        if isinstance(value, list):
            for item in value:
                walk(item)

    walk(candidate)
    return sorted(refs)


def _infer_workstream_digest_refs(candidate: dict, *, summary_text: str, pack_items: list[dict]) -> list[str]:
    candidate_tokens = _tokenize_digest_text(" ".join([summary_text, _flatten_digest_text(candidate)]))
    if not candidate_tokens or not pack_items:
        return []

    scored: list[tuple[int, str]] = []
    for item in pack_items:
        item_id = str(item.get("id") or "").strip()
        if not item.get("kind") or not item_id:
            ref = ""
        elif ":" in item_id:
            ref = item_id
        else:
            ref = f"{item.get('kind')}:{item_id}"
        if not ref:
            continue
        item_text = _clean_digest_text(item.get("text"))
        if not item_text:
            continue
        item_tokens = _tokenize_digest_text(item_text)
        score = len(candidate_tokens & item_tokens)
        if score > 0:
            scored.append((score, ref))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [ref for _, ref in scored]


def normalize_workstream_digest_draft_output(parsed: Any, *, pack_items: list[dict] | None = None) -> dict | None:
    candidate = _unwrap_workstream_digest_candidate(parsed)
    if not isinstance(candidate, dict):
        return None
    summary_text = _render_workstream_digest_summary(candidate)
    evidence_refs = _extract_workstream_digest_refs(candidate)
    if not evidence_refs and pack_items:
        evidence_refs = _infer_workstream_digest_refs(candidate, summary_text=summary_text, pack_items=pack_items)
    normalized = dict(candidate)
    normalized["summary_text"] = summary_text
    normalized["evidence_refs"] = evidence_refs
    return normalized


def validate_triage(parsed: Any) -> ValidatorResult:
    if not isinstance(parsed, dict):
        return ValidatorResult(False, "invalid_json", {"error": "triage output is not an object"})
    if parsed.get("decision") not in _TRIAGE_DECISIONS:
        return ValidatorResult(False, "schema_invalid", {"field": "decision"})
    if not isinstance(parsed.get("confidence"), (int, float)):
        return ValidatorResult(False, "schema_invalid", {"field": "confidence"})
    return ValidatorResult(True)


def validate_extraction(parsed: Any, *, prompt_has_content: bool = False) -> ValidatorResult:
    if not isinstance(parsed, dict):
        return ValidatorResult(False, "invalid_json", {"error": "extraction output is not an object"})
    for key in ("entities", "objects", "edges", "facts"):
        if not isinstance(parsed.get(key), list):
            return ValidatorResult(False, "schema_invalid", {"field": key})
    has_items = bool(parsed["entities"] or parsed["objects"] or parsed["facts"])
    if prompt_has_content and not has_items:
        return ValidatorResult(False, "empty_output", {"error": "empty extraction from contentful prompt"})
    for item in parsed["objects"]:
        if not item.get("object_type") or not item.get("key_hint") or not item.get("quote"):
            return ValidatorResult(False, "evidence_invalid", {"collection": "objects"})
    for item in parsed["facts"]:
        if not item.get("claim") or not item.get("quote"):
            return ValidatorResult(False, "evidence_invalid", {"collection": "facts"})
    return ValidatorResult(True)


def validate_drift(parsed: Any, *, pack_items: list[dict]) -> ValidatorResult:
    if not isinstance(parsed, dict):
        return ValidatorResult(False, "invalid_json", {"error": "drift output is not an object"})
    if parsed.get("decision") not in _DRIFT_DECISIONS:
        return ValidatorResult(False, "schema_invalid", {"field": "decision"})
    if parsed.get("severity") not in _SEVERITIES:
        return ValidatorResult(False, "schema_invalid", {"field": "severity"})
    verification = verify_evidence(parsed.get("evidence", []), pack_items)
    if not verification["valid"]:
        return ValidatorResult(False, "evidence_invalid", verification)
    return ValidatorResult(True, details=verification)


def _pack_refs(pack_items: list[dict]) -> set[str]:
    return {f"{i['kind']}:{i['id']}" for i in pack_items if i.get("kind") and i.get("id")}


def validate_brief_convergence(parsed: Any, *, pack_items: list[dict]) -> ValidatorResult:
    """Call 1 of slice 2.1's two-call brief synthesis: groups pack evidence into
    operating stories. Checks shape and that every evidence ref exists in the
    pack (hallucinated-source gate); the per-block citation/relevance check
    happens after call 2's markdown is rendered, same as before 2.1."""
    if not isinstance(parsed, dict):
        return ValidatorResult(False, "invalid_json", {"error": "brief_convergence output is not an object"})
    stories = parsed.get("stories")
    if not isinstance(stories, list) or not stories:
        return ValidatorResult(False, "schema_invalid", {"field": "stories"})
    refs = _pack_refs(pack_items)
    invalid_refs: list[str] = []
    for story in stories:
        if not isinstance(story, dict) or not str(story.get("title") or "").strip():
            return ValidatorResult(False, "schema_invalid", {"field": "title"})
        evidence_refs = story.get("evidence_refs")
        if not isinstance(evidence_refs, list) or not evidence_refs:
            return ValidatorResult(False, "schema_invalid", {"field": "evidence_refs"})
        invalid_refs.extend(ref for ref in evidence_refs if ref not in refs)
    if invalid_refs:
        return ValidatorResult(False, "evidence_invalid", {"invalid_refs": invalid_refs})
    return ValidatorResult(True, details={"story_count": len(stories)})


_STORY_REQUIRED_BLOCKS = ("focus_today", "what_changed", "blocked_or_at_risk", "needs_decision", "next_action")
_STORY_OPTIONAL_BLOCKS = ("implication", "who_needs_to_know")
_LEVERAGE_TIERS = {"high", "medium", "low"}
# Slice 2.2: optional day-over-day continuity marker, set only when a
# prior_brief pack item is present. Absent (first brief for a tenant) is valid.
_STORY_STATUSES = {"new", "continuing", "resolved", "unchanged"}
_MOVE_FIELDS = ("action", "creates", "deadline")


def validate_brief_structured(parsed: Any, *, pack_items: list[dict]) -> ValidatorResult:
    """Call 2 of slice 2.1: writes the brief as JSON stories (schema below),
    rendered to markdown deterministically by app.synthesis.brief before the
    existing block-citation/relevance check runs on that rendered text."""
    if not isinstance(parsed, dict):
        return ValidatorResult(False, "invalid_json", {"error": "brief output is not an object"})
    stories = parsed.get("stories")
    if not isinstance(stories, list) or not stories:
        return ValidatorResult(False, "schema_invalid", {"field": "stories"})
    refs = _pack_refs(pack_items)
    invalid_refs: list[str] = []
    tldr = parsed.get("tldr")
    if tldr is not None:
        if not isinstance(tldr, dict) or not str(tldr.get("text") or "").strip():
            return ValidatorResult(False, "schema_invalid", {"field": "tldr"})
        tldr_refs = tldr.get("refs")
        if not isinstance(tldr_refs, list):
            return ValidatorResult(False, "schema_invalid", {"field": "tldr.refs"})
        invalid_refs.extend(ref for ref in tldr_refs if ref not in refs)
    moves = parsed.get("moves")
    if moves is None:
        moves = []
    if not isinstance(moves, list):
        return ValidatorResult(False, "schema_invalid", {"field": "moves"})
    if len(moves) > 3:
        return ValidatorResult(False, "schema_invalid", {"field": "moves"})
    for move in moves:
        if not isinstance(move, dict):
            return ValidatorResult(False, "schema_invalid", {"field": "moves"})
        for field_name in _MOVE_FIELDS:
            if not str(move.get(field_name) or "").strip():
                return ValidatorResult(False, "schema_invalid", {"field": f"moves.{field_name}"})
        move_refs = move.get("refs")
        if not isinstance(move_refs, list) or not move_refs:
            return ValidatorResult(False, "schema_invalid", {"field": "moves.refs"})
        invalid_refs.extend(ref for ref in move_refs if ref not in refs)
    for story in stories:
        if not isinstance(story, dict) or not str(story.get("title") or "").strip():
            return ValidatorResult(False, "schema_invalid", {"field": "title"})
        if story.get("leverage") not in _LEVERAGE_TIERS:
            return ValidatorResult(False, "schema_invalid", {"field": "leverage"})
        if "status" in story and story.get("status") not in _STORY_STATUSES:
            return ValidatorResult(False, "schema_invalid", {"field": "status"})
        for field_name in _STORY_REQUIRED_BLOCKS:
            block = story.get(field_name)
            if not isinstance(block, dict) or not str(block.get("text") or "").strip():
                return ValidatorResult(False, "schema_invalid", {"field": field_name})
            block_refs = block.get("refs")
            if not isinstance(block_refs, list):
                return ValidatorResult(False, "schema_invalid", {"field": f"{field_name}.refs"})
            invalid_refs.extend(ref for ref in block_refs if ref not in refs)
        for field_name in _STORY_OPTIONAL_BLOCKS:
            block = story.get(field_name)
            if block is None:
                continue
            if not isinstance(block, dict) or not str(block.get("text") or "").strip():
                return ValidatorResult(False, "schema_invalid", {"field": field_name})
            invalid_refs.extend(ref for ref in (block.get("refs") or []) if ref not in refs)
    coverage = parsed.get("coverage")
    if not isinstance(coverage, dict) or not str(coverage.get("text") or "").strip():
        return ValidatorResult(False, "schema_invalid", {"field": "coverage"})
    invalid_refs.extend(ref for ref in (coverage.get("refs") or []) if ref not in refs)
    # Slice 2.3: optional forward-looking section, omitted entirely when the
    # pack has no deadlines/calendar events in the near-term window.
    coming_up = parsed.get("coming_up")
    if coming_up is not None:
        if not isinstance(coming_up, dict) or not str(coming_up.get("text") or "").strip():
            return ValidatorResult(False, "schema_invalid", {"field": "coming_up"})
        invalid_refs.extend(ref for ref in (coming_up.get("refs") or []) if ref not in refs)
    if invalid_refs:
        return ValidatorResult(False, "evidence_invalid", {"invalid_refs": invalid_refs})
    return ValidatorResult(True, details={"story_count": len(stories), "move_count": len(moves), "has_tldr": tldr is not None})


_WEEKLY_SECTION_TITLES = {
    "Two-week arc summary",
    "What shipped",
    "Trends",
    "Next week",
}


def validate_brief_weekly(parsed: Any, *, pack_items: list[dict]) -> ValidatorResult:
    """Slice 2.4 weekly surface: requires the narrative sections the weekly
    renderer depends on, but lets code inject the deterministic OKR and
    kill-list blocks."""
    if not isinstance(parsed, dict):
        return ValidatorResult(False, "invalid_json", {"error": "brief_weekly output is not an object"})
    sections = parsed.get("sections")
    if not isinstance(sections, list) or not sections:
        return ValidatorResult(False, "schema_invalid", {"field": "sections"})
    refs = _pack_refs(pack_items)
    invalid_refs: list[str] = []
    titles_seen: set[str] = set()
    for section in sections:
        if not isinstance(section, dict) or not str(section.get("title") or "").strip():
            return ValidatorResult(False, "schema_invalid", {"field": "title"})
        title = str(section.get("title") or "").strip()
        titles_seen.add(title)
        if title not in _WEEKLY_SECTION_TITLES:
            return ValidatorResult(False, "schema_invalid", {"field": "title"})
        if not str(section.get("text") or "").strip():
            return ValidatorResult(False, "schema_invalid", {"field": "text"})
        section_refs = section.get("refs")
        if not isinstance(section_refs, list):
            return ValidatorResult(False, "schema_invalid", {"field": "refs"})
        invalid_refs.extend(ref for ref in section_refs if ref not in refs)
    missing_titles = _WEEKLY_SECTION_TITLES - titles_seen
    if missing_titles:
        return ValidatorResult(False, "schema_invalid", {"field": "sections", "missing_titles": sorted(missing_titles)})
    coverage = parsed.get("coverage")
    if not isinstance(coverage, dict) or not str(coverage.get("text") or "").strip():
        return ValidatorResult(False, "schema_invalid", {"field": "coverage"})
    invalid_refs.extend(ref for ref in (coverage.get("refs") or []) if ref not in refs)
    if invalid_refs:
        return ValidatorResult(False, "evidence_invalid", {"invalid_refs": invalid_refs})
    return ValidatorResult(True, details={"section_count": len(sections)})


_PLAN_ACTUAL_TYPES = {"plan_no_activity", "activity_no_plan"}
_PLAN_ACTUAL_MONITORS = {"commitments", "deadlines", "strategy", "teammates", "roadmap", "cross_monitor"}


def validate_plan_actual(parsed: Any, *, pack_items: list[dict]) -> ValidatorResult:
    """Slice 2.4: an empty mismatches list is a valid, common result (a
    fully-consistent pack) -- only reject missing/malformed shape, not
    emptiness."""
    if not isinstance(parsed, dict):
        return ValidatorResult(False, "invalid_json", {"error": "plan_actual output is not an object"})
    mismatches = parsed.get("mismatches")
    if not isinstance(mismatches, list):
        return ValidatorResult(False, "schema_invalid", {"field": "mismatches"})
    refs = _pack_refs(pack_items)
    invalid_refs: list[str] = []
    for mismatch in mismatches:
        if not isinstance(mismatch, dict) or mismatch.get("type") not in _PLAN_ACTUAL_TYPES:
            return ValidatorResult(False, "schema_invalid", {"field": "type"})
        if mismatch.get("monitor") not in _PLAN_ACTUAL_MONITORS:
            return ValidatorResult(False, "schema_invalid", {"field": "monitor"})
        if not str(mismatch.get("text") or "").strip():
            return ValidatorResult(False, "schema_invalid", {"field": "text"})
        mismatch_refs = mismatch.get("refs")
        if not isinstance(mismatch_refs, list) or not mismatch_refs:
            return ValidatorResult(False, "schema_invalid", {"field": "refs"})
        invalid_refs.extend(ref for ref in mismatch_refs if ref not in refs)
    if invalid_refs:
        return ValidatorResult(False, "evidence_invalid", {"invalid_refs": invalid_refs})
    monitor_counts: dict[str, int] = {}
    for mismatch in mismatches:
        monitor = str(mismatch.get("monitor") or "")
        monitor_counts[monitor] = monitor_counts.get(monitor, 0) + 1
    return ValidatorResult(True, details={"mismatch_count": len(mismatches), "monitor_counts": monitor_counts})


_SEED_LIST_FIELDS = ("priorities", "watchlist", "important_people")


def _validate_seed_entries(entries: Any, *, field: str, refs: set[str]) -> list[str] | None:
    """Returns invalid refs found, or None if the shape itself is invalid."""
    if not isinstance(entries, list):
        return None
    invalid_refs: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or not str(entry.get("text") or "").strip():
            return None
        entry_refs = entry.get("refs")
        if not isinstance(entry_refs, list) or not entry_refs:
            return None
        invalid_refs.extend(ref for ref in entry_refs if ref not in refs)
    return invalid_refs


def validate_comms_draft(parsed: Any, *, pack_items: list[dict]) -> ValidatorResult:
    """Track B artifacts-lifecycle packet (2026-07-11), slice A.3. comms_draft
    proposes a leadership/investor update as a flat list of cited claims (no
    story grouping -- that already happened in brief_convergence, this task
    just re-projects it). Same per-claim ref shape as personalization_seed's
    list fields: every claim needs non-empty text and a non-empty refs list,
    and every ref must resolve in the pack this call was given. This is a
    schema/hallucinated-ref gate only; the *count* of uncited claims used by
    the comms_draft promotion precondition (A.4) is computed separately by
    `app.synthesis.verifier.verify_inline_citations` against the rendered
    markdown, not by this validator."""
    if not isinstance(parsed, dict):
        return ValidatorResult(False, "invalid_json", {"error": "comms_draft output is not an object"})
    claims = parsed.get("claims")
    if not isinstance(claims, list):
        return ValidatorResult(False, "schema_invalid", {"field": "claims"})
    refs = _pack_refs(pack_items)
    invalid_refs: list[str] = []
    for claim in claims:
        if not isinstance(claim, dict) or not str(claim.get("text") or "").strip():
            return ValidatorResult(False, "schema_invalid", {"field": "claims[].text"})
        claim_refs = claim.get("refs")
        if not isinstance(claim_refs, list) or not claim_refs:
            return ValidatorResult(False, "schema_invalid", {"field": "claims[].refs"})
        invalid_refs.extend(ref for ref in claim_refs if ref not in refs)
    if invalid_refs:
        return ValidatorResult(False, "evidence_invalid", {"invalid_refs": invalid_refs})
    return ValidatorResult(True, details={"claim_count": len(claims)})


def validate_personalization_seed(parsed: Any, *, pack_items: list[dict]) -> ValidatorResult:
    """Slice 2.6: a first-pass user_contexts draft from backfilled history,
    held for human approval (see app.personalization.seed). Empty lists and
    a null current_plan are valid -- a thin cold-start pack shouldn't force
    the model to invent entries to fill every category."""
    if not isinstance(parsed, dict):
        return ValidatorResult(False, "invalid_json", {"error": "personalization_seed output is not an object"})
    refs = _pack_refs(pack_items)
    invalid_refs: list[str] = []
    for field in _SEED_LIST_FIELDS:
        field_invalid = _validate_seed_entries(parsed.get(field), field=field, refs=refs)
        if field_invalid is None:
            return ValidatorResult(False, "schema_invalid", {"field": field})
        invalid_refs.extend(field_invalid)
    current_plan = parsed.get("current_plan")
    if current_plan is not None:
        if not isinstance(current_plan, dict) or not str(current_plan.get("text") or "").strip():
            return ValidatorResult(False, "schema_invalid", {"field": "current_plan"})
        plan_refs = current_plan.get("refs")
        if not isinstance(plan_refs, list) or not plan_refs:
            return ValidatorResult(False, "schema_invalid", {"field": "current_plan.refs"})
        invalid_refs.extend(ref for ref in plan_refs if ref not in refs)
    if invalid_refs:
        return ValidatorResult(False, "evidence_invalid", {"invalid_refs": invalid_refs})
    return ValidatorResult(True, details={
        "priorities": len(parsed.get("priorities") or []),
        "watchlist": len(parsed.get("watchlist") or []),
        "important_people": len(parsed.get("important_people") or []),
        "has_current_plan": current_plan is not None,
    })


_TENANT_CONTEXT_LIST_FIELDS = ("priorities", "active_narratives", "cadences")
_TENANT_CONTEXT_OPERATION_FIELDS = {"priorities", "active_narratives", "cadences", "current_plan"}
_TENANT_CONTEXT_OPERATION_TYPES = {"add", "retire", "amend", "set", "clear"}


_PERSON_CONTEXT_LIST_FIELDS = ("topic_stances", "communication_preferences", "sentiment_history")


def _validate_person_context_entries(value: Any, *, field: str, refs: set[str]) -> list[str] | None:
    if value is None:
        return []
    if not isinstance(value, list):
        return None
    invalid_refs: list[str] = []
    for item in value:
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            return None
        entry_refs = item.get("refs") or item.get("source_refs")
        if not isinstance(entry_refs, list):
            return None
        if not entry_refs:
            return None
        invalid_refs.extend(ref for ref in entry_refs if ref not in refs)
    return invalid_refs


def _validate_optional_person_context_object(value: Any, *, field: str, refs: set[str]) -> list[str] | None:
    if value is None:
        return []
    if not isinstance(value, dict) or not str(value.get("text") or "").strip():
        return None
    entry_refs = value.get("refs") or value.get("source_refs")
    if not isinstance(entry_refs, list) or not entry_refs:
        return None
    return [ref for ref in entry_refs if ref not in refs]


def validate_person_context_seed(parsed: Any, *, pack_items: list[dict]) -> ValidatorResult:
    if not isinstance(parsed, dict):
        return ValidatorResult(False, "invalid_json", {"error": "person_context_seed output is not an object"})
    refs = _pack_refs(pack_items)
    invalid_refs: list[str] = []
    for field in _PERSON_CONTEXT_LIST_FIELDS:
        field_invalid = _validate_person_context_entries(parsed.get(field), field=field, refs=refs)
        if field_invalid is None:
            return ValidatorResult(False, "schema_invalid", {"field": field})
        invalid_refs.extend(field_invalid)
    for field in ("relationship_kind", "sentiment_trend"):
        field_invalid = _validate_optional_person_context_object(parsed.get(field), field=field, refs=refs)
        if field_invalid is None:
            return ValidatorResult(False, "schema_invalid", {"field": field})
        invalid_refs.extend(field_invalid)
    if invalid_refs:
        return ValidatorResult(False, "evidence_invalid", {"invalid_refs": invalid_refs})
    return ValidatorResult(True, details={
        "topic_stances": len(parsed.get("topic_stances") or []),
        "communication_preferences": len(parsed.get("communication_preferences") or []),
        "sentiment_history": len(parsed.get("sentiment_history") or []),
        "has_relationship_kind": parsed.get("relationship_kind") is not None,
        "has_sentiment_trend": parsed.get("sentiment_trend") is not None,
    })


def validate_person_context_update(parsed: Any, *, pack_items: list[dict]) -> ValidatorResult:
    return validate_person_context_seed(parsed, pack_items=pack_items)


def validate_tenant_context_seed(parsed: Any, *, pack_items: list[dict]) -> ValidatorResult:
    if not isinstance(parsed, dict):
        return ValidatorResult(False, "invalid_json", {"error": "tenant_context_seed output is not an object"})
    refs = _pack_refs(pack_items)
    invalid_refs: list[str] = []
    for field in _TENANT_CONTEXT_LIST_FIELDS:
        field_invalid = _validate_seed_entries(parsed.get(field), field=field, refs=refs)
        if field_invalid is None:
            return ValidatorResult(False, "schema_invalid", {"field": field})
        invalid_refs.extend(field_invalid)
    current_plan = parsed.get("current_plan")
    if current_plan is not None:
        if not isinstance(current_plan, dict) or not str(current_plan.get("text") or "").strip():
            return ValidatorResult(False, "schema_invalid", {"field": "current_plan"})
        plan_refs = current_plan.get("refs")
        if not isinstance(plan_refs, list) or not plan_refs:
            return ValidatorResult(False, "schema_invalid", {"field": "current_plan.refs"})
        invalid_refs.extend(ref for ref in plan_refs if ref not in refs)
    if invalid_refs:
        return ValidatorResult(False, "evidence_invalid", {"invalid_refs": invalid_refs})
    return ValidatorResult(True, details={
        "priorities": len(parsed.get("priorities") or []),
        "active_narratives": len(parsed.get("active_narratives") or []),
        "cadences": len(parsed.get("cadences") or []),
        "has_current_plan": current_plan is not None,
    })


def validate_tenant_context_update(
    parsed: Any,
    *,
    pack_items: list[dict],
    current_context: dict | None = None,
) -> ValidatorResult:
    if not isinstance(parsed, dict):
        return ValidatorResult(False, "invalid_json", {"error": "tenant_context_update output is not an object"})
    operations = parsed.get("operations")
    if not isinstance(operations, list):
        return ValidatorResult(False, "schema_invalid", {"field": "operations"})
    refs = _pack_refs(pack_items)
    context_ids: set[str] = set()
    if current_context:
        for field in ("priorities", "active_narratives", "cadences"):
            context_ids.update(str(entry.get("id")) for entry in (current_context.get(field) or []) if entry.get("id"))
        if current_context.get("current_plan") and current_context["current_plan"].get("id"):
            context_ids.add(str(current_context["current_plan"]["id"]))
    invalid_refs: list[str] = []
    for op in operations:
        if not isinstance(op, dict):
            return ValidatorResult(False, "schema_invalid", {"field": "operation"})
        if op.get("op") not in _TENANT_CONTEXT_OPERATION_TYPES:
            return ValidatorResult(False, "schema_invalid", {"field": "op"})
        if op.get("field") not in _TENANT_CONTEXT_OPERATION_FIELDS:
            return ValidatorResult(False, "schema_invalid", {"field": "field"})
        action = op.get("op")
        field = op.get("field")
        entry = op.get("entry")
        entry_id = str(op.get("entry_id") or "")
        if action in {"retire", "amend"} and field != "current_plan":
            if not entry_id:
                return ValidatorResult(False, "schema_invalid", {"field": "entry_id"})
            if context_ids and entry_id not in context_ids:
                return ValidatorResult(False, "schema_invalid", {"field": "entry_id"})
        if action in {"add", "amend", "set"}:
            if not isinstance(entry, dict) or not str(entry.get("text") or "").strip():
                return ValidatorResult(False, "schema_invalid", {"field": "entry"})
            entry_refs = entry.get("refs") or entry.get("source_refs")
            if not isinstance(entry_refs, list) or not entry_refs:
                return ValidatorResult(False, "schema_invalid", {"field": "entry.refs"})
            invalid_refs.extend(ref for ref in entry_refs if ref not in refs)
        if action == "clear" and field != "current_plan":
            return ValidatorResult(False, "schema_invalid", {"field": "field"})
    if invalid_refs:
        return ValidatorResult(False, "evidence_invalid", {"invalid_refs": invalid_refs})
    return ValidatorResult(True, details={"operation_count": len(operations)})


def validate_chat(text: str, *, pack_items: list[dict]) -> ValidatorResult:
    if not text.strip():
        return ValidatorResult(False, "empty_output", {"error": "chat output is empty"})
    text = normalize_citations(text, pack_items)
    check = verify_inline_citations(text, pack_items, require_relevance=True)
    if not check["valid"]:
        return ValidatorResult(False, "evidence_invalid", check)
    return ValidatorResult(True, details=check)


def validate_workstream_digest_draft(
    parsed: Any,
    *,
    pack_items: list[dict],
    signal_tokens: list[str] | None = None,
) -> ValidatorResult:
    """Slice 3 (packpolicy-digests packet): extractive-not-abstractive gate
    for workstream digest drafts. Three checks per the packet's Authority
    Split / Evaluation sections: (1) token band, (2) every cited ref exists
    in the supplied evidence bundle (the hallucination-risk control), (3)
    when `signal_tokens` (named entities/dates/numbers pulled straight from
    the source evidence, computed by the caller -- see
    app/personalization/workstream_digest.py::_extractive_signal_tokens) are
    supplied, at least a minimum number must appear verbatim in the draft --
    a gist-style summary with no concrete names/dates/numbers fails this
    check even if it's otherwise well-formed JSON."""
    normalized = normalize_workstream_digest_draft_output(parsed, pack_items=pack_items)
    if not isinstance(normalized, dict):
        return ValidatorResult(False, "invalid_json", {"error": "workstream_digest_draft output is not an object"})
    summary_text = normalized.get("summary_text")
    if not isinstance(summary_text, str) or not summary_text.strip():
        return ValidatorResult(False, "schema_invalid", {"field": "summary_text"})
    evidence_refs = normalized.get("evidence_refs")
    if not isinstance(evidence_refs, list) or not evidence_refs:
        return ValidatorResult(False, "schema_invalid", {"field": "evidence_refs"})

    refs = _pack_refs(pack_items)
    invalid_refs = [ref for ref in evidence_refs if ref not in refs]
    if invalid_refs:
        return ValidatorResult(False, "evidence_invalid", {"invalid_refs": invalid_refs})

    # ~4 chars/token, same estimator used throughout this codebase (see
    # retrieve.py's token-cost accounting). A loose band around the
    # packet's target 100-150 tokens tolerates estimator noise across
    # different draft phrasing.
    token_estimate = len(summary_text) // 4
    if token_estimate < 40 or token_estimate > 220:
        return ValidatorResult(
            False,
            "schema_invalid",
            {"field": "summary_text_token_band", "token_estimate": token_estimate},
        )

    if signal_tokens:
        lowered_summary = summary_text.lower()
        present = [token for token in signal_tokens if token.lower() in lowered_summary]
        min_required = min(2, len(signal_tokens))
        if len(present) < min_required:
            return ValidatorResult(
                False,
                "evidence_invalid",
                {
                    "error": "digest reads as a gist summary, not extractive",
                    "signal_tokens": signal_tokens,
                    "present": present,
                },
            )

    return ValidatorResult(
        True,
        details={"evidence_ref_count": len(evidence_refs), "token_estimate": token_estimate},
    )


def _valid_entry_list(value: Any, *, valid_source_refs: set[str]) -> tuple[bool, str, list[str]]:
    """Each session_state entry (factor/decision/open_question) must be
    {"text": str, "source_refs": [str, ...]} with every source_ref pointing
    at a message that actually exists in this thread (`message:{id}`) --
    the same extractive-validator discipline the workstream digest
    validator applies to evidence_refs, applied here to
    session_messages instead of brain_objects/facts. An entry with no
    source_refs, or one citing a ref outside the supplied set, is rejected
    rather than silently written -- citation discipline extends to derived
    session state, not just chat/brief answers (packet's Failure Modes And
    Controls: "session_state drifts from what messages actually said").

    Returns (ok, failure_kind, detail_refs). failure_kind is "schema" for a
    malformed entry (not a list/object, missing text, missing source_refs)
    and "evidence" only when every entry is well-formed but cites a ref
    outside valid_source_refs -- the caller maps these to distinct
    FailureReasons rather than collapsing every rejection into one code."""
    if not isinstance(value, list):
        return False, "schema", ["not_a_list"]
    invalid_refs: list[str] = []
    for entry in value:
        if not isinstance(entry, dict):
            return False, "schema", ["entry_not_object"]
        if not isinstance(entry.get("text"), str) or not entry["text"].strip():
            return False, "schema", ["missing_text"]
        source_refs = entry.get("source_refs")
        if not isinstance(source_refs, list) or not source_refs:
            return False, "schema", ["missing_source_refs"]
        for ref in source_refs:
            if ref not in valid_source_refs:
                invalid_refs.append(str(ref))
    return (not invalid_refs), "evidence", invalid_refs


def validate_session_state_draft(parsed: Any, *, valid_source_refs: set[str]) -> ValidatorResult:
    """Slice 3.3's "regenerate state" v1 correction action: the maintenance
    task redrafts `factors`/`decisions`/`open_questions` from the thread's
    full message history. Extractive gate: every entry needs `text` +
    non-empty `source_refs`, and every source_ref must name a message
    actually present in the thread (passed in as `valid_source_refs`,
    `message:{id}` refs built by app.sessions.state from the same messages
    handed to the model) -- a hallucinated or dangling ref fails validation
    and the prior session_state version is kept, never overwritten with a
    partial/invalid draft."""
    if not isinstance(parsed, dict):
        return ValidatorResult(False, "invalid_json", {"error": "session_state_maintain output is not an object"})
    for field in ("factors", "decisions", "open_questions"):
        ok, failure_kind, invalid_refs = _valid_entry_list(parsed.get(field, []), valid_source_refs=valid_source_refs)
        if not ok:
            return ValidatorResult(
                False,
                "evidence_invalid" if failure_kind == "evidence" else "schema_invalid",
                {"field": field, "invalid_refs": invalid_refs},
            )
    total_entries = sum(len(parsed.get(f, []) or []) for f in ("factors", "decisions", "open_questions"))
    return ValidatorResult(True, details={"entry_count": total_entries})


def validate_repair(parsed: Any, *, expected_shape: dict[str, type]) -> ValidatorResult:
    if not isinstance(parsed, dict):
        return ValidatorResult(False, "invalid_json", {"error": "repair output is not an object"})
    for key, expected_type in expected_shape.items():
        if not isinstance(parsed.get(key), expected_type):
            return ValidatorResult(False, "schema_invalid", {"field": key})
    return ValidatorResult(True)


# Slice 3.2: session tool loop. Every "workspace" model call must resolve to
# exactly one of these two shapes. The registry of *tool names* the model is
# allowed to name lives in app.sessions.tools (WORKSPACE_TOOLS) -- this
# validator only checks shape, not whether a named tool exists in the
# registry. Requesting an unregistered tool name is not a validator failure:
# it is a golden-case rejection handled by the tool loop itself (case 2 of
# the session_authority eval bucket), so a hallucinated tool name still
# reaches the loop where it is traced as a rejected step, not silently
# reshaped here.
def validate_workspace(parsed: Any) -> ValidatorResult:
    if not isinstance(parsed, dict):
        return ValidatorResult(False, "invalid_json", {"error": "workspace output is not an object"})
    action = parsed.get("action")
    if action not in ("tool_call", "final"):
        return ValidatorResult(False, "schema_invalid", {"field": "action"})
    if action == "tool_call":
        if not isinstance(parsed.get("tool"), str) or not parsed["tool"].strip():
            return ValidatorResult(False, "schema_invalid", {"field": "tool"})
        if not isinstance(parsed.get("args"), dict):
            return ValidatorResult(False, "schema_invalid", {"field": "args"})
        return ValidatorResult(True, details={"action": "tool_call", "tool": parsed["tool"]})
    if not isinstance(parsed.get("text"), str) or not parsed["text"].strip():
        return ValidatorResult(False, "empty_output", {"error": "final output text is empty"})
    return ValidatorResult(True, details={"action": "final"})
