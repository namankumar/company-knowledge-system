"""Founder-facing chat projection helpers."""
import json
import re
from collections import OrderedDict

from app.core.destination_permissions import filter_claim_text_for_destination, filter_visible_sources
from app.presentation.alerts import _source_label


_MISSING_RE = re.compile(r"^[-*]?\s*Missing:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_INLINE_CITATION_RE = re.compile(r"\s*\[ref:[^\]]+\]")
_URL_RE = re.compile(r"https?://\S+|mailto:\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


def _jsonb(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _source_chips(sources, permission: dict | None = None) -> list[dict]:
    sources = _visible_sources(sources, permission)
    chips: OrderedDict[tuple[str, str | None], dict] = OrderedDict()
    if not isinstance(sources, list):
        return []
    for source in sources:
        if not isinstance(source, dict):
            continue
        url = source.get("source_url") or source.get("url")
        has_identity = bool(source.get("source_id") or source.get("source_external_id") or source.get("source_identity_hints"))
        if not url and not has_identity:
            continue
        if url:
            label = _source_label(url, source.get("source_title") or source.get("title") or source.get("source_type"))
        else:
            label = source.get("source_title") or source.get("title") or source.get("source_type") or "Source"
        chips.setdefault(
            (url or source.get("ref") or label, None),
            _with_source_identity(source, {
                "label": label,
                "url": url,
                "ref": source.get("ref"),
                "source_observed_at": source.get("source_observed_at"),
            }),
        )
        if len(chips) >= 6:
            break
    return list(chips.values())


def _visible_sources(sources, permission: dict | None = None) -> list[dict]:
    sources = _jsonb(sources, []) or []
    destination_principals = (permission or {}).get("allowed_principals")
    if not isinstance(sources, list):
        return []
    return filter_visible_sources(sources, destination_principals)


def _destination_safe_text(text: str, sources, permission: dict | None = None) -> str:
    destination_principals = (permission or {}).get("allowed_principals")
    if not destination_principals:
        return text or ""
    raw_sources = _jsonb(sources, []) or []
    if not isinstance(raw_sources, list):
        raw_sources = []
    claim_gate = filter_claim_text_for_destination(text or "", raw_sources, destination_principals)
    if not claim_gate["blocked_claims"]:
        return text or ""
    return claim_gate["text"] or "I couldn't verify an answer from sources visible to this destination."


def _with_source_identity(source: dict, chip: dict) -> dict:
    has_source_identity = bool(source.get("source_id") or source.get("source_external_id") or source.get("source_identity_hints"))
    if has_source_identity and source.get("source_type"):
        chip["source_type"] = source.get("source_type")
    for key in ("source_id", "source_external_id"):
        if source.get(key):
            chip[key] = source.get(key)
    if has_source_identity and source.get("source_identity_hints"):
        chip["source_identity_hints"] = source.get("source_identity_hints")
    return chip


def _safe_text(value) -> str:
    text = str(value)
    text = _URL_RE.sub("[redacted-url]", text)
    text = _EMAIL_RE.sub("[redacted-email]", text)
    return text


def _safe_string_list(value, *, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_safe_text(item) for item in value if item is not None][:limit]


def _safe_permission(permission) -> dict:
    if not isinstance(permission, dict):
        return {}
    return {
        "policy_version": permission.get("policy_version"),
        "target_audience": permission.get("target_audience"),
        "allowed_principals": _safe_string_list(permission.get("allowed_principals"), limit=50),
    }


def _safe_blocked_claims(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    blocked: list[dict] = []
    for claim in value[:8]:
        if not isinstance(claim, dict):
            continue
        blocked.append({
            "line": claim.get("line"),
            "claim_hash": claim.get("claim_hash"),
            "blocked_refs": _safe_string_list(claim.get("blocked_refs"), limit=20),
        })
    return blocked


def _safe_trust_metadata(trust) -> dict:
    if not isinstance(trust, dict):
        return {}
    safe: dict = {
        "citations_valid": trust.get("citations_valid"),
        "invalid_refs": _safe_string_list(trust.get("invalid_refs")),
        "uncited_claims": _safe_string_list(trust.get("uncited_claims")),
        "unsupported_claims": _safe_string_list(trust.get("unsupported_claims")),
        "confidence_gaps": _safe_string_list(trust.get("confidence_gaps")),
        "blocked_permission_claims": _safe_blocked_claims(trust.get("blocked_permission_claims")),
        "permission": _safe_permission(trust.get("permission")),
    }
    for key in (
        "llm_degraded",
        "permission_denied",
        "paused",
        "system_source_health",
        "system_workstream_answer",
        "source_specific_answer",
        "read_only_task_plan",
        "low_relevance",
    ):
        if key in trust:
            safe[key] = bool(trust.get(key))
    if trust.get("workstream_delta"):
        safe["workstream_delta"] = _safe_text(trust.get("workstream_delta"))
    if isinstance(trust.get("requested_source_types"), list):
        safe["requested_source_types"] = _safe_string_list(trust.get("requested_source_types"))
    if isinstance(trust.get("missing_requested_sources"), list):
        safe["missing_requested_sources"] = _safe_string_list(trust.get("missing_requested_sources"))
    return {key: value for key, value in safe.items() if value not in (None, [], {})}


def _missing_evidence(text: str, *, citations_valid: bool | None, uncited_claims=None) -> list[str]:
    gaps: list[str] = []
    match = _MISSING_RE.search(text or "")
    if match:
        value = match.group(1).strip().rstrip(".")
        if value and value.lower() not in {"nothing material", "nothing"}:
            gaps.append(value)
    if citations_valid is False:
        gaps.append("The answer had invalid source citations and was withheld.")
    for claim in uncited_claims or []:
        if claim and claim not in gaps:
            gaps.append(f"Uncited claim: {claim}")
    return gaps[:6]


def _source_gaps(sources, *, system_source_health: bool = False) -> list[str]:
    sources = _jsonb(sources, []) or []
    gaps: list[str] = []
    if not isinstance(sources, list):
        return gaps
    for source in sources:
        if not isinstance(source, dict):
            continue
        title = str(source.get("source_title") or source.get("title") or "")
        has_identity = bool(source.get("source_id") or source.get("source_external_id") or source.get("source_identity_hints"))
        if system_source_health and has_identity and title.startswith("Source health"):
            continue
        if source.get("ref") and not (source.get("source_url") or source.get("url")):
            gaps.append(f"Source unavailable for {source['ref']}")
    return gaps


def _display_text(text: str) -> str:
    lines = []
    for line in (text or "").splitlines():
        if line.strip().lower().startswith(("sources used:", "missing:")):
            continue
        lines.append(_INLINE_CITATION_RE.sub("", line))
    return "\n".join(lines).strip() or _INLINE_CITATION_RE.sub("", text or "").strip()


def project_chat_response(result: dict) -> dict:
    raw_trust = _jsonb(result.get("trust_metadata"), {}) or {}
    trust = _safe_trust_metadata(raw_trust)
    raw_sources = result.get("sources") or []
    answer = _destination_safe_text(result.get("answer") or "", raw_sources, raw_trust.get("permission"))
    sources = _visible_sources(raw_sources, raw_trust.get("permission"))
    gaps = list(trust.get("confidence_gaps") or []) or _missing_evidence(
        answer,
        citations_valid=result.get("citations_valid"),
        uncited_claims=result.get("uncited_claims"),
    )
    gaps.extend(gap for gap in _source_gaps(sources, system_source_health=bool(trust.get("system_source_health"))) if gap not in gaps)
    return {
        **result,
        "answer": answer,
        "sources": sources,
        "trust_metadata": trust,
        "display_answer": _display_text(answer),
        "source_chips": _source_chips(sources, raw_trust.get("permission")),
        "confidence_gaps": gaps,
        "missing_evidence": gaps,
        "trust": trust,
        "degraded": bool(trust.get("llm_degraded")),
        "degraded_reason": "Model provider unavailable. Answered from retrieved sources only." if trust.get("llm_degraded") else None,
    }


def project_chat_message(row) -> dict:
    message = dict(row)
    raw_trust = _jsonb(message.get("trust_metadata"), {}) or {}
    trust = _safe_trust_metadata(raw_trust)
    raw_sources = message.get("sources")
    text = _destination_safe_text(message.get("text") or "", raw_sources, raw_trust.get("permission"))
    sources = _visible_sources(raw_sources, raw_trust.get("permission"))
    message["text"] = text
    message["sources"] = sources
    message["trust_metadata"] = trust
    message["display_text"] = _display_text(text)
    message["source_chips"] = _source_chips(sources, raw_trust.get("permission"))
    gaps = list(trust.get("confidence_gaps") or []) or _missing_evidence(
        message.get("text") or "", citations_valid=trust.get("citations_valid"), uncited_claims=trust.get("uncited_claims")
    )
    gaps.extend(gap for gap in _source_gaps(sources, system_source_health=bool(trust.get("system_source_health"))) if gap not in gaps)
    message["confidence_gaps"] = gaps[:8]
    message["missing_evidence"] = message["confidence_gaps"]
    message["degraded"] = bool(trust.get("llm_degraded"))
    message["degraded_reason"] = "Model provider unavailable. Answered from retrieved sources only." if trust.get("llm_degraded") else None
    return message
