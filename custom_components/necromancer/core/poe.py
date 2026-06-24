"""PoE fabric: resolve a device id to its port and repair (power-cycle) it.

The fabric is the shared, port-level recovery primitive. It keeps a live +
last-known ``id -> port`` map (watching every configured port's id-entity), a
per-port status (``good`` / ``recovering`` / ``failed``) and a per-port in-flight
cycle, and exposes the ``necromancer.repair_poe_port`` service. Routing every PoE
repair through it means multiple guards — and other automations — that fire at
once **coalesce** onto one cycle instead of double-cycling the port; the per-port
status is fired as an event (``necromancer_poe_port``) so anything can react to it.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from ..const import (
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
)

LOGGER = logging.getLogger(__name__)

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
        """Initialize empty port list, caches, status map and listener handle."""
        self.hass = hass
        self._ports: list[dict] = []
        self._cache: dict[str, str] = {}  # normalized id -> port label (last-known)
        self._status: dict[str, str] = {}  # port label -> status
        self._inflight: dict[str, asyncio.Task] = {}  # port label -> running cycle
        self._unsub: CALLBACK_TYPE | None = None

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
        """Cancel listeners and in-flight cycles on unload."""
        if self._unsub:
            self._unsub()
            self._unsub = None

    @property
    def cache(self) -> dict[str, str]:
        """The current id -> port-label map (for persistence)."""
        return dict(self._cache)

    def _rewatch(self) -> None:
        """Re-subscribe to every configured port's id-entity."""
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
        """Re-learn the id->port cache when a watched id-entity changes."""
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
        """Read a port's current id (static, or from its id-entity).

        A port gives its id ONE way. If it has both a static value and an
        id-entity it is misconfigured — ignore it (return None, so it matches no
        guard and the guard blocks) and warn, rather than silently picking one.
        """
        static = port.get(CONF_ID_STATIC)
        entity_id = port.get(CONF_ID_ENTITY)
        if static and entity_id:
            LOGGER.warning(
                "PoE port %r is misconfigured: both a fixed id and an id-entity set "
                "— ignoring it; remove one",
                port.get(CONF_LABEL),
            )
            return None
        if static:
            return static
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        attribute = port.get(CONF_ID_ATTRIBUTE)
        value = state.state if not attribute else state.attributes.get(attribute)
        return None if value is None else str(value)

    def _by_label(self, label: str | None) -> dict | None:
        """Find a configured port by its label."""
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
            # Only trust the last-known port if it currently reports *nothing*
            # connected. If it now reports a different live id, the device was
            # re-cabled away and another one sits there — cycling it would reboot
            # the wrong device, so drop the stale entry and refuse instead.
            occupant = _norm(self._port_id(port))
            if occupant is None:
                LOGGER.warning(
                    "PoE fabric: %r not in any port's neighbour data — last-known port %r",
                    identifier,
                    port[CONF_LABEL],
                )
                return port, ""
            LOGGER.warning(
                "PoE fabric: last-known port %r for %r now serves %r — dropping stale cache",
                port[CONF_LABEL],
                identifier,
                occupant,
            )
            self._cache.pop(target, None)
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
        """Return a port's current status (defaults to good)."""
        return self._status.get(label, PORT_GOOD)

    def all_status(self) -> dict[str, str]:
        """Return a snapshot of every port's status by label."""
        return dict(self._status)

    def _set_status(self, label: str, status: str) -> None:
        """Update a port's status and fire the change event only on transitions."""
        if self._status.get(label) != status:
            self._status[label] = status
            LOGGER.debug("PoE port %r -> %s", label, status)
            self.hass.bus.async_fire(
                EVENT_PORT_STATUS, {"port": label, "status": status}
            )

    # ---------- repair ----------
    async def repair(self, identifier: str) -> bool:
        """Resolve the id to its port and power-cycle it (blocking, per port).

        Concurrent callers for the same port **coalesce**: a call that arrives
        while a cycle is in flight joins it and shares its result instead of
        queuing a second power-cycle. So multiple guards (and automations) firing
        at once produce exactly one cycle per port, not one each.
        """
        port, reason = self.resolve_with_reason(identifier)
        if port is None:
            LOGGER.error("PoE fabric: cannot repair %r — %s", identifier, reason)
            return False
        label = port[CONF_LABEL]
        task = self._inflight.get(label)
        if task is not None and not task.done():
            LOGGER.info(
                "PoE port %r already recovering — joining in-flight cycle", label
            )
            return await asyncio.shield(task)
        # Create + register the cycle task synchronously (no await in between), so a
        # caller arriving in the same tick joins it instead of starting a second.
        task = self.hass.async_create_task(self._run_cycle(port))
        self._inflight[label] = task
        try:
            return await task
        finally:
            self._inflight.pop(label, None)

    async def _run_cycle(self, port: dict) -> bool:
        """Mark the port recovering, run one cycle, then set good/failed by result."""
        label = port[CONF_LABEL]
        self._set_status(label, PORT_RECOVERING)
        ok = await self._cycle(port)
        self._set_status(label, PORT_GOOD if ok else PORT_FAILED)
        if not ok:
            LOGGER.warning("PoE port %r: repair did not confirm online", label)
        return ok

    async def _cycle(self, port: dict) -> bool:
        """Cut power, wait for offline, pause, restore power, wait for online."""
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
        """Wait until the port's status entity reaches an expected value or times out."""
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
