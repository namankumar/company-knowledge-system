"""Artifacts v0: durable draft objects with version history.

A standalone CRUD surface for durable drafts (comms drafts, docs, generic
content) that can cite/build on brain objects, facts, or other artifacts.
Not wired into retrieval (`app/retrieval/retrieve.py`) yet -- there is no
`artifact:{id}` pack item kind. That is future harness work; this slice is
a CRUD surface only.

Verification is deliberately flag-don't-block: unlike alerts and briefs
(which suppress/reject on failed citation verification), an artifact is a
draft the owner is actively iterating on, so an uncited or unresolvable
citation is recorded on the row (`verification_status`, `verification_detail`)
but never blocks the save. This is a stated deviation from the
verify-or-suppress pattern used elsewhere in `app/synthesis/`, not an
oversight.
"""
import json
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone

from app.core.db import acquire
from app.synthesis.verifier import REF_RE, verify_inline_citations

STATUSES = ("draft", "current", "superseded")
VISIBILITIES = ("private", "company")
VERIFICATION_STATUSES = ("unverified", "verified", "flagged")
VALID_PROMOTIONS = {
    ("draft", "current"),
    ("current", "superseded"),
    ("draft", "superseded"),
}

_REF_FINDER = re.compile(REF_RE)


def _jsonb(value, fallback):
    if value is None:
        return deepcopy(fallback)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return deepcopy(fallback)
    return deepcopy(value)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def extract_refs(content: str) -> list[str]:
    """Every [ref:kind:id] citation in artifact content, de-duplicated, order preserved."""
    out: list[str] = []
    seen: set[str] = set()
    for ref in _REF_FINDER.findall(content or ""):
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def normalize_artifact(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "kind": row["kind"],
        "title": row["title"],
        "content": row["content"],
        "current_version": row["current_version"],
        "status": row["status"],
        "visibility": row["visibility"],
        "allowed_principals": _jsonb(row.get("allowed_principals"), []),
        "verification_status": row["verification_status"],
        "verification_detail": _jsonb(row.get("verification_detail"), {}),
        # Track B artifacts-lifecycle packet (2026-07-11), A.1: additive
        # `metadata jsonb` column (migrations/0076_artifacts_metadata.sql).
        # Kind-specific fields live here (comms_draft's audience,
        # source_brief_ref) so no new migration is needed per kind.
        "metadata": _jsonb(row.get("metadata"), {}),
        "created_by": str(row["created_by"]) if row.get("created_by") else None,
        "updated_by": str(row["updated_by"]) if row.get("updated_by") else None,
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def normalize_artifact_version(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "artifact_id": str(row["artifact_id"]),
        "version_number": row["version_number"],
        "content": row["content"],
        "citations": _jsonb(row.get("citations"), []),
        "verification_status": row["verification_status"],
        "verification_detail": _jsonb(row.get("verification_detail"), {}),
        "created_by": row["created_by"],
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


async def verify_artifact_content(
    *,
    tenant_id: str,
    actor_user_id: str | None,
    visibility: str,
    content: str,
) -> dict:
    """Resolve every citation in `content` against a real, ACL-visible pack.

    Flag-don't-block: this only computes a status/detail pair for the caller
    to persist. It never raises and never prevents a save.
    """
    from app.retrieval.retrieve import build_authorized_context_pack

    refs = extract_refs(content)
    if not refs:
        return {
            "status": "unverified",
            "detail": {"cited": [], "invalid_refs": [], "uncited_claims": [], "reason": "no_citations"},
        }

    object_ids = [
        ref.split(":", 1)[1]
        for ref in refs
        if ref.startswith("object:") and _is_uuid(ref.split(":", 1)[1])
    ]
    target_audience = "company" if visibility == "company" else "private_user"
    try:
        pack = await build_authorized_context_pack(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            target_audience=target_audience,
            purpose="artifact_verify",
            object_ids=object_ids or None,
            recent_hours=24 * 30,
        )
    except Exception as e:  # verification is advisory; never block a save on infra failure
        return {
            "status": "flagged",
            "detail": {"cited": refs, "invalid_refs": [], "uncited_claims": [], "error": f"{type(e).__name__}: {e}"},
        }

    check = verify_inline_citations(content, pack["items"])
    status = "verified" if not check["invalid_refs"] and not check["uncited_claims"] else "flagged"
    return {"status": status, "detail": check}


async def create_artifact(
    *,
    tenant_id: str,
    kind: str,
    title: str,
    content: str,
    visibility: str,
    allowed_principals: list[str],
    created_by: str,
    builds_on: list[str] | None = None,
) -> dict:
    if visibility not in VISIBILITIES:
        raise ValueError(f"invalid visibility: {visibility}")
    verification = await verify_artifact_content(
        tenant_id=tenant_id, actor_user_id=created_by, visibility=visibility, content=content
    )
    artifact_id = str(uuid.uuid4())
    citations = extract_refs(content)
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO artifacts (
                id, tenant_id, kind, title, content, current_version, status,
                visibility, allowed_principals, verification_status, verification_detail,
                created_by, updated_by
            )
            VALUES ($1, $2, $3, $4, $5, 1, 'draft', $6, $7::jsonb, $8, $9::jsonb, $10, $10)
            RETURNING *
            """,
            artifact_id, tenant_id, kind, title, content, visibility,
            json.dumps(list(allowed_principals or [])),
            verification["status"], json.dumps(verification["detail"]), created_by,
        )
        await conn.execute(
            """
            INSERT INTO artifact_versions (
                tenant_id, artifact_id, version_number, content, citations,
                verification_status, verification_detail, created_by
            )
            VALUES ($1, $2, 1, $3, $4::jsonb, $5, $6::jsonb, $7)
            """,
            tenant_id, artifact_id, content, json.dumps(citations),
            verification["status"], json.dumps(verification["detail"]), created_by,
        )
        for to_ref in _dedupe(builds_on or citations):
            await conn.execute(
                """
                INSERT INTO artifact_builds_on (tenant_id, from_artifact_id, to_ref, created_by)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (from_artifact_id, to_ref) DO NOTHING
                """,
                tenant_id, artifact_id, to_ref, created_by,
            )
    return normalize_artifact(dict(row))


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


async def get_artifact(*, tenant_id: str, artifact_id: str) -> dict | None:
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM artifacts WHERE tenant_id = $1 AND id = $2",
            tenant_id, artifact_id,
        )
    return normalize_artifact(dict(row)) if row else None


async def list_artifacts(*, tenant_id: str, status: str | None = None, kind: str | None = None) -> list[dict]:
    query = "SELECT * FROM artifacts WHERE tenant_id = $1"
    args: list = [tenant_id]
    if status:
        args.append(status)
        query += f" AND status = ${len(args)}"
    if kind:
        args.append(kind)
        query += f" AND kind = ${len(args)}"
    query += " ORDER BY updated_at DESC"
    async with acquire() as conn:
        rows = await conn.fetch(query, *args)
    return [normalize_artifact(dict(row)) for row in rows]


async def list_artifact_versions(*, tenant_id: str, artifact_id: str) -> list[dict]:
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM artifact_versions
            WHERE tenant_id = $1 AND artifact_id = $2
            ORDER BY version_number DESC
            """,
            tenant_id, artifact_id,
        )
    return [normalize_artifact_version(dict(row)) for row in rows]


async def edit_artifact(
    *,
    tenant_id: str,
    artifact_id: str,
    content: str,
    updated_by: str,
    title: str | None = None,
) -> dict:
    async with acquire() as conn:
        current = await conn.fetchrow(
            "SELECT * FROM artifacts WHERE tenant_id = $1 AND id = $2 FOR UPDATE",
            tenant_id, artifact_id,
        )
        if not current:
            raise LookupError("artifact not found")
        if current["status"] == "superseded":
            raise ValueError("cannot edit a superseded artifact")
        next_version = current["current_version"] + 1
        verification = await verify_artifact_content(
            tenant_id=tenant_id,
            actor_user_id=updated_by,
            visibility=current["visibility"],
            content=content,
        )
        citations = extract_refs(content)
        await conn.execute(
            """
            INSERT INTO artifact_versions (
                tenant_id, artifact_id, version_number, content, citations,
                verification_status, verification_detail, created_by
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb, $8)
            """,
            tenant_id, artifact_id, next_version, content, json.dumps(citations),
            verification["status"], json.dumps(verification["detail"]), updated_by,
        )
        next_title = title if title is not None else current["title"]
        row = await conn.fetchrow(
            """
            UPDATE artifacts
            SET content = $3, title = $4, current_version = $5,
                verification_status = $6, verification_detail = $7::jsonb,
                updated_by = $8, updated_at = now()
            WHERE tenant_id = $1 AND id = $2
            RETURNING *
            """,
            tenant_id, artifact_id, content, next_title, next_version,
            verification["status"], json.dumps(verification["detail"]), updated_by,
        )
        for to_ref in _dedupe(citations):
            await conn.execute(
                """
                INSERT INTO artifact_builds_on (tenant_id, from_artifact_id, to_ref, created_by)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (from_artifact_id, to_ref) DO NOTHING
                """,
                tenant_id, artifact_id, to_ref, updated_by,
            )
    return normalize_artifact(dict(row))


async def promote_artifact(
    *,
    tenant_id: str,
    artifact_id: str,
    to_status: str,
    promoted_by: str,
) -> dict:
    """Human-only lifecycle transition. There is no machine/task caller of this
    function anywhere in the codebase; the only entry point is the API route,
    which always runs behind an authenticated human session."""
    if to_status not in STATUSES:
        raise ValueError(f"invalid status: {to_status}")
    async with acquire() as conn:
        current = await conn.fetchrow(
            "SELECT * FROM artifacts WHERE tenant_id = $1 AND id = $2 FOR UPDATE",
            tenant_id, artifact_id,
        )
        if not current:
            raise LookupError("artifact not found")
        transition = (current["status"], to_status)
        if transition not in VALID_PROMOTIONS:
            raise ValueError(f"invalid transition: {current['status']} -> {to_status}")
        # Track B artifacts-lifecycle packet (2026-07-11), A.4: comms_draft is
        # a leadership/investor-facing communication, a higher-stakes surface
        # than a generic draft, so it gets a stricter code-owned precondition
        # on top of v0's flag-don't-block default -- draft -> current is
        # blocked outright while any claim in the content is uncited.
        # Enforced here (server-side, in the one function every promote path
        # calls) rather than only in the API layer, so a direct call into
        # `promote_artifact` can't skip it.
        if current["kind"] == "comms_draft" and transition == ("draft", "current"):
            from app.synthesis.comms_draft import comms_draft_promotion_check

            check = await comms_draft_promotion_check(
                tenant_id=tenant_id,
                content=current["content"],
                metadata=_jsonb(dict(current).get("metadata"), {}),
            )
            # Deliberately NOT `check["valid"]`: verify_inline_citations only
            # folds uncited_claims into "valid" when called with
            # require_relevance=True (see app.synthesis.verifier), which this
            # call doesn't set (matching brief.py's own default use). A
            # comms_draft's stricter gate is "zero uncited claims", so check
            # both lists explicitly rather than relying on the verifier's
            # more permissive default notion of "valid".
            offending = check.get("uncited_claims") or check.get("invalid_refs") or []
            if offending:
                raise ValueError(
                    "cannot promote comms_draft to current: uncited claim(s) present: "
                    + "; ".join(offending)
                )
        row = await conn.fetchrow(
            """
            UPDATE artifacts
            SET status = $3, updated_by = $4, updated_at = now()
            WHERE tenant_id = $1 AND id = $2
            RETURNING *
            """,
            tenant_id, artifact_id, to_status, promoted_by,
        )
    return normalize_artifact(dict(row))


async def get_builds_on(*, tenant_id: str, artifact_id: str) -> list[str]:
    async with acquire() as conn:
        rows = await conn.fetch(
            "SELECT to_ref FROM artifact_builds_on WHERE tenant_id = $1 AND from_artifact_id = $2 ORDER BY created_at",
            tenant_id, artifact_id,
        )
    return [row["to_ref"] for row in rows]
