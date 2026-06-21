"""Health binary sensor for Necromancer."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NecromancerConfigEntry
from .core.engine import DeviceEngine
from .core.health import Health
from .entity import NecromancerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NecromancerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for subentry_id, engine in entry.runtime_data.engines.items():
        async_add_entities(
            [HealthBinarySensor(engine, subentry_id)], config_subentry_id=subentry_id
        )


class HealthBinarySensor(NecromancerEntity, BinarySensorEntity):
    """The aggregated health signal (on = healthy)."""

    _attr_translation_key = "health"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, engine: DeviceEngine, subentry_id: str) -> None:
        super().__init__(engine, subentry_id, "health")

    @property
    def available(self) -> bool:
        return self._engine.health.evaluate() != Health.UNKNOWN

    @property
    def is_on(self) -> bool:
        return self._engine.health.evaluate() == Health.OK
