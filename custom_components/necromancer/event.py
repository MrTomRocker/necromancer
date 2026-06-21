"""Recovery event entity for Necromancer.

Fires a typed event on each recovery outcome so dashboards and automations can
react to (and historise) the lifecycle without parsing logs or the notify action.
Recover-capable guards only — notify-only guards never recover.
"""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
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
            continue  # notify-only guard has no recovery events
        async_add_entities(
            [RecoveryEvent(engine, subentry_id)], config_subentry_id=subentry_id
        )


class RecoveryEvent(NecromancerEntity, EventEntity):
    """Fires on each recovery outcome: recovered / escalated / blocked."""

    _attr_translation_key = "recovery"
    _attr_icon = "mdi:pulse"
    _attr_event_types = ["recovered", "escalated", "blocked"]

    def __init__(self, engine: DeviceEngine, subentry_id: str) -> None:
        super().__init__(engine, subentry_id, "recovery_event")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._engine.add_event_listener(self._handle))

    @callback
    def _handle(self, event_type: str, data: dict) -> None:
        self._trigger_event(event_type, data)
        self.async_write_ha_state()
