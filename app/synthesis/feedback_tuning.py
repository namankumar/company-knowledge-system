"""Tenant-scoped alert-feedback tuning for drift synthesis."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

from app.core.db import acquire, _current_tenant_id, _rls_bypass
from app.core.tenant_settings import get_tenant_settings, synthesis_paused

DEFAULT_PUBLISH_BAR = 0.0
TARGET_PRECISION = 0.55
MIN_SAMPLES = 8
LOW_PRECISION_PUBLISH_BAR = 0.30
SEVERE_PRECISION_PUBLISH_BAR = 0.60
SEVERE_PRECISION_LB = 0.05
MAX_PUBLISH_BAR = 0.95
WINDOW_DAYS = 30
REJECT_RECENCY_DAYS = 14
Z_90 = 1.6448536269514722
REJECT_FEEDBACK_VALUES = {"wrong"}
NOISY_FEEDBACK_VALUES = {"noisy", "too_noisy"}
COUNTED_FEEDBACK_VALUES = {"useful", *REJECT_FEEDBACK_VALUES, *NOISY_FEEDBACK_VALUES}


def _loads(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


def wilson_lower_bound(*, positives: int, total: int, z: float = Z_90) -> float:
    if total <= 0:
        return 0.0
    phat = positives / total
    denom = 1 + (z * z / total)
    center = phat + (z * z / (2 * total))
    margin = z * math.sqrt((phat * (1 - phat) / total) + (z * z / (4 * total * total)))
    return max(0.0, min(1.0, (center - margin) / denom))


def next_publish_bar(
    *,
    current_bar: float,
    useful_count: int,
    wrong_count: int,
    noisy_count: int,
) -> tuple[float, float, float, int, str]:
    sample_size = max(0, useful_count + wrong_count + noisy_count)
    precision = (useful_count / sample_size) if sample_size else 0.0
    precision_lb = wilson_lower_bound(positives=useful_count, total=sample_size)
    if sample_size < MIN_SAMPLES:
        return (
            DEFAULT_PUBLISH_BAR,
            precision,
            precision_lb,
            sample_size,
            f"sample size {sample_size} below floor {MIN_SAMPLES}; publish bar reset",
        )
    if precision_lb < SEVERE_PRECISION_LB:
        next_bar = min(MAX_PUBLISH_BAR, SEVERE_PRECISION_PUBLISH_BAR)
        return (
            next_bar,
            precision,
            precision_lb,
            sample_size,
            f"precision {precision:.2f} (lb {precision_lb:.2f}) over {sample_size} votes -> publish bar {next_bar:.2f}",
        )
    if precision_lb < TARGET_PRECISION:
        next_bar = min(MAX_PUBLISH_BAR, LOW_PRECISION_PUBLISH_BAR)
        return (
            next_bar,
            precision,
            precision_lb,
            sample_size,
            f"precision {precision:.2f} (lb {precision_lb:.2f}) over {sample_size} votes -> publish bar {next_bar:.2f}",
        )
    return (
        DEFAULT_PUBLISH_BAR,
        precision,
        precision_lb,
        sample_size,
        f"precision {precision:.2f} (lb {precision_lb:.2f}) over {sample_size} votes -> publish bar reset",
    )


async def recent_rejected_claims(*, tenant_id: str, drift_type: str | None = None, limit: int = 3) -> list[dict]:
    _current_tenant_id.set(tenant_id)
    cutoff = datetime.now(timezone.utc) - timedelta(days=REJECT_RECENCY_DAYS)
    query = """
        SELECT id, drift_type, claim, why_now, feedback_note, feedback_at
        FROM alerts
        WHERE tenant_id = $1
          AND kind = 'alert'
          AND status = 'delivered'
          AND feedback = ANY($2::text[])
          AND feedback_at >= $3
    """
    args: list = [tenant_id, list(REJECT_FEEDBACK_VALUES), cutoff]
    if drift_type:
        query += " AND drift_type = $4"
        args.append(drift_type)
        limit_param = 5
    else:
        limit_param = 4
    query += f" ORDER BY feedback_at DESC NULLS LAST, created_at DESC LIMIT ${limit_param}"
    args.append(limit)
    async with acquire() as conn:
        rows = await conn.fetch(query, *args)
    out = []
    for row in rows:
        item = dict(row)
        out.append({
            "id": str(item["id"]),
            "drift_type": item.get("drift_type"),
            "claim": item.get("claim") or "",
            "why_now": item.get("why_now") or "",
            "feedback_note": item.get("feedback_note") or "",
            "feedback_at": item["feedback_at"].isoformat() if item.get("feedback_at") else None,
        })
    return out


def render_recent_rejected_block(claims: list[dict]) -> str:
    if not claims:
        return "None."
    lines = [
        f"Only use these as negative examples when the current signal matches the same drift_type. Ignore examples from other drift types. Examples older than {REJECT_RECENCY_DAYS} days are excluded."
    ]
    grouped: dict[str, list[str]] = {}
    for claim in claims:
        drift_type = str(claim.get("drift_type") or "unknown")
        grouped.setdefault(drift_type, []).append(claim.get("claim") or claim.get("why_now") or "rejected alert")
    for drift_type in sorted(grouped):
        lines.append(f"{drift_type}:")
        lines.extend(f"- {text}" for text in grouped[drift_type])
    return "\n".join(lines)


async def min_confidence_to_publish(*, tenant_id: str, drift_type: str | None) -> float:
    if not drift_type:
        return DEFAULT_PUBLISH_BAR
    _current_tenant_id.set(tenant_id)
    async with acquire() as conn:
        value = await conn.fetchval(
            """
            SELECT min_confidence_to_publish
            FROM drift_tuning
            WHERE tenant_id = $1 AND drift_type = $2
            """,
            tenant_id,
            drift_type,
        )
    return float(value or DEFAULT_PUBLISH_BAR)


async def list_drift_tuning(*, tenant_id: str) -> list[dict]:
    _current_tenant_id.set(tenant_id)
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT drift_type, useful_count, wrong_count, noisy_count, sample_size,
                   precision, precision_lb, min_confidence_to_publish, bias_reason,
                   computed_at, updated_by
            FROM drift_tuning
            WHERE tenant_id = $1
            ORDER BY min_confidence_to_publish DESC, sample_size DESC, drift_type ASC
            """,
            tenant_id,
        )
    return [
        {
            **dict(row),
            "precision": float(row["precision"] or 0),
            "precision_lb": float(row["precision_lb"] or 0),
            "min_confidence_to_publish": float(row["min_confidence_to_publish"] or 0),
            "computed_at": row["computed_at"].isoformat() if row.get("computed_at") else None,
        }
        for row in rows
    ]


async def recompute_tenant_tuning(*, tenant_id: str, updated_by: str = "feedback_tuning_cron") -> list[dict]:
    _current_tenant_id.set(tenant_id)
    tenant_settings = await get_tenant_settings(tenant_id)
    if synthesis_paused(tenant_settings):
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    async with acquire() as conn:
        vote_rows = await conn.fetch(
            """
            SELECT drift_type, feedback
            FROM alerts
            WHERE tenant_id = $1
              AND kind = 'alert'
              AND status = 'delivered'
              AND feedback_at >= $2
              AND feedback = ANY($3::text[])
              AND drift_type IS NOT NULL
            """,
            tenant_id,
            cutoff,
            list(COUNTED_FEEDBACK_VALUES),
        )
        existing_rows = await conn.fetch(
            "SELECT drift_type, min_confidence_to_publish FROM drift_tuning WHERE tenant_id = $1",
            tenant_id,
        )
        counts: dict[str, dict[str, int]] = {}
        for row in vote_rows:
            drift_type = str(row["drift_type"] or "")
            if not drift_type:
                continue
            bucket = counts.setdefault(drift_type, {"useful": 0, "wrong": 0, "noisy": 0})
            feedback = str(row["feedback"] or "")
            if feedback == "useful":
                bucket["useful"] += 1
            elif feedback in REJECT_FEEDBACK_VALUES:
                bucket["wrong"] += 1
            elif feedback in NOISY_FEEDBACK_VALUES:
                bucket["noisy"] += 1
        current_bars = {str(row["drift_type"]): float(row["min_confidence_to_publish"] or 0) for row in existing_rows}
        drift_types = sorted(set(counts.keys()) | set(current_bars.keys()))
        results = []
        for drift_type in drift_types:
            bucket = counts.get(drift_type, {"useful": 0, "wrong": 0, "noisy": 0})
            next_bar, precision, precision_lb, sample_size, reason = next_publish_bar(
                current_bar=current_bars.get(drift_type, DEFAULT_PUBLISH_BAR),
                useful_count=bucket["useful"],
                wrong_count=bucket["wrong"],
                noisy_count=bucket["noisy"],
            )
            await conn.execute(
                """
                INSERT INTO drift_tuning (
                    tenant_id, drift_type, useful_count, wrong_count, noisy_count,
                    sample_size, precision, precision_lb, min_confidence_to_publish,
                    bias_reason, computed_at, updated_by
                )
                VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9,
                    $10, now(), $11
                )
                ON CONFLICT (tenant_id, drift_type) DO UPDATE
                SET useful_count = EXCLUDED.useful_count,
                    wrong_count = EXCLUDED.wrong_count,
                    noisy_count = EXCLUDED.noisy_count,
                    sample_size = EXCLUDED.sample_size,
                    precision = EXCLUDED.precision,
                    precision_lb = EXCLUDED.precision_lb,
                    min_confidence_to_publish = EXCLUDED.min_confidence_to_publish,
                    bias_reason = EXCLUDED.bias_reason,
                    computed_at = EXCLUDED.computed_at,
                    updated_by = EXCLUDED.updated_by
                """,
                tenant_id,
                drift_type,
                bucket["useful"],
                bucket["wrong"],
                bucket["noisy"],
                sample_size,
                precision,
                precision_lb,
                next_bar,
                reason,
                updated_by,
            )
            results.append({
                "drift_type": drift_type,
                "useful_count": bucket["useful"],
                "wrong_count": bucket["wrong"],
                "noisy_count": bucket["noisy"],
                "sample_size": sample_size,
                "precision": precision,
                "precision_lb": precision_lb,
                "min_confidence_to_publish": next_bar,
                "bias_reason": reason,
                "updated_by": updated_by,
            })
    return results


async def feedback_tuning_cycle(ctx: dict) -> None:
    bypass = _rls_bypass.set(True)
    try:
        async with acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT tenant_id
                FROM (
                    SELECT tenant_id FROM alerts
                    WHERE kind = 'alert'
                      AND feedback = ANY($1::text[])
                    UNION
                    SELECT tenant_id FROM drift_tuning
                ) q
                """,
                list(COUNTED_FEEDBACK_VALUES),
            )
    finally:
        _rls_bypass.reset(bypass)
    for row in rows:
        tenant_id = str(row["tenant_id"])
        _current_tenant_id.set(tenant_id)
        await recompute_tenant_tuning(tenant_id=tenant_id)
