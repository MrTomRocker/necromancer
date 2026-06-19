"""Base class for Necromancer recovery policies.

The policy is the engine's pluggable politics: WHEN to act (gates) and HOW to
escalate. Phase 1 ships `standard`; `escalating`/`observe`/`manual_confirm`
and the gates (time_window/rate_limit/dependency) arrive later.
"""

from __future__ import annotations

from ..const import REASON_AUTO_OFF


class RecoveryPolicy:
    """Decides whether an automatic recovery may start now.

    `allows_recovery` tells the rest of the integration whether this guard can
    recover at all — a notify-only guard sets it False, which suppresses the
    auto-restart switch and the recover button.
    """

    allows_recovery: bool = True

    def __init__(self, config: dict) -> None:
        self.config = config

    def should_attempt(self, *, auto_enabled: bool) -> tuple[bool, str]:
        """Return (allowed, reason). Base gate = the per-device auto switch."""
        if not auto_enabled:
            return False, REASON_AUTO_OFF
        return True, ""
