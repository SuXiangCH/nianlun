"""Behavior fingerprints for the evaluation orchestration layer."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel


def evaluator_fingerprint(configuration: object) -> str:
    """Hash all behavior-bearing evaluator configuration."""
    if isinstance(configuration, BaseModel):
        payload = configuration.model_dump(mode="json")
    else:
        payload = configuration
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"
