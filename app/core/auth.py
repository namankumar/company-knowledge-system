"""
Bearer-token auth.

Users authenticate with durable bearer tokens. Legacy tokens are sha256 hashes
on users.api_token_hash; new multi-device sessions live in user_sessions.
Admin endpoints use a separate ADMIN_TOKEN env var. WebSocket auth takes the
token as a query param because browsers can't set WS headers.
"""
import hashlib
import hmac
import secrets

import structlog
from fastapi import HTTPException, Query, Request, Response, status

from app.core.config import get_settings
from app.core.db import acquire, _current_tenant_id, _rls_bypass
from app.core.permissions import normalize_email, role_can_view_company

logger = structlog.get_logger()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token() -> str:
    return f"ihq_{secrets.token_urlsafe(32)}"


SESSION_COOKIE_NAME = "ihq_session"
SESSION_COOKIE_MAX_AGE_SECONDS = 10 * 365 * 24 * 60 * 60
CONSOLE_OPERATOR_EMAIL = "naman@operator.example.invalid"


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def request_auth_token(request: Request) -> str | None:
    return _bearer(request) or request.cookies.get(SESSION_COOKIE_NAME)


def set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    cookie_domain = settings.auth_cookie_domain.strip() or None
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        secure=settings.is_production,
        samesite="none" if settings.is_production else "lax",
        domain=cookie_domain,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    settings = get_settings()
    cookie_domain = settings.auth_cookie_domain.strip() or None
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        httponly=True,
        secure=settings.is_production,
        samesite="none" if settings.is_production else "lax",
        domain=cookie_domain,
        path="/",
    )


async def _lookup_user(token: str) -> dict | None:
    bypass_token = _rls_bypass.set(True)
    try:
        async with acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT u.id, u.tenant_id, u.email, u.name, u.role, u.slack_user_id,
                       COALESCE(
                         (
                           SELECT array_agg(DISTINCT s.source_type ORDER BY s.source_type)
                           FROM sources s
                           WHERE s.tenant_id = u.tenant_id
                             AND s.status IN ('connected', 'backfilling', 'live', 'degraded')
                         ),
                         ARRAY[]::text[]
                       ) AS connected_source_scopes,
                       COALESCE(
                         (
                           SELECT array_agg(DISTINCT pm.principal_key ORDER BY pm.principal_key)
                           FROM principal_memberships pm
                           WHERE pm.tenant_id = u.tenant_id
                             AND pm.member_principal_key = 'user:' || u.id::text
                             AND pm.membership_state = 'active'
                             AND pm.confidence IN ('medium', 'high')
                             AND pm.principal_key IS NOT NULL
                             AND pm.principal_key <> ''
                         ),
                         ARRAY[]::text[]
                       ) AS actor_principals
                FROM users u
                WHERE u.api_token_hash = $1
                   OR EXISTS (
                     SELECT 1
                     FROM user_sessions us
                     WHERE us.user_id = u.id
                       AND us.tenant_id = u.tenant_id
                       AND us.token_hash = $1
                       AND us.revoked_at IS NULL
                       AND (us.expires_at IS NULL OR us.expires_at > now())
                   )
                """,
                hash_token(token),
            )
            if row:
                await conn.execute(
                    """
                    UPDATE user_sessions
                    SET last_used_at = now()
                    WHERE token_hash = $1 AND revoked_at IS NULL
                    """,
                    hash_token(token),
                )
    finally:
        _rls_bypass.reset(bypass_token)
    if not row:
        return None
    user = dict(row)
    connected = list(user.pop("connected_source_scopes") or [])
    principals = list(user.pop("actor_principals", []) or [])
    user["source_scopes"] = connected if role_can_view_company(user.get("role")) else []
    user["principals"] = principals
    return user


async def current_user(request: Request) -> dict:
    """Dependency: authenticated user. Sets the tenant context var for RLS."""
    token = request_auth_token(request)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Missing auth token")
    user = await _lookup_user(token)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    _current_tenant_id.set(str(user["tenant_id"]))
    return user


async def current_user_ws(token: str = Query(...)) -> dict:
    """WebSocket variant: token via query param."""
    user = await _lookup_user(token)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    _current_tenant_id.set(str(user["tenant_id"]))
    return user


async def require_admin(request: Request):
    """Dependency: operator-only endpoints.

    ADMIN_TOKEN remains the break-glass admin path. Browser console sessions can
    also use Google/session auth, but only for company-view users on the
    inferencehq.co domain. Customer-domain operators should never gain console
    access just because they can see their own tenant in the client app.

    Yield dependency so the RLS bypass is scoped to the request and reset on
    teardown — a leaked bypass would disable tenant isolation for whatever
    runs next on this context.
    """
    settings = get_settings()
    bearer_token = _bearer(request)
    request_state = getattr(request, "state", None)
    if request_state is not None:
        request_state.admin_user = None
    if settings.admin_token and bearer_token and hmac.compare_digest(bearer_token, settings.admin_token):
        bypass_token = _rls_bypass.set(True)
        try:
            yield
        finally:
            _rls_bypass.reset(bypass_token)
        return

    session_token = request_auth_token(request)
    user = await _lookup_user(session_token) if session_token else None
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Admin token or inferencehq.co session required")
    if normalize_email(user.get("email", "")) != CONSOLE_OPERATOR_EMAIL or not role_can_view_company(user.get("role")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Naman console session required")
    if request_state is not None:
        request_state.admin_user = user
    bypass_token = _rls_bypass.set(True)
    try:
        yield
    finally:
        _rls_bypass.reset(bypass_token)
