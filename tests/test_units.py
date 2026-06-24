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

from tests.common import async_mock_service, async_test_home_assistant

from custom_components.necromancer.config_flow import (
    DeviceSubentryFlow,
    NecromancerOptionsFlow,
)
from custom_components.necromancer.config_flow_helpers import schemas as cf
from custom_components.necromancer.core.actions import async_run, async_validate
from custom_components.necromancer.core.drivers import create_driver
from custom_components.necromancer.core.health import create_health
from custom_components.necromancer.core.health.base import Health
from custom_components.necromancer.core.links import group_of, link_components


# ---------------- core/health/entity_state ----------------


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
    hass.states.async_set("sensor.x", "unknown")
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


async def test_entity_state_unknown_as_off(hass, _):
    h = create_health(
        hass,
        {"type": "entity_state", "entity_id": "sensor.z",
         "on_value": ["on"], "off_value": ["unknown"]},
    )
    hass.states.async_set("sensor.z", "unknown")
    assert h.evaluate() is Health.UNHEALTHY  # explicit off wins over ambiguity


async def test_entity_state_legacy_healthy_state(hass, _):
    # no off_value -> anything not in on counts as unhealthy
    h = create_health(hass, {"type": "entity_state", "entity_id": "sensor.z",
                             "healthy_state": "on"})
    hass.states.async_set("sensor.z", "on")
    assert h.evaluate() is Health.OK
    hass.states.async_set("sensor.z", "off")
    assert h.evaluate() is Health.UNHEALTHY


# ---------------- core/health/template ----------------


async def test_template_boolean_and_unknown(hass, _):
    def verdict(tmpl):
        return create_health(hass, {"type": "template", "template": tmpl}).evaluate()

    # healthy whitelist: true / on / 1 / yes
    assert verdict("{{ true }}") is Health.OK
    assert verdict("{{ 1 == 1 }}") is Health.OK
    assert verdict("{{ 'on' }}") is Health.OK
    assert verdict("{{ 'yes' }}") is Health.OK
    assert verdict("{{ 1 }}") is Health.OK
    # faulty: the inverse (false / off / 0 / no)
    assert verdict("{{ false }}") is Health.UNHEALTHY
    assert verdict("{{ 0 }}") is Health.UNHEALTHY
    assert verdict("{{ 'off' }}") is Health.UNHEALTHY
    assert verdict("{{ 'no' }}") is Health.UNHEALTHY
    # everything else -> unknown (no false alarm)
    assert verdict("{{ '' }}") is Health.UNKNOWN
    assert verdict("{{ none }}") is Health.UNKNOWN
    assert verdict("{{ 'unavailable' }}") is Health.UNKNOWN
    assert verdict("{{ 'kaputt' }}") is Health.UNKNOWN  # unrecognized -> unknown, not faulty
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
    ) == cf.STRATEGY_ACTION
    assert cf._current_strategy(
        {"driver": {"type": "switch_cycle"}, "behavior": {"health_check": False}}
    ) == cf.STRATEGY_SWITCH


async def test_build_data_state_switch_check(hass, _):
    step1 = {cf.CONF_NAME: "G", cf.CONF_SOURCE_TYPE: cf.SOURCE_STATE,
             cf.CONF_ENTITY_ID: "binary_sensor.p",
             cf.CONF_ATTRIBUTE: None, cf.CONF_ON_VALUE: ["on"], cf.CONF_OFF_VALUE: ["off"]}
    step2 = {cf.CONF_DEBOUNCE: 30, cf.CONF_COOLDOWN: 60, cf.CONF_BOOT_WINDOW: 90,
             cf.CONF_MAX_ATTEMPTS: 3, cf.CONF_SWITCH_ENTITY: "switch.s",
             cf.CONF_OFF_ON_DELAY: 5}
    data = cf._build_data(step1, step2, cf.STRATEGY_SWITCH)
    assert data[cf.CONF_HEALTH]["type"] == "entity_state"
    assert data[cf.CONF_DRIVER]["type"] == "switch_cycle"
    assert data[cf.CONF_BEHAVIOR]["health_check"] is True
    assert data[cf.CONF_BEHAVIOR]["boot_window"] == 90


async def test_build_data_health_check_toggle(hass, _):
    step1 = {cf.CONF_NAME: "G", cf.CONF_SOURCE_TYPE: cf.SOURCE_STATE,
             cf.CONF_ENTITY_ID: "binary_sensor.p",
             cf.CONF_ATTRIBUTE: None, cf.CONF_ON_VALUE: ["on"], cf.CONF_OFF_VALUE: ["off"]}
    step2 = {cf.CONF_DEBOUNCE: 30, cf.CONF_COOLDOWN: 60, cf.CONF_BOOT_WINDOW: 90,
             cf.CONF_MAX_ATTEMPTS: 3, cf.CONF_SWITCH_ENTITY: "switch.s",
             cf.CONF_OFF_ON_DELAY: 5, cf.CONF_HEALTH_CHECK: False}
    # Toggle off -> not verified, but the numbers stay stored (editable later).
    off = cf._build_data(step1, step2, cf.STRATEGY_SWITCH)
    assert off[cf.CONF_BEHAVIOR]["health_check"] is False
    assert off[cf.CONF_BEHAVIOR]["boot_window"] == 90
    assert off[cf.CONF_BEHAVIOR]["max_attempts"] == 3
    # Absent toggle defaults to on (verify is the default).
    no_toggle = {k: v for k, v in step2.items() if k != cf.CONF_HEALTH_CHECK}
    on = cf._build_data(step1, no_toggle, cf.STRATEGY_SWITCH)
    assert on[cf.CONF_BEHAVIOR]["health_check"] is True


async def test_build_data_reload_entry(hass, _):
    base = {cf.CONF_NAME: "G", cf.CONF_SOURCE_TYPE: cf.SOURCE_STATE,
            cf.CONF_DEVICE_ID: "dev1", cf.CONF_ENTITY_ID: "binary_sensor.p",
            cf.CONF_ATTRIBUTE: None, cf.CONF_ON_VALUE: ["on"], cf.CONF_OFF_VALUE: ["off"]}
    step2 = {cf.CONF_DEBOUNCE: 30, cf.CONF_COOLDOWN: 60, cf.CONF_BOOT_WINDOW: 90,
             cf.CONF_MAX_ATTEMPTS: 3, cf.CONF_SWITCH_ENTITY: "switch.s",
             cf.CONF_OFF_ON_DELAY: 5, cf.CONF_RELOAD_ENTRY: True, cf.CONF_RELOAD_DELAY: 7}
    data = cf._build_data(base, step2, cf.STRATEGY_SWITCH)
    assert data[cf.CONF_BEHAVIOR][cf.CONF_RELOAD_ENTRY] is True
    assert data[cf.CONF_BEHAVIOR][cf.CONF_RELOAD_DELAY] == 7
    # no assigned device -> reload not stored even with the flag set
    nodev = {k: v for k, v in base.items() if k != cf.CONF_DEVICE_ID}
    data2 = cf._build_data(nodev, step2, cf.STRATEGY_SWITCH)
    assert cf.CONF_RELOAD_ENTRY not in data2[cf.CONF_BEHAVIOR]


async def test_build_data_template_notify(hass, _):
    step1 = {cf.CONF_NAME: "N", cf.CONF_SOURCE_TYPE: cf.SOURCE_TEMPLATE,
             cf.CONF_TEMPLATE: "{{ true }}"}
    step2 = {cf.CONF_DEBOUNCE: 30}
    data = cf._build_data(step1, step2, cf.MODE_NOTIFY)
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
        "- {label: x, actuator: switch.a, status_entity: s, id_static: dev, id_entity: sensor.n}",  # two id sources
        "- {label: x, actuator: switch.a, status_entity: s, id_attribute: mac}",  # attribute, no id entity
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


async def test_validate_port_identity(hass, _):
    v = cf._validate_port_identity
    assert v({cf.CONF_ID_STATIC: "x", cf.CONF_ID_ENTITY: "sensor.a"}) == (
        cf.CONF_ID_STATIC,
        "id_conflict",
    )
    assert v({cf.CONF_ID_ATTRIBUTE: "mac"}) == (
        cf.CONF_ID_ATTRIBUTE,
        "attribute_needs_entity",
    )
    assert v({cf.CONF_ID_STATIC: "x"}) is None
    assert v({cf.CONF_ID_ENTITY: "sensor.a", cf.CONF_ID_ATTRIBUTE: "mac"}) is None


async def test_ports_yaml_roundtrip_and_merge(hass, _):
    ports = [{cf.CONF_LABEL: "P1", cf.CONF_ACTUATOR: "switch.a",
              cf.CONF_STATUS_ENTITY: "binary_sensor.s",
              cf.CONF_STATUS_ON: ["on"], cf.CONF_STATUS_OFF: ["off"],
              cf.CONF_OFF_ON_DELAY: 5, cf.CONF_OFF_TIMEOUT: 20, cf.CONF_ON_TIMEOUT: 60}]
    dumped = cf._ports_to_yaml(ports)
    again = cf._parse_ports_yaml(dumped)
    assert again[0][cf.CONF_LABEL] == "P1" and again[0][cf.CONF_STATUS_ON] == ["on"]
    # merge upserts by label; replace overwrites
    flow = NecromancerOptionsFlow()
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


async def test_flow_rejects_empty_action(hass, _):
    flow = DeviceSubentryFlow()
    flow.hass = hass
    flow._reconfig = False
    flow._strategy = "action"
    flow._step1 = {cf.CONF_NAME: "X", cf.CONF_SOURCE_TYPE: cf.SOURCE_TEMPLATE,
                   cf.CONF_TEMPLATE: "{{ true }}"}
    flow._with_link = lambda schema: schema  # avoid needing a real config entry
    captured: dict = {}
    flow.async_show_form = lambda **kw: captured.update(kw) or kw
    await flow.async_step_action({"action": [], "behavior": {"debounce": 2, "cooldown": 2},
                                 "notification": {}, "linked_guards": {"linked_guards": []}})
    assert captured.get("errors", {}).get(cf.CONF_ACTION) == "action_required", captured


async def test_own_guard_entities_only_self(hass, _):
    from homeassistant.helpers import entity_registry as er

    from custom_components.necromancer.config_flow_helpers.schemas import (
        _own_guard_entities,
    )

    reg = er.async_get(hass)
    a = reg.async_get_or_create("sensor", "necromancer", "GUARDA_status")
    b = reg.async_get_or_create("sensor", "necromancer", "GUARDB_status")
    own = _own_guard_entities(hass, "GUARDA")
    # only this guard's entity is excluded; another guard's stays pickable
    assert a.entity_id in own, own
    assert b.entity_id not in own, own
    # adding a new guard (no subentry yet) excludes nothing necromancer-wise
    assert _own_guard_entities(hass, None) == []


async def test_notify_resolve_tts_and_event_text(hass, _):
    from custom_components.necromancer.core.notify import _resolve

    # TTS: number as "von", not a slash; message = "name: text", event_text = text only
    msg, ev = _resolve("de", "G", "recovery_attempt", {"attempt": 1, "max": 2})
    assert ev == "Reparaturversuch 1 von 2." and msg == "G: Reparaturversuch 1 von 2.", (msg, ev)
    # plural-correct attempts (de)
    _m, a1 = _resolve("de", "G", "recovery_failed", {"attempt": 1})
    _m, a3 = _resolve("de", "G", "recovery_failed", {"attempt": 3})
    assert "1 Versuch" in a1 and "Versuche" not in a1, a1
    assert "3 Versuche" in a3, a3
    # plural-correct attempts (en)
    _m, e1 = _resolve("en", "G", "recovery_failed", {"attempt": 1})
    assert "1 attempt" in e1 and "attempts" not in e1, e1
    # unknown language -> english fallback
    _m, efb = _resolve("xx", "G", "recovery_success", {})
    assert efb == "Recovery succeeded.", efb


async def test_notify_custom(hass, _):
    """async_notify_custom passes the documented notify variables to the action."""
    from custom_components.necromancer.core.notify import async_notify_custom

    calls = async_mock_service(hass, "test", "notify_sink")
    action = [
        {
            "action": "test.notify_sink",
            "data": {
                "message": "{{ message }}",
                "name": "{{ name }}",
                "event_text": "{{ event_text }}",
                "event": "{{ event }}",
                # no `| default` — the integration must supply "" so this never errors
                "attempt": "{{ attempt }}",
                "attempts": "{{ attempts }}",
                "max": "{{ max }}",
                "reason": "{{ reason }}",
            },
        }
    ]
    # minimal: unset optionals arrive as "" (no | default needed), event_text = message
    await async_notify_custom(hass, "Markise", action, "Lege hart nach")
    await hass.async_block_till_done()
    d = calls[0].data
    assert d["message"] == "Lege hart nach" and d["event_text"] == "Lege hart nach", d
    assert d["name"] == "Markise" and d["event"] == "custom", d
    assert d["attempt"] == "" and d["attempts"] == "" and d["max"] == "", d
    assert d["reason"] == "", d
    # explicit event_text + attempt + max -> attempts derived (plural-correct, en)
    await async_notify_custom(
        hass,
        "Markise",
        action,
        "Härter",
        event="retry",
        event_text="nur Text",
        attempt=2,
        max=2,
    )
    await hass.async_block_till_done()
    d2 = calls[1].data
    assert d2["event_text"] == "nur Text" and d2["event"] == "retry", d2
    assert d2["attempt"] == 2 and d2["max"] == 2 and d2["attempts"] == "2 attempts", d2
    # no action configured -> silent no-op
    await async_notify_custom(hass, "Markise", None, "x")
    await hass.async_block_till_done()
    assert len(calls) == 2, calls


async def test_notify_full_variable_set(hass, _):
    """Every notify path always passes the exact same variable set (>= "" each).

    Locks the contract that a notify action template can reference any documented
    variable without a `| default` guard, across every lifecycle event and the
    custom (notify_guard) path. Captures the variables dict handed to the action.
    """
    import custom_components.necromancer.core.notify as notify_mod
    from custom_components.necromancer.const import NOTIFY_MESSAGES

    full = {"message", "name", "event_text", "event", "attempt", "max", "attempts", "reason"}
    captured: list[dict] = []

    async def _cap(_hass, _action, _name, variables=None):
        captured.append(dict(variables or {}))
        return {}

    orig = notify_mod.async_run
    notify_mod.async_run = _cap
    try:
        action = [{"action": "test.sink"}]
        # built-in path: every lifecycle event delivers exactly the full set
        for key in NOTIFY_MESSAGES["en"]:
            captured.clear()
            await notify_mod.async_notify(hass, "G", action, key)
            await hass.async_block_till_done()
            assert captured and set(captured[0]) == full, (key, captured)
        # custom path (notify_guard): same full set, unset optionals are ""
        captured.clear()
        await notify_mod.async_notify_custom(hass, "G", action, "hi")
        await hass.async_block_till_done()
        assert set(captured[0]) == full, captured[0]
        assert captured[0]["reason"] == "" and captured[0]["attempt"] == "", captured[0]
        # attempts is derived from attempt on the custom path too
        captured.clear()
        await notify_mod.async_notify_custom(hass, "G", action, "hi", attempt=3)
        await hass.async_block_till_done()
        assert captured[0]["attempts"] == "3 attempts", captured[0]
    finally:
        notify_mod.async_run = orig


async def test_policy_reasons(hass, _):
    from custom_components.necromancer.const import REASON_AUTO_OFF, REASON_OBSERVE
    from custom_components.necromancer.core.policies.notify import NotifyPolicy
    from custom_components.necromancer.core.policies.standard import StandardPolicy

    assert StandardPolicy({}).should_attempt(auto_enabled=True) == (True, "")
    assert StandardPolicy({}).should_attempt(auto_enabled=False) == (False, REASON_AUTO_OFF)
    assert NotifyPolicy({}).should_attempt(auto_enabled=True) == (False, REASON_OBSERVE)
    assert REASON_AUTO_OFF == "auto_off"  # english, no more "auto_aus"


async def test_template_referenced_entities(hass, _):
    h = create_health(hass, {"type": "template",
                             "template": "{{ is_state('sensor.foo', 'on') }}"})
    assert "sensor.foo" in h.referenced_entities(), h.referenced_entities()


# ---------------- core/drivers/action_cycle: off → on variable scope ----------------


async def test_action_cycle_passes_off_vars_to_on(hass, _):
    """A `variables:` set in the off action is readable in the on action."""
    calls = async_mock_service(hass, "test", "cycle_vars")
    drv = create_driver(
        hass,
        {
            "type": "action_cycle",
            "off_on_delay": 0,
            "off_action": [{"variables": {"marker": "carried"}}],
            "on_action": [{"action": "test.cycle_vars", "data": {"v": "{{ marker }}"}}],
        },
    )
    await drv.recover()
    await hass.async_block_till_done()
    assert len(calls) == 1, calls
    assert calls[0].data["v"] == "carried", calls[0].data


async def test_action_cycle_no_vars_backward_compatible(hass, _):
    """Without a `variables:` carry-over, both phases still run as before."""
    calls = async_mock_service(hass, "test", "cycle_plain")
    drv = create_driver(
        hass,
        {
            "type": "action_cycle",
            "off_on_delay": 0,
            "off_action": [{"action": "test.cycle_plain", "data": {"phase": "off"}}],
            "on_action": [{"action": "test.cycle_plain", "data": {"phase": "on"}}],
        },
    )
    await drv.recover()
    await hass.async_block_till_done()
    assert [c.data["phase"] for c in calls] == ["off", "on"], calls


async def test_async_run_returns_vars_without_context(hass, _):
    """Returned scope carries real variables, not HA's injected run `context`."""
    out = await async_run(hass, [{"variables": {"x": "1"}}], "ctxprobe")
    assert out.get("x") == "1", out
    assert "context" not in out, out


async def test_action_cycle_seeds_engine_vars(hass, _):
    """Engine vars passed to recover() are readable in both off and on phases."""
    calls = async_mock_service(hass, "test", "cycle_seed")
    drv = create_driver(
        hass,
        {
            "type": "action_cycle",
            "off_on_delay": 0,
            "off_action": [{"action": "test.cycle_seed", "data": {"a": "{{ attempt }}"}}],
            "on_action": [{"action": "test.cycle_seed", "data": {"a": "{{ attempt }}"}}],
        },
    )
    await drv.recover({"attempt": 2, "max": 3, "name": "X", "guard_entity_id": "sensor.x"})
    await hass.async_block_till_done()
    assert [c.data["a"] for c in calls] == [2, 2], calls


async def test_action_call_seeds_engine_vars(hass, _):
    """Engine vars passed to recover() are readable in an action_call recovery."""
    calls = async_mock_service(hass, "test", "call_seed")
    drv = create_driver(
        hass,
        {
            "type": "action_call",
            "action": [{"action": "test.call_seed", "data": {"a": "{{ attempt }}"}}],
        },
    )
    await drv.recover({"attempt": 5})
    await hass.async_block_till_done()
    assert calls[0].data["a"] == 5, calls[0].data


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
