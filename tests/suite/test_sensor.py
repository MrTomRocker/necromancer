"""Status sensor tests."""

from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant

from .conftest import SetupGuards, entity_id_for, make_guard


async def test_status_sensor_reflects_engine_state(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """A healthy guard's status sensor reads 'ok' with the expected attributes."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    await setup_guards(make_guard("Status Guard"))

    eid = entity_id_for(hass, "guard0", "sensor", "status")
    assert eid == "sensor.status_guard_status"
    state = hass.states.get(eid)
    assert state is not None
    assert state.state == "ok"
    assert set(state.attributes) >= {
        "attempt",
        "recover_count",
        "last_recover",
        "target",
        "snooze_until",
    }
    # auto_restart (use the switch) and last_seen are intentionally not attributes.
    assert "auto_restart" not in state.attributes
    assert "last_seen" not in state.attributes
    assert state.attributes["attempt"] == 0
    assert state.attributes["recover_count"] == 0


async def test_status_sensor_enum_device_class(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """The status sensor is an enum sensor exposing all lifecycle states."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    await setup_guards(make_guard("Enum Guard", strategy="notify"))

    eid = entity_id_for(hass, "guard0", "sensor", "status")
    state = hass.states.get(eid)
    assert state.attributes["device_class"] == "enum"
    assert {"ok", "suspect", "recovering", "escalated", "cooldown"} <= set(
        state.attributes["options"]
    )


@pytest.mark.parametrize("strategy", ["switch_check", "notify"])
async def test_status_sensor_present_for_every_mode(
    hass: HomeAssistant, setup_guards: SetupGuards, strategy: str
) -> None:
    """Both recover and notify-only guards expose a status sensor."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    await setup_guards(make_guard("Mode Guard", strategy=strategy))

    eid = entity_id_for(hass, "guard0", "sensor", "status")
    assert eid is not None
    assert hass.states.get(eid).state == "ok"
