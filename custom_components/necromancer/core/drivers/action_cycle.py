"""Recovery driver: run an "off" action, wait, then an "on" action.

A power-cycle expressed as two user-defined action sequences (e.g. cut power via
one service, restore via another) with a delay in between. Whether the result is
verified against the device's health entity is the engine's job (health-check
flag).
"""

from __future__ import annotations

import asyncio
import logging

import voluptuous as vol

from ...const import (
    CONF_OFF_ACTION,
    CONF_OFF_ON_DELAY,
    CONF_ON_ACTION,
    DEFAULT_OFF_ON_DELAY,
)
from ..actions import async_run, async_validate, static_errors
from .base import RecoveryDriver

LOGGER = logging.getLogger(__name__)


class ActionCycleDriver(RecoveryDriver):
    """Run the off action → wait `off_on_delay` → run the on action."""

    @property
    def off_action(self) -> list | dict | None:
        """Return the configured off action sequence, if any."""
        return self.config.get(CONF_OFF_ACTION)

    @property
    def on_action(self) -> list | dict | None:
        """Return the configured on action sequence, if any."""
        return self.config.get(CONF_ON_ACTION)

    async def can_recover(self) -> tuple[bool, str]:
        """Refuse unless both off and on actions are configured and valid."""
        for label, action in (("off", self.off_action), ("on", self.on_action)):
            if not action:
                return False, f"no {label} action configured"
            try:
                await async_validate(self.hass, action)
            except vol.Invalid as err:
                LOGGER.error("Invalid %s action: %s", label, err)
                return False, f"invalid {label} action: {err}"
        return True, ""

    async def recover(self, variables: dict | None = None) -> None:
        """Run the off action, wait `off_on_delay`, then the on action.

        The engine's run context (`variables`: `attempt`, `max`, …) seeds the off
        action, and the off action's final variables flow into the on action — so
        engine vars and any `variables:` set during the off phase are both
        readable in the on phase, no external helper needed across the power-cycle.
        """
        delay = int(self.config.get(CONF_OFF_ON_DELAY, DEFAULT_OFF_ON_DELAY))
        LOGGER.debug("Recovery: off action")
        off_vars = await async_run(
            self.hass, self.off_action, "necromancer recovery (off)", variables
        )
        await asyncio.sleep(delay)
        LOGGER.debug("Recovery: on action")
        await async_run(
            self.hass, self.on_action, "necromancer recovery (on)", variables=off_vars
        )

    def target_info(self) -> str:
        """Return a short human description of the recovery target."""
        return "off/on actions"

    def config_errors(self) -> list[str]:
        """Return static config errors for the off and on actions, if any."""
        errors: list[str] = []
        for label, action in (("off", self.off_action), ("on", self.on_action)):
            if not action:
                errors.append(f"no {label} action configured")
            else:
                errors += [f"{label} {err}" for err in static_errors(action)]
        return errors
