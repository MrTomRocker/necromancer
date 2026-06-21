"""Recovery driver: run an "off" action, wait, then an "on" action.

A power-cycle expressed as two user-defined action sequences (e.g. cut power via
one service, restore via another) with a delay in between. Whether the result is
verified against the device's health entity is the engine's job (health-check
flag).
"""

from __future__ import annotations

import asyncio

import voluptuous as vol

from ...const import (
    CONF_OFF_ACTION,
    CONF_OFF_ON_DELAY,
    CONF_ON_ACTION,
    DEFAULT_OFF_ON_DELAY,
    LOGGER,
)
from ..actions import async_run, async_validate, static_errors
from .base import RecoveryDriver


class ActionCycleDriver(RecoveryDriver):
    """Run the off action → wait `off_on_delay` → run the on action."""

    @property
    def off_action(self) -> list | dict | None:
        return self.config.get(CONF_OFF_ACTION)

    @property
    def on_action(self) -> list | dict | None:
        return self.config.get(CONF_ON_ACTION)

    async def can_recover(self) -> tuple[bool, str]:
        for label, action in (("off", self.off_action), ("on", self.on_action)):
            if not action:
                return False, f"no {label} action configured"
            try:
                await async_validate(self.hass, action)
            except vol.Invalid as err:
                LOGGER.error("Invalid %s action: %s", label, err)
                return False, f"invalid {label} action: {err}"
        return True, ""

    async def recover(self) -> None:
        delay = int(self.config.get(CONF_OFF_ON_DELAY, DEFAULT_OFF_ON_DELAY))
        LOGGER.debug("Recovery: off action")
        await async_run(self.hass, self.off_action, "necromancer recovery (off)")
        await asyncio.sleep(delay)
        LOGGER.debug("Recovery: on action")
        await async_run(self.hass, self.on_action, "necromancer recovery (on)")

    def target_info(self) -> str:
        return "off/on actions"

    def config_errors(self) -> list[str]:
        errors: list[str] = []
        for label, action in (("off", self.off_action), ("on", self.on_action)):
            if not action:
                errors.append(f"no {label} action configured")
            else:
                errors += [f"{label} {err}" for err in static_errors(action)]
        return errors
