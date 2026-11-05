"""Destination visibility checks for generated surfaces and source chips."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

REF_RE = re.compile(r"\[ref:([A-Za-z][A-Za-z0-9_]*:[A-Za-z0-9_.:-]+)\]")
STRUCTURAL_LINE_RE = re.compile(r"^(#{1,6}\s+.+|[-*_]{3,})\s*$")


def source_visible_to_principals(source: dict[str, Any], destination_principals: list[str] | None) -> bool:
    """Return whether a permissioned source chip can render for a destination."""
    if not _has_acl(source):
        return False
    if source.get("acl_state") != "stamped" or source.get("acl_freshness_state") != "fresh":
        return False
    principals = set(_json_list(source.get("allowed_principals")))
    if not principals:
        return False
    return bool(principals & set(destination_principals or []))


def filter_visible_sources(sources: list[dict[str, Any]], destination_principals: list[str] | None) -> list[dict[str, Any]]:
    return [source for source in sources if source_visible_to_principals(source, destination_principals)]


def verify_destination_visibility(
    *,
    sources: list[dict[str, Any]],
    destination_principals: list[str] | None,
) -> dict[str, Any]:
    blocked = [
        _blocked_ref(source, index)
        for index, source in enumerate(sources, start=1)
        if not source_visible_to_principals(source, destination_principals)
    ]
    return {"ok": not blocked, "blocked_refs": blocked}


def prepare_destination_surface(
    *,
    surface_type: str,
    content: str,
    sources: list[dict[str, Any]],
    destination_principals: list[str] | None,
) -> dict[str, Any]:
    """Prepare a generated destination payload without leaking hidden evidence.

    This is the reusable gate for future export/doc surfaces. ``content`` and
    ``sources`` are publishable only when ``ok`` is true.
    """
    visibility = verify_destination_visibility(
        sources=sources,
        destination_principals=destination_principals,
    )
    claim_gate = filter_claim_text_for_destination(
        content,
        sources,
        destination_principals,
    )
    uncited_claims = _uncited_surface_claims(surface_type, claim_gate["text"])
    blocked_claims = claim_gate["blocked_claims"] + uncited_claims
    ok = visibility["ok"] and not blocked_claims
    publishable_text = claim_gate["text"] if ok else ""
    publishable_sources = _sources_cited_in_text(
        filter_visible_sources(sources, destination_principals),
        publishable_text,
    ) if ok else []
    return {
        "ok": ok,
        "status": "ready" if ok else "blocked_by_destination_visibility",
        "surface_type": surface_type,
        "content": publishable_text,
        "sources": publishable_sources,
        "blocked_refs": visibility["blocked_refs"],
        "blocked_claims": blocked_claims,
    }


def filter_claim_text_for_destination(
    text: str,
    sources: list[dict[str, Any]],
    destination_principals: list[str] | None,
) -> dict[str, Any]:
    """Remove cited claim lines whose evidence is not visible to a destination."""
    by_ref = {
        str(source.get("ref")): source
        for source in sources
        if source.get("ref")
    }
    blocked_claims: list[dict[str, Any]] = []
    kept_lines: list[str] = []

    for line_number, line in enumerate((text or "").splitlines(), start=1):
        refs = REF_RE.findall(line)
        if not refs:
            kept_lines.append(line)
            continue

        blocked_refs = [
            ref
            for ref in dict.fromkeys(refs)
            if ref not in by_ref or not source_visible_to_principals(by_ref[ref], destination_principals)
        ]
        if blocked_refs:
            blocked_claims.append({
                "line": line_number,
                "claim_hash": hashlib.sha256(line.strip().encode("utf-8")).hexdigest(),
                "blocked_refs": blocked_refs,
            })
            continue
        kept_lines.append(line)

    return {
        "text": "\n".join(kept_lines).strip(),
        "blocked_claims": blocked_claims,
    }


def verify_claim_visibility(
    *,
    text: str,
    sources: list[dict[str, Any]],
    destination_principals: list[str] | None,
) -> dict[str, Any]:
    return filter_claim_text_for_destination(text, sources, destination_principals)


def _has_acl(source: dict[str, Any]) -> bool:
    return any(key in source for key in ("acl_state", "acl_freshness_state", "allowed_principals"))


def _blocked_ref(source: dict[str, Any], index: int) -> str:
    return str(source.get("ref") or f"blocked-source:{index}")


def _sources_cited_in_text(sources: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    refs = set(_refs_in_text(text))
    return [source for source in sources if source.get("ref") in refs]


def _refs_in_text(text: str) -> list[str]:
    refs: list[str] = []
    for ref in REF_RE.findall(text or ""):
        if ref not in refs:
            refs.append(ref)
    return refs


def _uncited_surface_claims(surface_type: str, text: str) -> list[dict[str, Any]]:
    blocked: list[dict[str, Any]] = []
    lines = (text or "").splitlines()
    has_cited_line = any(REF_RE.findall(line) for line in lines)
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or REF_RE.findall(line) or STRUCTURAL_LINE_RE.match(stripped):
            continue
        if surface_type == "csv_export" and line_number == 1 and has_cited_line:
            continue
        blocked.append({
            "line": line_number,
            "claim_hash": hashlib.sha256(stripped.encode("utf-8")).hexdigest(),
            "blocked_refs": ["missing_citation"],
        })
    return blocked


def _json_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return [str(item) for item in parsed] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return []
