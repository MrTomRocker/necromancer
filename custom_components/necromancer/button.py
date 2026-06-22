"""Recover-now button for Necromancer."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
    """Set up the button platform from a config entry."""
    for subentry_id, engine in entry.runtime_data.engines.items():
        if not engine.allows_recovery:
            continue  # notify-only guard has no recover action
        async_add_entities(
            [RecoverButton(engine, subentry_id)], config_subentry_id=subentry_id
        )


class RecoverButton(NecromancerEntity, ButtonEntity):
    """Trigger a recovery cycle immediately."""

    _attr_translation_key = "recover"
    _attr_icon = "mdi:skull-crossbones"

    def __init__(self, engine: DeviceEngine, subentry_id: str) -> None:
        """Initialize the recover button."""
        super().__init__(engine, subentry_id, "recover")

    async def async_press(self) -> None:
        """Trigger a manual recovery cycle."""
        await self._engine.async_manual_recover()
