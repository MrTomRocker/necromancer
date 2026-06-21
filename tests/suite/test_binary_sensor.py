"""Health binary sensor tests."""

from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant

from .conftest import SetupGuards, entity_id_for, make_guard


@pytest.mark.parametrize(
    ("health_state", "expected_state"),
    [
        pytest.param("on", "on", id="healthy"),
        pytest.param("off", "off", id="unhealthy"),
        pytest.param("unavailable", "unavailable", id="unknown_unavailable"),
    ],
)
async def test_health_binary_sensor_reflects_health(
    hass: HomeAssistant,
    setup_guards: SetupGuards,
    health_state: str,
    expected_state: str,
) -> None:
    """The health binary sensor maps the watched entity's state to its own state."""
    hass.states.async_set("binary_sensor.guard_health", health_state)
    hass.states.async_set("switch.guard_target", "on")
    await setup_guards(
        make_guard(
            "Health Guard",
            strategy="switch_check",
            health_entity="binary_sensor.guard_health",
            on_value=["on"],
            off_value=["off"],
        )
    )

    eid = entity_id_for(hass, "guard0", "binary_sensor", "health")
    assert eid == "binary_sensor.health_guard_health"
    state = hass.states.get(eid)
    assert state is not None
    assert state.state == expected_state


async def test_health_binary_sensor_device_class(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """The health binary sensor exposes the connectivity device class."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    await setup_guards(
        make_guard(
            "Class Guard",
            strategy="switch_check",
            health_entity="binary_sensor.guard_health",
            on_value=["on"],
            off_value=["off"],
        )
    )

    eid = entity_id_for(hass, "guard0", "binary_sensor", "health")
    state = hass.states.get(eid)
    assert state is not None
    assert state.attributes["device_class"] == "connectivity"
