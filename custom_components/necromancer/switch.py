"""Auto-restart switch for Necromancer (display only; state lives in the Store).

The toggle writes through to the engine, which persists `auto` in the Store — so
the choice survives a restart even if this entity is disabled.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NecromancerConfigEntry
from .core.engine import DeviceEngine
from .entity import NecromancerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NecromancerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for subentry_id, engine in entry.runtime_data.engines.items():
        if not engine.allows_recovery:
            continue  # notify-only guard has nothing to toggle
        async_add_entities(
            [AutoRestartSwitch(engine, subentry_id)], config_subentry_id=subentry_id
        )


class AutoRestartSwitch(NecromancerEntity, SwitchEntity):
    """Enable/disable automatic recovery for this device."""

    _attr_translation_key = "auto_restart"
    _attr_icon = "mdi:auto-fix"

    def __init__(self, engine: DeviceEngine, subentry_id: str) -> None:
        super().__init__(engine, subentry_id, "auto_restart")

    @property
    def is_on(self) -> bool:
        return self._engine.auto

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._engine.set_auto(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._engine.set_auto(False)
