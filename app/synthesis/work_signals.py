"""Shadow WorkSignal classifier for Brief and Feed.

The P0 path is deterministic on purpose. It gives us a replayable middle
layer before we let LLMs write user-facing Brief or Feed items.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
import json
import re
from typing import Any
from uuid import UUID

from app.ingestion.normalizers import NormalizedEvent
from app.workstreams.attach import resolve_signal_attachment, sync_persisted_member_attachment


CLASSIFIER_VERSION = "work-signal-shadow-v0"

COVERAGE_SCAN = "coverage_scan"
RELEVANCE_PACK = "relevance_pack"
PROJECTION_PACK = "projection_pack"
ASK_ANSWER_PACK = "ask_answer_pack"

SIGNAL_TYPES = {
    "needs_user",
    "blocked",
    "priority_shift",
    "plan_reality_mismatch",
    "deadline_risk",
    "unclear_owner",
    "decision_made",
    "decision_needed",
    "customer_impact",
    "stale_work",
    "quality_or_release_risk",
    "people_or_process_signal",
    "progress_update",
    "new_possible_workstream",
    "possible_risk",
    "noise",
}

ROUTE_HINTS = {"brief", "feed", "ask_only", "suppress"}
UNKNOWN_STATE_SUBTYPES = {
    "not_enough_access",
    "not_enough_recent_evidence",
    "nothing_detected",
    "conflicting_evidence",
    "mapping_uncertain",
}


@dataclass(frozen=True)
class WorkSignalCandidate:
    tenant_id: str | None
    signal_type: str
    title: str
    summary: str
    evidence_quote: str | None
    source_refs: list[dict[str, Any]]
    source_event_id: str | None = None
    normalized_event_id: str | None = None
    context_pack_id: str | None = None
    signal_subtype: str | None = None
    related_object_ids: list[str] = field(default_factory=list)
    candidate_object_ids: list[str] = field(default_factory=list)
    related_people: list[dict[str, Any]] = field(default_factory=list)
    actor_label: str | None = None
    occurred_at: datetime | None = None
    confidence: float = 0.0
    urgency_score: float = 0.0
    actionability_score: float = 0.0
    awareness_score: float = 0.0
    novelty_score: float = 0.0
    retrieval_strategy: str = COVERAGE_SCAN
    route_hint: str = "ask_only"
    status: str = "candidate"
    classifier_version: str = CLASSIFIER_VERSION
    route_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    acl_state: str = "unstamped"
    acl_freshness_state: str = "unknown"
    allowed_principals: list[str] = field(default_factory=list)
    workstream_id: str | None = None

    @property
    def publishable(self) -> bool:
        return bool(self.source_refs) and self.status != "rejected" and self.route_hint != "suppress"

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["occurred_at"] = self.occurred_at
        return record


BLOCKER_RE = re.compile(r"\b(blocked|blocker|blocking|stuck|can't proceed|cannot proceed|waiting on)\b", re.I)
DEADLINE_RE = re.compile(r"\b(overdue|slipped|slipping|delayed|delay|pushed back|missed|at risk|off track|due today|due tomorrow)\b", re.I)
DECISION_MADE_RE = re.compile(r"\b(approved|approval granted|decided|decision made|rejected|signed off|ship it|go ahead)\b", re.I)
DECISION_NEEDED_RE = re.compile(r"\b(need(s)? (a )?decision|decide|approval needed|can someone approve|should we|which option)\b", re.I)
OWNER_RE = re.compile(
    r"\b(unassigned|unowned|no owner|owner unclear|who owns|need(s)? an owner|dri unclear|"
    r"nobody has committed|no one has committed)\b",
    re.I,
)
CUSTOMER_RE = re.compile(r"\b(customer|pilot|renewal|churn|contract|procurement|deal|hubspot)\b", re.I)
QUALITY_RE = re.compile(r"\b(P0|P1|sev[ -]?1|sev[ -]?2|bug|regression|failing check|test failed|production down|incident)\b", re.I)
PRIORITY_RE = re.compile(
    r"\b(priority changed|new priority|reprioritize|drop this|pause (this|batch|non-critical|changes)|switching focus)\b",
    re.I,
)
PROGRESS_RE = re.compile(r"\b(done|closed|merged|shipped|released|completed|resolved|state:\s*(done|closed|merged))\b", re.I)
REVIEW_NEEDED_RE = re.compile(r"\b(review requested|needs review|waiting for review|requested review)\b", re.I)
CHATTER_RE = re.compile(r"^\s*(thanks|thank you|lol|haha|sounds good|sgtm|ok|okay|cool|nice|great|yep|yes|no)\s*[.!?]*\s*$", re.I)
TINY_CONTEXT_RE = re.compile(r"^\s*(approved|ship it|ok|okay|done|blocked|go|yes|no)\s*[.!?]*\s*$", re.I)
SLACK_DIRECT_ASK_RE = re.compile(
    r"\b(can you|could you|would you|please review|please take a look|take a look|look at this|need your review|need your input|need your approval|thoughts\??|can someone)\b",
    re.I,
)
TINY_APPROVAL_REPLY_RE = re.compile(r"^\s*(approved|ship it|ok|okay|done|go|yes)\s*[.!?]*\s*$", re.I)
TINY_NEGATIVE_REPLY_RE = re.compile(r"^\s*(no|blocked)\s*[.!?]*\s*$", re.I)


def classify_normalized_event_shadow(
    event: NormalizedEvent,
    *,
    tenant_id: str | None = None,
    source_event_id: str | None = None,
    normalized_event_id: str | None = None,
    context_pack_id: str | None = None,
    allowed_principals: list[str] | None = None,
) -> WorkSignalCandidate:
    """Classify one normalized event into a shadow WorkSignal candidate."""

    text = (event.text or "").strip()
    source_refs = _source_refs(
        source_event_id=source_event_id,
        normalized_event_id=normalized_event_id,
        source_url=event.source_url,
        quote=text,
    )
    related_people = _related_people(event)
    common = {
        "tenant_id": tenant_id,
        "source_event_id": source_event_id,
        "normalized_event_id": normalized_event_id,
        "context_pack_id": context_pack_id,
        "source_refs": source_refs,
        "evidence_quote": _quote(text),
        "actor_label": event.actor_label,
        "occurred_at": event.occurred_at,
        "related_people": related_people,
        "allowed_principals": allowed_principals or [],
        "metadata": {
            "source_type": event.source_type,
            "thread_key": event.thread_key,
            "channel_key": event.channel_key,
            "source_url": event.source_url,
            "event_metadata": event.metadata,
            "needs_context_expansion": bool(
                (event.metadata or {}).get("context_expanded_from_tiny_reply")
            )
            or needs_context_expansion(event),
        },
    }

    if not source_refs:
        return WorkSignalCandidate(
            **common,
            signal_type="noise",
            signal_subtype="missing_source_refs",
            title="Signal rejected because it has no source evidence",
            summary="Alloy could not safely use this event because it has no source ref.",
            confidence=1.0,
            route_hint="suppress",
            status="rejected",
            route_reason="source refs are required before publication",
        )

    if CHATTER_RE.search(text):
        return _candidate(
            common,
            signal_type="noise",
            title="Low-signal message",
            summary="This looks like ordinary acknowledgement chatter.",
            confidence=0.9,
            route_hint="suppress",
            route_reason="acknowledgement chatter without operating implication",
        )

    source_specific = _source_specific_candidate(event, common)
    if source_specific is not None:
        return source_specific

    patterns: list[tuple[re.Pattern[str], str, str, str, float, float, float, float]] = [
        (BLOCKER_RE, "blocked", "Work may be blocked", "The event describes blocked or stuck work.", 0.9, 0.9, 0.85, 0.7),
        (DEADLINE_RE, "deadline_risk", "Deadline risk detected", "The event mentions slipping, delay, overdue work, or off-track execution.", 0.86, 0.86, 0.8, 0.75),
        (OWNER_RE, "unclear_owner", "Ownership is unclear", "The event suggests a missing or unclear owner.", 0.82, 0.75, 0.8, 0.7),
        (DECISION_NEEDED_RE, "decision_needed", "Decision needed", "The event appears to require a decision or approval.", 0.82, 0.8, 0.78, 0.7),
        (DECISION_MADE_RE, "decision_made", "Decision made", "The event records an approval, rejection, or decision.", 0.8, 0.45, 0.55, 0.75),
        (PRIORITY_RE, "priority_shift", "Priority changed", "The event describes a priority or focus change.", 0.78, 0.75, 0.7, 0.8),
        (QUALITY_RE, "quality_or_release_risk", "Quality or release risk", "The event mentions defects, failed checks, production incidents, or release risk.", 0.84, 0.78, 0.74, 0.8),
        (REVIEW_NEEDED_RE, "needs_user", "Review may be needed", "The event requests review or user attention.", 0.82, 0.7, 0.78, 0.65),
        (PROGRESS_RE, "progress_update", "Progress update", "The event records completed, merged, shipped, or resolved work.", 0.78, 0.35, 0.45, 0.8),
        (CUSTOMER_RE, "customer_impact", "Customer-impacting work", "The event relates to a customer, pilot, renewal, contract, or deal.", 0.72, 0.55, 0.55, 0.75),
    ]
    for pattern, signal_type, title, summary, confidence, urgency, actionability, awareness in patterns:
        match_text = text
        if event.source_type == "linear" and signal_type == "quality_or_release_risk":
            match_text = _linear_active_risk_text(event)
        match = pattern.search(match_text)
        if not match:
            continue
        return _candidate(
            common,
            signal_type=signal_type,
            signal_subtype=match.group(0).lower(),
            title=_signal_title(event, signal_type, default=title),
            summary=_signal_summary(event, signal_type, default=summary),
            confidence=confidence,
            urgency_score=urgency,
            actionability_score=actionability,
            awareness_score=awareness,
            route_hint=_route_hint(signal_type, urgency, actionability),
            route_reason=f"matched {signal_type} pattern: {match.group(0)}",
        )

    if event.source_type in {"linear", "github", "hubspot", "gcal", "googledocs", "googlesheets", "googledrive"}:
        return _candidate(
            common,
            signal_type="progress_update",
            title=_signal_title(event, "progress_update", default=f"{event.source_type.title()} activity"),
            summary=_signal_summary(event, "progress_update", default="Structured source activity worth keeping in the operating memory."),
            confidence=0.55,
            urgency_score=0.2,
            actionability_score=0.25,
            awareness_score=0.55,
            route_hint="feed",
            route_reason="structured source activity defaults to Feed unless a stronger signal is present",
        )

    return _candidate(
        common,
        signal_type="noise",
        title="No operating signal detected",
        summary="The event did not contain enough operating signal for Brief or Feed.",
        confidence=0.65,
        route_hint="suppress",
        route_reason="no supported operating pattern matched",
    )


def _source_specific_candidate(
    event: NormalizedEvent,
    common: dict[str, Any],
) -> WorkSignalCandidate | None:
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    state = str(metadata.get("state") or "").strip().lower()
    kind = str(metadata.get("kind") or "").strip().lower()
    text = event.text or ""
    base_text = str(metadata.get("base_text") or text)
    expanded_text = str(metadata.get("expanded_context_text") or text)

    if event.source_type == "slack":
        if metadata.get("context_expanded_from_tiny_reply"):
            if TINY_APPROVAL_REPLY_RE.search(base_text) and (
                REVIEW_NEEDED_RE.search(expanded_text)
                or DECISION_NEEDED_RE.search(expanded_text)
                or SLACK_DIRECT_ASK_RE.search(expanded_text)
            ):
                return _candidate(
                    common,
                    signal_type="decision_made",
                    signal_subtype="slack_tiny_approval_reply",
                    title=_signal_title(event, "decision_made", default="Approval or decision landed in Slack"),
                    summary=_signal_summary(
                        event,
                        "decision_made",
                        default="A short Slack reply resolved an approval or decision request in an active thread.",
                    ),
                    confidence=0.84,
                    urgency_score=0.42,
                    actionability_score=0.56,
                    awareness_score=0.76,
                    route_hint="feed",
                    route_reason="tiny Slack approval reply resolved a request once thread context was expanded",
                )
            if TINY_NEGATIVE_REPLY_RE.search(base_text) and (
                REVIEW_NEEDED_RE.search(expanded_text)
                or DECISION_NEEDED_RE.search(expanded_text)
                or SLACK_DIRECT_ASK_RE.search(expanded_text)
            ):
                return _candidate(
                    common,
                    signal_type="blocked",
                    signal_subtype="slack_tiny_blocked_reply",
                    title=_signal_title(event, "blocked", default="Slack reply indicates blocked work"),
                    summary=_signal_summary(
                        event,
                        "blocked",
                        default="A short Slack reply indicates the approval or decision thread is still blocked.",
                    ),
                    confidence=0.82,
                    urgency_score=0.82,
                    actionability_score=0.76,
                    awareness_score=0.7,
                    route_hint="brief",
                    route_reason="tiny Slack negative reply kept a decision or approval thread blocked after context expansion",
                )
        mentioned_handles = _mentioned_non_actor_handles(event)
        direct_ask = bool(
            mentioned_handles
            and ("?" in text or SLACK_DIRECT_ASK_RE.search(text) or REVIEW_NEEDED_RE.search(text) or DECISION_NEEDED_RE.search(text))
        )
        stronger_risk = bool(BLOCKER_RE.search(text) or DEADLINE_RE.search(text) or QUALITY_RE.search(text))
        if direct_ask and not stronger_risk:
            return _candidate(
                common,
                signal_type="needs_user",
                signal_subtype="slack_direct_ask",
                title=_signal_title(event, "needs_user", default="Slack attention requested"),
                summary=_signal_summary(
                    event,
                    "needs_user",
                    default="The Slack message directly asks a mentioned person for review, input, approval, or a response.",
                ),
                confidence=0.86,
                urgency_score=0.72,
                actionability_score=0.84,
                awareness_score=0.7,
                route_hint=_route_hint("needs_user", 0.72, 0.84),
                route_reason=f"slack direct ask for mentioned handle(s): {', '.join(mentioned_handles[:3])}",
            )

    if event.source_type == "linear":
        if state in {"done", "closed", "completed", "canceled", "cancelled"}:
            return _candidate(
                common,
                signal_type="progress_update",
                signal_subtype=state or None,
                title=_signal_title(event, "progress_update", default="Linear activity"),
                summary=_signal_summary(event, "progress_update", default="Structured Linear progress worth keeping in the operating memory."),
                confidence=0.82,
                urgency_score=0.18,
                actionability_score=0.28,
                awareness_score=0.74,
                route_hint="feed",
                route_reason=f"linear state is {state}",
            )
        linear_risk_text = _linear_active_risk_text(event)
        if state in {"in progress", "todo", "backlog", "assigned"} and QUALITY_RE.search(linear_risk_text):
            return _candidate(
                common,
                signal_type="quality_or_release_risk",
                signal_subtype=state or None,
                title=_signal_title(event, "quality_or_release_risk", default="Quality or release risk"),
                summary=_signal_summary(event, "quality_or_release_risk", default="The event mentions defects, failed checks, production incidents, or release risk."),
                confidence=0.84,
                urgency_score=0.78,
                actionability_score=0.74,
                awareness_score=0.8,
                route_hint="feed",
                route_reason=f"linear state is {state} and active quality-risk terms are present",
            )

    if event.source_type == "github":
        if kind in {"pull_request", "issue"} and state in {"merged", "closed"}:
            return _candidate(
                common,
                signal_type="progress_update",
                signal_subtype=state or None,
                title=_signal_title(event, "progress_update", default="GitHub activity"),
                summary=_signal_summary(event, "progress_update", default="Structured GitHub progress worth keeping in the operating memory."),
                confidence=0.84,
                urgency_score=0.18,
                actionability_score=0.3,
                awareness_score=0.76,
                route_hint="feed",
                route_reason=f"github {kind} state is {state}",
            )
    return None


def needs_context_expansion(event: NormalizedEvent) -> bool:
    """Tiny high-importance messages should pull parent thread before final classification."""
    return bool(event.thread_key and TINY_CONTEXT_RE.search((event.text or "").strip()))


def build_missing_evidence_signal(
    *,
    tenant_id: str | None,
    object_id: str,
    title: str,
    last_evidence_at: datetime | None,
    expected_cadence_days: int,
    now: datetime | None = None,
    source_refs: list[dict[str, Any]] | None = None,
) -> WorkSignalCandidate:
    """Create a candidate for silent non-progress without claiming off-track."""
    now = now or datetime.now(UTC)
    days_since = None
    if last_evidence_at:
        days_since = max(0, (now - last_evidence_at).days)
    is_stale = last_evidence_at is None or now - last_evidence_at > timedelta(days=expected_cadence_days)
    return WorkSignalCandidate(
        tenant_id=tenant_id,
        signal_type="stale_work",
        signal_subtype="not_enough_recent_evidence",
        title=f"No recent evidence for {title}",
        summary=(
            f"Alloy expected evidence every {expected_cadence_days} day(s), "
            f"but found no recent update. This is a missing-evidence state, not an off-track claim."
        ),
        evidence_quote=None,
        source_refs=source_refs or [],
        related_object_ids=[object_id],
        confidence=0.65 if is_stale else 0.25,
        urgency_score=0.45 if is_stale else 0.1,
        actionability_score=0.55 if is_stale else 0.2,
        awareness_score=0.65,
        retrieval_strategy=COVERAGE_SCAN,
        route_hint="feed" if is_stale else "ask_only",
        status="candidate",
        route_reason="coverage scan found an object with insufficient recent evidence",
        metadata={
            "unknown_state": "not_enough_recent_evidence",
            "expected_cadence_days": expected_cadence_days,
            "days_since_last_evidence": days_since,
        },
    )


def build_unknown_state_signal(
    *,
    tenant_id: str | None,
    title: str,
    unknown_state: str,
    reason: str,
    source_refs: list[dict[str, Any]] | None = None,
) -> WorkSignalCandidate:
    if unknown_state not in UNKNOWN_STATE_SUBTYPES:
        raise ValueError(f"Unsupported unknown state: {unknown_state}")
    return WorkSignalCandidate(
        tenant_id=tenant_id,
        signal_type="plan_reality_mismatch",
        signal_subtype=unknown_state,
        title=title,
        summary=reason,
        evidence_quote=None,
        source_refs=source_refs or [],
        confidence=0.5,
        urgency_score=0.35,
        actionability_score=0.45,
        awareness_score=0.6,
        retrieval_strategy=COVERAGE_SCAN,
        route_hint="feed",
        route_reason=f"coverage scan produced explicit unknown state: {unknown_state}",
        metadata={"unknown_state": unknown_state},
    )


def build_conflicting_evidence_signal(
    *,
    tenant_id: str | None,
    object_id: str,
    title: str,
    summary: str,
    source_refs: list[dict[str, Any]],
    signal_subtype: str = "conflicting_evidence",
    occurred_at: datetime | None = None,
) -> WorkSignalCandidate:
    return WorkSignalCandidate(
        tenant_id=tenant_id,
        signal_type="plan_reality_mismatch",
        signal_subtype=signal_subtype,
        title=title,
        summary=summary,
        evidence_quote=None,
        source_refs=source_refs,
        related_object_ids=[object_id],
        occurred_at=occurred_at,
        confidence=0.68,
        urgency_score=0.58,
        actionability_score=0.62,
        awareness_score=0.74,
        retrieval_strategy=COVERAGE_SCAN,
        route_hint="feed",
        status="classified",
        route_reason="coverage scan found conflicting evidence across the live work and stored object state",
        metadata={"unknown_state": "conflicting_evidence"},
    )


def build_unmapped_important_signal(
    *,
    tenant_id: str | None,
    title: str,
    summary: str,
    source_refs: list[dict[str, Any]],
    evidence_quote: str | None = None,
) -> WorkSignalCandidate:
    return WorkSignalCandidate(
        tenant_id=tenant_id,
        signal_type="new_possible_workstream",
        title=title,
        summary=summary,
        evidence_quote=evidence_quote,
        source_refs=source_refs,
        confidence=0.55,
        urgency_score=0.35,
        actionability_score=0.45,
        awareness_score=0.75,
        novelty_score=0.9,
        retrieval_strategy=COVERAGE_SCAN,
        route_hint="feed",
        route_reason="coverage scan found high-signal activity that did not map to a known object",
    )


async def insert_work_signal(conn: Any, candidate: WorkSignalCandidate) -> str:
    """Persist a shadow WorkSignal candidate and return its id."""
    candidate = await resolve_signal_attachment(conn, candidate)
    row = await conn.fetchrow(
        """
        INSERT INTO work_signals (
            tenant_id, source_event_id, normalized_event_id, context_pack_id,
            signal_type, signal_subtype, title, summary, evidence_quote,
            source_refs, related_object_ids, candidate_object_ids, related_people,
            actor_label, occurred_at, confidence, urgency_score, actionability_score,
            awareness_score, novelty_score, retrieval_strategy, route_hint, status,
            classifier_version, route_reason, metadata, acl_state, acl_freshness_state,
            allowed_principals, workstream_id
        )
        VALUES (
            $1, $2, $3, $4,
            $5, $6, $7, $8, $9,
            $10::jsonb, $11::jsonb, $12::jsonb, $13::jsonb,
            $14, $15, $16, $17, $18,
            $19, $20, $21, $22, $23,
            $24, $25, $26::jsonb, $27, $28,
            $29::jsonb, $30
        )
        RETURNING id
        """,
        candidate.tenant_id,
        candidate.source_event_id,
        candidate.normalized_event_id,
        candidate.context_pack_id,
        candidate.signal_type,
        candidate.signal_subtype,
        candidate.title,
        candidate.summary,
        candidate.evidence_quote,
        json.dumps(_json_ready(candidate.source_refs)),
        json.dumps(candidate.related_object_ids),
        json.dumps(candidate.candidate_object_ids),
        json.dumps(candidate.related_people),
        candidate.actor_label,
        candidate.occurred_at,
        candidate.confidence,
        candidate.urgency_score,
        candidate.actionability_score,
        candidate.awareness_score,
        candidate.novelty_score,
        candidate.retrieval_strategy,
        candidate.route_hint,
        candidate.status,
        candidate.classifier_version,
        candidate.route_reason,
        json.dumps(candidate.metadata),
        candidate.acl_state,
        candidate.acl_freshness_state,
        json.dumps(candidate.allowed_principals),
        candidate.workstream_id,
    )
    signal_id = str(row["id"])
    await sync_persisted_member_attachment(
        conn,
        tenant_id=candidate.tenant_id,
        workstream_id=candidate.workstream_id,
        member_type="work_signal",
        member_id=signal_id,
        resolution_method="signal_attachment_ladder",
        reason="attached work signal to durable workstream during synthesis",
        metadata={
            "signal_type": candidate.signal_type,
            "signal_subtype": candidate.signal_subtype,
            "workstream_resolution_state": (candidate.metadata or {}).get("workstream_resolution_state"),
            "workstream_resolution_reason": (candidate.metadata or {}).get("workstream_resolution_reason"),
        },
    )
    return signal_id


async def upsert_work_signal(conn: Any, candidate: WorkSignalCandidate) -> str:
    """Upsert the shadow WorkSignal for one normalized event/classifier pair.

    Source-level synthesis replays a bounded recent window, so we need stable
    ids instead of inserting duplicates on every run.
    """
    candidate = await resolve_signal_attachment(conn, candidate)
    if candidate.normalized_event_id:
        existing_id = await conn.fetchval(
            """
            SELECT id
            FROM work_signals
            WHERE tenant_id = $1
              AND normalized_event_id = $2
              AND classifier_version = $3
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            candidate.tenant_id,
            candidate.normalized_event_id,
            candidate.classifier_version,
        )
        if existing_id:
            row = await conn.fetchrow(
                """
                UPDATE work_signals
                SET source_event_id = $2,
                    context_pack_id = $3,
                    signal_type = $4,
                    signal_subtype = $5,
                    title = $6,
                    summary = $7,
                    evidence_quote = $8,
                    source_refs = $9::jsonb,
                    related_object_ids = $10::jsonb,
                    candidate_object_ids = $11::jsonb,
                    related_people = $12::jsonb,
                    actor_label = $13,
                    occurred_at = $14,
                    confidence = $15,
                    urgency_score = $16,
                    actionability_score = $17,
                    awareness_score = $18,
                    novelty_score = $19,
                    retrieval_strategy = $20,
                    route_hint = $21,
                    status = $22,
                    route_reason = $23,
                    metadata = $24::jsonb,
                    acl_state = $25,
                    acl_freshness_state = $26,
                    allowed_principals = $27::jsonb,
                    workstream_id = $28,
                    updated_at = now()
                WHERE id = $1
                RETURNING id
                """,
                existing_id,
                candidate.source_event_id,
                candidate.context_pack_id,
                candidate.signal_type,
                candidate.signal_subtype,
                candidate.title,
                candidate.summary,
                candidate.evidence_quote,
                json.dumps(_json_ready(candidate.source_refs)),
                json.dumps(candidate.related_object_ids),
                json.dumps(candidate.candidate_object_ids),
                json.dumps(candidate.related_people),
                candidate.actor_label,
                candidate.occurred_at,
                candidate.confidence,
                candidate.urgency_score,
                candidate.actionability_score,
                candidate.awareness_score,
                candidate.novelty_score,
                candidate.retrieval_strategy,
                candidate.route_hint,
                candidate.status,
                candidate.route_reason,
                json.dumps(candidate.metadata),
                candidate.acl_state,
                candidate.acl_freshness_state,
                json.dumps(candidate.allowed_principals),
                candidate.workstream_id,
            )
            signal_id = str(row["id"])
            await sync_persisted_member_attachment(
                conn,
                tenant_id=candidate.tenant_id,
                workstream_id=candidate.workstream_id,
                member_type="work_signal",
                member_id=signal_id,
                resolution_method="signal_attachment_ladder",
                reason="updated work signal durable workstream attachment",
                metadata={
                    "signal_type": candidate.signal_type,
                    "signal_subtype": candidate.signal_subtype,
                    "workstream_resolution_state": (candidate.metadata or {}).get("workstream_resolution_state"),
                    "workstream_resolution_reason": (candidate.metadata or {}).get("workstream_resolution_reason"),
                },
            )
            return signal_id
    cluster_key = str((candidate.metadata or {}).get("cluster_key") or "").strip()
    if not candidate.normalized_event_id and cluster_key:
        existing_id = await conn.fetchval(
            """
            SELECT id
            FROM work_signals
            WHERE tenant_id = $1
              AND normalized_event_id IS NULL
              AND classifier_version = $2
              AND signal_type = $3
              AND metadata->>'cluster_key' = $4
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            candidate.tenant_id,
            candidate.classifier_version,
            candidate.signal_type,
            cluster_key,
        )
        if existing_id:
            row = await conn.fetchrow(
                """
                UPDATE work_signals
                SET source_event_id = $2,
                    context_pack_id = $3,
                    signal_type = $4,
                    signal_subtype = $5,
                    title = $6,
                    summary = $7,
                    evidence_quote = $8,
                    source_refs = $9::jsonb,
                    related_object_ids = $10::jsonb,
                    candidate_object_ids = $11::jsonb,
                    related_people = $12::jsonb,
                    actor_label = $13,
                    occurred_at = $14,
                    confidence = $15,
                    urgency_score = $16,
                    actionability_score = $17,
                    awareness_score = $18,
                    novelty_score = $19,
                    retrieval_strategy = $20,
                    route_hint = $21,
                    status = $22,
                    route_reason = $23,
                    metadata = $24::jsonb,
                    acl_state = $25,
                    acl_freshness_state = $26,
                    allowed_principals = $27::jsonb,
                    workstream_id = $28,
                    updated_at = now()
                WHERE id = $1
                RETURNING id
                """,
                existing_id,
                candidate.source_event_id,
                candidate.context_pack_id,
                candidate.signal_type,
                candidate.signal_subtype,
                candidate.title,
                candidate.summary,
                candidate.evidence_quote,
                json.dumps(_json_ready(candidate.source_refs)),
                json.dumps(candidate.related_object_ids),
                json.dumps(candidate.candidate_object_ids),
                json.dumps(candidate.related_people),
                candidate.actor_label,
                candidate.occurred_at,
                candidate.confidence,
                candidate.urgency_score,
                candidate.actionability_score,
                candidate.awareness_score,
                candidate.novelty_score,
                candidate.retrieval_strategy,
                candidate.route_hint,
                candidate.status,
                candidate.route_reason,
                json.dumps(candidate.metadata),
                candidate.acl_state,
                candidate.acl_freshness_state,
                json.dumps(candidate.allowed_principals),
                candidate.workstream_id,
            )
            signal_id = str(row["id"])
            await sync_persisted_member_attachment(
                conn,
                tenant_id=candidate.tenant_id,
                workstream_id=candidate.workstream_id,
                member_type="work_signal",
                member_id=signal_id,
                resolution_method="signal_attachment_ladder",
                reason="updated work signal durable workstream attachment",
                metadata={
                    "signal_type": candidate.signal_type,
                    "signal_subtype": candidate.signal_subtype,
                    "workstream_resolution_state": (candidate.metadata or {}).get("workstream_resolution_state"),
                    "workstream_resolution_reason": (candidate.metadata or {}).get("workstream_resolution_reason"),
                },
            )
            return signal_id
    if not candidate.normalized_event_id and candidate.related_object_ids:
        existing_id = await conn.fetchval(
            """
            SELECT id
            FROM work_signals
            WHERE tenant_id = $1
              AND normalized_event_id IS NULL
              AND classifier_version = $2
              AND signal_type = $3
              AND coalesce(signal_subtype, '') = coalesce($4, '')
              AND related_object_ids @> $5::jsonb
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            candidate.tenant_id,
            candidate.classifier_version,
            candidate.signal_type,
            candidate.signal_subtype,
            json.dumps([candidate.related_object_ids[0]]),
        )
        if existing_id:
            row = await conn.fetchrow(
                """
                UPDATE work_signals
                SET source_event_id = $2,
                    context_pack_id = $3,
                    signal_type = $4,
                    signal_subtype = $5,
                    title = $6,
                    summary = $7,
                    evidence_quote = $8,
                    source_refs = $9::jsonb,
                    related_object_ids = $10::jsonb,
                    candidate_object_ids = $11::jsonb,
                    related_people = $12::jsonb,
                    actor_label = $13,
                    occurred_at = $14,
                    confidence = $15,
                    urgency_score = $16,
                    actionability_score = $17,
                    awareness_score = $18,
                    novelty_score = $19,
                    retrieval_strategy = $20,
                    route_hint = $21,
                    status = $22,
                    route_reason = $23,
                    metadata = $24::jsonb,
                    acl_state = $25,
                    acl_freshness_state = $26,
                    allowed_principals = $27::jsonb,
                    workstream_id = $28,
                    updated_at = now()
                WHERE id = $1
                RETURNING id
                """,
                existing_id,
                candidate.source_event_id,
                candidate.context_pack_id,
                candidate.signal_type,
                candidate.signal_subtype,
                candidate.title,
                candidate.summary,
                candidate.evidence_quote,
                json.dumps(_json_ready(candidate.source_refs)),
                json.dumps(candidate.related_object_ids),
                json.dumps(candidate.candidate_object_ids),
                json.dumps(candidate.related_people),
                candidate.actor_label,
                candidate.occurred_at,
                candidate.confidence,
                candidate.urgency_score,
                candidate.actionability_score,
                candidate.awareness_score,
                candidate.novelty_score,
                candidate.retrieval_strategy,
                candidate.route_hint,
                candidate.status,
                candidate.route_reason,
                json.dumps(candidate.metadata),
                candidate.acl_state,
                candidate.acl_freshness_state,
                json.dumps(candidate.allowed_principals),
                candidate.workstream_id,
            )
            signal_id = str(row["id"])
            await sync_persisted_member_attachment(
                conn,
                tenant_id=candidate.tenant_id,
                workstream_id=candidate.workstream_id,
                member_type="work_signal",
                member_id=signal_id,
                resolution_method="signal_attachment_ladder",
                reason="updated work signal durable workstream attachment",
                metadata={
                    "signal_type": candidate.signal_type,
                    "signal_subtype": candidate.signal_subtype,
                    "workstream_resolution_state": (candidate.metadata or {}).get("workstream_resolution_state"),
                    "workstream_resolution_reason": (candidate.metadata or {}).get("workstream_resolution_reason"),
                },
            )
            return signal_id
    return await insert_work_signal(conn, candidate)


def _candidate(common: dict[str, Any], **kwargs: Any) -> WorkSignalCandidate:
    if kwargs["signal_type"] not in SIGNAL_TYPES:
        raise ValueError(f"Unsupported signal type: {kwargs['signal_type']}")
    route_hint = kwargs.get("route_hint", "ask_only")
    if route_hint not in ROUTE_HINTS:
        raise ValueError(f"Unsupported route hint: {route_hint}")
    status = "classified" if route_hint != "suppress" else "rejected"
    return WorkSignalCandidate(**common, status=status, **kwargs)


def _route_hint(signal_type: str, urgency: float, actionability: float) -> str:
    if signal_type in {"blocked", "deadline_risk", "decision_needed", "needs_user"} and max(urgency, actionability) >= 0.75:
        return "brief"
    if signal_type == "noise":
        return "suppress"
    return "feed"


def _quote(text: str) -> str | None:
    compact = " ".join(text.split())
    if not compact:
        return None
    return compact[:320]


def _signal_title(event: NormalizedEvent, signal_type: str, *, default: str) -> str:
    headline = _structured_headline(event)
    if not headline:
        return default
    if signal_type == "progress_update":
        return headline
    if signal_type in {
        "quality_or_release_risk",
        "blocked",
        "deadline_risk",
        "needs_user",
        "decision_needed",
        "decision_made",
        "priority_shift",
        "customer_impact",
        "unclear_owner",
    }:
        return headline
    return default


def _signal_summary(event: NormalizedEvent, signal_type: str, *, default: str) -> str:
    headline = _structured_headline(event)
    details = _structured_details(event)
    if not headline:
        return default
    if signal_type == "progress_update":
        if details:
            return f"{headline}. {details}"
        return headline
    if signal_type == "quality_or_release_risk":
        if details:
            return f"{headline}. Risk context: {details}"
        return f"{headline}. {default}"
    if signal_type in {"blocked", "deadline_risk", "needs_user", "decision_needed", "decision_made", "priority_shift", "customer_impact", "unclear_owner"}:
        if details:
            return f"{headline}. {details}"
        return f"{headline}. {default}"
    return default


def _structured_headline(event: NormalizedEvent) -> str | None:
    first_line = str((event.text or "").splitlines()[0] if event.text else "").strip()
    if not first_line:
        return None
    if event.source_type == "linear" and first_line.startswith("Linear issue: "):
        return _strip_inline_details(first_line[len("Linear issue: ") :].strip())[:180]
    if event.source_type == "github" and first_line.startswith("GitHub pull request: "):
        return _strip_inline_details(first_line[len("GitHub pull request: ") :].strip())[:180]
    return " ".join(first_line.split())[:180]


def _structured_details(event: NormalizedEvent) -> str:
    details: list[str] = []
    for raw_line in str(event.text or "").splitlines()[1:12]:
        line = raw_line.strip()
        if not line:
            continue
        if event.source_type == "slack":
            details.append(line)
        elif line.startswith(("State:", "Assignee:", "Author:", "Priority:", "Team:", "Due:", "Branches:")):
            details.append(line)
    limit = 700 if event.source_type == "slack" else 240
    return _join_customer_details(details)[:limit]


def _join_customer_details(lines: list[str]) -> str:
    parts: list[str] = []
    for line in lines:
        clean = " ".join(str(line or "").split()).strip()
        if not clean:
            continue
        if clean[-1] not in ".!?:":
            clean = f"{clean}."
        parts.append(clean)
    return " ".join(parts)


def _linear_active_risk_text(event: NormalizedEvent) -> str:
    """Use only the visible issue headline and structured status fields for active risk.

    Long Linear descriptions often contain historical P0/P1/bug language that explains why
    an issue exists, not whether it is an active quality risk right now.
    """

    return " ".join(
        part
        for part in (
            _structured_headline(event),
            _structured_details(event),
        )
        if part
    )


def _strip_inline_details(value: str) -> str:
    text = " ".join(str(value or "").split()).strip()
    parts = re.split(r"\s+(State:|Assignee:|Author:|Priority:|Team:|Due:|Branches:)", text, maxsplit=1)
    if not parts:
        return text
    return parts[0].strip()


def _source_refs(
    *,
    source_event_id: str | None,
    normalized_event_id: str | None,
    source_url: str | None,
    quote: str,
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    if source_event_id or normalized_event_id or source_url:
        ref: dict[str, Any] = {"quote": _quote(quote)}
        if source_event_id:
            ref["raw_event_id"] = str(source_event_id)
        if normalized_event_id:
            ref["normalized_event_id"] = str(normalized_event_id)
        if source_url:
            ref["url"] = source_url
        refs.append(ref)
    return refs


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _related_people(event: NormalizedEvent) -> list[dict[str, Any]]:
    people: list[dict[str, Any]] = []
    for entity in event.entities or []:
        if entity.get("type") != "person":
            continue
        people.append({
            key: value
            for key, value in entity.items()
            if key in {"handle_type", "handle", "name", "is_actor"}
        })
    if people or event.source_type != "slack":
        return people
    for handle in re.findall(r"<@([A-Z0-9]+)>", str(event.text or "")):
        people.append({
            "handle_type": "slack",
            "handle": handle,
        })
    return people


def _mentioned_non_actor_handles(event: NormalizedEvent) -> list[str]:
    handles: list[str] = []
    for person in _related_people(event):
        if person.get("is_actor"):
            continue
        handle = str(person.get("handle") or "").strip()
        if handle and handle not in handles:
            handles.append(handle)
    if handles:
        return handles
    if event.source_type != "slack":
        return handles
    for handle in re.findall(r"<@([A-Z0-9]+)>", str(event.text or "")):
        if handle and handle not in handles:
            handles.append(handle)
    return handles
