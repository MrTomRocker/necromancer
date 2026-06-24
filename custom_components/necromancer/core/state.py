"""The guard lifecycle state enum — shared vocabulary for engine + linking.

Kept in its own module so the link coordinator can reference it without importing
the engine (which imports the coordinator).
"""

from __future__ import annotations

from enum import StrEnum


class GState(StrEnum):
    """The lifecycle state of a guard (the `sensor.<guard>_status` value)."""

    OK = "ok"
    SUSPECT = "suspect"
    # Health is unknown (source unavailable / render error) while idly monitoring:
    # the guard can't read the device, so it shows blind instead of a stale ok. No
    # recovery is triggered (unknown is never a fault).
    BLIND = "blind"
    RECOVERING = "recovering"
    VERIFY = "verify"
    COOLDOWN = "cooldown"
    ESCALATED = "escalated"
    # Operator-snoozed (necromancer.snooze): health ignored until the timer
    # elapses or unsnooze; survives restart (re-arms the remaining time).
    SNOOZED = "snoozed"
