"""Source import gates.

Manual import approval keeps OAuth connection separate from data import. This is
different from the pilot pause switches, which intentionally retain raw events
while pausing downstream brain/synthesis/client updates.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

PENDING_IMPORT_STATUS = "connected_pending_import"
IMPORT_PAUSED_STATUS = "import_paused"
IMPORT_DISABLED_STATUSES = {PENDING_IMPORT_STATUS, IMPORT_PAUSED_STATUS}


def json_object(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def manual_import_required(settings: dict | str | None) -> bool:
    """Require operator approval unless a tenant explicitly opts into auto-import."""
    return json_object(settings).get("manual_import_approval_required") is not False


def import_enabled(sync_policy: dict | str | None, *, status: str | None = None) -> bool:
    if status in IMPORT_DISABLED_STATUSES:
        return False
    policy = json_object(sync_policy)
    return policy.get("import_enabled") is not False


def import_disabled_reason(sync_policy: dict | str | None, *, status: str | None = None) -> str | None:
    if import_enabled(sync_policy, status=status):
        return None
    if status == PENDING_IMPORT_STATUS:
        return "operator approval required before import"
    if status == IMPORT_PAUSED_STATUS:
        return "source import is paused"
    return "source import is disabled"


def with_import_state(sync_policy: dict | str | None, *, enabled: bool, reason: str | None = None) -> dict:
    policy = json_object(sync_policy)
    policy["import_enabled"] = enabled
    now = datetime.now(timezone.utc).isoformat()
    if enabled:
        policy["import_enabled_at"] = now
        policy.pop("import_disabled_reason", None)
    else:
        policy["import_disabled_at"] = now
        if reason:
            policy["import_disabled_reason"] = reason
    return policy
