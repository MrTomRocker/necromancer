"""Extra device subentry (guard) config-flow tests.

Covers the subentry-flow paths not exercised in `test_subentry_flow.py`:
every recovery strategy's CREATE driver shape, reconfigure-rename, the
`no_self_link` rejection, and the device/link-scoped sections (`reload`,
`linked_guards`) that only appear under specific conditions.
"""

from __future__ import annotations

import pytest

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr

from .conftest import SetupGuards, make_guard

from tests.common import MockConfigEntry

STATE_HEALTH = {
    "entity_id": "binary_sensor.guard_health",
    "on_value": ["on"],
    "off_value": ["off"],
}
NON_CHECK_BEHAVIOR = {"debounce": 5, "cooldown": 10}
CHECK_BEHAVIOR = {"debounce": 5, "boot_window": 5, "cooldown": 10, "max_attempts": 2}
SAMPLE_ACTION = [
    {"action": "input_boolean.turn_on", "data": {"entity_id": "input_boolean.x"}}
]


async def _init(hass: HomeAssistant, entry_id: str) -> dict:
    return await hass.config_entries.subentries.async_init(
        (entry_id, "device"), context={"source": SOURCE_USER}
    )


async def _cfg(hass: HomeAssistant, flow_id: str, data: dict) -> dict:
    return await hass.config_entries.subentries.async_configure(flow_id, data)


async def _to_strategy(
    hass: HomeAssistant, entry_id: str, name: str, *, assigned_device: dict
) -> dict:
    """Drive source → device, returning the strategy-step result."""
    result = await _init(hass, entry_id)
    result = await _cfg(hass, result["flow_id"], {"source_type": "state_based"})
    return await _cfg(
        hass,
        result["flow_id"],
        {
            "name": name,
            "assigned_device": assigned_device,
            "state_check": STATE_HEALTH,
        },
    )


@pytest.mark.parametrize(
    ("strategy", "final_step", "final_input", "driver_type", "health_check"),
    [
        pytest.param(
            "switch_check",
            "switch",
            {
                "switch_entity": "switch.guard_target",
                "off_on_delay": 2,
                "behavior": CHECK_BEHAVIOR,
                "notification": {},
            },
            "switch_cycle",
            True,
            id="switch_check",
        ),
        pytest.param(
            "action",
            "action",
            {
                "recovery_action": {"action": SAMPLE_ACTION},
                "behavior": NON_CHECK_BEHAVIOR,
                "notification": {},
            },
            "action_call",
            False,
            id="action",
        ),
        pytest.param(
            "action_check",
            "action",
            {
                "recovery_action": {"action": SAMPLE_ACTION},
                "behavior": CHECK_BEHAVIOR,
                "notification": {},
            },
            "action_call",
            True,
            id="action_check",
        ),
        pytest.param(
            "actions",
            "actions",
            {
                "recovery_action": {
                    "off_action": SAMPLE_ACTION,
                    "on_action": SAMPLE_ACTION,
                    "off_on_delay": 2,
                },
                "behavior": NON_CHECK_BEHAVIOR,
                "notification": {},
            },
            "action_cycle",
            False,
            id="actions",
        ),
        pytest.param(
            "actions_check",
            "actions",
            {
                "recovery_action": {
                    "off_action": SAMPLE_ACTION,
                    "on_action": SAMPLE_ACTION,
                    "off_on_delay": 2,
                },
                "behavior": CHECK_BEHAVIOR,
                "notification": {},
            },
            "action_cycle",
            True,
            id="actions_check",
        ),
        pytest.param(
            "poe_port",
            "poe_port",
            {
                "expected_id": "aa:bb",
                "behavior": CHECK_BEHAVIOR,
                "notification": {},
            },
            "poe_port",
            True,
            id="poe_port",
        ),
    ],
)
async def test_create_recover_strategy_driver(
    hass: HomeAssistant,
    setup_guards: SetupGuards,
    strategy: str,
    final_step: str,
    final_input: dict,
    driver_type: str,
    health_check: bool,
) -> None:
    """Each recovery strategy creates a guard with the matching driver type."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    entry = await setup_guards()

    result = await _to_strategy(hass, entry.entry_id, "Drv Guard", assigned_device={})
    assert result["step_id"] == "strategy"

    result = await _cfg(hass, result["flow_id"], {"strategy": strategy})
    assert result["step_id"] == final_step

    result = await _cfg(hass, result["flow_id"], final_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["driver"]["type"] == driver_type
    assert result["data"]["behavior"]["health_check"] is health_check
    assert result["data"]["policy"] == {"type": "standard"}


async def test_create_notify_guard(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """The notify strategy creates a noop-driver, notify-policy guard."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    entry = await setup_guards()

    result = await _to_strategy(
        hass, entry.entry_id, "Notify Guard", assigned_device={}
    )
    result = await _cfg(hass, result["flow_id"], {"strategy": "notify"})
    assert result["step_id"] == "notify"

    result = await _cfg(
        hass,
        result["flow_id"],
        {"debounce": 5, "notification": {}},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["driver"]["type"] == "noop"
    assert result["data"]["policy"] == {"type": "notify"}


async def test_reconfigure_renames_same_subentry(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """Reconfiguring a guard updates its name while keeping the same subentry id."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    entry = await setup_guards(make_guard("Original"))

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "device"),
        context={"source": "reconfigure", "subentry_id": "guard0"},
    )
    assert result["step_id"] == "reconfigure"

    result = await _cfg(hass, result["flow_id"], {"source_type": "state_based"})
    assert result["step_id"] == "device"

    result = await _cfg(
        hass,
        result["flow_id"],
        {"name": "Renamed", "assigned_device": {}, "state_check": STATE_HEALTH},
    )
    assert result["step_id"] == "strategy"

    result = await _cfg(hass, result["flow_id"], {"strategy": "switch_check"})
    assert result["step_id"] == "switch"

    result = await _cfg(
        hass,
        result["flow_id"],
        {
            "switch_entity": "switch.guard_target",
            "off_on_delay": 2,
            "behavior": CHECK_BEHAVIOR,
            "notification": {},
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    assert "guard0" in entry.subentries
    assert entry.subentries["guard0"].data["name"] == "Renamed"


async def test_no_self_link_rejected(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """Assigning a guard's own Necromancer device is rejected at the device step."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    entry = await setup_guards(make_guard("Self"))

    own_device = dr.async_get(hass).async_get_device(
        identifiers={("necromancer", "guard0")}
    )
    assert own_device is not None

    result = await _init(hass, entry.entry_id)
    result = await _cfg(hass, result["flow_id"], {"source_type": "state_based"})
    result = await _cfg(
        hass,
        result["flow_id"],
        {
            "name": "Linker",
            "assigned_device": {"device_id": own_device.id},
            "state_check": STATE_HEALTH,
        },
    )
    assert result["step_id"] == "device"
    assert result["errors"] == {"device_id": "no_self_link"}


async def test_reload_section_only_with_device(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """The switch step exposes a 'reload' section only when a device is assigned."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    entry = await setup_guards()

    foreign_entry = MockConfigEntry(domain="test")
    foreign_entry.add_to_hass(hass)
    foreign_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=foreign_entry.entry_id,
        identifiers={("test", "dev1")},
    )

    result = await _to_strategy(
        hass,
        entry.entry_id,
        "With Device",
        assigned_device={"device_id": foreign_device.id},
    )
    result = await _cfg(hass, result["flow_id"], {"strategy": "switch"})
    assert result["step_id"] == "switch"
    keys_with = {str(k) for k in result["data_schema"].schema}
    assert "reload" in keys_with

    result = await _to_strategy(hass, entry.entry_id, "No Device", assigned_device={})
    result = await _cfg(hass, result["flow_id"], {"strategy": "switch"})
    assert result["step_id"] == "switch"
    keys_without = {str(k) for k in result["data_schema"].schema}
    assert "reload" not in keys_without


async def test_link_section_with_other_recover_guard(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """The switch step exposes a 'linked_guards' section when another recover guard exists."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    entry = await setup_guards(make_guard("First"))

    result = await _to_strategy(hass, entry.entry_id, "Second", assigned_device={})
    result = await _cfg(hass, result["flow_id"], {"strategy": "switch"})
    assert result["step_id"] == "switch"
    keys = {str(k) for k in result["data_schema"].schema}
    assert "linked_guards" in keys
