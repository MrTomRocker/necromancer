"""Level-1 unit tests (testing.md §2) against a real Home Assistant core.

Covers the pure-logic rows of the test spec: health sources, driver pre-flight
(`can_recover`), config-flow helpers, the port-YAML import/export, `actions`
normalisation and `links` closure. PoE fabric + poe_port driver have their own
module (`test_poe.py`).

    PYTHONPATH=<ha-core>:<ha-core>/config python tests/test_units.py
"""

from __future__ import annotations

import asyncio
import sys

import voluptuous as vol

from tests.common import async_test_home_assistant

from custom_components.necromancer import config_flow as cf
from custom_components.necromancer.actions import async_validate
from custom_components.necromancer.drivers import create_driver
from custom_components.necromancer.health import create_health
from custom_components.necromancer.health.base import Health
from custom_components.necromancer.links import group_of, link_components


# ---------------- health/entity_state ----------------


async def test_entity_state_on_off_unknown(hass, _):
    h = create_health(
        hass,
        {"type": "entity_state", "entity_id": "sensor.x",
         "on_value": ["on", "home"], "off_value": ["off"]},
    )
    hass.states.async_set("sensor.x", "home")
    assert h.evaluate() is Health.OK
    hass.states.async_set("sensor.x", "off")
    assert h.evaluate() is Health.UNHEALTHY
    hass.states.async_set("sensor.x", "weird")
    assert h.evaluate() is Health.UNKNOWN  # not in on/off lists -> unknown
    hass.states.async_set("sensor.x", "unavailable")
    assert h.evaluate() is Health.UNKNOWN  # ambiguous -> no false alarm


async def test_entity_state_unavailable_as_off(hass, _):
    h = create_health(
        hass,
        {"type": "entity_state", "entity_id": "sensor.y",
         "on_value": ["on"], "off_value": ["unavailable"]},
    )
    hass.states.async_set("sensor.y", "unavailable")
    assert h.evaluate() is Health.UNHEALTHY  # explicit off wins over ambiguity
    assert create_health(hass, {"type": "entity_state", "entity_id": "sensor.absent",
                                "on_value": ["on"]}).evaluate() is Health.UNKNOWN


async def test_entity_state_legacy_healthy_state(hass, _):
    # no off_value -> anything not in on counts as unhealthy
    h = create_health(hass, {"type": "entity_state", "entity_id": "sensor.z",
                             "healthy_state": "on"})
    hass.states.async_set("sensor.z", "on")
    assert h.evaluate() is Health.OK
    hass.states.async_set("sensor.z", "off")
    assert h.evaluate() is Health.UNHEALTHY


# ---------------- health/template ----------------


async def test_template_boolean_and_unknown(hass, _):
    def verdict(tmpl):
        return create_health(hass, {"type": "template", "template": tmpl}).evaluate()

    assert verdict("{{ true }}") is Health.OK
    assert verdict("{{ 1 == 1 }}") is Health.OK
    assert verdict("{{ 'on' }}") is Health.OK
    assert verdict("{{ false }}") is Health.UNHEALTHY
    assert verdict("{{ 0 }}") is Health.UNHEALTHY
    assert verdict("{{ '' }}") is Health.UNKNOWN
    assert verdict("{{ none }}") is Health.UNKNOWN
    assert verdict("{{ states('sensor.does_not_exist') }}") is Health.UNKNOWN
    assert verdict("{{ 1/0 }}") is Health.UNKNOWN  # render error -> unknown


# ---------------- drivers.can_recover ----------------


async def test_switch_cycle_can_recover(hass, _):
    d = create_driver(hass, {"type": "switch_cycle", "switch_entity": "switch.missing"})
    ok, reason = await d.can_recover()
    assert not ok and "not found" in reason, reason
    hass.states.async_set("switch.present", "on")
    d2 = create_driver(hass, {"type": "switch_cycle", "switch_entity": "switch.present"})
    ok2, _r = await d2.can_recover()
    assert ok2


async def test_action_cycle_can_recover(hass, _):
    missing = create_driver(hass, {"type": "action_cycle", "off_action": None,
                                   "on_action": [{"delay": 1}]})
    ok, reason = await missing.can_recover()
    assert not ok and "off" in reason, reason
    good = create_driver(hass, {"type": "action_cycle",
                                "off_action": [{"delay": 1}], "on_action": [{"delay": 1}]})
    ok2, _r = await good.can_recover()
    assert ok2


# ---------------- config_flow helpers ----------------


async def test_flatten_sections_and_as_list(hass, _):
    assert cf._flatten_sections({"a": 1, "sec": {"b": 2, "c": 3}}) == {"a": 1, "b": 2, "c": 3}
    assert cf._as_list(None) == []
    assert cf._as_list("x") == ["x"]
    assert cf._as_list(["x", "y"]) == ["x", "y"]


async def test_source_type_and_current_strategy(hass, _):
    assert cf._source_type_of({"health": {"type": "template"}}) == cf.SOURCE_TEMPLATE
    assert cf._source_type_of({"health": {"type": "entity_state"}}) == cf.SOURCE_STATE
    assert cf._current_strategy({"driver": {"type": "poe_port"}}) == cf.STRATEGY_POE
    assert cf._current_strategy(
        {"driver": {"type": "action_call"}, "behavior": {"health_check": True}}
    ) == cf.STRATEGY_ACTION_CHECK
    assert cf._current_strategy(
        {"driver": {"type": "switch_cycle"}, "behavior": {"health_check": False}}
    ) == cf.STRATEGY_SWITCH


async def test_build_data_state_switch_check(hass, _):
    step1 = {cf.CONF_NAME: "G", cf.CONF_SOURCE_TYPE: cf.SOURCE_STATE,
             cf.CONF_MODE: cf.MODE_RECOVER, cf.CONF_ENTITY_ID: "binary_sensor.p",
             cf.CONF_ATTRIBUTE: None, cf.CONF_ON_VALUE: ["on"], cf.CONF_OFF_VALUE: ["off"]}
    step2 = {cf.CONF_DEBOUNCE: 30, cf.CONF_COOLDOWN: 60, cf.CONF_BOOT_WINDOW: 90,
             cf.CONF_MAX_ATTEMPTS: 3, cf.CONF_SWITCH_ENTITY: "switch.s",
             cf.CONF_OFF_ON_DELAY: 5}
    data = cf._build_data(step1, step2, cf.STRATEGY_SWITCH_CHECK)
    assert data[cf.CONF_HEALTH]["type"] == "entity_state"
    assert data[cf.CONF_DRIVER]["type"] == "switch_cycle"
    assert data[cf.CONF_BEHAVIOR]["health_check"] is True
    assert data[cf.CONF_BEHAVIOR]["boot_window"] == 90


async def test_build_data_template_notify(hass, _):
    step1 = {cf.CONF_NAME: "N", cf.CONF_SOURCE_TYPE: cf.SOURCE_TEMPLATE,
             cf.CONF_MODE: cf.MODE_NOTIFY, cf.CONF_TEMPLATE: "{{ true }}"}
    step2 = {cf.CONF_DEBOUNCE: 30}
    data = cf._build_data(step1, step2, cf.STRATEGY_SWITCH)
    assert data[cf.CONF_HEALTH]["type"] == "template"
    assert data[cf.CONF_DRIVER]["type"] == "noop"
    assert data[cf.CONF_POLICY]["type"] == cf.MODE_NOTIFY


# ---------------- config_flow ports YAML ----------------


async def test_ports_yaml_valid_and_normalise(hass, _):
    text = """
- label:  P1
  actuator: switch.a
  status_entity: binary_sensor.s
  status_on: on
  id_static: 42
  off_timeout: 30
"""
    ports = cf._parse_ports_yaml(text)
    assert len(ports) == 1
    p = ports[0]
    assert p[cf.CONF_LABEL] == "P1"  # trimmed
    assert p[cf.CONF_STATUS_ON] == ["on"]  # scalar -> list, bool-safe
    assert p[cf.CONF_ID_STATIC] == "42"  # int -> str
    assert p[cf.CONF_STATUS_OFF] == ["off"]  # default


async def test_ports_yaml_rejections(hass, _):
    bad = [
        "not: a list",                       # a dict, not a list
        "- {label: x}",                      # missing actuator
        "- {label: x, actuator: switch.a}",  # missing status_entity
        "- label: x\n  actuator: switch.a\n  status_entity: s\n  off_timeout: -1",  # negative
        "- label: x\n  actuator: switch.a\n  status_entity: s\n  off_timeout: abc",  # non-numeric
        "- just_a_scalar",                   # list of scalar (not a mapping)
        ": : bad yaml :",                    # malformed YAML
        "",                                  # nothing
        "null",                              # explicit null
    ]
    for t in bad:
        try:
            cf._parse_ports_yaml(t)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for: {t!r}")
    # an explicit empty list is valid (it just means "no ports")
    assert cf._parse_ports_yaml("[]") == []


async def test_ports_yaml_roundtrip_and_merge(hass, _):
    ports = [{cf.CONF_LABEL: "P1", cf.CONF_ACTUATOR: "switch.a",
              cf.CONF_STATUS_ENTITY: "binary_sensor.s",
              cf.CONF_STATUS_ON: ["on"], cf.CONF_STATUS_OFF: ["off"],
              cf.CONF_OFF_ON_DELAY: 5, cf.CONF_OFF_TIMEOUT: 20, cf.CONF_ON_TIMEOUT: 60}]
    dumped = cf._ports_to_yaml(ports)
    again = cf._parse_ports_yaml(dumped)
    assert again[0][cf.CONF_LABEL] == "P1" and again[0][cf.CONF_STATUS_ON] == ["on"]
    # merge upserts by label; replace overwrites
    flow = cf.NecromancerOptionsFlow()
    flow._ports = list(ports)
    flow._merge_ports([{cf.CONF_LABEL: "P1", cf.CONF_ACTUATOR: "switch.NEW",
                        cf.CONF_STATUS_ENTITY: "binary_sensor.s",
                        cf.CONF_STATUS_ON: ["on"], cf.CONF_STATUS_OFF: ["off"],
                        cf.CONF_OFF_ON_DELAY: 5, cf.CONF_OFF_TIMEOUT: 20, cf.CONF_ON_TIMEOUT: 60},
                       {cf.CONF_LABEL: "P2", cf.CONF_ACTUATOR: "switch.b",
                        cf.CONF_STATUS_ENTITY: "binary_sensor.s2",
                        cf.CONF_STATUS_ON: ["on"], cf.CONF_STATUS_OFF: ["off"],
                        cf.CONF_OFF_ON_DELAY: 5, cf.CONF_OFF_TIMEOUT: 20, cf.CONF_ON_TIMEOUT: 60}])
    assert len(flow._ports) == 2  # P1 updated in place, P2 added
    assert flow._ports[0][cf.CONF_ACTUATOR] == "switch.NEW"


# ---------------- actions ----------------


async def test_actions_normalise_and_invalid(hass, _):
    seq = await async_validate(hass, {"service": "homeassistant.turn_on",
                                      "entity_id": "light.x"})
    assert isinstance(seq, list) and "action" in seq[0], seq  # service -> action
    raised = False
    try:
        await async_validate(hass, [{"not_a_valid_action_key": 1}])
    except vol.Invalid:
        raised = True
    assert raised, "invalid action should raise vol.Invalid"


# ---------------- links ----------------


async def test_links_closure(hass, _):
    valid = {"A", "B", "C", "D"}
    links = {"A": {"B"}, "B": {"C"}, "C": set(), "D": set()}  # one-sided chain
    assert group_of(links, valid, "A") == {"B", "C"}  # transitive
    assert group_of(links, valid, "C") == {"A", "B"}  # symmetric read
    assert group_of(links, valid, "D") == set()
    assert group_of({"A": {"Z"}}, {"A"}, "A") == set()  # stale id dropped
    comp = link_components(links, valid)
    assert comp["A"] == comp["B"] == comp["C"] == {"A", "B", "C"}


async def test_policy_reasons(hass, _):
    from custom_components.necromancer.const import REASON_AUTO_OFF, REASON_OBSERVE
    from custom_components.necromancer.policies.notify import NotifyPolicy
    from custom_components.necromancer.policies.standard import StandardPolicy

    assert StandardPolicy({}).should_attempt(auto_enabled=True) == (True, "")
    assert StandardPolicy({}).should_attempt(auto_enabled=False) == (False, REASON_AUTO_OFF)
    assert NotifyPolicy({}).should_attempt(auto_enabled=True) == (False, REASON_OBSERVE)
    assert REASON_AUTO_OFF == "auto_off"  # english, no more "auto_aus"


async def test_template_referenced_entities(hass, _):
    h = create_health(hass, {"type": "template",
                             "template": "{{ is_state('sensor.foo', 'on') }}"})
    assert "sensor.foo" in h.referenced_entities(), h.referenced_entities()


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
