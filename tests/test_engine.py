"""Level-2 engine state-machine tests (testing.md §3) against a real hass.

Drives the DeviceEngine through its lifecycle with deterministic time-travel
(`async_fire_time_changed`) instead of the flaky live dev runtime: happy path,
max-attempts → escalated, auto-off → escalated, manual recover, cooldown →
suspect, and persistence/restore. A stub health + driver stand in for the real
sources so the *engine* logic is what's under test.

    PYTHONPATH=<ha-core>:<ha-core>/config python tests/test_engine.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta

from homeassistant.util import dt as dt_util

from tests.common import async_fire_time_changed, async_test_home_assistant

from custom_components.necromancer.drivers.base import RecoveryDriver
from custom_components.necromancer.engine import DeviceEngine, GState
from custom_components.necromancer.health.base import Health, HealthSource
from custom_components.necromancer.policies.standard import StandardPolicy


class FakeHealth(HealthSource):
    def __init__(self, hass, verdict=Health.OK):
        super().__init__(hass, {})
        self.verdict = verdict

    @property
    def watched_entities(self):
        return []

    def evaluate(self):
        return self.verdict

    async def async_setup(self, on_change):
        return None


class StubDriver(RecoveryDriver):
    def __init__(self, hass, on_recover=None):
        super().__init__(hass, {"type": "stub"})
        self.calls = 0
        self.on_recover = on_recover
        self.raise_it = False

    async def can_recover(self):
        return True, ""

    async def recover(self):
        self.calls += 1
        if self.raise_it:
            raise RuntimeError("boom")
        if self.on_recover:
            self.on_recover()


def make(hass, health, driver, **behavior):
    b = {"debounce": 30, "boot_window": 30, "cooldown": 30, "max_attempts": 2,
         "health_check": True}
    b.update(behavior)
    return DeviceEngine(hass, "G", health, driver, StandardPolicy({}), b,
                        subentry_id="g", engines={})


async def _advance(hass, seconds):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


# ---------------- state machine ----------------


async def test_happy_path(hass, _):
    health = FakeHealth(hass, Health.OK)
    driver = StubDriver(hass, on_recover=lambda: setattr(health, "verdict", Health.OK))
    eng = make(hass, health, driver)
    await eng.async_start()
    assert eng.state is GState.OK
    health.verdict = Health.UNHEALTHY
    eng._evaluate()
    assert eng.state is GState.SUSPECT
    await _advance(hass, 30)  # debounce -> recover -> verify(OK) -> cooldown
    assert eng.state is GState.COOLDOWN, eng.state
    assert eng.recover_count == 1 and driver.calls == 1
    await _advance(hass, 30)  # cooldown -> ok (healthy)
    assert eng.state is GState.OK
    await eng.async_stop()


async def test_max_attempts_escalates(hass, _):
    health = FakeHealth(hass, Health.UNHEALTHY)
    driver = StubDriver(hass)  # recover() never fixes health
    eng = make(hass, health, driver, boot_window=0, max_attempts=2)
    await eng.async_start()  # already unhealthy -> suspect
    assert eng.state is GState.SUSPECT
    await _advance(hass, 30)  # debounce -> 2 attempts, both verify-timeout -> escalated
    assert eng.state is GState.ESCALATED, eng.state
    assert driver.calls == 2
    await eng.async_stop()


async def test_raising_driver_is_failed_attempt(hass, _):
    health = FakeHealth(hass, Health.UNHEALTHY)
    driver = StubDriver(hass)
    driver.raise_it = True
    eng = make(hass, health, driver, boot_window=0, max_attempts=1)
    await eng.async_start()
    await _advance(hass, 30)
    assert eng.state is GState.ESCALATED  # raise = failed, not false success
    await eng.async_stop()


async def test_auto_off_escalates(hass, _):
    health = FakeHealth(hass, Health.UNHEALTHY)
    driver = StubDriver(hass)
    eng = make(hass, health, driver)
    eng.auto = False
    await eng.async_start()
    await _advance(hass, 30)
    assert eng.state is GState.ESCALATED and driver.calls == 0
    await eng.async_stop()


async def test_manual_recover(hass, _):
    health = FakeHealth(hass, Health.UNHEALTHY)
    driver = StubDriver(hass, on_recover=lambda: setattr(health, "verdict", Health.OK))
    eng = make(hass, health, driver, boot_window=0)
    await eng.async_start()
    await eng.async_manual_recover()  # bypass debounce + auto gate
    await hass.async_block_till_done()
    assert driver.calls >= 1 and eng.state in (GState.COOLDOWN, GState.OK)
    await eng.async_stop()


async def test_cooldown_to_suspect(hass, _):
    health = FakeHealth(hass, Health.OK)
    driver = StubDriver(hass, on_recover=lambda: setattr(health, "verdict", Health.OK))
    eng = make(hass, health, driver)
    await eng.async_start()
    health.verdict = Health.UNHEALTHY
    eng._evaluate()
    await _advance(hass, 30)  # -> cooldown
    assert eng.state is GState.COOLDOWN
    health.verdict = Health.UNHEALTHY  # broke again during cooldown
    await _advance(hass, 30)  # cooldown elapsed, still unhealthy -> suspect
    assert eng.state is GState.SUSPECT, eng.state
    await eng.async_stop()


async def test_debounce_blip_absorbed(hass, _):
    health = FakeHealth(hass, Health.OK)
    driver = StubDriver(hass)
    eng = make(hass, health, driver)
    await eng.async_start()
    health.verdict = Health.UNHEALTHY
    eng._evaluate()
    assert eng.state is GState.SUSPECT
    health.verdict = Health.OK  # recovered before debounce elapsed
    await _advance(hass, 30)
    assert eng.state is GState.OK and driver.calls == 0
    await eng.async_stop()


# ---------------- persistence ----------------


async def test_persistence_escalated_stays(hass, _):
    snap = {"state": "escalated", "attempt": 2, "recover_count": 3, "auto": False}
    health = FakeHealth(hass, Health.UNHEALTHY)
    eng = DeviceEngine(hass, "G", health, StubDriver(hass), StandardPolicy({}),
                       {"debounce": 30}, subentry_id="g", engines={}, persisted=snap)
    assert eng.state is GState.ESCALATED  # restored, no free retry
    assert eng.recover_count == 3 and eng.auto is False
    await eng.async_start()
    await hass.async_block_till_done()
    assert eng.state is GState.ESCALATED  # still unhealthy -> stays
    await eng.async_stop()


async def test_persistence_escalated_autoclears(hass, _):
    snap = {"state": "escalated", "attempt": 1, "recover_count": 1, "auto": True}
    health = FakeHealth(hass, Health.OK)  # healthy again at restart
    eng = DeviceEngine(hass, "G", health, StubDriver(hass), StandardPolicy({}),
                       {"debounce": 30}, subentry_id="g", engines={}, persisted=snap)
    assert eng.state is GState.ESCALATED
    await eng.async_start()  # _evaluate: escalated + healthy -> ok
    await hass.async_block_till_done()
    assert eng.state is GState.OK
    await eng.async_stop()


async def test_snapshot_roundtrip(hass, _):
    health = FakeHealth(hass, Health.OK)
    eng = make(hass, health, StubDriver(hass))
    eng.recover_count = 5
    eng.auto = False
    snap = eng.snapshot()
    assert snap["recover_count"] == 5 and snap["auto"] is False
    assert "resolved_port" not in snap  # dropped in the fabric refactor (H1b)
    await eng.async_stop()


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


async def main() -> int:
    passed, failed = 0, 0
    async with async_test_home_assistant() as hass:
        for t in TESTS:
            try:
                await t(hass, None)
            except Exception as err:  # noqa: BLE001
                failed += 1
                print(f"FAIL  {t.__name__}: {err!r}")
            else:
                passed += 1
                print(f"ok    {t.__name__}")
        await hass.async_stop()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
