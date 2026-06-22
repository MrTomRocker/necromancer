"""Base classes for Necromancer health sources.

A HealthSource answers one question: is this device healthy right now?
The most generic is `entity_state` (one on/off entity: binary_sensor, switch
or input_boolean); a list/group and technology-specific sources (ping/http/…)
layer on top later.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum

from homeassistant.core import CALLBACK_TYPE, HomeAssistant


class Health(StrEnum):
    """Health verdict. UNKNOWN is explicitly NOT unhealthy (no false alarm)."""

    OK = "ok"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class HealthSource(ABC):
    """Determines whether a guarded device is healthy.

    State sources expose `watched_entities` and the engine re-evaluates on every
    change. Sources that track something else (e.g. a template) register their
    own listener in `async_setup` and call `on_change`; their `watched_entities`
    is empty. Either way `evaluate()` is always callable (so recovery verify
    works).
    """

    def __init__(self, hass: HomeAssistant, config: dict) -> None:
        """Store hass and the source's config block."""
        self.hass = hass
        self.config = config

    @property
    @abstractmethod
    def watched_entities(self) -> list[str]:
        """Entity ids to subscribe to for change events."""

    @abstractmethod
    def evaluate(self) -> Health:
        """Return the current health verdict."""

    def referenced_entities(self) -> list[str]:
        """Entities the verdict depends on (for self-reference / loop checks).

        Defaults to `watched_entities`; a source that tracks something else (a
        template) overrides this with its real dependency set.
        """
        return list(self.watched_entities)

    async def async_setup(self, on_change: CALLBACK_TYPE) -> CALLBACK_TYPE | None:
        """Register own listeners (e.g. a template tracker). Returns an unsub."""
        return None

    def describe(self) -> str:
        """Short human description for diagnostics."""
        return self.config.get("type", "")
