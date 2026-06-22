"""Recovery driver: run a single user-defined action sequence.

The generic strategy — the action can call any service/script, an SSH command,
a webhook, a sequence with delays/conditions, … Whether the result is verified
against the device's health entity is the engine's job (health-check flag).
"""

from __future__ import annotations

import logging

import voluptuous as vol

from ...const import CONF_ACTION
from ..actions import async_run, async_validate, static_errors
from .base import RecoveryDriver

LOGGER = logging.getLogger(__name__)


class ActionCallDriver(RecoveryDriver):
    """Run one action sequence, blocking until it finishes."""

    @property
    def action(self) -> list | dict | None:
        """Return the configured recovery action sequence, if any."""
        return self.config.get(CONF_ACTION)

    async def can_recover(self) -> tuple[bool, str]:
        """Refuse unless an action is configured and validates."""
        if not self.action:
            return False, "no recovery action configured"
        try:
            await async_validate(self.hass, self.action)
        except vol.Invalid as err:
            LOGGER.error("Invalid recovery action: %s", err)
            return False, f"invalid recovery action: {err}"
        return True, ""

    async def recover(self, variables: dict | None = None) -> None:
        """Run the configured action, blocking until it finishes."""
        LOGGER.debug("Running recovery action")
        await async_run(self.hass, self.action, "necromancer recovery", variables)

    def target_info(self) -> str:
        """Return a short human description of the recovery target."""
        return "action"

    def config_errors(self) -> list[str]:
        """Return static config errors for the recovery action, if any."""
        if not self.action:
            return ["no recovery action configured"]
        return [f"recovery {err}" for err in static_errors(self.action)]
