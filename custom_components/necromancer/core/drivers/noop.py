"""No-op recovery driver for notify-only guards.

A notify-only guard never recovers, but the engine always holds a driver. This
placeholder keeps that uniform; `recover` is never reached (the notify policy
blocks before RECOVERING).
"""

from __future__ import annotations

from .base import RecoveryDriver


class NoopDriver(RecoveryDriver):
    """Does nothing; used when the guard only notifies."""

    async def recover(self) -> None:
        """Do nothing (notify-only guard never recovers)."""
        return

    def target_info(self) -> str:
        """Return a short human description of the recovery target."""
        return "—"
