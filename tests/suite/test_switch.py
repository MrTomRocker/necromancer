"""Auto-restart switch tests (recover guards only)."""

from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant

from .conftest import SetupGuards, entity_id_for, make_guard


async def test_auto_restart_switch_default_on(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """A recover guard exposes an auto_restart switch that defaults to 'on'."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    await setup_guards(make_guard("Auto Guard"))

    eid = entity_id_for(hass, "guard0", "switch", "auto_restart")
    assert eid == "switch.auto_guard_auto_recovery"
    assert hass.states.get(eid).state == "on"


async def test_auto_restart_toggle_writes_through_to_engine(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """Toggling the switch flips both its state and the engine's `auto` flag."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    entry = await setup_guards(make_guard("Toggle Guard"))

    eid = entity_id_for(hass, "guard0", "switch", "auto_restart")
    engine = entry.runtime_data.engines["guard0"]
    assert engine.auto is True

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": eid}, blocking=True
    )
    assert hass.states.get(eid).state == "off"
    assert engine.auto is False

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": eid}, blocking=True
    )
    assert hass.states.get(eid).state == "on"
    assert engine.auto is True


async def test_auto_restart_toggle_idempotent(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """Turning the switch off twice leaves it off with engine `auto` False."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    entry = await setup_guards(make_guard("Idem Guard"))

    eid = entity_id_for(hass, "guard0", "switch", "auto_restart")
    engine = entry.runtime_data.engines["guard0"]

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": eid}, blocking=True
    )
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": eid}, blocking=True
    )
    assert hass.states.get(eid).state == "off"
    assert engine.auto is False


@pytest.mark.parametrize(
    "strategy",
    [
        pytest.param("switch", id="switch"),
        pytest.param("action", id="action"),
        pytest.param("poe_port", id="poe_port"),
    ],
)
async def test_auto_restart_present_for_recover_strategies(
    hass: HomeAssistant, setup_guards: SetupGuards, strategy: str
) -> None:
    """Every recovery strategy exposes an auto_restart switch."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    await setup_guards(
        make_guard(
            "Strat Guard",
            strategy=strategy,
            action=[{"action": "input_boolean.turn_on"}],
        )
    )

    eid = entity_id_for(hass, "guard0", "switch", "auto_restart")
    assert eid is not None
    assert hass.states.get(eid).state == "on"


async def test_notify_guard_has_no_auto_restart_switch(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """A notify-only guard has nothing to recover, so no auto_restart switch."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    await setup_guards(make_guard("Notify Guard", strategy="notify"))

    assert entity_id_for(hass, "guard0", "switch", "auto_restart") is None
