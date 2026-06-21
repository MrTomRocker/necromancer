"""Base entity wiring to a DeviceEngine.

One guarded device = one config subentry. Entities are linked to the subentry
via `config_subentry_id` at add time.

Device association:
- standalone (default): we spawn our own device via `device_info`.
- linked: if the subentry points at an existing HA device, our `device_info`
  reuses that device's identifiers/connections, so entity_platform attaches our
  entities (and our subentry) to it — the guarded device then shows up under our
  subentry in the UI. `_reconcile_devices` in __init__ detaches us again on
  unlink, so this stays clean.
"""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .core.engine import DeviceEngine


class NecromancerEntity(Entity):
    """Common base: device info + live updates from the engine."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, engine: DeviceEngine, subentry_id: str, key: str) -> None:
        self._engine = engine
        self._subentry_id = subentry_id
        self._attr_unique_id = f"{subentry_id}_{key}"

        linked = self._linked_device(engine)
        if linked is not None:
            # Reuse the target device's identity -> attach to it (and surface our
            # subentry on it) without overwriting its name/model.
            self._attr_device_info = DeviceInfo(
                identifiers=linked.identifiers,
                connections=linked.connections,
            )
        else:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, subentry_id)},
                name=engine.name,
                manufacturer="Necromancer",
                model="Monitored device",
            )

    @staticmethod
    def _linked_device(engine: DeviceEngine) -> dr.DeviceEntry | None:
        if engine.link_device_id:
            return dr.async_get(engine.hass).async_get(engine.link_device_id)
        return None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._engine.add_listener(self.async_write_ha_state))
