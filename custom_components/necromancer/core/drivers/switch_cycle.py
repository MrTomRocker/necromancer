"""Recovery driver: power-cycle a switch (off → wait → on).

Whether recovery is then verified against the device's health entity is the
engine's job (the VERIFY step, controlled by the strategy's health-check flag);
this driver just does the power-cycle. The switch is always turned back on.
"""

from __future__ import annotations

import asyncio

from ...const import CONF_OFF_ON_DELAY, CONF_SWITCH_ENTITY, DEFAULT_OFF_ON_DELAY, LOGGER
from .base import RecoveryDriver


class SwitchCycleDriver(RecoveryDriver):
    """Turn a switch off → wait `off_on_delay` → on."""

    @property
    def switch_entity(self) -> str:
        return self.config[CONF_SWITCH_ENTITY]

    async def can_recover(self) -> tuple[bool, str]:
        if self.hass.states.get(self.switch_entity) is None:
            LOGGER.error("Switch entity %s not found", self.switch_entity)
            return False, f"switch entity {self.switch_entity} not found"
        return True, ""

    async def recover(self) -> None:
        delay = int(self.config.get(CONF_OFF_ON_DELAY, DEFAULT_OFF_ON_DELAY))
        LOGGER.debug("Cycling %s: off", self.switch_entity)
        await self.hass.services.async_call(
            "homeassistant",
            "turn_off",
            {"entity_id": self.switch_entity},
            blocking=True,
        )
        await asyncio.sleep(delay)
        LOGGER.debug("Cycling %s: on", self.switch_entity)
        await self.hass.services.async_call(
            "homeassistant", "turn_on", {"entity_id": self.switch_entity}, blocking=True
        )

    def target_info(self) -> str:
        return self.switch_entity

    def config_errors(self) -> list[str]:
        if self.hass.states.get(self.switch_entity) is None:
            return [f"switch entity {self.switch_entity} not found"]
        return []
