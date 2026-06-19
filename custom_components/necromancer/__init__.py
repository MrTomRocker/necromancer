"""The Necromancer integration — revives dead devices.

HealthSource -> Engine (RecoveryPolicy) -> RecoveryDriver.
One config entry = the Necromancer service. Each **guarded device** is a config
*subentry* (added via "Add device"). One DeviceEngine per subentry lives in
entry.runtime_data, keyed by subentry_id.
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store

from .const import (
    CONF_BEHAVIOR,
    CONF_DEVICE_ID,
    CONF_DRIVER,
    CONF_ENTITY_ID,
    CONF_HEALTH,
    CONF_POLICY,
    CONF_PORTS,
    CONF_TYPE,
    DOMAIN,
    LOGGER,
    PLATFORMS,
    SAVE_DELAY,
    STORAGE_VERSION,
    SUBENTRY_TYPE_DEVICE,
)
from .drivers import create_driver
from .engine import DeviceEngine
from .health import create_health
from .policies import create_policy

type NecromancerConfigEntry = ConfigEntry[dict[str, DeviceEngine]]


def _build_engine(
    hass: HomeAssistant,
    name: str,
    cfg: dict,
    persisted: dict | None,
    save: Callable[[], None],
    on_health_renamed: Callable[[str], None],
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


async def async_setup_entry(hass: HomeAssistant, entry: NecromancerConfigEntry) -> bool:
    """Set up the service: one engine per guarded-device subentry."""
    store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
    stored = await store.async_load() or {}
    engines: dict[str, DeviceEngine] = {}

    @callback
    def _serialize() -> dict:
        return {sid: engine.snapshot() for sid, engine in engines.items()}

    def _save() -> None:
        store.async_delay_save(_serialize, SAVE_DELAY)

    # PoE ports are a flat list in the entry's options; every poe_port guard
    # searches the whole list, so inject it into each such driver at setup. An
    # options change reloads us (the update listener below), keeping it fresh.
    ports = entry.options.get(CONF_PORTS, [])
    for port in ports:
        LOGGER.info("PoE port loaded — %s", port)

    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_TYPE_DEVICE:
            continue
        cfg = dict(subentry.data)
        driver = cfg.get(CONF_DRIVER, {})
        if driver.get(CONF_TYPE) == "poe_port":
            cfg = {**cfg, CONF_DRIVER: {**driver, CONF_PORTS: ports}}
        engine = _build_engine(
            hass,
            cfg.get(CONF_NAME, subentry.title),
            cfg,
            stored.get(subentry_id),
            _save,
            _rename_handler(hass, entry, subentry_id),
        )
        await engine.async_start()
        engines[subentry_id] = engine
        LOGGER.info(
            "Guard %r loaded — mode=%s, health=%s, strategy=%s (%s), "
            "behavior=%s, device_link=%s, auto=%s",
            engine.name,
            "notify-only" if not engine.allows_recovery else "recover",
            engine.health.describe(),
            cfg.get(CONF_DRIVER, {}).get(CONF_TYPE, "—"),
            engine.driver.target_info(),
            cfg.get(CONF_BEHAVIOR, {}),
            engine.link_device_id or "none",
            engine.auto,
        )

    entry.runtime_data = engines
    hass.data.setdefault(DOMAIN, {}).setdefault("stores", {})[entry.entry_id] = (
        store,
        _serialize,
    )
    LOGGER.debug("Service set up with %s guarded device(s)", len(engines))

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _reconcile_devices(hass, entry, engines)
    _reconcile_entities(hass, entry, engines)
    return True


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
        for domain, key in (("switch", "auto_restart"), ("button", "recover")):
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

    - Remove our standalone "Überwachtes Gerät" devices for subentries that are
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
    # Flush runtime state before tearing down (engines still hold it), so a reload
    # (rename/reconfigure) does not read a stale store.
    stores = hass.data.get(DOMAIN, {}).get("stores", {})
    if (info := stores.pop(entry.entry_id, None)) is not None:
        store, serialize = info
        await store.async_save(serialize())

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        for engine in entry.runtime_data.values():
            await engine.async_stop()
    return unload_ok
