"""Founder chat API."""
import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import current_user
from app.core.db import acquire
from app.presentation.chat import project_chat_message, project_chat_response
from app.synthesis.chat import answer_question

router = APIRouter(prefix="/chat", tags=["chat"])
logger = structlog.get_logger()
COMPANY_VIEW_ROLES = {"founder", "operator"}


def _require_company_view(user: dict) -> None:
    if user.get("role") not in COMPANY_VIEW_ROLES:
        raise HTTPException(403, detail="Company-wide Discuss access required")


class QueryIn(BaseModel):
    question: str


@router.post("/query")
async def chat_query(body: QueryIn, user: dict = Depends(current_user)):
    result = await answer_question(
        tenant_id=str(user["tenant_id"]),
        user_id=str(user["id"]),
        user_role=str(user.get("role") or ""),
        question=body.question,
        channel="api",
    )
    return project_chat_response(result)


@router.get("/history")
async def chat_history(user: dict = Depends(current_user), limit: int = 50):
    _require_company_view(user)
    tenant_id = str(user["tenant_id"])
    user_id = str(user["id"])
    principals = [f"user:{user_id}", f"company:{tenant_id}"]
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, role, text, sources, trust_metadata, created_at FROM chat_messages
            WHERE tenant_id = $1
              AND (user_id = $2 OR user_id IS NULL)
              AND channel != 'slack_dm'
              AND acl_state = 'stamped'
              AND acl_freshness_state = 'fresh'
              AND allowed_principals ?| $3::text[]
            ORDER BY created_at DESC LIMIT $4
            """,
            tenant_id, user_id, principals, min(limit, 200),
        )
    return {"messages": [project_chat_message(r) for r in reversed(rows)]}
