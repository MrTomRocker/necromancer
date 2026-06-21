"""Recovery event-entity tests: recovered / escalated / blocked."""

from __future__ import annotations

from datetime import timedelta

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from tests.common import async_fire_time_changed, async_mock_service

from .conftest import SetupGuards, entity_id_for, make_guard


@pytest.fixture
def mock_switch_services(hass: HomeAssistant) -> None:
    """Stub the generic switch services the switch-cycle driver power-cycles with."""
    async_mock_service(hass, "homeassistant", "turn_off")
    async_mock_service(hass, "homeassistant", "turn_on")


def _event_type(hass: HomeAssistant) -> str | None:
    eid = entity_id_for(hass, "guard0", "event", "recovery_event")
    return hass.states.get(eid).attributes.get("event_type")


@pytest.mark.usefixtures("mock_switch_services")
async def test_recovered_event(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """A successful recovery fires the `recovered` event."""
    hass.states.async_set("binary_sensor.guard_health", "off")
    hass.states.async_set("switch.guard_target", "on")
    entry = await setup_guards(make_guard("Evt Rec", strategy="switch"))

    button = entity_id_for(hass, "guard0", "button", "recover")
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    await hass.async_block_till_done()

    assert _event_type(hass) == "recovered"
    assert entry.runtime_data.engines["guard0"].recover_count == 1


async def test_escalated_event(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """A guard that exhausts its attempts fires the `escalated` event."""
    hass.states.async_set("binary_sensor.guard_health", "off")
    await setup_guards(
        make_guard(
            "Evt Esc",
            strategy="action",
            action=[{"action": "script.does_not_exist"}],
        )
    )

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()

    assert _event_type(hass) == "escalated"


async def test_blocked_event(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """A pre-flight block (missing switch) fires the `blocked` event."""
    hass.states.async_set("binary_sensor.guard_health", "off")
    await setup_guards(
        make_guard("Evt Blk", strategy="switch", switch_entity="switch.gone")
    )

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()

    assert _event_type(hass) == "blocked"


async def test_notify_only_guard_has_no_event(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """Notify-only guards never recover, so they get no recovery event entity."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    await setup_guards(make_guard("Evt Notify", strategy="notify"))

    assert entity_id_for(hass, "guard0", "event", "recovery_event") is None
    assert entity_id_for(hass, "guard0", "sensor", "status") is not None
