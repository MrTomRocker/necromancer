"""Status sensor for Necromancer (display only; state lives in the Store)."""

from __future__ import annotations

from datetime import timedelta

import voluptuous as vol

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NecromancerConfigEntry
from .const import ATTR_DURATION, SERVICE_RESET, SERVICE_SNOOZE, SERVICE_UNSNOOZE
from .core.engine import DeviceEngine, GState
from .entity import NecromancerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NecromancerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for subentry_id, engine in entry.runtime_data.engines.items():
        async_add_entities(
            [StatusSensor(engine, subentry_id)], config_subentry_id=subentry_id
        )

    # Per-guard operator services, targeted at the status sensor (device/area
    # targets expand to it). The status sensor is every guard's stable anchor.
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(SERVICE_RESET, None, "async_reset")
    platform.async_register_entity_service(
        SERVICE_SNOOZE,
        {vol.Required(ATTR_DURATION): cv.positive_time_period},
        "async_snooze",
    )
    platform.async_register_entity_service(SERVICE_UNSNOOZE, None, "async_unsnooze")


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
            "target": e.driver.target_info(),
            "snooze_until": e._snooze_until,
        }

    # ---------- operator services (registered above) ----------
    async def async_reset(self) -> None:
        """necromancer.reset — clear an ESCALATED guard."""
        self._engine.reset()

    async def async_snooze(self, duration: timedelta) -> None:
        """necromancer.snooze — suspend guarding for a while."""
        self._engine.snooze(duration)

    async def async_unsnooze(self) -> None:
        """necromancer.unsnooze — lift a snooze early."""
        self._engine.unsnooze()
