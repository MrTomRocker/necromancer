"""The Necromancer engine — per-device self-healing state machine.

Fixed runtime; Health / Policy / Driver are pluggable:

  OK --(unhealthy)--> SUSPECT --(debounce)--> RECOVERING --> VERIFY(boot_window)
  VERIFY: ok -> COOLDOWN -> OK | fail&retry<max -> RECOVERING | else -> ESCALATED
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
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
    CONF_NOTIFY_FOLLOWER_SUCCESS,
    CONF_RELOAD_DELAY,
    CONF_RELOAD_ENTRY,
    DEFAULT_AUTO_RESTART,
    DEFAULT_BOOT_WINDOW,
    DEFAULT_COOLDOWN,
    DEFAULT_DEBOUNCE,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RELOAD_DELAY,
    DOMAIN,
    LOGGER,
    REASON_OBSERVE,
)
from .drivers import RecoveryDriver
from .health import Health, HealthSource
from .links import LinkCoordinator
from .notify import async_notify
from .policies import RecoveryPolicy
from .state import GState


def _noop() -> None:
    """Default save callback when none is provided (e.g. in tests)."""


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
        subentry_id: str | None = None,
        linked_guards: list[str] | None = None,
        engines: dict[str, DeviceEngine] | None = None,
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
        # Guard linking: a coordinator owns the group membership + leader/follower
        # protocol; we keep our subentry id (also our guard identity). Peers are
        # reached through peer.links, so engines never touch each other's internals.
        self._subentry_id = subentry_id
        self.links = LinkCoordinator(self, linked_guards, engines)

        self.state = GState.OK
        self.attempt = 0
        self.recover_count = 0
        self.last_seen: datetime | None = None
        self.last_recover: datetime | None = None
        self.auto = bool(behavior.get(CONF_AUTO_RESTART, DEFAULT_AUTO_RESTART))

        self._unsub_health: Callable[[], None] | None = None
        self._unsub_registry: Callable[[], None] | None = None
        self._unsub_started: Callable[[], None] | None = None
        self._unsub_source: Callable[[], None] | None = None
        self._unsub_driver: Callable[[], None] | None = None
        self._unsub_timer: Callable[[], None] | None = None
        self._verify_event: asyncio.Event | None = None
        self._cycle_task: asyncio.Task | None = None
        self._stopping = False
        self._last_eval_log: tuple[Health, GState] | None = None
        self._listeners: list[Callable[[], None]] = []

        self._apply_persisted(persisted or {})

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
        # A tracking source (template) subscribes to nothing directly, so the loop
        # above never sees it. Validate the entities its verdict actually reads:
        # a single missing/disabled entity is a warning (a template may read many),
        # but if every referenced entity is gone the guard is blind.
        if not self.health.watched_entities:
            referenced = self.health.referenced_entities()
            blind = []
            for eid in referenced:
                entry = ent_reg.async_get(eid)
                if entry is None and self.hass.states.get(eid) is None:
                    blind.append(eid)
                    LOGGER.warning(
                        "%s: health template references %s, which does not exist",
                        self.name,
                        eid,
                    )
                elif entry is not None and entry.disabled:
                    blind.append(eid)
                    LOGGER.warning(
                        "%s: health template references %s, which is disabled",
                        self.name,
                        eid,
                    )
            if referenced and len(blind) == len(referenced):
                LOGGER.error(
                    "%s: health template reads only missing/disabled entities %s "
                    "— guard is blind",
                    self.name,
                    sorted(blind),
                )
        # Feedback-loop guard: a (template) health that depends on this guard's own
        # entities would re-evaluate on its own state changes. State health can't
        # (the picker excludes our entities) but a free-text template can.
        if self._subentry_id:
            own = {
                e.entity_id
                for e in ent_reg.entities.values()
                if e.platform == DOMAIN and e.unique_id.startswith(self._subentry_id)
            }
            loop = own.intersection(self.health.referenced_entities())
            if loop:
                LOGGER.warning(
                    "%s: health references its own entit(ies) %s — feedback loop; "
                    "point health at the guarded device, not the guard",
                    self.name,
                    sorted(loop),
                )

    async def async_stop(self) -> None:
        LOGGER.debug("Stopping engine for %s", self.name)
        # Mark teardown first: the cancelled cycle's finally must NOT escalate
        # linked partners off a half-finished repair, and our link state is reset
        # here instead of via a partner notification we are about to skip.
        self._stopping = True
        self.links.reset()
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

    def _cancel_timer(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None

    # ---------- guard linking ----------
    def _busy(self) -> bool:
        """True while our own recovery cycle runs (then don't self-suppress)."""
        return self._cycle_task is not None and not self._cycle_task.done()

    # Linking lives in LinkCoordinator (self.links); these thin delegators keep the
    # engine surface — and the tests — stable while the protocol moves out.
    @property
    def _following(self) -> bool:
        return self.links.following

    @_following.setter
    def _following(self, value: bool) -> None:
        self.links.following = value

    @property
    def _leader(self) -> str | None:
        return self.links.leader

    @_leader.setter
    def _leader(self, value: str | None) -> None:
        self.links.leader = value

    def _find_repairing_partner(self) -> DeviceEngine | None:
        return self.links.find_repairing_partner()

    def _on_partner_repair_start(self, leader_id: str) -> None:
        self.links.on_partner_repair_start(leader_id)

    def _on_partner_repair_done(self, leader_id: str, success: bool) -> None:
        self.links.on_partner_repair_done(leader_id, success)

    # ---------- health handling ----------
    @callback
    def _handle_health_event(self, event) -> None:
        self._evaluate()

    @callback
    def _evaluate(self) -> None:
        h = self.health.evaluate()
        # Only log when the (health, state) pair actually changed — skips the
        # duplicate evaluation at startup and repeated identical re-evaluations.
        if (h, self.state) != self._last_eval_log:
            LOGGER.debug("%s health=%s state=%s", self.name, h, self.state)
            self._last_eval_log = (h, self.state)
        if h == Health.OK:
            self.last_seen = dt_util.utcnow()
        # While following a linked guard's repair, expect our device to drop too;
        # hold (no competing recovery). _on_partner_repair_done resumes us.
        if self._following:
            self._emit()
            return
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
            if reason == REASON_OBSERVE:
                LOGGER.warning("%s problem detected (notify-only)", self.name)
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
        # Linking arbitration: if a group partner is already repairing, follow it
        # (hold + verify after) instead of launching a competing recovery.
        if (leader := self._find_repairing_partner()) is not None:
            LOGGER.info(
                "%s: linked guard %r already repairing — following instead",
                self.name,
                leader.name,
            )
            self._on_partner_repair_start(leader._subentry_id)
            return
        LOGGER.info("%s debounce elapsed, starting recovery", self.name)
        self._start_cycle()

    def _start_cycle(self) -> None:
        if self._cycle_task and not self._cycle_task.done():
            return
        # Claim the leader role *synchronously* (before the cycle task runs) so a
        # linked partner whose debounce elapses in the same tick already sees us as
        # RECOVERING and follows, instead of both starting a competing recovery.
        self._set_state(GState.RECOVERING)
        self._cycle_task = self.hass.async_create_task(self._run_recovery_cycle())

    async def async_manual_recover(self) -> None:
        """Button: force a recovery cycle now (bypasses debounce + auto gate).

        A press while a cycle is already running is ignored — otherwise resetting
        `attempt` mid-flight would defeat `max_attempts`.
        """
        if self._busy():
            LOGGER.info("%s manual recover ignored — already recovering", self.name)
            return
        LOGGER.info("%s manual recovery requested", self.name)
        self.attempt = 0
        self._cancel_timer()
        self._start_cycle()

    async def _run_recovery_cycle(self) -> None:
        # Tell our group we're repairing so partners follow (hold) instead of
        # launching their own recovery for the same root cause.
        self.links.notify_start()
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
                except Exception as err:  # noqa: BLE001
                    # The action raised (e.g. a missing service): a failed attempt,
                    # never a success — retry or escalate, even without a check.
                    if self.attempt >= self.max_attempts:
                        # Terminal failure: keep the full traceback for diagnosis.
                        LOGGER.exception("Recovery driver failed for %s", self.name)
                        self._escalate()
                        return
                    # Expected, retryable: a concise warning, not an alarming
                    # traceback per attempt.
                    LOGGER.warning(
                        "%s recovery attempt %s/%s failed (%s) — retrying",
                        self.name,
                        self.attempt,
                        self.max_attempts,
                        err,
                    )
                    continue

                # Optionally reload the assigned device's integration after the
                # repair (and before VERIFY), so HA reconnects to a device that
                # just came back. Best-effort: a reload failure must not abort.
                await self._maybe_reload_device_entry()

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
            # A stop/unload cancellation must not report a (failed) repair to the
            # group — that would escalate followers off our half-finished cycle.
            if not self._stopping:
                self.links.notify_done(self.state == GState.COOLDOWN)

    async def _maybe_reload_device_entry(self) -> None:
        """Reload the assigned device's integration after a repair, if enabled.

        Best-effort: a missing device or a failing reload is logged but never
        aborts the recovery — VERIFY still decides success.
        """
        if not self.behavior.get(CONF_RELOAD_ENTRY) or not self.link_device_id:
            return
        delay = self._int(CONF_RELOAD_DELAY, DEFAULT_RELOAD_DELAY)
        if delay:
            await asyncio.sleep(delay)
        device = dr.async_get(self.hass).async_get(self.link_device_id)
        if device is None:
            LOGGER.warning(
                "%s: assigned device %s gone, skipping integration reload",
                self.name,
                self.link_device_id,
            )
            return
        entry_ids = (
            [device.primary_config_entry]
            if device.primary_config_entry
            else list(device.config_entries)
        )
        for entry_id in entry_ids:
            LOGGER.info(
                "%s: reloading the assigned device's integration (entry %s)",
                self.name,
                entry_id,
            )
            try:
                await self.hass.config_entries.async_reload(entry_id)
            except Exception:  # noqa: BLE001
                LOGGER.exception(
                    "%s: failed to reload config entry %s", self.name, entry_id
                )

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

    def _recover_success(self, *, via_link: bool = False) -> None:
        self.recover_count += 1
        self.last_recover = dt_util.utcnow()
        if via_link:
            LOGGER.info(
                "%s recovered via linked-guard repair (total: %s)",
                self.name,
                self.recover_count,
            )
        else:
            LOGGER.info(
                "%s recovered after %s attempt(s) (total: %s)",
                self.name,
                self.attempt,
                self.recover_count,
            )
        self.attempt = 0
        self._set_state(GState.COOLDOWN)
        # A follower that recovered by following a group repair stays silent on
        # success by default (the leader already reported it); opt in per guard.
        # Failures always notify, so silence here means "came back fine".
        if not via_link or self.behavior.get(CONF_NOTIFY_FOLLOWER_SUCCESS):
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
        if notify_key == "recovery_failed":
            # Genuine give-up after real attempts. A pre-flight block
            # (`recovery_blocked`) already logged its own WARNING with the reason,
            # so don't add a misleading "could not be recovered after N attempts".
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
