"""The Necromancer engine — per-device self-healing state machine.

Fixed runtime; Health / Policy / Driver are pluggable:

  OK --(unhealthy)--> SUSPECT --(debounce)--> RECOVERING --> VERIFY(boot_window)
  VERIFY: ok -> COOLDOWN -> OK | fail&retry<max -> RECOVERING | else -> ESCALATED
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import EventEntityRegistryUpdatedData
from homeassistant.helpers.event import (
    async_call_later,
    async_track_entity_registry_updated_event,
    async_track_state_change_event,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AUTO_RESTART,
    CONF_BOOT_WINDOW,
    CONF_COOLDOWN,
    CONF_DEBOUNCE,
    CONF_HEALTH_CHECK,
    CONF_MAX_ATTEMPTS,
    CONF_NOTIFY_ACTION,
    DEFAULT_AUTO_RESTART,
    DEFAULT_BOOT_WINDOW,
    DEFAULT_COOLDOWN,
    DEFAULT_DEBOUNCE,
    DEFAULT_MAX_ATTEMPTS,
    LOGGER,
)
from .drivers import RecoveryDriver
from .health import Health, HealthSource
from .notify import async_notify
from .policies import RecoveryPolicy


def _noop() -> None:
    """Default save callback when none is provided (e.g. in tests)."""


class GState(StrEnum):
    OK = "ok"
    SUSPECT = "suspect"
    RECOVERING = "recovering"
    VERIFY = "verify"
    COOLDOWN = "cooldown"
    ESCALATED = "escalated"


class DeviceEngine:
    """Runs the self-healing lifecycle for one guarded device."""

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        health: HealthSource,
        driver: RecoveryDriver,
        policy: RecoveryPolicy,
        behavior: dict,
        link_device_id: str | None = None,
        persisted: dict | None = None,
        save: Callable[[], None] | None = None,
        on_health_renamed: Callable[[str], None] | None = None,
    ) -> None:
        self.hass = hass
        self.name = name
        self.health = health
        self.driver = driver
        self.policy = policy
        self.behavior = behavior
        # Optional: attach our entities to an existing HA device instead of
        # spawning a standalone one.
        self.link_device_id = link_device_id
        self._save = save or _noop
        self._on_health_renamed = on_health_renamed

        self.state = GState.OK
        self.attempt = 0
        self.recover_count = 0
        self.last_seen: datetime | None = None
        self.last_recover: datetime | None = None
        self.auto = bool(behavior.get(CONF_AUTO_RESTART, DEFAULT_AUTO_RESTART))
        # Last-known resolved target for the driver (e.g. the poe_port port label),
        # persisted so a down device that has aged out of the switch's neighbour
        # table can still be recovered via the cached port.
        self.resolved_port: str | None = None

        self._unsub_health: Callable[[], None] | None = None
        self._unsub_registry: Callable[[], None] | None = None
        self._unsub_started: Callable[[], None] | None = None
        self._unsub_source: Callable[[], None] | None = None
        self._unsub_driver: Callable[[], None] | None = None
        self._unsub_timer: Callable[[], None] | None = None
        self._verify_event: asyncio.Event | None = None
        self._cycle_task: asyncio.Task | None = None
        self._listeners: list[Callable[[], None]] = []

        self._apply_persisted(persisted or {})
        # Wire the driver's persistent resolution cache to our Store-backed state.
        self.driver.bind_cache(self._get_resolved_port, self._set_resolved_port)

    def _apply_persisted(self, data: dict) -> None:
        """Seed runtime state from the Store (entity-independent persistence).

        Stats + the `auto` flag are always restored. A terminal ESCALATED verdict
        is restored so a dead device gets no free retry on reboot (auto-clears via
        ESCALATED->OK once health returns). Transient states are re-derived from
        live health by the first evaluation in async_start.
        """
        self.recover_count = int(data.get("recover_count", 0) or 0)
        self.last_recover = dt_util.parse_datetime(data.get("last_recover") or "")
        self.last_seen = dt_util.parse_datetime(data.get("last_seen") or "")
        if "auto" in data:
            self.auto = bool(data["auto"])
        if data.get("resolved_port"):
            self.resolved_port = data["resolved_port"]
            LOGGER.debug(
                "%s: restored resolved_port %r from Store",
                self.name,
                self.resolved_port,
            )
        if data.get("state") == GState.ESCALATED.value:
            self.state = GState.ESCALATED
            self.attempt = int(data.get("attempt", 0) or 0)

    def snapshot(self) -> dict:
        """Serialise persistent runtime state for the Store."""
        return {
            "state": self.state.value,
            "attempt": self.attempt,
            "recover_count": self.recover_count,
            "last_recover": self.last_recover.isoformat()
            if self.last_recover
            else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "auto": self.auto,
            "resolved_port": self.resolved_port,
        }

    # ---------- lifecycle ----------
    async def async_start(self) -> None:
        watched = self.health.watched_entities
        self._unsub_health = async_track_state_change_event(
            self.hass, watched, self._handle_health_event
        )
        # Follow renames / removals of the health entity (we are event-driven, so
        # a renamed entity would otherwise silently stop reaching us).
        self._unsub_registry = async_track_entity_registry_updated_event(
            self.hass, watched, self._handle_registry_event
        )
        LOGGER.debug("Watching %s for %s", watched, self.name)
        # Validate referenced entities/services once HA is fully started (so a
        # service that simply hasn't registered yet during boot isn't flagged).
        self._unsub_started = async_at_started(self.hass, self._check_config)
        # Sources that track something else (e.g. a template) register here and
        # call _evaluate on change; state sources use watched_entities above.
        self._unsub_source = await self.health.async_setup(self._evaluate)
        # The driver may watch its own inputs (poe_port: the port id-entities, so
        # it caches the resolved port the moment the neighbour table reports it).
        self._unsub_driver = await self.driver.async_setup()
        self._evaluate()

    @callback
    def _check_config(self, _hass: HomeAssistant) -> None:
        """Log config errors (missing referenced entities/services) at ERROR."""
        ent_reg = er.async_get(self.hass)
        for eid in self.health.watched_entities:
            entry = ent_reg.async_get(eid)
            if entry is None and self.hass.states.get(eid) is None:
                LOGGER.error("%s: health entity %s does not exist", self.name, eid)
            elif entry is not None and entry.disabled:
                LOGGER.error(
                    "%s: health entity %s is disabled — guard is blind",
                    self.name,
                    eid,
                )
        for err in self.driver.config_errors():
            LOGGER.error("%s: %s", self.name, err)

    async def async_stop(self) -> None:
        LOGGER.debug("Stopping engine for %s", self.name)
        if self._unsub_health:
            self._unsub_health()
            self._unsub_health = None
        if self._unsub_registry:
            self._unsub_registry()
            self._unsub_registry = None
        if self._unsub_started:
            self._unsub_started()
            self._unsub_started = None
        if self._unsub_source:
            self._unsub_source()
            self._unsub_source = None
        if self._unsub_driver:
            self._unsub_driver()
            self._unsub_driver = None
        self._cancel_timer()
        if self._cycle_task and not self._cycle_task.done():
            self._cycle_task.cancel()

    @callback
    def _handle_registry_event(
        self, event: Event[EventEntityRegistryUpdatedData]
    ) -> None:
        data = event.data
        eid = data["entity_id"]
        if data["action"] == "remove":
            LOGGER.error("%s: health entity %s was removed", self.name, eid)
            return
        if data["action"] != "update":
            return
        changes = data.get("changes", {})
        if (old := data.get("old_entity_id")) and old != eid:
            LOGGER.info("Health entity for %s renamed %s -> %s", self.name, old, eid)
            if self._on_health_renamed:
                self._on_health_renamed(eid)
        elif "disabled_by" in changes:
            entry = er.async_get(self.hass).async_get(eid)
            if entry is not None and entry.disabled:
                LOGGER.error(
                    "%s: health entity %s is disabled — guard is blind", self.name, eid
                )
            else:
                LOGGER.info("%s: health entity %s re-enabled", self.name, eid)

    # ---------- entity glue ----------
    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)

        def _remove() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)

        return _remove

    @callback
    def _emit(self) -> None:
        for cb in list(self._listeners):
            cb()

    def _set_state(self, state: GState) -> None:
        if state != self.state:
            self.state = state
            LOGGER.debug("%s entered state %s", self.name, state)
            self._save()
        self._emit()

    def _int(self, key: str, default: int) -> int:
        try:
            return int(self.behavior.get(key, default))
        except (TypeError, ValueError):
            return default

    @property
    def max_attempts(self) -> int:
        return self._int(CONF_MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS)

    @property
    def allows_recovery(self) -> bool:
        """False for a notify-only guard (no auto switch, no recover button)."""
        return self.policy.allows_recovery

    def set_auto(self, value: bool) -> None:
        """Toggle auto-recovery, persist it, and refresh entities."""
        self.auto = value
        self._save()
        self._emit()

    def _get_resolved_port(self) -> str | None:
        return self.resolved_port

    def _set_resolved_port(self, label: str | None) -> None:
        """Driver callback: persist the last-known resolved target on change."""
        if label != self.resolved_port:
            LOGGER.debug(
                "%s: persisting resolved_port %r (was %r)",
                self.name,
                label,
                self.resolved_port,
            )
            self.resolved_port = label
            self._save()

    def _cancel_timer(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None

    # ---------- health handling ----------
    @callback
    def _handle_health_event(self, event) -> None:
        self._evaluate()

    @callback
    def _evaluate(self) -> None:
        h = self.health.evaluate()
        LOGGER.debug("%s health=%s state=%s", self.name, h, self.state)
        if h == Health.OK:
            self.last_seen = dt_util.utcnow()
            # Learn the driver's current target while healthy (poe_port cache).
            self.driver.observe()
        if self._verify_event is not None and h == Health.OK:
            self._verify_event.set()

        if self.state == GState.OK and h == Health.UNHEALTHY:
            self._enter_suspect()
        elif self.state == GState.SUSPECT and h == Health.OK:
            self._cancel_timer()
            self._set_state(GState.OK)
        elif self.state == GState.ESCALATED and h == Health.OK:
            self.attempt = 0
            self._set_state(GState.OK)
        else:
            self._emit()

    # ---------- transitions ----------
    def _enter_suspect(self) -> None:
        debounce = self._int(CONF_DEBOUNCE, DEFAULT_DEBOUNCE)
        LOGGER.info("%s unhealthy, waiting %ss (debounce)", self.name, debounce)
        self._set_state(GState.SUSPECT)
        self._cancel_timer()
        self._unsub_timer = async_call_later(self.hass, debounce, self._debounce_done)

    @callback
    def _debounce_done(self, _now) -> None:
        self._unsub_timer = None
        if self.state != GState.SUSPECT:
            return
        if self.health.evaluate() != Health.UNHEALTHY:
            LOGGER.debug("%s recovered during debounce", self.name)
            self._set_state(GState.OK)
            return
        allowed, reason = self.policy.should_attempt(auto_enabled=self.auto)
        if not allowed:
            if reason == "observe":
                LOGGER.info("%s problem detected (notify-only)", self.name)
                self.hass.async_create_task(self._notify("problem_detected"))
            else:
                LOGGER.warning(
                    "%s still unhealthy but auto-recovery is off (%s)",
                    self.name,
                    reason,
                )
                self.hass.async_create_task(
                    self._notify("no_auto_recovery", reason=reason)
                )
            self._set_state(GState.ESCALATED)
            return
        LOGGER.info("%s debounce elapsed, starting recovery", self.name)
        self._start_cycle()

    def _start_cycle(self) -> None:
        if self._cycle_task and not self._cycle_task.done():
            return
        self._cycle_task = self.hass.async_create_task(self._run_recovery_cycle())

    async def async_manual_recover(self) -> None:
        """Button: force a recovery cycle now (bypasses debounce + auto gate)."""
        LOGGER.info("%s manual recovery requested", self.name)
        self.attempt = 0
        self._cancel_timer()
        self._start_cycle()

    async def _run_recovery_cycle(self) -> None:
        try:
            while True:
                self.attempt += 1
                self._set_state(GState.RECOVERING)
                LOGGER.info(
                    "%s recovery attempt %s/%s via %s",
                    self.name,
                    self.attempt,
                    self.max_attempts,
                    self.driver.target_info(),
                )
                await self._notify(
                    "recovery_attempt", attempt=self.attempt, max=self.max_attempts
                )
                ok, reason = await self.driver.can_recover()
                if not ok:
                    LOGGER.warning("%s recovery blocked: %s", self.name, reason)
                    self._escalate("recovery_blocked", reason=reason)
                    return
                try:
                    await self.driver.recover()
                except Exception:  # noqa: BLE001
                    # The action raised (e.g. a missing service): a failed attempt,
                    # never a success — retry or escalate, even without a check.
                    LOGGER.exception("Recovery driver failed for %s", self.name)
                    if self.attempt >= self.max_attempts:
                        self._escalate()
                        return
                    continue

                # Without a health-check the action is assumed to have worked; the
                # continuous health monitoring re-triggers if it didn't.
                if not self.behavior.get(CONF_HEALTH_CHECK, True):
                    self._recover_success()
                    return
                self._set_state(GState.VERIFY)
                if await self._wait_health_ok(
                    self._int(CONF_BOOT_WINDOW, DEFAULT_BOOT_WINDOW)
                ):
                    self._recover_success()
                    return
                if self.attempt >= self.max_attempts:
                    self._escalate()
                    return
        finally:
            self._cycle_task = None

    async def _wait_health_ok(self, timeout: int) -> bool:
        if self.health.evaluate() == Health.OK:
            return True
        self._verify_event = asyncio.Event()
        try:
            await asyncio.wait_for(self._verify_event.wait(), timeout)
            return True
        except TimeoutError:
            return self.health.evaluate() == Health.OK
        finally:
            self._verify_event = None

    def _recover_success(self) -> None:
        self.recover_count += 1
        self.last_recover = dt_util.utcnow()
        LOGGER.info(
            "%s recovered after %s attempt(s) (total: %s)",
            self.name,
            self.attempt,
            self.recover_count,
        )
        self.attempt = 0
        self._set_state(GState.COOLDOWN)
        self.hass.async_create_task(self._notify("recovery_success"))
        self._cancel_timer()
        self._unsub_timer = async_call_later(
            self.hass, self._int(CONF_COOLDOWN, DEFAULT_COOLDOWN), self._cooldown_done
        )

    @callback
    def _cooldown_done(self, _now) -> None:
        self._unsub_timer = None
        if self.state != GState.COOLDOWN:
            return
        if self.health.evaluate() == Health.UNHEALTHY:
            self._enter_suspect()
        else:
            self._set_state(GState.OK)

    def _escalate(self, notify_key: str = "recovery_failed", **params: object) -> None:
        LOGGER.error(
            "%s could not be recovered after %s attempt(s)", self.name, self.attempt
        )
        params.setdefault("attempt", self.attempt)
        self._set_state(GState.ESCALATED)
        self.hass.async_create_task(self._notify(notify_key, **params))

    async def _notify(self, key: str, **params: object) -> None:
        await async_notify(
            self.hass, self.name, self.behavior.get(CONF_NOTIFY_ACTION), key, **params
        )
