"""Notify-only policy: detect + notify, never recover.

`allows_recovery = False` drops the auto-restart switch and recover button; the
engine routes a confirmed problem straight to ESCALATED (with a notification).
"""

from __future__ import annotations

from ..const import REASON_OBSERVE
from .base import RecoveryPolicy


class NotifyPolicy(RecoveryPolicy):
    """Observe-only: a confirmed problem is reported, never acted on."""

    allows_recovery = False

    def should_attempt(self, *, auto_enabled: bool) -> tuple[bool, str]:
        return False, REASON_OBSERVE
