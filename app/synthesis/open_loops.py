"""Deterministic projection helpers for open-loop brief sections."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from app.presentation.alerts import _source_label

_OPEN_LOOP_STATUS_ORDER = {"open": 0, "reopened": 1, "stale": 2}
_DROP_OR_DELEGATE_DAYS = 3
_ENERGY_AUDIT_MIN_OLD_ITEMS = 3
_ENERGY_AUDIT_MAX_OPEN_LOOPS = 5


def _parse_iso_date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _human_date(value: Any) -> str | None:
    parsed = _parse_iso_date(value)
    return parsed.strftime("%Y-%m-%d") if parsed else None


def _object_title(text: str) -> str:
    match = re.match(r"^\[(?P<kind>[^\]]+)\]\s*(?P<title>.+?)(?:\s+\(|$)", text or "")
    if match:
        return match.group("title").strip()
    return (text or "").strip()


def _loop_payload(item: dict[str, Any]) -> dict[str, Any] | None:
    if str(item.get("kind") or "") != "object":
        return None
    status = str(item.get("loop_status") or "").strip().lower()
    if status not in _OPEN_LOOP_STATUS_ORDER:
        return None
    opened_at = _parse_iso_date(item.get("loop_opened_at"))
    last_activity_at = _parse_iso_date(item.get("loop_last_activity_at") or item.get("source_observed_at"))
    days_open = max(0, (datetime.now(UTC) - opened_at).days) if opened_at else None
    days_silent = max(0, (datetime.now(UTC) - last_activity_at).days) if last_activity_at else None
    title = _object_title(str(item.get("text") or ""))
    state_bits: list[str] = [f"loop_status={status}"]
    owner = None
    for marker in re.finditer(r"\bowner=([^,\)]+)", str(item.get("text") or "")):
        owner = marker.group(1).strip()
        break
    if owner:
        state_bits.append(f"owner={owner}")
    if days_open is not None:
        state_bits.append(f"days_open={days_open}")
    if days_silent is not None:
        state_bits.append(f"days_silent={days_silent}")
    if opened_at:
        state_bits.append(f"opened={opened_at:%Y-%m-%d}")
    if last_activity_at:
        state_bits.append(f"last_activity={last_activity_at:%Y-%m-%d}")
    if item.get("loop_stale_after"):
        stale_after = _human_date(item.get("loop_stale_after"))
        if stale_after:
            state_bits.append(f"stale_after={stale_after}")
    if item.get("loop_reopened_count"):
        state_bits.append(f"reopened_count={int(item.get('loop_reopened_count') or 0)}")
    score = (
        float(days_open or 0)
        + float(days_silent or 0)
        + (2.0 if status == "stale" else 1.0 if status == "reopened" else 0.0)
    )
    if item.get("days_silent") is not None:
        score += float(item.get("days_silent") or 0) * 0.25
    return {
        "kind": "object",
        "id": str(item.get("id")),
        "title": title,
        "owner": owner,
        "status": status,
        "days_open": days_open,
        "days_silent": days_silent,
        "text": str(item.get("text") or "").strip(),
        "line": f"{title} ({', '.join(state_bits)})",
        "source_url": item.get("source_url"),
        "source_ref": f"object:{item.get('id')}",
            "source_chips": [
                {
                    "label": _source_label(item.get("source_url"), item.get("source_type")),
                    "url": item.get("source_url"),
                    "ref": f"object:{item.get('id')}",
                    "source_id": item.get("source_id"),
                "source_external_id": item.get("source_external_id"),
                "source_identity_hints": item.get("source_identity_hints") or [],
            }
        ] if item.get("source_url") else [],
        "source_refs": [{"ref": f"object:{item.get('id')}"}],
        "score": score,
    }


def open_loop_payloads(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payloads = [payload for item in items if (payload := _loop_payload(item))]
    payloads.sort(
        key=lambda payload: (
            -float(payload["score"]),
            -int(payload["days_open"] or 0),
            str(payload["title"]).lower(),
        )
    )
    return payloads


def project_open_loop_sections(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payloads = open_loop_payloads(items)
    if not payloads:
        return []

    kill_list_items = [
        {
            "signal_id": None,
            "work_signal_ids": [],
            "person_signal_score_id": None,
            "workstream_id": None,
            "workstream": None,
            "text": f"{payload['line']} [ref:{payload['source_ref']}]",
            "label": None,
            "value": f"{payload['line']} [ref:{payload['source_ref']}]",
            "source_refs": [{"url": payload["source_url"], "ref": payload["source_ref"]}] if payload.get("source_url") else [{"ref": payload["source_ref"]}],
            "source_chips": payload["source_chips"],
            "occurred_at": None,
            "has_confidence_gap": False,
            "passes_quality_eval": True,
            "route_decision": "brief",
            "route_reason": "open loop surfaced for operating attention",
            "relevance_score": float(payload.get("score") or 0.0),
        }
        for payload in payloads
    ]

    sections: list[dict] = [
        {
            "title": "Kill list",
            "kind": "risk",
            "items": kill_list_items,
            "source_chips": [chip for payload in payloads for chip in payload["source_chips"]],
        }
    ]

    drop_or_delegate_items = [
        {
            "text": f"{payload['line']} [ref:{payload['source_ref']}]",
            "label": None,
            "value": f"{payload['line']} [ref:{payload['source_ref']}]",
            "source_refs": [{"url": payload["source_url"], "ref": payload["source_ref"]}] if payload.get("source_url") else [{"ref": payload["source_ref"]}],
            "source_chips": payload["source_chips"],
            "signal_id": None,
            "work_signal_ids": [],
            "person_signal_score_id": None,
            "workstream_id": None,
            "workstream": None,
            "occurred_at": None,
            "has_confidence_gap": False,
            "passes_quality_eval": True,
            "route_decision": "brief",
            "route_reason": "open loop carried 3+ days",
            "relevance_score": float(payload.get("score") or 0.0),
        }
        for payload in payloads
        if (payload.get("days_open") or 0) >= _DROP_OR_DELEGATE_DAYS
    ]
    if drop_or_delegate_items:
        sections.append(
            {
                "title": "Drop or delegate",
                "kind": "risk",
                "items": drop_or_delegate_items,
                "source_chips": [chip for payload in payloads if (payload.get("days_open") or 0) >= _DROP_OR_DELEGATE_DAYS for chip in payload["source_chips"]],
            }
        )

    audit_candidates = [
        payload
        for payload in payloads
        if (payload.get("days_open") or 0) >= _DROP_OR_DELEGATE_DAYS
    ]
    if len(audit_candidates) >= _ENERGY_AUDIT_MIN_OLD_ITEMS or len(payloads) > _ENERGY_AUDIT_MAX_OPEN_LOOPS:
        sections.append(
            {
                "title": "Energy audit",
                "kind": "risk",
                "items": [
                    {
                        "text": (
                            f"{len(audit_candidates)} open loops have carried for 3+ days and "
                            f"{len(payloads)} open loops are active overall"
                        ),
                        "label": None,
                        "value": (
                            f"{len(audit_candidates)} open loops have carried for 3+ days and "
                            f"{len(payloads)} open loops are active overall"
                        ),
                        "source_refs": [
                            {"url": payload["source_url"], "ref": payload["source_ref"]}
                            for payload in audit_candidates
                            if payload.get("source_url")
                        ],
                        "source_chips": [chip for payload in audit_candidates for chip in payload["source_chips"]],
                        "signal_id": None,
                        "work_signal_ids": [],
                        "person_signal_score_id": None,
                        "workstream_id": None,
                        "workstream": None,
                        "occurred_at": None,
                        "has_confidence_gap": False,
                        "passes_quality_eval": True,
                        "route_decision": "brief",
                        "route_reason": "energy audit for accumulated open loops",
                        "relevance_score": float(len(audit_candidates)),
                    }
                ],
                "source_chips": [chip for payload in audit_candidates for chip in payload["source_chips"]],
            }
        )

    return sections
