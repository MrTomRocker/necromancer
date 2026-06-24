"""PoE fabric + poe_port driver tests against a real Home Assistant core.

Runs with a real `hass` (state machine, event bus, state tracking, service
registry, asyncio locks) so the fabric's event-driven resolution, staged cycle,
per-port lock and status events are exercised for real — not against a fake.

Run from a checkout that can import both ``tests.common`` (the ha-core test
helpers) and ``custom_components.necromancer`` — e.g. inside an HA-core dev
container with the component on the path:

    PYTHONPATH=<ha-core>:<ha-core>/config python tests/test_poe.py

It is a self-contained asyncio runner rather than a pytest module (the custom
component isn't installed as a pytest plugin): each ``test_*`` asserts, and the
runner prints PASS/FAIL and exits non-zero on any failure.
"""

from __future__ import annotations

import asyncio
import sys

from tests.common import async_test_home_assistant  # ha-core test helper

from custom_components.necromancer.const import (
    CONF_ACTUATOR,
    CONF_EXPECTED_ID,
    CONF_ID_ATTRIBUTE,
    CONF_ID_ENTITY,
    CONF_ID_STATIC,
    CONF_LABEL,
    CONF_OFF_ON_DELAY,
    CONF_OFF_TIMEOUT,
    CONF_ON_TIMEOUT,
    CONF_STATUS_ENTITY,
    CONF_STATUS_OFF,
    CONF_STATUS_ON,
    CONF_TYPE,
    DOMAIN,
)
from custom_components.necromancer.core.drivers.poe_port import PoePortDriver
from custom_components.necromancer.core.poe import (
    EVENT_PORT_STATUS,
    PORT_FAILED,
    PORT_GOOD,
    PORT_RECOVERING,
    PoeFabric,
)


def port(label, actuator, status_entity, **kw):
    p = {
        CONF_LABEL: label,
        CONF_ACTUATOR: actuator,
        CONF_STATUS_ENTITY: status_entity,
        CONF_STATUS_ON: list(kw.get("on", ["on"])),
        CONF_STATUS_OFF: list(kw.get("off", ["off"])),
        CONF_OFF_ON_DELAY: kw.get("delay", 0),
        CONF_OFF_TIMEOUT: kw.get("off_to", 2),
        CONF_ON_TIMEOUT: kw.get("on_to", 2),
    }
    for key, conf in (
        ("id_entity", CONF_ID_ENTITY),
        ("id_attribute", CONF_ID_ATTRIBUTE),
        ("id_static", CONF_ID_STATIC),
    ):
        if kw.get(key):
            p[conf] = kw[key]
    return p


class Stubs:
    """Stub homeassistant.turn_off/on that drive actuator + port status states.

    Tracks concurrency inside turn_off so the per-port lock can be asserted.
    """

    def __init__(self, hass):
        self.hass = hass
        self.status_of = {}  # actuator -> status entity
        self.conc = 0
        self.max_conc = 0
        self.cycles = 0  # one per turn_off = one power-cycle
        self.slow = 0.0

    def bind(self, actuator, status_entity):
        self.status_of[actuator] = status_entity

    @staticmethod
    def _ids(call):
        eid = call.data["entity_id"]
        return eid if isinstance(eid, list) else [eid]

    def register(self):
        async def _off(call):
            self.conc += 1
            self.cycles += 1
            self.max_conc = max(self.max_conc, self.conc)
            try:
                if self.slow:
                    await asyncio.sleep(self.slow)
                for eid in self._ids(call):
                    self.hass.states.async_set(eid, "off")
                    if eid in self.status_of:
                        self.hass.states.async_set(self.status_of[eid], "off")
            finally:
                self.conc -= 1

        async def _on(call):
            for eid in self._ids(call):
                self.hass.states.async_set(eid, "on")
                if eid in self.status_of:
                    self.hass.states.async_set(self.status_of[eid], "on")

        self.hass.services.async_register("homeassistant", "turn_off", _off)
        self.hass.services.async_register("homeassistant", "turn_on", _on)


def _events(hass):
    seen = []
    hass.bus.async_listen(
        EVENT_PORT_STATUS, lambda e: seen.append((e.data["port"], e.data["status"]))
    )
    return seen


# ---------------- resolution ----------------


async def test_resolve_live_single(hass, _stubs):
    hass.states.async_set("sensor.nb1", "x", {"mac": "aa:bb:cc"})
    f = PoeFabric(hass)
    f.set_ports(
        [
            port(
                "P1",
                "switch.a1",
                "binary_sensor.s1",
                id_entity="sensor.nb1",
                id_attribute="mac",
            )
        ]
    )
    p, reason = f.resolve_with_reason("AA:BB:CC")  # case-insensitive
    assert p is not None and p[CONF_LABEL] == "P1", (p, reason)
    assert reason == ""
    assert f.cache.get("aa:bb:cc") == "P1", f.cache


async def test_resolve_ambiguous(hass, _stubs):
    hass.states.async_set("sensor.nb1", "x", {"mac": "dup"})
    hass.states.async_set("sensor.nb2", "x", {"mac": "dup"})
    f = PoeFabric(hass)
    f.set_ports(
        [
            port(
                "P1",
                "switch.a1",
                "binary_sensor.s1",
                id_entity="sensor.nb1",
                id_attribute="mac",
            ),
            port(
                "P2",
                "switch.a2",
                "binary_sensor.s2",
                id_entity="sensor.nb2",
                id_attribute="mac",
            ),
        ]
    )
    p, reason = f.resolve_with_reason("dup")
    assert p is None and "2 ports" in reason, (p, reason)


async def test_resolve_last_known(hass, _stubs):
    f = PoeFabric(hass)
    # no live id (entity has no mac), but cache seeded -> last-known fallback
    f.set_ports(
        [
            port(
                "P1",
                "switch.a1",
                "binary_sensor.s1",
                id_entity="sensor.missing",
                id_attribute="mac",
            )
        ],
        cache={"aa:bb:cc": "P1"},
    )
    p, reason = f.resolve_with_reason("aa:bb:cc")
    assert p is not None and p[CONF_LABEL] == "P1" and reason == "", (p, reason)


async def test_resolve_last_known_skips_occupied_port(hass, _stubs):
    # device A was learned on P1, then re-cabled away; another device B now sits
    # on P1. A is still configured and goes unhealthy -> the fabric must NOT cycle
    # P1 (that would reboot the innocent B); it drops the stale entry and refuses.
    hass.states.async_set("sensor.nb1", "x", {"mac": "bb:bb"})  # P1 now serves B
    f = PoeFabric(hass)
    f.set_ports(
        [
            port(
                "P1",
                "switch.a1",
                "binary_sensor.s1",
                id_entity="sensor.nb1",
                id_attribute="mac",
            )
        ],
        cache={"aa:aa": "P1"},  # stale: A -> P1
    )
    p, reason = f.resolve_with_reason("aa:aa")
    assert p is None and "no port matches" in reason, (p, reason)
    assert f.cache.get("aa:aa") is None, f"stale entry not dropped: {f.cache}"


async def test_resolve_none(hass, _stubs):
    f = PoeFabric(hass)
    f.set_ports([port("P1", "switch.a1", "binary_sensor.s1", id_static="hue")])
    p, reason = f.resolve_with_reason("nope")
    assert p is None and "no port matches" in reason, (p, reason)


async def test_resolve_static_caseinsensitive(hass, _stubs):
    f = PoeFabric(hass)
    f.set_ports([port("P1", "switch.a1", "binary_sensor.s1", id_static="Hue-Bridge")])
    p, _ = f.resolve_with_reason("hue-bridge")
    assert p is not None and p[CONF_LABEL] == "P1"


async def test_port_with_both_id_sources_is_ignored(hass, _stubs):
    hass.states.async_set("sensor.nb", "dev")
    f = PoeFabric(hass)
    # both a fixed id and an id-entity -> misconfigured -> ignored, matches nothing
    f.set_ports(
        [
            port(
                "PX",
                "switch.a",
                "binary_sensor.s",
                id_static="dev",
                id_entity="sensor.nb",
            )
        ]
    )
    p, reason = f.resolve_with_reason("dev")
    assert p is None and "no port matches" in reason, (p, reason)


async def test_relearn_recable_updates_cache(hass, _stubs):
    hass.states.async_set("sensor.nb1", "x", {"mac": "aa:bb"})
    hass.states.async_set("sensor.nb2", "x", {})
    f = PoeFabric(hass)
    f.set_ports(
        [
            port(
                "P1",
                "switch.a1",
                "binary_sensor.s1",
                id_entity="sensor.nb1",
                id_attribute="mac",
            ),
            port(
                "P2",
                "switch.a2",
                "binary_sensor.s2",
                id_entity="sensor.nb2",
                id_attribute="mac",
            ),
        ]
    )
    assert f.cache.get("aa:bb") == "P1", f.cache
    # device re-cabled to P2: P1 loses it, P2 reports it -> cache follows (WARNING)
    hass.states.async_set("sensor.nb1", "x", {})
    hass.states.async_set("sensor.nb2", "x", {"mac": "aa:bb"})
    await hass.async_block_till_done()
    assert f.cache.get("aa:bb") == "P2", f.cache


async def test_placeholder_ids_are_never_learned(hass, _stubs):
    # ports with nothing connected report a placeholder ("-"); the fabric must not
    # treat that as a device hopping between ports (would log a WARNING storm).
    hass.states.async_set("sensor.nb1", "-", {})
    hass.states.async_set("sensor.nb2", "-", {})
    f = PoeFabric(hass)
    f.set_ports(
        [
            port("P1", "switch.a1", "binary_sensor.s1", id_entity="sensor.nb1"),
            port("P2", "switch.a2", "binary_sensor.s2", id_entity="sensor.nb2"),
        ]
    )
    assert f.cache == {}, f.cache  # nothing learned from "-"
    # a placeholder identifier resolves to nothing (never matches a "-" port)
    p, reason = f.resolve_with_reason("-")
    assert p is None and "no port matches" in reason, (p, reason)


# ---------------- cycle / status / lock ----------------


async def test_repair_cycles_and_fires_status(hass, stubs):
    stubs.bind("switch.act", "binary_sensor.st")
    hass.states.async_set("switch.act", "on")
    hass.states.async_set("binary_sensor.st", "on")
    seen = _events(hass)
    f = PoeFabric(hass)
    f.set_ports([port("PX", "switch.act", "binary_sensor.st", id_static="dev")])
    ok = await f.repair("dev")
    await hass.async_block_till_done()
    assert ok is True
    assert hass.states.get("switch.act").state == "on"  # ends powered on
    assert (PORT_RECOVERING in [s for _, s in seen]) and (
        PORT_GOOD in [s for _, s in seen]
    ), seen
    assert f.status("PX") == PORT_GOOD


async def test_repair_unresolvable_returns_false(hass, stubs):
    f = PoeFabric(hass)
    f.set_ports([port("PX", "switch.act", "binary_sensor.st", id_static="dev")])
    ok = await f.repair("ghost")
    assert ok is False


async def test_run_cycle_marks_failed_when_actuator_raises(hass, stubs):
    # If the actuator service raises mid-cycle, the port must end FAILED (and fire
    # the failed transition), not stay stuck on RECOVERING forever.
    hass.states.async_set("switch.actX", "on")
    hass.states.async_set("binary_sensor.stX", "on")
    seen = _events(hass)

    async def _boom(call):
        raise RuntimeError("actuator offline")

    hass.services.async_register("homeassistant", "turn_off", _boom)  # override stub
    f = PoeFabric(hass)
    f.set_ports([port("PX", "switch.actX", "binary_sensor.stX", id_static="dev")])
    try:
        await f.repair("dev")  # raises -> propagates (engine would retry/escalate)
    except RuntimeError:
        pass
    finally:
        stubs.register()  # restore normal turn_off/on for the remaining tests
    await hass.async_block_till_done()
    assert f.status("PX") == PORT_FAILED, f.status("PX")  # not stuck on RECOVERING
    assert PORT_FAILED in [s for _, s in seen], seen  # failed transition fired


async def test_concurrent_callers_coalesce(hass, stubs):
    stubs.bind("switch.actL", "binary_sensor.stL")
    stubs.slow = 0.05  # widen the window so a second cycle would run if not coalesced
    hass.states.async_set("switch.actL", "on")
    hass.states.async_set("binary_sensor.stL", "on")
    f = PoeFabric(hass)
    f.set_ports(
        [port("PL", "switch.actL", "binary_sensor.stL", id_static="dev", delay=0)]
    )
    res = await asyncio.gather(f.repair("dev"), f.repair("dev"))
    await hass.async_block_till_done()
    stubs.slow = 0.0
    assert res == [True, True], res  # both callers share the one cycle's result
    assert stubs.cycles == 1, f"concurrent callers ran {stubs.cycles} cycles (want 1)"
    assert stubs.max_conc == 1
    assert f.status("PL") == PORT_GOOD


# ---------------- driver delegation ----------------


def _driver(hass, fabric, expected_id):
    hass.data.setdefault(DOMAIN, {})["fabric"] = fabric
    return PoePortDriver(hass, {CONF_TYPE: "poe_port", CONF_EXPECTED_ID: expected_id})


async def test_driver_can_recover_and_target(hass, stubs):
    f = PoeFabric(hass)
    f.set_ports([port("PD", "switch.actD", "binary_sensor.stD", id_static="dev")])
    d = _driver(hass, f, "dev")
    ok, reason = await d.can_recover()
    assert ok and reason == "", reason
    assert "PD" in d.target_info() and "switch.actD" in d.target_info(), d.target_info()
    assert d.config_errors() == []


async def test_driver_blocks_on_no_match(hass, stubs):
    f = PoeFabric(hass)
    f.set_ports([port("PD", "switch.actD", "binary_sensor.stD", id_static="dev")])
    d = _driver(hass, f, "other")
    ok, reason = await d.can_recover()
    assert not ok and "no port matches" in reason, reason


async def test_driver_recover_cycles_via_fabric(hass, stubs):
    stubs.bind("switch.actR", "binary_sensor.stR")
    hass.states.async_set("switch.actR", "on")
    hass.states.async_set("binary_sensor.stR", "on")
    f = PoeFabric(hass)
    f.set_ports([port("PR", "switch.actR", "binary_sensor.stR", id_static="dev")])
    d = _driver(hass, f, "dev")
    assert await d.recover() is True  # fabric verdict: port confirmed online
    await hass.async_block_till_done()
    assert hass.states.get("switch.actR").state == "on"
    assert f.status("PR") == PORT_GOOD


async def test_driver_recover_returns_fabric_verdict(hass, stubs):
    # poe_port.recover() returns the fabric's verdict so the engine can use it
    # when there is no device health-check (unresolvable id -> False).
    f = PoeFabric(hass)
    f.set_ports([port("PD", "switch.actD", "binary_sensor.stD", id_static="dev")])
    d = _driver(hass, f, "ghost")  # unresolvable -> no cycle, verdict False
    assert await d.recover() is False


async def test_driver_no_ports_config_error(hass, stubs):
    f = PoeFabric(hass)
    f.set_ports([])
    d = _driver(hass, f, "dev")
    assert d.config_errors(), "expected a config error when no ports configured"


async def test_driver_and_service_coalesce(hass, stubs):
    stubs.bind("switch.actS", "binary_sensor.stS")
    stubs.slow = 0.05
    hass.states.async_set("switch.actS", "on")
    hass.states.async_set("binary_sensor.stS", "on")
    f = PoeFabric(hass)
    f.set_ports([port("PS", "switch.actS", "binary_sensor.stS", id_static="dev")])
    d = _driver(hass, f, "dev")
    # driver-guard recovery + the repair_poe_port service hit the same port at once
    await asyncio.gather(d.recover(), f.repair("dev"))
    await hass.async_block_till_done()
    stubs.slow = 0.0
    assert stubs.cycles == 1, f"driver & service ran {stubs.cycles} cycles (want 1)"
    assert stubs.max_conc == 1


async def test_port_problems_no_id_and_missing_entity(hass, stubs):
    # No id source -> port_no_id; an actuator/status entity with no state ->
    # port_entity_missing; a fully-configured port -> nothing.
    f = PoeFabric(hass)
    f.set_ports(
        [
            port("NOID", "switch.act_noid", "binary_sensor.st_noid"),
            port("OK", "switch.act_ok", "binary_sensor.st_ok", id_static="dev"),
        ]
    )
    hass.states.async_set("switch.act_ok", "on")
    hass.states.async_set("binary_sensor.st_ok", "on")
    found = {(p["placeholders"]["label"], p["key"]) for p in f.port_problems()}
    assert ("NOID", "port_no_id") in found
    assert ("NOID", "port_entity_missing") in found
    assert ("OK", "port_no_id") not in found
    assert ("OK", "port_entity_missing") not in found


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


async def main() -> int:
    passed, failed = 0, 0
    async with async_test_home_assistant() as hass:
        stubs = Stubs(hass)
        stubs.register()
        for t in TESTS:
            # reset concurrency tracking per test
            stubs.max_conc = 0
            stubs.conc = 0
            stubs.cycles = 0
            stubs.slow = 0.0
            try:
                await t(hass, stubs)
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
