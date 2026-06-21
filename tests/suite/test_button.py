"""Recover button tests."""

from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant

from .conftest import SetupGuards, entity_id_for, make_guard

from tests.common import async_mock_service


@pytest.fixture
def mock_switch_services(hass: HomeAssistant) -> None:
    """Stub the generic switch services the switch-cycle driver power-cycles with."""
    async_mock_service(hass, "homeassistant", "turn_off")
    async_mock_service(hass, "homeassistant", "turn_on")


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        pytest.param("switch_check", True, id="recover_guard_has_button"),
        pytest.param("notify", False, id="notify_guard_has_no_button"),
    ],
)
async def test_recover_button_presence(
    hass: HomeAssistant,
    setup_guards: SetupGuards,
    strategy: str,
    expected: bool,
) -> None:
    """Only recover guards expose a recover button; notify-only guards do not."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    await setup_guards(make_guard("Presence Guard", strategy=strategy))

    eid = entity_id_for(hass, "guard0", "button", "recover")
    assert (eid is not None) is expected


async def test_recover_button_entity_id(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """The recover button id derives from the guard name."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    await setup_guards(make_guard("Recover Guard", strategy="switch"))

    eid = entity_id_for(hass, "guard0", "button", "recover")
    assert eid == "button.recover_guard_revive"
    assert hass.states.get(eid) is not None


@pytest.mark.usefixtures("mock_switch_services")
async def test_press_triggers_manual_recovery(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """Pressing the button runs a recovery cycle, bumping recover_count."""
    hass.states.async_set("switch.guard_target", "on")
    hass.states.async_set("binary_sensor.guard_health", "off")
    entry = await setup_guards(make_guard("Press Guard", strategy="switch"))

    eid = entity_id_for(hass, "guard0", "button", "recover")
    await hass.services.async_call("button", "press", {"entity_id": eid}, blocking=True)
    await hass.async_block_till_done()

    engine = entry.runtime_data.engines["guard0"]
    assert engine.recover_count == 1


@pytest.mark.usefixtures("mock_switch_services")
async def test_press_moves_status_off_ok(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """A manual recovery leaves the status sensor in cooldown (or back at ok)."""
    hass.states.async_set("switch.guard_target", "on")
    hass.states.async_set("binary_sensor.guard_health", "off")
    entry = await setup_guards(make_guard("Status Move Guard", strategy="switch"))

    button_eid = entity_id_for(hass, "guard0", "button", "recover")
    await hass.services.async_call(
        "button", "press", {"entity_id": button_eid}, blocking=True
    )
    await hass.async_block_till_done()

    status_eid = entity_id_for(hass, "guard0", "sensor", "status")
    assert hass.states.get(status_eid).state in {"cooldown", "ok"}
    assert entry.runtime_data.engines["guard0"].recover_count == 1


@pytest.mark.usefixtures("mock_switch_services")
async def test_press_bypasses_debounce(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """Manual recover ignores debounce: a healthy guard still recovers on press."""
    hass.states.async_set("switch.guard_target", "on")
    hass.states.async_set("binary_sensor.guard_health", "on")
    entry = await setup_guards(
        make_guard("Debounce Guard", strategy="switch", debounce=3600)
    )

    eid = entity_id_for(hass, "guard0", "button", "recover")
    await hass.services.async_call("button", "press", {"entity_id": eid}, blocking=True)
    await hass.async_block_till_done()

    assert entry.runtime_data.engines["guard0"].recover_count == 1
