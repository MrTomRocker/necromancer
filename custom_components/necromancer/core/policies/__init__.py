"""Recovery policy registry + factory."""

from __future__ import annotations

from .base import RecoveryPolicy
from .notify import NotifyPolicy
from .standard import StandardPolicy

POLICY_TYPES: dict[str, type[RecoveryPolicy]] = {
    "standard": StandardPolicy,
    "notify": NotifyPolicy,
}


def create_policy(config: dict) -> RecoveryPolicy:
    """Build a RecoveryPolicy from its config dict."""
    return POLICY_TYPES[config["type"]](config)


__all__ = ["POLICY_TYPES", "RecoveryPolicy", "create_policy"]
