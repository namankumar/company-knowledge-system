"""Stable source-event identity helpers."""
import hashlib
import json


def payload_sha256(payload: dict) -> str:
    """Hash provider payloads deterministically for raw-event append history."""
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
