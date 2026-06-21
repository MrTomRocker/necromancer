"""Status sensor for Necromancer (display only; state lives in the Store)."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NecromancerConfigEntry
from .core.engine import DeviceEngine, GState
from .entity import NecromancerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NecromancerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for subentry_id, engine in entry.runtime_data.items():
        async_add_entities(
            [StatusSensor(engine, subentry_id)], config_subentry_id=subentry_id
        )


class StatusSensor(NecromancerEntity, SensorEntity):
    """Current lifecycle state of the guarded device."""

    _attr_translation_key = "status"
    _attr_icon = "mdi:heart-pulse"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [s.value for s in GState]

    def __init__(self, engine: DeviceEngine, subentry_id: str) -> None:
        super().__init__(engine, subentry_id, "status")

    @property
    def native_value(self) -> str:
        return self._engine.state.value

    @property
    def extra_state_attributes(self) -> dict:
        e = self._engine
        return {
            "attempt": e.attempt,
            "recover_count": e.recover_count,
            "last_recover": e.last_recover,
            "last_seen": e.last_seen,
            "target": e.driver.target_info(),
            "auto_restart": e.auto,
        }
