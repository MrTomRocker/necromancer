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
from collections.abc import Callable

from homeassistant.core import Event, HomeAssistant, callback
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

    def __init__(self, hass: HomeAssistant, config: dict) -> None:
        super().__init__(hass, config)
        # Port chosen by can_recover, reused by recover (resolve once per cycle).
        self._selected: dict | None = None

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

    def _live_matches(self, *, trace: bool = False) -> list[dict]:
        """Ports whose currently-reported id equals the expected id.

        With ``trace`` each port's reported id and match verdict is logged at
        DEBUG, so a recovery's port resolution is fully traceable.
        """
        target = _norm(self.expected_id)
        matches: list[dict] = []
        for port in self.ports:
            pid = self._port_id(port)
            hit = _norm(pid) == target
            if trace:
                LOGGER.debug(
                    "PoE %s:   port %r reports id %r%s",
                    self.expected_id,
                    port.get(CONF_LABEL),
                    pid,
                    "  <- MATCH" if hit else "",
                )
            if hit:
                matches.append(port)
        return matches

    def _port_by_label(self, label: str | None) -> dict | None:
        if not label:
            return None
        return next((p for p in self.ports if p.get(CONF_LABEL) == label), None)

    def _remember(self, label: str | None) -> None:
        if not label or self._cache_set is None:
            return
        current = self._cache_get() if self._cache_get is not None else None
        if current is None:
            LOGGER.info("PoE %s: learned port %r", self.expected_id, label)
        elif label != current:
            # The device is now seen on a different port than last known — worth a
            # human glance (re-cabling, or a MAC showing up on the wrong port).
            LOGGER.warning(
                "PoE %s: resolved port changed %r -> %r",
                self.expected_id,
                current,
                label,
            )
        self._cache_set(label)

    def observe(self) -> None:
        """While healthy: cache the single port the device currently sits on.

        This is what makes the fallback possible — once a device goes down it may
        age out of the switch's neighbour table, so its port must be learned while
        it is still visible.
        """
        matches = self._live_matches()
        if len(matches) == 1:
            self._remember(matches[0].get(CONF_LABEL))

    async def async_setup(self) -> Callable[[], None] | None:
        """Refresh the cache whenever a port's id-entity changes.

        The switch's neighbour table updates independently of the device's health
        entity, so observing only on health events would miss the window where the
        device is resolvable. Watch every dynamic port's id-entity and re-learn.
        """
        self.observe()
        entities = [
            p[CONF_ID_ENTITY]
            for p in self.ports
            if p.get(CONF_ID_ENTITY) and not p.get(CONF_ID_STATIC)
        ]
        if not entities:
            return None
        LOGGER.debug(
            "PoE %s: watching %d id-entity(ies) to keep the cached port fresh",
            self.expected_id,
            len(entities),
        )

        @callback
        def _changed(_event: Event) -> None:
            self.observe()

        return async_track_state_change_event(self.hass, entities, _changed)

    def _select(self) -> tuple[dict | None, str]:
        """Pick the port to cycle.

        A single live match wins (and refreshes the cache); on *no* live match we
        fall back to the last-known cached port (the device aged out while down);
        an ambiguous (>1) live match blocks — a config issue, never guess.
        """
        LOGGER.debug(
            "PoE %s: resolving among %d configured port(s)",
            self.expected_id,
            len(self.ports),
        )
        matches = self._live_matches(trace=True)
        if len(matches) == 1:
            label = matches[0].get(CONF_LABEL)
            LOGGER.debug("PoE %s: resolved live to port %r", self.expected_id, label)
            self._remember(label)
            return matches[0], ""
        if len(matches) > 1:
            LOGGER.debug(
                "PoE %s: %d ports match — ambiguous, refusing to guess",
                self.expected_id,
                len(matches),
            )
            return None, f"'{self.expected_id}' matches {len(matches)} ports"
        cached = self._cache_get() if self._cache_get is not None else None
        port = self._port_by_label(cached)
        if port is not None:
            LOGGER.warning(
                "PoE %s: not visible in any port's neighbour data — falling back "
                "to last-known port '%s' (from persistence)",
                self.expected_id,
                cached,
            )
            return port, ""
        LOGGER.debug(
            "PoE %s: no live match and no cached port — cannot resolve",
            self.expected_id,
        )
        return None, f"no port matches '{self.expected_id}'"

    async def can_recover(self) -> tuple[bool, str]:
        port, reason = self._select()
        self._selected = port
        if port is None:
            LOGGER.error("PoE %s: %s", self.expected_id, reason)
            return False, reason
        return True, ""

    async def recover(self) -> None:
        port = self._selected or self._select()[0]
        self._selected = None
        if port is None:
            LOGGER.error("PoE %s: cannot resolve a single port", self.expected_id)
            return
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
        matches = self._live_matches()
        if len(matches) == 1:
            return f"{matches[0].get(CONF_LABEL, '?')} → {matches[0][CONF_ACTUATOR]}"
        cached = self._cache_get() if self._cache_get is not None else None
        port = self._port_by_label(cached)
        if not matches and port is not None:
            return f"{cached} → {port[CONF_ACTUATOR]} (last-known)"
        return f"id={self.expected_id} ({len(self.ports)} port(s) in scope)"

    def config_errors(self) -> list[str]:
        if not self.ports:
            return [
                f"poe_port '{self.expected_id}': no ports configured — "
                "add ports in the integration's options"
            ]
        return []
