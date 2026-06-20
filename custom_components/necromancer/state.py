"""The guard lifecycle state enum — shared vocabulary for engine + linking.

Kept in its own module so the link coordinator can reference it without importing
the engine (which imports the coordinator).
"""

from __future__ import annotations

from enum import StrEnum


class GState(StrEnum):
    OK = "ok"
    SUSPECT = "suspect"
    RECOVERING = "recovering"
    VERIFY = "verify"
    COOLDOWN = "cooldown"
    ESCALATED = "escalated"
