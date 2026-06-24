"""The Necromancer integration — revives dead devices.

HealthSource -> Engine (RecoveryPolicy) -> RecoveryDriver.
One config entry = the Necromancer service. Each **guarded device** is a config
*subentry* (added via "Add device"). One DeviceEngine per subentry lives in
entry.runtime_data.engines, keyed by subentry_id.

HA lifecycle entry points:
    async_setup_entry                 load an entry (boot / add / after reload)
    async_unload_entry                tear it down (shutdown / before reload)
    async_remove_config_entry_device  user deletes a guarded device in the UI
Plus the registered reload hook (_async_reload_entry) on options/subentry change.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import time

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

from .const import (
    ATTR_CHECK_FIRST,
    ATTR_DURATION,
    ATTR_GUARD,
    ATTR_ID,
    ATTR_TIMEOUT,
    CONF_BEHAVIOR,
    CONF_DEVICE_ID,
    CONF_DRIVER,
    CONF_ENTITY_ID,
    CONF_HEALTH,
    CONF_LINKED_GUARDS,
    CONF_POLICY,
    CONF_PORTS,
    CONF_TYPE,
    DOMAIN,
    MODE_NOTIFY,
    PLATFORMS,
    SAVE_DELAY,
    SERVICE_CHECK_HEALTH,
    SERVICE_REPAIR_POE_PORT,
    SERVICE_SNOOZE_ALL,
    SERVICE_UNSNOOZE_ALL,
    SERVICE_WAIT_FOR_HEALTH,
    STORAGE_VERSION,
    SUBENTRY_TYPE_DEVICE,
)
from .core.drivers import create_driver
from .core.engine import DeviceEngine
from .core.health import create_health
from .core.links import link_components
from .core.poe import PoeFabric
from .core.policies import create_policy

LOGGER = logging.getLogger(__name__)


@dataclass
class NecromancerData:
    """Typed per-entry runtime state (``entry.runtime_data``).

    One engine per guarded-device subentry, plus the Store + its serializer so
    unload can flush without a side `hass.data` registry. The PoE fabric and the
    `name_reset` signal stay in `hass.data[DOMAIN]` on purpose: they outlive a
    single entry's reload.
    """

    engines: dict[str, DeviceEngine]
    store: Store
    serialize: Callable[[], dict]


type NecromancerConfigEntry = ConfigEntry[NecromancerData]


# ---------- setup helpers ----------
def _build_engine(
    hass: HomeAssistant,
    name: str,
    cfg: dict,
    persisted: dict | None,
    save: CALLBACK_TYPE,
    on_health_renamed: Callable[[str], None],
    subentry_id: str,
    linked_guards: list[str],
    engines: dict[str, DeviceEngine],
) -> DeviceEngine:
    """Construct a DeviceEngine from a subentry's config dict."""
    return DeviceEngine(
        hass,
        name,
        create_health(hass, cfg[CONF_HEALTH]),
        create_driver(hass, cfg[CONF_DRIVER]),
        create_policy(cfg.get(CONF_POLICY, {CONF_TYPE: "standard"})),
        cfg.get(CONF_BEHAVIOR, {}),
        cfg.get(CONF_DEVICE_ID),
        persisted,
        save,
        on_health_renamed,
        subentry_id=subentry_id,
        linked_guards=linked_guards,
        engines=engines,
    )


def _rename_handler(
    hass: HomeAssistant, entry: NecromancerConfigEntry, subentry_id: str
) -> Callable[[str], None]:
    """Persist a health-entity rename into the subentry (triggers a reload)."""

    @callback
    def _renamed(new_entity_id: str) -> None:
        subentry = entry.subentries.get(subentry_id)
        if subentry is None:
            return
        health = {**subentry.data.get(CONF_HEALTH, {}), CONF_ENTITY_ID: new_entity_id}
        hass.config_entries.async_update_subentry(
            entry, subentry, data={**subentry.data, CONF_HEALTH: health}
        )

    return _renamed


# ---------- HA lifecycle: setup ----------
def _engine_for_guard(
    hass: HomeAssistant, entry: NecromancerConfigEntry, guard_entity_id: str
) -> DeviceEngine:
    """Resolve a guard's status entity to its engine (for the health services)."""
    ent = er.async_get(hass).async_get(guard_entity_id)
    engine = (
        entry.runtime_data.engines.get(ent.config_subentry_id)
        if ent is not None and ent.platform == DOMAIN
        else None
    )
    if engine is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_guard",
            translation_placeholders={"entity_id": guard_entity_id},
        )
    return engine


_ISSUE_SEVERITY = {
    "health_entity_missing": ir.IssueSeverity.ERROR,
    "health_entity_disabled": ir.IssueSeverity.ERROR,
    "health_template_blind": ir.IssueSeverity.ERROR,
    "recovery_action_invalid": ir.IssueSeverity.ERROR,
    "port_no_id": ir.IssueSeverity.WARNING,
    "port_entity_missing": ir.IssueSeverity.ERROR,
}


def _reconcile_issues(
    hass: HomeAssistant, engines: dict[str, DeviceEngine], fabric: PoeFabric
) -> None:
    """Surface user-fixable config problems in Repairs, clearing resolved ones.

    Idempotent: re-run at startup and on every reload — so fixing the config and
    saving (which reloads the entry) makes a stale issue disappear by itself.
    """
    desired: dict[str, dict] = {}
    for eng in engines.values():
        for problem in eng.config_problems():
            desired[problem["id"]] = problem
    for problem in fabric.port_problems():
        desired[problem["id"]] = problem

    reg = ir.async_get(hass)
    ours = {
        iid
        for (dom, iid), issue in reg.issues.items()
        if dom == DOMAIN and issue.translation_key in _ISSUE_SEVERITY
    }
    for iid, problem in desired.items():
        ir.async_create_issue(
            hass,
            DOMAIN,
            iid,
            is_fixable=False,
            severity=_ISSUE_SEVERITY[problem["key"]],
            translation_key=problem["key"],
            translation_placeholders=problem["placeholders"],
        )
    for iid in ours - set(desired):
        ir.async_delete_issue(hass, DOMAIN, iid)


async def async_setup_entry(hass: HomeAssistant, entry: NecromancerConfigEntry) -> bool:
    """Set up the service: one engine per guarded-device subentry."""
    store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
    stored = await store.async_load() or {}
    engines: dict[str, DeviceEngine] = {}

    # PoE fabric: shared id->port resolver + per-port status/lock, driving the
    # necromancer.repair_poe_port service. A domain-level singleton so it survives
    # reloads (and the service handler keeps a stable reference).
    domain_data = hass.data.setdefault(DOMAIN, {})
    fabric: PoeFabric = domain_data.get("fabric") or PoeFabric(hass)
    domain_data["fabric"] = fabric

    @callback
    def _serialize() -> dict:
        data: dict = {sid: engine.snapshot() for sid, engine in engines.items()}
        data["_poe_cache"] = fabric.cache
        return data

    def _save() -> None:
        store.async_delay_save(_serialize, SAVE_DELAY)

    # PoE ports are a flat list in the entry's options; every poe_port guard
    # searches the whole list, so inject it into each such driver at setup. An
    # options change reloads us (the update listener below), keeping it fresh.
    ports = entry.options.get(CONF_PORTS, [])
    fabric.set_ports(ports, cache=stored.get("_poe_cache"))
    for port in ports:
        LOGGER.debug("PoE port loaded — %s", port)

    if not hass.services.has_service(DOMAIN, SERVICE_REPAIR_POE_PORT):

        async def _repair_poe_port(call: ServiceCall) -> None:
            port_id = call.data[ATTR_ID]
            LOGGER.info("repair_poe_port requested for %s", port_id)
            await fabric.repair(port_id)

        hass.services.async_register(
            DOMAIN,
            SERVICE_REPAIR_POE_PORT,
            _repair_poe_port,
            schema=vol.Schema({vol.Required(ATTR_ID): cv.string}),
        )

    # Bulk "maintenance mode" services (no target — every guard, incl. notify-only).
    # Re-registered each setup so the handler closes over the current entry's engines.
    async def _snooze_all(call: ServiceCall) -> None:
        duration = call.data[ATTR_DURATION]
        busy: list[str] = []
        for engine in entry.runtime_data.engines.values():
            try:
                engine.snooze(duration)
            except ServiceValidationError:
                busy.append(engine.name)  # mid-recovery — best-effort, skip it
        if busy:
            LOGGER.warning(
                "snooze_all: skipped %s guard(s) busy recovering: %s",
                len(busy),
                ", ".join(busy),
            )

    async def _unsnooze_all(call: ServiceCall) -> None:
        for engine in entry.runtime_data.engines.values():
            engine.unsnooze()

    hass.services.async_register(
        DOMAIN,
        SERVICE_SNOOZE_ALL,
        _snooze_all,
        schema=vol.Schema({vol.Required(ATTR_DURATION): cv.positive_time_period}),
    )
    hass.services.async_register(DOMAIN, SERVICE_UNSNOOZE_ALL, _unsnooze_all)

    # Per-guard health primitives (response services) — a recovery script passes the
    # guard's status entity and re-uses the guard's own health-check.
    async def _check_health(call: ServiceCall) -> ServiceResponse:
        engine = _engine_for_guard(hass, entry, call.data[ATTR_GUARD])
        return {"health": engine.current_health().value}

    async def _wait_for_health(call: ServiceCall) -> ServiceResponse:
        engine = _engine_for_guard(hass, entry, call.data[ATTR_GUARD])
        start = time.monotonic()
        ok = await engine.async_service_wait_health(
            call.data.get(ATTR_TIMEOUT), check_first=call.data[ATTR_CHECK_FIRST]
        )
        return {
            "health": engine.current_health().value,
            "timed_out": not ok,
            "waited_s": round(time.monotonic() - start),
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_CHECK_HEALTH,
        _check_health,
        schema=vol.Schema({vol.Required(ATTR_GUARD): cv.entity_id}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_WAIT_FOR_HEALTH,
        _wait_for_health,
        schema=vol.Schema(
            {
                vol.Required(ATTR_GUARD): cv.entity_id,
                vol.Optional(ATTR_TIMEOUT): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(ATTR_CHECK_FIRST, default=True): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )

    # Guard linking: resolve each guard's group (clique-closed) from the declared
    # links. Only **recover** guards can link (matching the config flow's options),
    # so notify-only guards are excluded from the closure — a guard reconfigured to
    # notify-only therefore drops out of every group instead of lingering inertly.
    def _is_recover(se) -> bool:
        return (
            se.subentry_type == SUBENTRY_TYPE_DEVICE
            and se.data.get(CONF_POLICY, {}).get(CONF_TYPE) != MODE_NOTIFY
        )

    device_ids = {sid for sid, se in entry.subentries.items() if _is_recover(se)}
    declared_links = {
        sid: set(se.data.get(CONF_LINKED_GUARDS, []) or [])
        for sid, se in entry.subentries.items()
        if _is_recover(se)
    }
    groups = link_components(declared_links, device_ids)

    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_TYPE_DEVICE:
            continue
        cfg = dict(subentry.data)
        name = cfg.get(CONF_NAME, subentry.title)
        # poe_port guards resolve + cycle through the shared fabric (which owns the
        # port list), so nothing port-specific needs injecting into the driver here.
        linked = sorted(groups.get(subentry_id, {subentry_id}) - {subentry_id})
        try:
            engine = _build_engine(
                hass,
                name,
                cfg,
                stored.get(subentry_id),
                _save,
                _rename_handler(hass, entry, subentry_id),
                subentry_id,
                linked,
                engines,
            )
            await engine.async_start()
        except Exception:
            # One malformed guard must not take down the whole entry (all guards):
            # log which one and carry on with the rest.
            LOGGER.exception("Failed to set up guard %r — skipping", name)
            continue
        engines[subentry_id] = engine
        LOGGER.info(
            "Guard %r loaded — mode=%s, health=%s, strategy=%s (%s), "
            "behavior=%s, device_link=%s, linked=%s, auto=%s",
            engine.name,
            "notify-only" if not engine.allows_recovery else "recover",
            engine.health.describe(),
            cfg.get(CONF_DRIVER, {}).get(CONF_TYPE, "—"),
            engine.driver.target_info(),
            cfg.get(CONF_BEHAVIOR, {}),
            engine.link_device_id or "none",
            linked or "none",
            engine.auto,
        )

    entry.runtime_data = NecromancerData(
        engines=engines, store=store, serialize=_serialize
    )
    LOGGER.info("Service set up with %s guarded device(s)", len(engines))

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _reconcile_devices(hass, entry, engines)
    _reconcile_entities(hass, entry, engines)

    # Validate guard configs once HA is started AND the platforms above have
    # registered each guard's own view-entities — so the self-reference (feedback
    # loop) check sees them even when a guard is added at runtime. async_at_started
    # defers to "started" on boot (avoids false positives) and runs right away at
    # runtime, by which point the entities exist (forward_entry_setups is awaited).
    @callback
    def _validate_configs(_hass: HomeAssistant) -> None:
        for eng in engines.values():
            eng._check_config(_hass)
        _reconcile_issues(_hass, engines, fabric)

    entry.async_on_unload(async_at_started(hass, _validate_configs))

    # Live config re-validation: re-reconcile when a watched entity appears or
    # disappears (a state-only entity loading, an entity removed or disabled). Only
    # existence transitions matter — a value change doesn't affect config issues.
    watched: set[str] = set(fabric.referenced_entities())
    for eng in engines.values():
        watched.update(eng.health.watched_entities)
        watched.update(eng.health.referenced_entities())

    @callback
    def _on_watched_change(event: Event) -> None:
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if (old is None) != (new is None):  # appeared or disappeared
            _reconcile_issues(hass, engines, fabric)

    if watched:
        entry.async_on_unload(
            async_track_state_change_event(hass, list(watched), _on_watched_change)
        )
    return True


# ---------- reconciliation helpers ----------
def _reconcile_entities(
    hass: HomeAssistant,
    entry: NecromancerConfigEntry,
    engines: dict[str, DeviceEngine],
) -> None:
    """Drop control entities (switch/button) of notify-only guards.

    A guard reconfigured recover -> notify-only would otherwise leave its
    auto-restart switch and recover button behind as orphans.
    """
    ent_reg = er.async_get(hass)
    for subentry_id, engine in engines.items():
        if engine.allows_recovery:
            continue
        for domain, key in (
            ("switch", "auto_restart"),
            ("button", "recover"),
            ("event", "recovery_event"),
        ):
            eid = ent_reg.async_get_entity_id(domain, DOMAIN, f"{subentry_id}_{key}")
            if eid is not None:
                LOGGER.debug("Removing %s (notify-only guard %s)", eid, engine.name)
                ent_reg.async_remove(eid)


def _reconcile_devices(
    hass: HomeAssistant,
    entry: NecromancerConfigEntry,
    engines: dict[str, DeviceEngine],
) -> None:
    """Clean up our device registry footprint.

    - Remove our standalone "Necromancer guard monitored device" devices for subentries that are
      now linked to an existing device or no longer exist.
    - Detach us from any foreign device that is no longer a current link target
      (i.e. a device a subentry was unlinked from).
    """
    dev_reg = dr.async_get(hass)
    standalone = {sid for sid, engine in engines.items() if not engine.link_device_id}
    linked_targets = {
        engine.link_device_id for engine in engines.values() if engine.link_device_id
    }

    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        ours = {ident for domain, ident in device.identifiers if domain == DOMAIN}
        if ours:
            if not ours & standalone:
                LOGGER.debug("Removing stale guard device %s", device.name or device.id)
                dev_reg.async_remove_device(device.id)
        elif device.id not in linked_targets:
            LOGGER.debug("Detaching from foreign device %s", device.name or device.id)
            dev_reg.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )

    # A just-unlinked guard: reset the device name to the guard name. HA restores
    # the previously-deleted standalone device WITH its name_by_user, so we clear
    # that override here (only on the unlink transition, never on a plain rename).
    pending = hass.data.get(DOMAIN, {}).get("name_reset", set())
    for subentry_id, engine in engines.items():
        if subentry_id not in pending or engine.link_device_id:
            continue
        device = dev_reg.async_get_device(identifiers={(DOMAIN, subentry_id)})
        if device is not None:
            LOGGER.debug("Resetting device name to %s after unlink", engine.name)
            dev_reg.async_update_device(device.id, name=engine.name, name_by_user=None)
        pending.discard(subentry_id)


# ---------- HA lifecycle: reload / remove / unload ----------
async def _async_reload_entry(
    hass: HomeAssistant, entry: NecromancerConfigEntry
) -> None:
    """Reload the service when subentries (devices) are added/changed/removed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: NecromancerConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow manual deletion of a guarded-device entry from the UI."""
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: NecromancerConfigEntry
) -> bool:
    """Unload the service and stop all engines."""
    data = entry.runtime_data
    # Flush runtime state before tearing down (engines still hold it), so a reload
    # (rename/reconfigure) does not read a stale store.
    await data.store.async_save(data.serialize())

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        for engine in data.engines.values():
            await engine.async_stop()
        if (fabric := hass.data.get(DOMAIN, {}).get("fabric")) is not None:
            fabric.shutdown()
    return unload_ok


async def async_remove_entry(
    hass: HomeAssistant, entry: NecromancerConfigEntry
) -> None:
    """Drop our config-health repair issues when the integration is removed.

    Without this they would linger in Settings → Repairs until the next restart
    (they are non-persistent), even though the guards they referred to are gone.
    """
    reg = ir.async_get(hass)
    stale = [
        iid
        for (dom, iid), issue in reg.issues.items()
        if dom == DOMAIN and issue.translation_key in _ISSUE_SEVERITY
    ]
    for iid in stale:
        ir.async_delete_issue(hass, DOMAIN, iid)
