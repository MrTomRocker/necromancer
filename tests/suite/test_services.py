"""Operator service tests: necromancer.reset / snooze / unsnooze."""

from __future__ import annotations

from datetime import timedelta

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from tests.common import async_fire_time_changed

from .conftest import SetupGuards, entity_id_for, make_guard

DOMAIN = "necromancer"


async def _snooze(hass: HomeAssistant, eid: str, **dur: int) -> None:
    await hass.services.async_call(
        DOMAIN, "snooze", {"entity_id": eid, "duration": dur}, blocking=True
    )
    await hass.async_block_till_done()


async def test_reset_clears_escalation(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """reset on an ESCALATED guard returns it to OK (health is fine again)."""
    from custom_components.necromancer.core.state import GState

    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    entry = await setup_guards(make_guard("Reset Guard"))
    engine = entry.runtime_data.engines["guard0"]
    engine.state = GState.ESCALATED

    eid = entity_id_for(hass, "guard0", "sensor", "status")
    await hass.services.async_call(DOMAIN, "reset", {"entity_id": eid}, blocking=True)
    await hass.async_block_till_done()

    assert engine.state is GState.OK
    assert hass.states.get(eid).state == "ok"


async def test_snooze_ignores_health(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """A snoozed guard ignores its health entirely — no SUSPECT on a break."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    entry = await setup_guards(make_guard("Snooze Guard"))
    engine = entry.runtime_data.engines["guard0"]
    eid = entity_id_for(hass, "guard0", "sensor", "status")

    await _snooze(hass, eid, hours=1)
    assert hass.states.get(eid).state == "snoozed"
    assert engine._snoozed is True
    assert engine._snooze_until is not None

    hass.states.async_set("binary_sensor.guard_health", "off")
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "snoozed"


async def test_unsnooze_rederives_state(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """unsnooze lifts the snooze and re-derives from live health (OK here)."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    entry = await setup_guards(make_guard("Unsnooze Guard"))
    eid = entity_id_for(hass, "guard0", "sensor", "status")

    await _snooze(hass, eid, hours=1)
    assert hass.states.get(eid).state == "snoozed"

    await hass.services.async_call(
        DOMAIN, "unsnooze", {"entity_id": eid}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "ok"


async def test_snooze_auto_resumes_on_elapse(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """The snooze timer fires after its duration and the guard resumes."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    entry = await setup_guards(make_guard("Auto Resume"))
    eid = entity_id_for(hass, "guard0", "sensor", "status")

    await _snooze(hass, eid, seconds=30)
    assert hass.states.get(eid).state == "snoozed"

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "ok"


async def test_snooze_during_recovery_raises(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """snooze is refused while a recovery cycle is in flight (busy)."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    entry = await setup_guards(make_guard("Busy Guard"))
    engine = entry.runtime_data.engines["guard0"]
    eid = entity_id_for(hass, "guard0", "sensor", "status")

    pending = hass.loop.create_future()  # a non-done "cycle" -> _busy() is True
    engine._cycle_task = pending
    try:
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN, "snooze", {"entity_id": eid, "duration": {"hours": 1}},
                blocking=True,
            )
    finally:
        pending.cancel()


async def test_snooze_persisted_in_snapshot(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """The snooze (state + snooze_until) is captured in the persisted snapshot."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    entry = await setup_guards(make_guard("Persist Guard"))
    engine = entry.runtime_data.engines["guard0"]
    eid = entity_id_for(hass, "guard0", "sensor", "status")

    await _snooze(hass, eid, hours=1)
    snapshot = engine.snapshot()
    assert snapshot["state"] == "snoozed"
    assert snapshot["snooze_until"] is not None


async def test_snooze_all_and_unsnooze_all(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """snooze_all suspends every guard; unsnooze_all resumes them (no target)."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    entry = await setup_guards(make_guard("All A"), make_guard("All B"))
    engines = entry.runtime_data.engines

    await hass.services.async_call(
        DOMAIN, "snooze_all", {"duration": {"hours": 1}}, blocking=True
    )
    await hass.async_block_till_done()
    assert engines["guard0"]._snoozed is True
    assert engines["guard1"]._snoozed is True

    await hass.services.async_call(DOMAIN, "unsnooze_all", {}, blocking=True)
    await hass.async_block_till_done()
    assert engines["guard0"]._snoozed is False
    assert engines["guard1"]._snoozed is False


async def test_snooze_all_skips_busy_guard(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """snooze_all is best-effort: a guard mid-recovery is skipped, not raised on."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    entry = await setup_guards(make_guard("Bz A"), make_guard("Bz B"))
    engines = entry.runtime_data.engines

    pending = engines["guard1"]._cycle_task = hass.loop.create_future()  # busy
    try:
        await hass.services.async_call(
            DOMAIN, "snooze_all", {"duration": {"hours": 1}}, blocking=True
        )
        await hass.async_block_till_done()
        assert engines["guard0"]._snoozed is True  # free guard snoozed
        assert engines["guard1"]._snoozed is False  # busy guard skipped
    finally:
        pending.cancel()
