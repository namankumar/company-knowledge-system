"""Direct GitHub App client for PR metadata only.

This intentionally avoids GitHub OAuth's broad ``repo`` scope. The app token is
installation-scoped and the backend only calls metadata/PR endpoints.
"""
from __future__ import annotations

import time
from typing import Any

import httpx
import jwt

from app.core.config import get_settings
from app.core.source_permissions import sanitize_github_pull_request

GITHUB_API_BASE = "https://api.github.com"
GITHUB_APP_ALLOWED_REPOSITORY_PERMISSIONS = {
    "metadata": "read",
    "pull_requests": "read",
    "issues": "read",
}
GITHUB_APP_REQUIRED_REPOSITORY_PERMISSIONS = {
    "metadata": "read",
    "pull_requests": "read",
}
GITHUB_APP_DENIED_REPOSITORY_PERMISSIONS = {
    "contents",
    "actions",
    "administration",
    "checks",
    "secrets",
    "statuses",
    "workflows",
}


class GitHubAppNotConfigured(RuntimeError):
    pass


def github_app_install_url(*, state: str) -> str:
    settings = get_settings()
    if not settings.github_app_slug:
        raise GitHubAppNotConfigured("GITHUB_APP_SLUG is not configured")
    return f"https://github.com/apps/{settings.github_app_slug}/installations/new?state={state}"


def github_app_jwt() -> str:
    settings = get_settings()
    if not settings.github_app_id or not settings.github_app_private_key:
        raise GitHubAppNotConfigured("GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY are required")
    private_key = settings.github_app_private_key.replace("\\n", "\n")
    now = int(time.time())
    return jwt.encode(
        {
            "iat": now - 60,
            "exp": now + (9 * 60),
            "iss": settings.github_app_id,
        },
        private_key,
        algorithm="RS256",
    )


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def get_installation(installation_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            f"{GITHUB_API_BASE}/app/installations/{installation_id}",
            headers=_github_headers(github_app_jwt()),
        )
        response.raise_for_status()
        return response.json()


async def list_app_installations() -> list[dict[str, Any]]:
    installations: list[dict[str, Any]] = []
    page = 1
    async with httpx.AsyncClient(timeout=20.0) as client:
        while True:
            response = await client.get(
                f"{GITHUB_API_BASE}/app/installations",
                headers=_github_headers(github_app_jwt()),
                params={"per_page": 100, "page": page},
            )
            response.raise_for_status()
            batch = response.json()
            installations.extend(batch)
            if len(batch) < 100:
                return installations
            page += 1


async def create_installation_token(installation_id: str) -> str:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens",
            headers=_github_headers(github_app_jwt()),
        )
        response.raise_for_status()
        return str(response.json()["token"])


async def list_installation_repositories(installation_token: str) -> list[dict[str, Any]]:
    repos: list[dict[str, Any]] = []
    page = 1
    async with httpx.AsyncClient(timeout=20.0) as client:
        while True:
            response = await client.get(
                f"{GITHUB_API_BASE}/installation/repositories",
                headers=_github_headers(installation_token),
                params={"per_page": 100, "page": page},
            )
            response.raise_for_status()
            data = response.json()
            batch = data.get("repositories") or []
            repos.extend(batch)
            if len(batch) < 100:
                return repos
            page += 1


async def fetch_pull_requests_for_repo(
    *,
    installation_token: str,
    full_name: str,
    page: int,
    per_page: int,
) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            f"{GITHUB_API_BASE}/repos/{full_name}/pulls",
            headers=_github_headers(installation_token),
            params={
                "state": "all",
                "sort": "updated",
                "direction": "desc",
                "per_page": per_page,
                "page": page,
            },
        )
        response.raise_for_status()
        return [sanitize_github_pull_request(pr) for pr in response.json()]


def repository_allowlist(repositories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowlist = []
    for repo in repositories:
        full_name = repo.get("full_name")
        if not full_name:
            continue
        allowlist.append({
            "id": repo.get("id"),
            "full_name": full_name,
            "private": bool(repo.get("private")),
            "html_url": repo.get("html_url"),
        })
    return allowlist


def permission_list_from_installation(installation: dict[str, Any]) -> list[str]:
    permissions = installation.get("permissions") or {}
    result = []
    for name, level in sorted(permissions.items()):
        if level:
            result.append(f"{name}:{level}")
    return result


def validate_installation_permissions(installation: dict[str, Any]) -> list[str]:
    permissions = installation.get("permissions") or {}
    problems: list[str] = []
    for denied in sorted(GITHUB_APP_DENIED_REPOSITORY_PERMISSIONS):
        if permissions.get(denied):
            problems.append(f"GitHub App granted denied permission: {denied}:{permissions[denied]}")
    for name, expected in GITHUB_APP_REQUIRED_REPOSITORY_PERMISSIONS.items():
        actual = permissions.get(name)
        if actual != expected:
            problems.append(f"GitHub App permission {name} is required as {expected}, got {actual or 'missing'}")
    for name, expected in GITHUB_APP_ALLOWED_REPOSITORY_PERMISSIONS.items():
        actual = permissions.get(name)
        if actual and actual != expected:
            problems.append(f"GitHub App permission {name} should be {expected}, got {actual}")
    return problems
