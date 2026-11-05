"""Deterministic noise suppression before model spend.

Raw and normalized events remain stored for audit/replay. Suppressed events do
not become retrieval chunks or brain updates unless a future replay removes the
matching rule.
"""
from dataclasses import dataclass
import re

from app.core.db import acquire
from app.ingestion.normalizers import NormalizedEvent


SCHEDULING_PATTERNS = [
    re.compile(r"\bcal\.ai\b", re.I),
    re.compile(r"\bcal\.com\b", re.I),
    re.compile(r"\bcalendly\.com\b", re.I),
    re.compile(r"\bhas booked a meeting\b", re.I),
    re.compile(r"\bnew booking\b", re.I),
    re.compile(r"\bbooking confirmed\b", re.I),
]

OPERATIONAL_TERMS = re.compile(
    r"\b(customer|commitment|pilot|launch|deadline|blocked|blocker|risk|approval|okr|"
    r"project|slipped|delayed|cancelled|canceled|rescheduled)\b",
    re.I,
)


@dataclass
class NoiseDecision:
    suppressed: bool
    rule_type: str | None = None
    value: str | None = None
    reason: str | None = None


async def should_suppress_event(tenant_id: str, event: NormalizedEvent) -> NoiseDecision:
    built_in = _built_in_scheduling_noise(event)
    if built_in.suppressed:
        return built_in

    rules = await _active_rules(tenant_id)
    sender = _actor_email(event)
    domain = sender.split("@", 1)[1] if sender and "@" in sender else None
    text = event.text.lower()

    for rule in rules:
        rule_type = str(rule["rule_type"])
        value = str(rule["value"]).lower()
        if rule_type == "sender" and sender and sender == value:
            return NoiseDecision(True, rule_type, value, str(_row_value(rule, "reason") or "ignored sender"))
        if rule_type == "domain" and domain and domain == value:
            return NoiseDecision(True, rule_type, value, str(_row_value(rule, "reason") or "ignored domain"))
        if rule_type == "pattern" and value and value in text:
            return NoiseDecision(True, rule_type, value, str(_row_value(rule, "reason") or "ignored pattern"))
    return NoiseDecision(False)


async def muted_workstream_keys(tenant_id: str) -> set[str]:
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT value FROM feedback_rules
            WHERE tenant_id = $1 AND active AND rule_type = 'workstream'
            """,
            tenant_id,
        )
    return {str(row["value"]) for row in rows}


async def muted_drift_types(tenant_id: str) -> set[str]:
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT value FROM feedback_rules
            WHERE tenant_id = $1 AND active AND rule_type = 'drift_type'
            """,
            tenant_id,
        )
    return {str(row["value"]) for row in rows}


async def _active_rules(tenant_id: str):
    async with acquire() as conn:
        return await conn.fetch(
            """
            SELECT rule_type, value, reason FROM feedback_rules
            WHERE tenant_id = $1 AND active AND rule_type IN ('sender', 'domain', 'pattern')
            ORDER BY created_at DESC
            """,
            tenant_id,
        )


def _built_in_scheduling_noise(event: NormalizedEvent) -> NoiseDecision:
    if event.source_type != "gmail":
        return NoiseDecision(False)
    haystack = f"{event.actor_label or ''}\n{event.text}".lower()
    if OPERATIONAL_TERMS.search(haystack):
        return NoiseDecision(False)
    for pattern in SCHEDULING_PATTERNS:
        match = pattern.search(haystack)
        if match:
            return NoiseDecision(
                True,
                "builtin",
                match.group(0).lower(),
                "scheduling email suppressed before LLM triage",
            )
    return NoiseDecision(False)


def _actor_email(event: NormalizedEvent) -> str | None:
    actor = event.actor_label or ""
    if actor.startswith("email:"):
        return actor.removeprefix("email:").lower()
    for entity in event.entities:
        if entity.get("is_actor") and entity.get("handle_type") == "email" and entity.get("handle"):
            return str(entity["handle"]).lower()
    return None


def _row_value(row, key: str):
    try:
        return row[key]
    except (KeyError, TypeError):
        return None
