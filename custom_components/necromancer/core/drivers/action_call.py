"""Recovery driver: run a single user-defined action sequence.

The generic strategy — the action can call any service/script, an SSH command,
a webhook, a sequence with delays/conditions, … Whether the result is verified
against the device's health entity is the engine's job (health-check flag).
"""

from __future__ import annotations

import voluptuous as vol

from ...const import CONF_ACTION, LOGGER
from ..actions import async_run, async_validate, static_errors
from .base import RecoveryDriver


class ActionCallDriver(RecoveryDriver):
    """Run one action sequence, blocking until it finishes."""

    @property
    def action(self) -> list | dict | None:
        return self.config.get(CONF_ACTION)

    async def can_recover(self) -> tuple[bool, str]:
        if not self.action:
            return False, "no recovery action configured"
        try:
            await async_validate(self.hass, self.action)
        except vol.Invalid as err:
            LOGGER.error("Invalid recovery action: %s", err)
            return False, f"invalid recovery action: {err}"
        return True, ""

    async def recover(self) -> None:
        LOGGER.debug("Running recovery action")
        await async_run(self.hass, self.action, "necromancer recovery")

    def target_info(self) -> str:
        return "action"

    def config_errors(self) -> list[str]:
        if not self.action:
            return ["no recovery action configured"]
        return [f"recovery {err}" for err in static_errors(self.action)]
