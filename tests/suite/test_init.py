"""Integration init / setup / lifecycle tests.

Exercises async_setup_entry (engine construction, service registration, per-guard
isolation, view-entity reconciliation, device-registry footprint), async_unload_entry
and the deferred config-validation warnings (feedback loop / blind template).
"""

from __future__ import annotations

import logging

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .conftest import SetupGuards, entity_id_for, make_guard


async def test_setup_builds_one_engine_per_guard(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """Two guards yield two engines in runtime_data and a loaded entry."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    entry = await setup_guards(make_guard("A"), make_guard("B"))

    assert entry.state is ConfigEntryState.LOADED
    assert len(entry.runtime_data.engines) == 2
    assert set(entry.runtime_data.engines) == {"guard0", "guard1"}


async def test_repair_poe_port_service_registered(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """Setup registers the necromancer.repair_poe_port service."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    await setup_guards(make_guard("Svc Guard"))

    assert hass.services.has_service("necromancer", "repair_poe_port")


async def test_malformed_guard_is_skipped(
    hass: HomeAssistant,
    setup_guards: SetupGuards,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A guard whose health type is unknown is skipped; the good one still loads."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    bad = make_guard("Bad")
    bad["health"] = {"type": "does_not_exist"}

    with caplog.at_level(logging.ERROR):
        entry = await setup_guards(bad, make_guard("Good"))

    assert entry.state is ConfigEntryState.LOADED
    assert set(entry.runtime_data.engines) == {"guard1"}
    assert "Failed to set up guard" in caplog.text
    assert "Bad" in caplog.text


async def test_notify_only_guard_has_no_control_entities(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """A notify-only guard exposes status + health but no switch/button."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    await setup_guards(make_guard("Watcher", strategy="notify"))

    assert entity_id_for(hass, "guard0", "switch", "auto_restart") is None
    assert entity_id_for(hass, "guard0", "button", "recover") is None
    assert entity_id_for(hass, "guard0", "sensor", "status") is not None
    assert entity_id_for(hass, "guard0", "binary_sensor", "health") is not None


async def test_recover_guard_has_all_four_entities(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """A recover guard exposes status, health, auto_restart and recover entities."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    await setup_guards(make_guard("Reviver"))

    assert entity_id_for(hass, "guard0", "sensor", "status") is not None
    assert entity_id_for(hass, "guard0", "binary_sensor", "health") is not None
    assert entity_id_for(hass, "guard0", "switch", "auto_restart") is not None
    assert entity_id_for(hass, "guard0", "button", "recover") is not None


async def test_standalone_recover_guard_creates_device(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """A standalone recover guard registers a Necromancer device."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    await setup_guards(make_guard("Phoenix"))

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device(identifiers={("necromancer", "guard0")})
    assert device is not None
    assert device.name == "Phoenix"
    assert device.manufacturer == "Necromancer"


async def test_unload_sets_not_loaded(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """Unloading the entry tears engines down and marks it NOT_LOADED."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    entry = await setup_guards(make_guard("Bye"))

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_feedback_loop_warning(
    hass: HomeAssistant,
    setup_guards: SetupGuards,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A template health reading the guard's own status sensor warns about a loop."""
    hass.states.async_set("input_boolean.x", "off")
    with caplog.at_level(logging.WARNING):
        await setup_guards(
            make_guard(
                "Loopy",
                source="template_based",
                strategy="action_check",
                template="{{ is_state('sensor.loopy_status','ok') }}",
                action=[
                    {
                        "action": "input_boolean.turn_on",
                        "data": {"entity_id": "input_boolean.x"},
                    }
                ],
            )
        )
        await hass.async_block_till_done()

    assert "feedback loop" in caplog.text
    assert "references its own entit" in caplog.text


async def test_blind_template_warning(
    hass: HomeAssistant,
    setup_guards: SetupGuards,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A template reading only a missing entity warns the guard is blind."""
    with caplog.at_level(logging.WARNING):
        await setup_guards(
            make_guard(
                "Blind",
                source="template_based",
                strategy="notify",
                template="{{ is_state('binary_sensor.ghost_xyz','on') }}",
            )
        )
        await hass.async_block_till_done()

    assert "guard is blind" in caplog.text
    assert "binary_sensor.ghost_xyz" in caplog.text
