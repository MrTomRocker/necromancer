"""PoE fabric: resolve a device id to its port and repair (power-cycle) it.

The fabric is the shared, port-level recovery primitive. It keeps a live +
last-known ``id -> port`` map (watching every configured port's id-entity), a
per-port status (``good`` / ``recovering`` / ``failed``) and a per-port lock, and
exposes the ``necromancer.repair_poe_port`` service. Routing every PoE repair
through it means multiple guards — and other automations — coordinate on one port
instead of double-cycling it; the per-port status is fired as an event
(``necromancer_poe_port``) so anything can react to it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_ACTUATOR,
    CONF_ID_ATTRIBUTE,
    CONF_ID_ENTITY,
    CONF_ID_STATIC,
    CONF_LABEL,
    CONF_OFF_ON_DELAY,
    CONF_OFF_TIMEOUT,
    CONF_ON_TIMEOUT,
    CONF_STATUS_ATTRIBUTE,
    CONF_STATUS_ENTITY,
    CONF_STATUS_OFF,
    CONF_STATUS_ON,
    DEFAULT_OFF_ON_DELAY,
    DEFAULT_PORT_OFF_TIMEOUT,
    DEFAULT_PORT_ON_TIMEOUT,
    DOMAIN,
    LOGGER,
)

PORT_GOOD = "good"
PORT_RECOVERING = "recovering"
PORT_FAILED = "failed"

EVENT_PORT_STATUS = f"{DOMAIN}_poe_port"


# Values a port reports when *nothing* is connected — never a real device id.
_PLACEHOLDER_IDS = {"", "-", "unknown", "unavailable", "none"}


def _norm(value: object) -> str | None:
    """Normalize an id; placeholder/empty values collapse to None ("no id")."""
    if not isinstance(value, str):
        return value
    norm = value.strip().lower()
    return None if norm in _PLACEHOLDER_IDS else norm


class PoeFabric:
    """Port-level resolve + repair, shared by guards and the repair service."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._ports: list[dict] = []
        self._cache: dict[str, str] = {}  # normalized id -> port label (last-known)
        self._status: dict[str, str] = {}  # port label -> status
        self._locks: dict[str, asyncio.Lock] = {}
        self._unsub: Callable[[], None] | None = None

    # ---------- lifecycle ----------
    def set_ports(self, ports: list[dict], cache: dict[str, str] | None = None) -> None:
        """(Re)load the port list; seed the cache and start watching id-entities."""
        self._ports = list(ports)
        if cache:
            self._cache.update(cache)
        for port in self._ports:
            self._status.setdefault(port[CONF_LABEL], PORT_GOOD)
        self._rewatch()
        self._relearn()

    def shutdown(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    @property
    def cache(self) -> dict[str, str]:
        """The current id -> port-label map (for persistence)."""
        return dict(self._cache)

    def _rewatch(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        entities = [
            p[CONF_ID_ENTITY]
            for p in self._ports
            if p.get(CONF_ID_ENTITY) and not p.get(CONF_ID_STATIC)
        ]
        if entities:
            self._unsub = async_track_state_change_event(
                self.hass, entities, self._on_change
            )

    @callback
    def _on_change(self, _event: Event) -> None:
        self._relearn()

    def _learn(self, pid: str, label: str) -> None:
        """Record id -> port; INFO on first learn, WARNING on a move (re-cabling)."""
        prev = self._cache.get(pid)
        if prev == label:
            return
        if prev is None:
            LOGGER.info("PoE fabric: learned %r -> port %r", pid, label)
        else:
            LOGGER.warning("PoE fabric: %r moved port %r -> %r", pid, prev, label)
        self._cache[pid] = label

    def _relearn(self) -> None:
        """Refresh the last-known map from whatever the ports report right now."""
        for port in self._ports:
            pid = _norm(self._port_id(port))
            if pid:
                self._learn(pid, port[CONF_LABEL])

    # ---------- resolution ----------
    def _port_id(self, port: dict) -> str | None:
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

    def _by_label(self, label: str | None) -> dict | None:
        if not label:
            return None
        return next((p for p in self._ports if p.get(CONF_LABEL) == label), None)

    @property
    def port_count(self) -> int:
        """How many ports are configured (for a guard's config check)."""
        return len(self._ports)

    def resolve_with_reason(self, identifier: str) -> tuple[dict | None, str]:
        """Resolve an id to its port, with a human reason when it can't.

        One live match wins (and refreshes the cache); zero live falls back to the
        last-known cached port; an ambiguous (>1) match refuses to guess. Each
        port's reported id is traced at DEBUG so a resolution is fully auditable.
        """
        target = _norm(identifier)
        live: list[dict] = []
        for port in self._ports:
            pid = self._port_id(port)
            hit = target is not None and _norm(pid) == target
            LOGGER.debug(
                "PoE %s:   port %r reports id %r%s",
                identifier,
                port.get(CONF_LABEL),
                pid,
                "  <- MATCH" if hit else "",
            )
            if hit:
                live.append(port)
        if len(live) == 1:
            self._learn(target, live[0][CONF_LABEL])
            return live[0], ""
        if len(live) > 1:
            return None, f"'{identifier}' matches {len(live)} ports"
        port = self._by_label(self._cache.get(target))
        if port is not None:
            LOGGER.warning(
                "PoE fabric: %r not in any port's neighbour data — last-known port %r",
                identifier,
                port[CONF_LABEL],
            )
            return port, ""
        return None, f"no port matches '{identifier}'"

    def target_info(self, identifier: str) -> str:
        """Where an id currently resolves (for the guard's diagnostics line)."""
        target = _norm(identifier)
        live = [
            p
            for p in self._ports
            if target is not None and _norm(self._port_id(p)) == target
        ]
        if len(live) == 1:
            return f"{live[0].get(CONF_LABEL, '?')} → {live[0][CONF_ACTUATOR]}"
        cached = self._by_label(self._cache.get(target))
        if not live and cached is not None:
            return f"{cached[CONF_LABEL]} → {cached[CONF_ACTUATOR]} (last-known)"
        return f"id={identifier} ({len(self._ports)} port(s) in scope)"

    # ---------- status ----------
    def status(self, label: str) -> str:
        return self._status.get(label, PORT_GOOD)

    def all_status(self) -> dict[str, str]:
        return dict(self._status)

    def _set_status(self, label: str, status: str) -> None:
        if self._status.get(label) != status:
            self._status[label] = status
            LOGGER.debug("PoE port %r -> %s", label, status)
            self.hass.bus.async_fire(
                EVENT_PORT_STATUS, {"port": label, "status": status}
            )

    def _lock(self, label: str) -> asyncio.Lock:
        return self._locks.setdefault(label, asyncio.Lock())

    # ---------- repair ----------
    async def repair(self, identifier: str) -> bool:
        """Resolve the id to its port and power-cycle it (blocking, per port)."""
        port, reason = self.resolve_with_reason(identifier)
        if port is None:
            LOGGER.error("PoE fabric: cannot repair %r — %s", identifier, reason)
            return False
        label = port[CONF_LABEL]
        lock = self._lock(label)
        if lock.locked():
            LOGGER.info("PoE port %r already recovering — waiting for it", label)
        async with lock:
            self._set_status(label, PORT_RECOVERING)
            ok = await self._cycle(port)
            self._set_status(label, PORT_GOOD if ok else PORT_FAILED)
            if not ok:
                LOGGER.warning("PoE port %r: repair did not confirm online", label)
            return ok

    async def _cycle(self, port: dict) -> bool:
        actuator = port[CONF_ACTUATOR]
        label = port[CONF_LABEL]
        delay = int(port.get(CONF_OFF_ON_DELAY, DEFAULT_OFF_ON_DELAY))
        LOGGER.debug("PoE port %r: cutting power via %s", label, actuator)
        await self.hass.services.async_call(
            "homeassistant", "turn_off", {"entity_id": actuator}, blocking=True
        )
        await self._await_status(
            port,
            port[CONF_STATUS_OFF],
            int(port.get(CONF_OFF_TIMEOUT, DEFAULT_PORT_OFF_TIMEOUT)),
            "offline",
        )
        await asyncio.sleep(delay)
        LOGGER.debug("PoE port %r: restoring power via %s", label, actuator)
        await self.hass.services.async_call(
            "homeassistant", "turn_on", {"entity_id": actuator}, blocking=True
        )
        return await self._await_status(
            port,
            port[CONF_STATUS_ON],
            int(port.get(CONF_ON_TIMEOUT, DEFAULT_PORT_ON_TIMEOUT)),
            "online",
        )

    async def _await_status(
        self, port: dict, expected: list | str, timeout: int, label_text: str
    ) -> bool:
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
                "PoE port %r: status never reached %s (%s) on %s within %ss",
                port.get(CONF_LABEL, "?"),
                expected,
                label_text,
                entity_id,
                timeout,
            )
            return False
        else:
            return True
        finally:
            unsub()
