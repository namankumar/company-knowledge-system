"""Deterministic OKR context for Discuss and daily briefs."""
from __future__ import annotations

import re
from typing import Any

from app.core.db import acquire
from app.presentation.okrs import project_okrs

ATTENTION_STATES = {"at_risk", "blocked", "needs_review", "stale"}
OKR_TERMS = {
    "okr",
    "okrs",
    "objective",
    "objectives",
    "kr",
    "krs",
    "key result",
    "key results",
    "contribution",
    "contributions",
    "contribute",
    "contributed",
    "team",
    "person",
    "people",
    "right work",
    "working on the right thing",
}


async def fetch_okr_state(tenant_id: str, *, include_private_contributions: bool = False) -> list[dict]:
    async with acquire() as conn:
        okr_rows = await conn.fetch(
            """
            SELECT id, title, cycle, code, sort_order, final_deadline, status, visibility,
                   source_refs, confidence, plan_context, updated_at
            FROM okrs
            WHERE tenant_id = $1
            ORDER BY sort_order, updated_at DESC, title
            """,
            tenant_id,
        )
        key_result_rows = await conn.fetch(
            """
            SELECT id, okr_id, title, code, sort_order, target_value, current_value, unit,
                   calculation_method, final_deadline, status, source_refs, confidence,
                   trackability_state, planning_status, definition_of_done,
                   planning_missing_inputs, first_check, check_count, source_tab,
                   plan_context, updated_at
            FROM key_results
            WHERE tenant_id = $1
            ORDER BY sort_order, created_at, title
            """,
            tenant_id,
        )
        snapshot_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (okr_id, key_result_id)
                   id, okr_id, key_result_id, state, percent_complete,
                   calculation_method, calculation_inputs, summary, changed_reason,
                   next_step, evidence_refs, missing_evidence, confidence, created_at
            FROM okr_progress_snapshots
            WHERE tenant_id = $1
            ORDER BY okr_id, key_result_id, created_at DESC
            """,
            tenant_id,
        )
        mapping_rows = await conn.fetch(
            """
            SELECT id, okr_id, key_result_id, subject_type, subject_id,
                   relationship_type, confidence, evidence_refs, missing_evidence,
                   review_status, review_note, correction_reason
            FROM okr_work_mappings
            WHERE tenant_id = $1 AND review_status != 'rejected'
            ORDER BY updated_at DESC
            """,
            tenant_id,
        )
        contribution_rows = await conn.fetch(
            """
            SELECT id, okr_id, key_result_id, person_name, contribution_type,
                   contribution_summary, related_subject_type, related_subject_id,
                   evidence_refs, confidence, caveat, visibility, review_status,
                   execution_metrics, right_work_status, manager_review_flags
            FROM okr_contributions
            WHERE tenant_id = $1
              AND review_status NOT IN ('rejected', 'manager_rejected')
            ORDER BY updated_at DESC
            """,
            tenant_id,
        )
        dri_question_rows = await conn.fetch(
            """
            SELECT id, okr_id, key_result_id, recipient, question, reason,
                   missing_evidence, source_refs, status, created_at, updated_at
            FROM okr_dri_questions
            WHERE tenant_id = $1
              AND status != 'dismissed'
            ORDER BY updated_at DESC
            """,
            tenant_id,
        )
        milestone_rows = await conn.fetch(
            """
            SELECT id, okr_id, key_result_id, milestone_key, title, owner, due_date,
                   definition_of_done, measurement_rule, weight, status, source_refs,
                   evidence_refs, missing_evidence, confidence, review_status,
                   notes_actual, raw_payload, sort_order, row_type, source_tab,
                   source_row_index, owner_names, strategy_text, updated_at
            FROM okr_milestones
            WHERE tenant_id = $1
            ORDER BY key_result_id, sort_order, due_date, title
            """,
            tenant_id,
        )
    return project_okrs(
        okr_rows,
        key_result_rows,
        snapshot_rows,
        mapping_rows,
        contribution_rows,
        dri_question_rows,
        None,
        milestone_rows,
        include_private_contributions=include_private_contributions,
        destination_principals=[f"company:{tenant_id}"],
    )


def is_okr_question(question: str) -> bool:
    clean = question.lower()
    return any(term in clean for term in OKR_TERMS)


def okr_attention_items(okrs: list[dict]) -> list[dict]:
    items: list[dict] = []
    for okr in okrs:
        okr_item = _attention_item_for_objective(okr)
        if okr_item:
            items.append(okr_item)
        for kr in okr.get("key_results") or []:
            kr_item = _attention_item_for_key_result(okr, kr)
            if kr_item:
                items.append(kr_item)
    return items[:20]


def okr_status_answer(question: str, okrs: list[dict]) -> dict | None:
    if not is_okr_question(question):
        return None
    selected = _select_okrs(question, okrs)
    if not selected:
        return {
            "answer": "I do not have any OKRs in the company memory yet.\n\nSources used:\nMissing: seeded OKR state.",
            "sources": [],
            "trust_metadata": {
                "confidence_gaps": ["No OKR state is available."],
                "okr_state_answer": True,
            },
        }

    items = []
    lower_question = question.lower()
    wants_people = any(term in lower_question for term in ("contribution", "contribute", "contributed", "team", "person", "people", "right work", "working on the right thing"))
    person_filter = _person_filter(question, selected) if wants_people else None
    lines = ["OKR state"]
    missing: list[str] = []
    for okr in selected[:5]:
        item = _context_item_for_objective(okr)
        items.append(item)
        ref = f"[ref:{item['kind']}:{item['id']}]"
        snapshot = okr.get("snapshot") or {}
        lines.append(
            f"- {okr['title']}: {_status_label(snapshot.get('state') or okr.get('status'))}, "
            f"{_percent(snapshot.get('percent_complete'))} complete. "
            f"{snapshot.get('summary') or 'No source-backed summary yet.'} {ref}"
        )
        if snapshot.get("next_step"):
            lines.append(f"  - Next step: {snapshot['next_step']} {ref}")
        missing.extend(snapshot.get("missing_evidence") or [])
        for kr in (okr.get("key_results") or [])[:4]:
            kr_item = _context_item_for_key_result(okr, kr)
            items.append(kr_item)
            kr_ref = f"[ref:{kr_item['kind']}:{kr_item['id']}]"
            kr_snapshot = kr.get("snapshot") or {}
            lines.append(
                f"  - KR {kr['title']}: {_status_label(kr_snapshot.get('state') or kr.get('status'))}, "
                f"{_percent(kr_snapshot.get('percent_complete'))} complete. "
                f"{kr_snapshot.get('summary') or 'No source-backed KR summary yet.'} {kr_ref}"
            )
            if kr_snapshot.get("next_step"):
                lines.append(f"    - Next step: {kr_snapshot['next_step']} {kr_ref}")
            missing.extend(kr_snapshot.get("missing_evidence") or [])
            if wants_people:
                contribution_lines, contribution_missing, contribution_items = _contribution_lines(okr, kr, person_filter)
                lines.extend(contribution_lines)
                missing.extend(contribution_missing)
                items.extend(contribution_items)
        if wants_people:
            contribution_lines, contribution_missing, contribution_items = _contribution_lines(okr, None, person_filter)
            lines.extend(contribution_lines)
            missing.extend(contribution_missing)
            items.extend(contribution_items)

    if wants_people and not any("Contribution:" in line for line in lines):
        lines.append("- Contribution: I do not have source-backed contribution evidence for that person or OKR yet.")
        missing.append("No matching OKR contribution evidence found.")

    gap_line = "; ".join(dict.fromkeys(missing)) if missing else "nothing material"
    lines.extend(["", "Sources used:", f"Missing: {gap_line}"])
    return {
        "answer": "\n".join(lines),
        "sources": items,
        "trust_metadata": {
            "confidence_gaps": list(dict.fromkeys(missing))[:8],
            "okr_state_answer": True,
        },
    }




def _person_filter(question: str, okrs: list[dict]) -> str | None:
    clean = question.lower()
    names: list[str] = []
    for okr in okrs:
        contributions = list(okr.get("contributions") or [])
        for kr in okr.get("key_results") or []:
            contributions.extend(kr.get("contributions") or [])
        for contribution in contributions:
            name = contribution.get("person_name")
            if name and name not in names:
                names.append(name)
    matches = [name for name in names if name.lower() in clean]
    if len(matches) == 1:
        return matches[0]
    return None


def _contribution_lines(okr: dict, kr: dict | None, person_filter: str | None) -> tuple[list[str], list[str], list[dict]]:
    contributions = (kr if kr is not None else okr).get("contributions") or []
    if person_filter:
        contributions = [item for item in contributions if (item.get("person_name") or "").lower() == person_filter.lower()]
    lines: list[str] = []
    missing: list[str] = []
    items: list[dict] = []
    target = f" / {kr.get('title')}" if kr is not None else ""
    for contribution in contributions[:4]:
        person = contribution.get("person_name") or "Unknown"
        ref_id = f"okr-contribution-{contribution.get('id')}"
        ref = f"[ref:okr_context:{ref_id}]"
        right_work = (contribution.get("right_work_status") or "missing_evidence").replace("_", " ")
        summary = contribution.get("contribution_summary") or "No source-backed contribution summary yet."
        lines.append(
            f"  - Contribution: {person} on {okr.get('title')}{target}: {summary} "
            f"Right work: {right_work}. {ref}"
        )
        items.append(_context_item(
            item_id=ref_id,
            kind="okr_context",
            text=_join_context_lines([
                f"Objective: {okr.get('title')}",
                f"Key result: {kr.get('title') if kr is not None else 'objective-level'}",
                f"Person: {person}",
                f"Contribution: {summary}",
                f"Right work: {right_work}",
                f"Caveat: {contribution.get('caveat') or 'none'}",
                f"Review flags: {'; '.join(contribution.get('manager_review_flags') or []) or 'none'}",
            ]),
            refs=contribution.get("source_chips") or [],
        ))
        if contribution.get("caveat"):
            missing.append(contribution["caveat"])
        missing.extend(contribution.get("manager_review_flags") or [])
    return lines, missing, items

def okr_attention_brief_section(items: list[dict]) -> str:
    if not items:
        return ""
    lines = ["## OKRs needing attention"]
    for item in items[:6]:
        ref = f"[ref:{item['kind']}:{item['id']}]"
        lines.append(f"- Focus today: {item['focus']} {ref}")
        if item.get("changed"):
            lines.append(f"- What changed: {item['changed']} {ref}")
        if item.get("risk"):
            lines.append(f"- Blocked or at risk: {item['risk']} {ref}")
        if item.get("missing"):
            lines.append(f"- Confidence gap: {item['missing']} {ref}")
        if item.get("next_action"):
            lines.append(f"- Next action: {item['next_action']} {ref}")
    return "\n".join(lines).strip()


def _attention_item_for_objective(okr: dict) -> dict | None:
    snapshot = okr.get("snapshot") or {}
    if not _needs_attention(snapshot, okr):
        return None
    summary = snapshot.get("summary") or f"{okr['title']} needs attention."
    return {
        **_context_item_for_objective(okr),
        "focus": snapshot.get("next_step") or summary,
        "changed": snapshot.get("changed_reason") or snapshot.get("summary"),
        "risk": snapshot.get("summary") if (snapshot.get("state") or okr.get("status")) in ATTENTION_STATES else None,
        "missing": "; ".join(snapshot.get("missing_evidence") or []),
        "next_action": snapshot.get("next_step"),
    }


def _attention_item_for_key_result(okr: dict, kr: dict) -> dict | None:
    snapshot = kr.get("snapshot") or {}
    if not _needs_attention(snapshot, kr):
        return None
    summary = snapshot.get("summary") or f"{kr['title']} needs attention."
    return {
        **_context_item_for_key_result(okr, kr),
        "focus": snapshot.get("next_step") or summary,
        "changed": snapshot.get("changed_reason") or snapshot.get("summary"),
        "risk": snapshot.get("summary") if (snapshot.get("state") or kr.get("status")) in ATTENTION_STATES else None,
        "missing": "; ".join(snapshot.get("missing_evidence") or []),
        "next_action": snapshot.get("next_step"),
    }


def _context_item_for_objective(okr: dict) -> dict:
    snapshot = okr.get("snapshot") or {}
    text = _join_context_lines([
        f"Objective: {okr.get('title')}",
        f"Cycle: {okr.get('cycle') or 'unspecified'}",
        f"Status: {_status_label(snapshot.get('state') or okr.get('status'))}",
        f"Progress: {_percent(snapshot.get('percent_complete'))}",
        f"Summary: {snapshot.get('summary') or 'No source-backed summary.'}",
        f"What changed: {snapshot.get('changed_reason') or 'No source-backed change reason.'}",
        f"Next step: {snapshot.get('next_step') or 'No source-backed next step.'}",
        f"Missing evidence: {'; '.join(snapshot.get('missing_evidence') or []) or 'nothing material'}",
    ])
    return _context_item(
        item_id=f"okr-{okr['id']}",
        text=text,
        refs=(snapshot.get("source_chips") or okr.get("source_chips") or []),
    )


def _context_item_for_key_result(okr: dict, kr: dict) -> dict:
    snapshot = kr.get("snapshot") or {}
    text = _join_context_lines([
        f"Objective: {okr.get('title')}",
        f"Key result: {kr.get('title')}",
        f"Status: {_status_label(snapshot.get('state') or kr.get('status'))}",
        f"Progress: {_percent(snapshot.get('percent_complete'))}",
        f"Target: {_target(kr)}",
        f"Trackability: {(kr.get('planning') or {}).get('trackability_state') or 'unknown'}",
        f"Definition of done: {(kr.get('planning') or {}).get('definition_of_done') or 'missing'}",
        f"Summary: {snapshot.get('summary') or 'No source-backed summary.'}",
        f"What changed: {snapshot.get('changed_reason') or 'No source-backed change reason.'}",
        f"Next step: {snapshot.get('next_step') or 'No source-backed next step.'}",
        f"Missing evidence: {'; '.join(snapshot.get('missing_evidence') or []) or 'nothing material'}",
        f"Mapped work count: {len(kr.get('mappings') or [])}",
        f"Contribution count: {len(kr.get('contributions') or [])}",
    ])
    return _context_item(
        item_id=f"okr-kr-{kr['id']}",
        text=text,
        refs=(snapshot.get("source_chips") or kr.get("source_chips") or []),
    )


def _context_item(*, item_id: str, text: str, refs: list[dict], kind: str = "okr") -> dict:
    first = next((ref for ref in refs if ref.get("url")), {})
    return {
        "kind": kind,
        "id": item_id,
        "text": text,
        "source_url": first.get("url"),
        "source_title": first.get("label") or "OKR state",
        "source_type": first.get("source_type") or "okr",
        "priority": 1,
        "score": 1.0,
    }


def _select_okrs(question: str, okrs: list[dict]) -> list[dict]:
    terms = _tokens(question)
    scored = []
    for okr in okrs:
        haystack = " ".join([
            okr.get("title") or "",
            *(kr.get("title") or "" for kr in okr.get("key_results") or []),
        ]).lower()
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, okr))
    if scored:
        return [okr for _, okr in sorted(scored, key=lambda item: item[0], reverse=True)]
    return okrs


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 2}


def _needs_attention(snapshot: dict, obj: dict) -> bool:
    state = snapshot.get("state") or obj.get("status")
    return (
        state in ATTENTION_STATES
        or bool(snapshot.get("missing_evidence"))
        or bool(snapshot.get("next_step"))
    )


def _status_label(status: str | None) -> str:
    return (status or "unknown").replace("_", " ")


def _percent(value: Any) -> str:
    if value is None:
        return "unknown"
    try:
        return f"{round(float(value))}%"
    except (TypeError, ValueError):
        return "unknown"


def _target(kr: dict) -> str:
    current = kr.get("current_value")
    target = kr.get("target_value")
    unit = kr.get("unit") or ""
    if current is None and target is None:
        return "unknown"
    if target is None:
        return f"{current}{unit} current"
    if current is None:
        return f"{target}{unit} target"
    return f"{current}{unit} / {target}{unit}"


def _join_context_lines(lines: list[str]) -> str:
    return "\n".join(line for line in lines if line)
