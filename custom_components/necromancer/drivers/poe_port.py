"""Recovery driver: auto-resolve a device to its PoE port, then power-cycle it.

`poe_port` = `switch_cycle` with the actuator resolved at runtime instead of
fixed. The guard carries an `expected_id` (anything: MAC, hostname, neighbour);
each port in the flat port list reports an id (`id_source`: an entity/attribute
or a static value). The match (trim + lowercase) picks the port whose `actuator`
is cycled.

Verify is staged for diagnosis: (1) after cutting power the port's `status` must
go to its off value, (2) after restoring it must go to its on value. Stage 3 —
the device's health — is the engine's own VERIFY step (boot_window), so it is not
repeated here.

No match / ambiguous match -> `can_recover` blocks -> the engine escalates and
logs (no blind cycling). The port list lives in the service entry's options and is
injected into the driver config at setup (see __init__).
"""

from __future__ import annotations

import asyncio

from homeassistant.core import Event, callback
from homeassistant.helpers.event import async_track_state_change_event

from ..const import (
    CONF_ACTUATOR,
    CONF_EXPECTED_ID,
    CONF_ID_ATTRIBUTE,
    CONF_ID_ENTITY,
    CONF_ID_STATIC,
    CONF_LABEL,
    CONF_OFF_ON_DELAY,
    CONF_OFF_TIMEOUT,
    CONF_ON_TIMEOUT,
    CONF_PORTS,
    CONF_STATUS_ATTRIBUTE,
    CONF_STATUS_ENTITY,
    CONF_STATUS_OFF,
    CONF_STATUS_ON,
    DEFAULT_OFF_ON_DELAY,
    DEFAULT_PORT_OFF_TIMEOUT,
    DEFAULT_PORT_ON_TIMEOUT,
    LOGGER,
)
from .base import RecoveryDriver


def _norm(value: str | None) -> str | None:
    return value.strip().lower() if isinstance(value, str) else value


class PoePortDriver(RecoveryDriver):
    """Resolve `expected_id` to a port in the flat port list, then cycle it."""

    @property
    def expected_id(self) -> str:
        return self.config[CONF_EXPECTED_ID]

    @property
    def ports(self) -> list[dict]:
        return self.config.get(CONF_PORTS, [])

    def _port_id(self, port: dict) -> str | None:
        """The id a port currently reports (static, or live entity/attribute)."""
        if static := port.get(CONF_ID_STATIC):
            return static
        entity_id = port.get(CONF_ID_ENTITY)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        attribute = port.get(CONF_ID_ATTRIBUTE)
        value = state.state if not attribute else state.attributes.get(attribute)
        return None if value is None else str(value)

    def _matches(self) -> list[dict]:
        target = _norm(self.expected_id)
        return [port for port in self.ports if _norm(self._port_id(port)) == target]

    async def can_recover(self) -> tuple[bool, str]:
        matches = self._matches()
        if not matches:
            LOGGER.error("PoE %s: no matching port in the list", self.expected_id)
            return False, f"no port matches '{self.expected_id}'"
        if len(matches) > 1:
            LOGGER.error(
                "PoE %s: %s ports match (ambiguous)", self.expected_id, len(matches)
            )
            return False, f"'{self.expected_id}' matches {len(matches)} ports"
        return True, ""

    async def recover(self) -> None:
        matches = self._matches()
        if len(matches) != 1:
            LOGGER.error("PoE %s: cannot resolve a single port", self.expected_id)
            return
        port = matches[0]
        actuator = port[CONF_ACTUATOR]
        delay = int(port.get(CONF_OFF_ON_DELAY, DEFAULT_OFF_ON_DELAY))

        LOGGER.debug("PoE %s: cutting power via %s", self.expected_id, actuator)
        await self.hass.services.async_call(
            "homeassistant", "turn_off", {"entity_id": actuator}, blocking=True
        )
        await self._await_status(
            port,
            port[CONF_STATUS_OFF],
            int(port.get(CONF_OFF_TIMEOUT, DEFAULT_PORT_OFF_TIMEOUT)),
            "went offline",
        )

        await asyncio.sleep(delay)

        LOGGER.debug("PoE %s: restoring power via %s", self.expected_id, actuator)
        await self.hass.services.async_call(
            "homeassistant", "turn_on", {"entity_id": actuator}, blocking=True
        )
        await self._await_status(
            port,
            port[CONF_STATUS_ON],
            int(port.get(CONF_ON_TIMEOUT, DEFAULT_PORT_ON_TIMEOUT)),
            "came online",
        )

    async def _await_status(
        self, port: dict, expected: list | str, timeout: int, label: str
    ) -> bool:
        """Wait until the port's status entity/attribute reaches `expected`.

        `expected` is the list of values that count (a bare string is accepted).
        """
        entity_id = port[CONF_STATUS_ENTITY]
        attribute = port.get(CONF_STATUS_ATTRIBUTE)
        targets = (
            {str(v) for v in expected}
            if isinstance(expected, (list, tuple, set))
            else {str(expected)}
        )

        def current() -> str | None:
            state = self.hass.states.get(entity_id)
            if state is None:
                return None
            value = state.state if not attribute else state.attributes.get(attribute)
            return None if value is None else str(value)

        if current() in targets:
            return True

        reached = asyncio.Event()

        @callback
        def _changed(_event: Event) -> None:
            if current() in targets:
                reached.set()

        unsub = async_track_state_change_event(self.hass, [entity_id], _changed)
        try:
            await asyncio.wait_for(reached.wait(), timeout)
        except TimeoutError:
            LOGGER.warning(
                "PoE %s: port %s — status %s never reached %s on %s within %ss",
                self.expected_id,
                port.get(CONF_LABEL, "?"),
                label,
                expected,
                entity_id,
                timeout,
            )
            return False
        else:
            return True
        finally:
            unsub()

    def target_info(self) -> str:
        matches = self._matches()
        if len(matches) == 1:
            return f"{matches[0].get(CONF_LABEL, '?')} → {matches[0][CONF_ACTUATOR]}"
        return f"id={self.expected_id} ({len(self.ports)} port(s) in scope)"

    def config_errors(self) -> list[str]:
        if not self.ports:
            return [
                f"poe_port '{self.expected_id}': no ports configured — "
                "add ports in the integration's options"
            ]
        return []
