"""Base classes for Necromancer recovery drivers.

A RecoveryDriver performs the actual repair: power-cycle a switch
(`switch_cycle`), run one or two user-defined action sequences (`action_call` /
`action_cycle`), or auto-resolve a device to its PoE port (`poe_port`). Whether
the result is verified against the device's health entity is the engine's job.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from homeassistant.core import CALLBACK_TYPE, HomeAssistant


class RecoveryDriver(ABC):
    """Performs a recovery action for a guarded device."""

    def __init__(self, hass: HomeAssistant, config: dict) -> None:
        """Store the hass instance and the guard's driver config."""
        self.hass = hass
        self.config = config

    async def async_setup(self) -> CALLBACK_TYPE | None:
        """Optional: start watching what affects recovery; return an unsub.

        A driver that must react to external changes can subscribe here. Default:
        nothing to watch. (PoE id→port resolution and its last-known cache are
        owned by the shared fabric, not the driver, so `poe_port` needs nothing.)
        """
        return None

    async def can_recover(self) -> tuple[bool, str]:
        """Guard right before recovering. Returns (allowed, reason)."""
        return True, ""

    @abstractmethod
    async def recover(self, variables: dict | None = None) -> None:
        """Perform the recovery. Should return once the action is done.

        ``variables`` carries the engine's run context (``attempt``, ``max``,
        ``name``, ``guard_entity_id``) into action-running drivers; the switch /
        PoE / no-op drivers ignore it.
        """

    def target_info(self) -> str:
        """Short human description of the target (e.g. the service or port)."""
        return self.config.get("type", "")

    def config_errors(self) -> list[str]:
        """Return human-readable config errors (e.g. a missing service).

        Checked at startup and logged at ERROR. Empty list = all good.
        """
        return []
