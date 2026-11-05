"""
OKR trajectory drift: deterministic expected-vs-actual pace check.

For each KR with milestones and a final_deadline, compares how many
milestones SHOULD be done by today (due date on or before today) against
how many actually are, and projects a landing date from current
completion velocity. When the pace implies landing past the deadline,
emits a drift alert through the same evidence-verification, merge, and
review pipeline as LLM-driven drift synthesis (`app.synthesis.drift`) --
no LLM call, since this is deterministic math over milestone due dates
and status scores that `presentation/okrs.py` already tracks.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import structlog

from app.core.db import acquire
from app.core import trace
from app.core.tenant_settings import client_publishing_paused, synthesis_paused
from app.synthesis.brief import _tenant_today
from app.presentation.okrs import MILESTONE_STATUS_SCORES, _coerce_date, _milestone_due_date, _normalize_status
from app.synthesis.drift import _autopublish_read_only, _company_principals, _publish_autopublished_alert, _upsert_alert_issue
from app.synthesis.verifier import verify_evidence

logger = structlog.get_logger()

DRIFT_TYPE = "deadline_risk"
DONE_SCORE = 100.0


async def run_okr_pace_check(tenant_id: str, *, pipeline_run_id: str | None = None) -> list[str]:
    """Check every KR with milestones + a final_deadline for the tenant.
    Emits one alert per behind-pace KR through the normal drift pipeline.
    Returns the ids of alerts created or merged into."""
    from app.core.db import _current_tenant_id
    _current_tenant_id.set(tenant_id)

    async with acquire() as conn:
        tenant_settings_raw = await conn.fetchval("SELECT settings FROM tenants WHERE id = $1", tenant_id)
        kr_rows = await conn.fetch(
            """
            SELECT kr.id, kr.title, kr.final_deadline, kr.okr_id, o.title AS okr_title
            FROM key_results kr
            JOIN okrs o ON o.id = kr.okr_id
            WHERE kr.tenant_id = $1 AND kr.final_deadline IS NOT NULL
            """,
            tenant_id,
        )
        if not kr_rows:
            return []
        milestone_rows = await conn.fetch(
            """
            SELECT key_result_id, title, due_date, status
            FROM okr_milestones
            WHERE tenant_id = $1 AND key_result_id = ANY($2::uuid[])
            """,
            tenant_id,
            [row["id"] for row in kr_rows],
        )
    tenant_settings = (
        json.loads(tenant_settings_raw) if isinstance(tenant_settings_raw, str) else dict(tenant_settings_raw or {})
    )
    if synthesis_paused(tenant_settings):
        return []

    milestones_by_kr: dict[str, list[dict]] = {}
    for row in milestone_rows:
        milestones_by_kr.setdefault(str(row["key_result_id"]), []).append(dict(row))

    today = _tenant_today(tenant_settings)
    run_id = pipeline_run_id or await trace.start_run(tenant_id=tenant_id, kind="okr_pace")
    alert_ids: list[str] = []
    try:
        for kr in kr_rows:
            kr_id = str(kr["id"])
            final_deadline = _coerce_date(kr["final_deadline"])
            milestones = milestones_by_kr.get(kr_id) or []
            if not final_deadline or not milestones:
                continue
            verdict = _pace_verdict(milestones, final_deadline, today)
            if verdict is None:
                continue
            alert_id = await _emit_pace_alert(
                tenant_id=tenant_id,
                run_id=run_id,
                tenant_settings=tenant_settings,
                okr_title=kr["okr_title"] or "Objective",
                kr_id=kr_id,
                kr_title=kr["title"] or "Key result",
                verdict=verdict,
            )
            if alert_id:
                alert_ids.append(alert_id)
        await trace.finish_run(run_id, status="delivered" if alert_ids else "suppressed")
    except Exception as e:
        await trace.finish_run(run_id, status="failed", error=f"{type(e).__name__}: {e}")
        raise
    return alert_ids


async def summarize_okr_pace_items(tenant_id: str) -> list[dict]:
    """Deterministic OKR trajectory summary for weekly briefs.

    This uses the same milestone math as `run_okr_pace_check` but returns
    display items instead of writing alerts.
    """
    from app.core.db import _current_tenant_id

    _current_tenant_id.set(tenant_id)

    async with acquire() as conn:
        tenant_settings_raw = await conn.fetchval("SELECT settings FROM tenants WHERE id = $1", tenant_id)
        kr_rows = await conn.fetch(
            """
            SELECT kr.id, kr.title, kr.final_deadline, kr.okr_id, o.title AS okr_title
            FROM key_results kr
            JOIN okrs o ON o.id = kr.okr_id
            WHERE kr.tenant_id = $1 AND kr.final_deadline IS NOT NULL
            ORDER BY o.sort_order, kr.sort_order, kr.updated_at DESC, kr.title
            """,
            tenant_id,
        )
        if not kr_rows:
            return []
        milestone_rows = await conn.fetch(
            """
            SELECT key_result_id, title, due_date, status
            FROM okr_milestones
            WHERE tenant_id = $1 AND key_result_id = ANY($2::uuid[])
            """,
            tenant_id,
            [row["id"] for row in kr_rows],
        )
    tenant_settings = (
        json.loads(tenant_settings_raw) if isinstance(tenant_settings_raw, str) else dict(tenant_settings_raw or {})
    )
    if synthesis_paused(tenant_settings):
        return []

    milestones_by_kr: dict[str, list[dict]] = {}
    for row in milestone_rows:
        milestones_by_kr.setdefault(str(row["key_result_id"]), []).append(dict(row))

    today = _tenant_today(tenant_settings)
    items: list[dict] = []
    for kr in kr_rows:
        kr_id = str(kr["id"])
        final_deadline = _coerce_date(kr["final_deadline"])
        milestones = milestones_by_kr.get(kr_id) or []
        snapshot = _pace_snapshot(milestones, final_deadline, today)
        if snapshot is None:
            continue
        items.append(_pace_summary_item(
            okr_title=kr["okr_title"] or "Objective",
            kr_id=kr_id,
            kr_title=kr["title"] or "Key result",
            snapshot=snapshot,
            allowed_principals=[f"company:{tenant_id}"],
        ))
    return items


def _pace_snapshot(milestones: list[dict], final_deadline: date, today: date) -> dict | None:
    """Return pace math for a KR, including on-pace and behind-pace states."""
    dated = sorted(
        ((m, _milestone_due_date(m)) for m in milestones),
        key=lambda pair: pair[1] or date.max,
    )
    dated = [(m, d) for m, d in dated if d is not None]
    if not dated:
        return None

    total = len(dated)
    done_count = sum(1 for m, _ in dated if _milestone_score(m.get("status")) == DONE_SCORE)
    expected_done = sum(1 for _, d in dated if d <= today)
    if expected_done == 0 or done_count >= expected_done:
        return {
            "total": total,
            "done_count": done_count,
            "expected_done": expected_done,
            "final_deadline": final_deadline,
            "projected_landing": final_deadline,
            "days_late": 0,
            "severity": "low",
            "behind_pace": False,
        }

    first_due = dated[0][1]
    if final_deadline <= first_due:
        return None  # degenerate window; not enough runway to project against

    elapsed_days = (today - first_due).days
    if elapsed_days <= 0:
        return None

    velocity = done_count / elapsed_days  # completions per day
    remaining = total - done_count
    if velocity <= 0:
        projected_landing = None  # behind pace with zero completions: can't project a date
    else:
        projected_landing = today + timedelta(days=round(remaining / velocity))

    behind_pace = projected_landing is None or projected_landing > final_deadline
    if not behind_pace:
        return {
            "total": total,
            "done_count": done_count,
            "expected_done": expected_done,
            "final_deadline": final_deadline,
            "projected_landing": projected_landing,
            "days_late": 0,
            "severity": "low",
            "behind_pace": False,
        }

    days_late = (projected_landing - final_deadline).days if projected_landing else None
    return {
        "total": total,
        "done_count": done_count,
        "expected_done": expected_done,
        "final_deadline": final_deadline,
        "projected_landing": projected_landing,
        "days_late": days_late,
        "severity": _severity_for(days_late),
        "behind_pace": True,
    }


def _pace_verdict(milestones: list[dict], final_deadline: date, today: date) -> dict | None:
    snapshot = _pace_snapshot(milestones, final_deadline, today)
    if not snapshot or not snapshot.get("behind_pace"):
        return None
    return snapshot


def _severity_for(days_late: int | None) -> str:
    if days_late is None or days_late > 14:
        return "high"
    return "medium"


def _milestone_score(status: str) -> float | None:
    return MILESTONE_STATUS_SCORES.get(_normalize_status(status))


async def _emit_pace_alert(
    *,
    tenant_id: str,
    run_id: str,
    tenant_settings: dict,
    okr_title: str,
    kr_id: str,
    kr_title: str,
    verdict: dict,
) -> str | None:
    pack_item = _pace_pack_item(okr_title=okr_title, kr_id=kr_id, kr_title=kr_title, verdict=verdict)
    ref = f"{pack_item['kind']}:{pack_item['id']}"
    evidence = [{"ref": ref, "quote": pack_item["text"].splitlines()[-1]}]
    verification = verify_evidence(evidence, [pack_item])
    if not verification["valid"]:
        # Should not happen (the quote is copied verbatim from the item we just
        # built), but fail closed like the LLM drift path does rather than
        # publish an unverifiable claim.
        logger.error("okr_pace_evidence_invalid", tenant_id=tenant_id, kr_id=kr_id)
        return None

    landing_text = (
        verdict["projected_landing"].isoformat() if verdict["projected_landing"] else "no projected landing date"
    )
    parsed = {
        "decision": "alert",
        "drift_type": DRIFT_TYPE,
        "severity": verdict["severity"],
        "confidence": 0.9,
        "claim": f"{kr_title} is behind pace to land by its {verdict['final_deadline'].isoformat()} deadline.",
        "why_now": (
            f"{verdict['done_count']} of {verdict['total']} milestones are done, but {verdict['expected_done']} "
            f"were due on or before today. At the current completion rate, {kr_title} projects to land "
            f"{landing_text}, past the {verdict['final_deadline'].isoformat()} deadline for \"{okr_title}\"."
        ),
        "evidence": evidence,
        "missing_evidence": [],
        "recommended_action": f"Review {kr_title}'s remaining milestones and either re-baseline the deadline or unblock the owner.",
        "related": {"people": [], "projects": [okr_title], "commitments": []},
    }

    kind = "alert"
    autopublish = _autopublish_read_only(tenant_settings) and not client_publishing_paused(tenant_settings)
    status = "publishing" if autopublish else "pending_review"
    evidence_out = verification["verified"] + verification["flagged"]

    async with acquire() as conn:
        output = await _upsert_alert_issue(
            conn,
            tenant_id=tenant_id,
            kind=kind,
            status=status,
            parsed=parsed,
            evidence=evidence_out,
            context_pack_id=None,
            model_call_id=None,
            pipeline_run_id=run_id,
        )
    alert_id = output["alert_id"]

    if autopublish and (not output["merged"] or output["status"] == "delivered"):
        receivers = await _publish_autopublished_alert(tenant_id=tenant_id, alert_id=alert_id)
        if receivers is None:
            if output["status"] == "publishing":
                async with acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE alerts SET status = 'delivery_blocked'
                        WHERE id = $1 AND tenant_id = $2 AND status = 'publishing'
                        """,
                        alert_id, tenant_id,
                    )
        elif output["status"] == "publishing":
            async with acquire() as conn:
                await conn.execute(
                    """
                    UPDATE alerts SET status = 'delivered', delivered_via = delivered_via || $3::jsonb
                    WHERE id = $1 AND tenant_id = $2 AND status = 'publishing'
                    """,
                    alert_id, tenant_id, json.dumps(["app_auto"]),
                )

    logger.info(
        "okr_pace_alert", tenant_id=tenant_id, alert_id=alert_id, kr_id=kr_id,
        merged=output["merged"], status=output["status"],
    )
    return alert_id


def _pace_pack_item(*, okr_title: str, kr_id: str, kr_title: str, verdict: dict) -> dict:
    landing_text = (
        verdict["projected_landing"].isoformat() if verdict["projected_landing"] else "no projected landing date at current pace"
    )
    verdict_line = (
        f"Pace verdict: {verdict['done_count']}/{verdict['total']} milestones done "
        f"({verdict['expected_done']} due by today), projected landing {landing_text}, "
        f"deadline {verdict['final_deadline'].isoformat()}."
    )
    text = "\n".join([
        f"Objective: {okr_title}",
        f"Key result: {kr_title}",
        verdict_line,
    ])
    return {
        "kind": "okr_context",
        "id": f"okr-pace-{kr_id}",
        "title": f"OKR trajectory · {kr_title}",
        "text": text,
        "source_url": None,
        "source_title": "OKR trajectory",
        "source_type": "okr",
    }


def _pace_summary_item(*, okr_title: str, kr_id: str, kr_title: str, snapshot: dict, allowed_principals: list[str]) -> dict:
    landing_text = (
        snapshot["projected_landing"].isoformat() if snapshot["projected_landing"] else "no projected landing date"
    )
    if snapshot.get("behind_pace"):
        line = (
            f"{kr_title}: behind pace, {snapshot['done_count']}/{snapshot['total']} milestones done "
            f"({snapshot['expected_done']} due by today), projected landing {landing_text}, "
            f"deadline {snapshot['final_deadline'].isoformat()}."
        )
    else:
        line = (
            f"{kr_title}: on pace, {snapshot['done_count']}/{snapshot['total']} milestones done "
            f"({snapshot['expected_done']} due by today), deadline {snapshot['final_deadline'].isoformat()}."
        )
    text = "\n".join([
        f"Objective: {okr_title}",
        f"Key result: {kr_title}",
        line,
    ])
    return {
        "kind": "okr_context",
        "id": f"okr-pace-{kr_id}",
        "title": f"OKR trajectory · {kr_title}",
        "text": text,
        "source_url": None,
        "source_title": "OKR trajectory",
        "source_type": "okr",
        "visibility": "company",
        "allowed_principals": allowed_principals,
        "acl_state": "stamped",
        "acl_freshness_state": "fresh",
    }
