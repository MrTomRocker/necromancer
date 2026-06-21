"""Standard policy: debounce -> retry up to max -> escalate.

The retry/backoff/cooldown timing lives in the engine + behavior config; the
standard policy itself only enforces the auto-switch gate (the base default).
"""

from __future__ import annotations

from .base import RecoveryPolicy


class StandardPolicy(RecoveryPolicy):
    """Default single-driver policy."""
