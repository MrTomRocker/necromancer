"""Health-primitive service tests: necromancer.check_health / wait_for_health.

Both take the guard's status entity as the `guard` field (the value a recovery
action receives as `guard_entity_id`) and return a bare response.
"""

from __future__ import annotations

import asyncio

import pytest

from homeassistant.core import HomeAssistant

from .conftest import SetupGuards, entity_id_for, make_guard

DOMAIN = "necromancer"


async def _call(hass: HomeAssistant, service: str, guard: str, **data: object) -> dict:
    """Call a response service for the guard, return its (bare) response."""
    return await hass.services.async_call(
        DOMAIN,
        service,
        {"guard": guard, **data},
        blocking=True,
        return_response=True,
    )


async def _arm(hass: HomeAssistant, guard: str, **data: object) -> asyncio.Task:
    """Start wait_for_health as a background task and let it arm its waiter."""
    task = hass.async_create_task(
        hass.services.async_call(
            DOMAIN,
            "wait_for_health",
            {"guard": guard, **data},
            blocking=True,
            return_response=True,
        )
    )
    await asyncio.sleep(0.1)  # let the service reach + arm its waiter
    return task


@pytest.mark.parametrize(
    ("health_state", "expected"),
    [("on", "ok"), ("off", "unhealthy"), ("unavailable", "unknown")],
)
async def test_check_health_returns_verdict(
    hass: HomeAssistant,
    setup_guards: SetupGuards,
    health_state: str,
    expected: str,
) -> None:
    """check_health evaluates the guard's health right now and returns the verdict."""
    hass.states.async_set("binary_sensor.guard_health", health_state)
    await setup_guards(make_guard("CheckH", strategy="notify"))
    await hass.async_block_till_done()
    eid = entity_id_for(hass, "guard0", "sensor", "status")

    assert await _call(hass, "check_health", eid) == {"health": expected}


async def test_check_health_unknown_guard_raises(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """A guard that isn't a Necromancer status entity is rejected."""
    from homeassistant.exceptions import ServiceValidationError

    await setup_guards(make_guard("Whatever", strategy="notify"))
    with pytest.raises(ServiceValidationError):
        await _call(hass, "check_health", "sensor.not_a_guard")


async def test_wait_for_health_already_ok_returns_at_once(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """Healthy + check_first (default) returns immediately, no wait."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    await setup_guards(make_guard("WaitOK", strategy="notify"))
    await hass.async_block_till_done()
    eid = entity_id_for(hass, "guard0", "sensor", "status")

    resp = await _call(hass, "wait_for_health", eid, timeout=30)
    assert resp["health"] == "ok"
    assert resp["timed_out"] is False
    assert resp["waited_s"] == 0


async def test_wait_for_health_heals_during_wait(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """Unhealthy then healed mid-wait: the waiter fires and it returns ok."""
    hass.states.async_set("binary_sensor.guard_health", "off")
    await setup_guards(make_guard("WaitHeal", strategy="notify"))
    await hass.async_block_till_done()
    eid = entity_id_for(hass, "guard0", "sensor", "status")

    task = await _arm(hass, eid, timeout=30)
    hass.states.async_set("binary_sensor.guard_health", "on")  # heal
    await hass.async_block_till_done()
    resp = await task
    assert resp["health"] == "ok"
    assert resp["timed_out"] is False


async def test_wait_for_health_timeout_defaults_to_boot_window(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """No timeout given uses boot_window; staying unhealthy reports timed_out."""
    hass.states.async_set("switch.guard_target", "on")
    hass.states.async_set("binary_sensor.guard_health", "off")
    # boot_window=1 is the wait window; debounce high so the engine doesn't recover.
    await setup_guards(make_guard("WaitTO", boot_window=1, debounce=60))
    await hass.async_block_till_done()
    eid = entity_id_for(hass, "guard0", "sensor", "status")

    resp = await _call(hass, "wait_for_health", eid)  # no timeout -> boot_window
    assert resp["health"] == "unhealthy"
    assert resp["timed_out"] is True


async def test_wait_for_health_check_first_false_waits(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """check_first=False skips the immediate return even when already healthy."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    await setup_guards(make_guard("WaitCF", strategy="notify"))
    await hass.async_block_till_done()
    eid = entity_id_for(hass, "guard0", "sensor", "status")

    resp = await _call(hass, "wait_for_health", eid, timeout=1, check_first=False)
    # It did NOT return at once; the end-guard re-check found health still OK.
    assert resp["health"] == "ok"
    assert resp["timed_out"] is False
    assert resp["waited_s"] >= 1


async def test_wait_for_health_uses_own_waiter_not_verify_event(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """A service wait uses its own waiter, never clobbering the engine VERIFY event."""
    hass.states.async_set("binary_sensor.guard_health", "off")
    entry = await setup_guards(make_guard("WaitSep", strategy="notify"))
    await hass.async_block_till_done()
    engine = entry.runtime_data.engines["guard0"]
    eid = entity_id_for(hass, "guard0", "sensor", "status")

    task = await _arm(hass, eid, timeout=30)
    assert engine._verify_event is None  # the engine's own event is untouched
    assert len(engine._health_waiters) == 1  # the service armed its own waiter

    hass.states.async_set("binary_sensor.guard_health", "on")
    await hass.async_block_till_done()
    await task
    assert len(engine._health_waiters) == 0  # cleaned up in finally
