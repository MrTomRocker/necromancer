"""Level-2 integration tests (testing.md §3) in-process against real HA core.

The dev-container's long-lived HA server is unreliable here, so the integration
items that need a real entity registry / services / engine wiring run in-process
with `async_test_home_assistant` instead — same fidelity, deterministic. Covers
the health-registry robustness path (disable/rename/remove) and PoE pre-flight
blocking (no blind cycling), which the unit suites don't reach.

    PYTHONPATH=<ha-core>:<ha-core>/config python tests/test_integration.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import timedelta

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from tests.common import async_fire_time_changed, async_test_home_assistant

from custom_components.necromancer.const import DOMAIN
from custom_components.necromancer.drivers.poe_port import PoePortDriver
from custom_components.necromancer.engine import DeviceEngine, GState
from custom_components.necromancer.health import create_health
from custom_components.necromancer.poe import PoeFabric
from custom_components.necromancer.policies.standard import StandardPolicy

results: list[tuple[str, bool, str]] = []
findings: list[str] = []


def ok(name, cond, detail=""):
    results.append((name, bool(cond), detail))


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())

    def text(self):
        return "\n".join(self.records)

    def clear(self):
        self.records.clear()


def engine(hass, health, driver, **behavior):
    b = {"debounce": 2, "boot_window": 0, "cooldown": 2, "max_attempts": 1,
         "health_check": True}
    b.update(behavior)
    return DeviceEngine(hass, "G", health, driver, StandardPolicy({}), b,
                        subentry_id="g", engines={})


async def _advance(hass, seconds):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


# ---------- health registry robustness (§3) ----------


async def test_health_disable_logs_blind(hass, cap):
    reg = er.async_get(hass)
    e = reg.async_get_or_create("binary_sensor", "test", "blind1")
    hass.states.async_set(e.entity_id, "on")
    h = create_health(hass, {"type": "entity_state", "entity_id": e.entity_id,
                             "on_value": ["on"], "off_value": ["off"]})
    eng = engine(hass, h, _Noop(hass))
    await eng.async_start()
    cap.clear()
    reg.async_update_entity(e.entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()
    log = cap.text()
    ok("health:disable_logs_blind", "is disabled — guard is blind" in log, log[-200:])
    ok("health:disable_no_crash", "Traceback" not in log, "")
    await eng.async_stop()


async def test_health_rename_follows(hass, cap):
    reg = er.async_get(hass)
    e = reg.async_get_or_create("binary_sensor", "test", "ren1")
    hass.states.async_set(e.entity_id, "on")
    renamed_to = []
    h = create_health(hass, {"type": "entity_state", "entity_id": e.entity_id,
                             "on_value": ["on"]})
    eng = DeviceEngine(hass, "G", h, _Noop(hass), StandardPolicy({}), {"debounce": 2},
                       subentry_id="g", engines={},
                       on_health_renamed=renamed_to.append)
    await eng.async_start()
    cap.clear()
    new_id = "binary_sensor.renamed_target"
    reg.async_update_entity(e.entity_id, new_entity_id=new_id)
    await hass.async_block_till_done()
    ok("health:rename_fires_callback", renamed_to == [new_id], f"{renamed_to}")
    ok("health:rename_logs", "renamed" in cap.text(), "")
    await eng.async_stop()


async def test_health_remove_logs(hass, cap):
    reg = er.async_get(hass)
    e = reg.async_get_or_create("binary_sensor", "test", "rem1")
    hass.states.async_set(e.entity_id, "on")
    h = create_health(hass, {"type": "entity_state", "entity_id": e.entity_id,
                             "on_value": ["on"]})
    eng = engine(hass, h, _Noop(hass))
    await eng.async_start()
    cap.clear()
    reg.async_remove(e.entity_id)
    await hass.async_block_till_done()
    log = cap.text()
    ok("health:remove_logs", "was removed" in log, log[-200:])
    ok("health:remove_no_crash", "Traceback" not in log, "")
    await eng.async_stop()


# ---------- PoE pre-flight blocks blind action (§3 / invariant 5) ----------


class _Noop:
    """Minimal driver double that records recover() calls."""

    def __init__(self, hass):
        self.hass = hass
        self.calls = 0

    async def async_setup(self):
        return None

    async def can_recover(self):
        return True, ""

    async def recover(self):
        self.calls += 1

    def target_info(self):
        return "noop"

    def config_errors(self):
        return []


async def test_poe_bogus_id_blocks_no_blind(hass, cap):
    from custom_components.necromancer.const import (
        CONF_ACTUATOR, CONF_EXPECTED_ID, CONF_ID_STATIC, CONF_LABEL,
        CONF_STATUS_ENTITY, CONF_STATUS_OFF, CONF_STATUS_ON, CONF_TYPE,
    )
    fabric = PoeFabric(hass)
    fabric.set_ports([{CONF_LABEL: "P1", CONF_ACTUATOR: "switch.a",
                       CONF_STATUS_ENTITY: "binary_sensor.s",
                       CONF_STATUS_ON: ["on"], CONF_STATUS_OFF: ["off"],
                       CONF_ID_STATIC: "real-device"}])
    hass.data.setdefault(DOMAIN, {})["fabric"] = fabric
    drv = PoePortDriver(hass, {CONF_TYPE: "poe_port", CONF_EXPECTED_ID: "BOGUS-XYZ"})

    from custom_components.necromancer.health.base import Health, HealthSource

    class FH(HealthSource):
        @property
        def watched_entities(self):
            return []

        def evaluate(self):
            return Health.UNHEALTHY

        async def async_setup(self, on_change):
            return None

    eng = engine(hass, FH(hass, {}), drv)
    await eng.async_start()  # unhealthy -> suspect
    await _advance(hass, 2)  # debounce -> can_recover blocks -> ESCALATED, no cycle
    ok("poe:bogus_id_blocks", eng.state is GState.ESCALATED, f"state={eng.state}")
    await eng.async_stop()


async def test_health_self_reference_warns(hass, cap):
    # a template health that reads the guard's OWN status entity -> feedback loop
    reg = er.async_get(hass)
    e = reg.async_get_or_create("sensor", DOMAIN, "g_status",
                                suggested_object_id="g_status")
    h = create_health(hass, {"type": "template",
                             "template": f"{{{{ is_state('{e.entity_id}', 'ok') }}}}"})
    eng = DeviceEngine(hass, "G", h, _Noop(hass), StandardPolicy({}), {"debounce": 2},
                       subentry_id="g", engines={})
    await eng.async_start()
    cap.clear()
    eng._check_config(hass)  # the startup config check
    await hass.async_block_till_done()
    ok("health:self_reference_warns", "feedback loop" in cap.text(), cap.text()[-200:])
    await eng.async_stop()


TESTS = [test_health_disable_logs_blind, test_health_rename_follows,
         test_health_remove_logs, test_poe_bogus_id_blocks_no_blind,
         test_health_self_reference_warns]


async def main():
    cap = LogCapture()
    logging.getLogger("custom_components.necromancer").addHandler(cap)
    logging.getLogger("custom_components.necromancer").setLevel(logging.DEBUG)
    async with async_test_home_assistant() as hass:
        for t in TESTS:
            cap.clear()
            try:
                await t(hass, cap)
            except Exception as err:  # noqa: BLE001
                ok(t.__name__, False, f"EXC {err!r}")
        await hass.async_stop()
    print("==== RESULTS ====")
    p = sum(1 for _n, c, _d in results if c)
    for n, c, d in results:
        print(f"{'ok  ' if c else 'FAIL'} {n}{('  — ' + d) if (d and not c) else ''}")
    print(f"\n{p}/{len(results)} checks passed")
    return 1 if p != len(results) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
