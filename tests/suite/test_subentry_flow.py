"""Device subentry (guard) config-flow tests."""

from __future__ import annotations

import pytest

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from .conftest import SetupGuards, make_guard

STATE_HEALTH = {
    "entity_id": "binary_sensor.guard_health",
    "on_value": ["on"],
    "off_value": ["off"],
}


async def _init(hass: HomeAssistant, entry_id: str) -> dict:
    return await hass.config_entries.subentries.async_init(
        (entry_id, "device"), context={"source": SOURCE_USER}
    )


async def _cfg(hass: HomeAssistant, flow_id: str, data: dict) -> dict:
    return await hass.config_entries.subentries.async_configure(flow_id, data)


async def test_create_switch_guard(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """Full happy path: source → device → strategy → switch → created subentry."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    hass.states.async_set("switch.guard_target", "on")
    entry = await setup_guards()

    result = await _init(hass, entry.entry_id)
    assert result["step_id"] == "user"

    result = await _cfg(hass, result["flow_id"], {"source_type": "state_based"})
    assert result["step_id"] == "device"

    result = await _cfg(
        hass,
        result["flow_id"],
        {"name": "Switch Guard", "assigned_device": {}, "state_check": STATE_HEALTH},
    )
    assert result["step_id"] == "strategy"

    result = await _cfg(hass, result["flow_id"], {"strategy": "switch"})
    assert result["step_id"] == "switch"

    result = await _cfg(
        hass,
        result["flow_id"],
        {
            "switch_entity": "switch.guard_target",
            "off_on_delay": 2,
            "behavior": {"debounce": 5, "cooldown": 10},
            "notification": {},
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Switch Guard"

    data = result["data"]
    assert data["name"] == "Switch Guard"
    assert data["driver"] == {
        "type": "switch_cycle",
        "switch_entity": "switch.guard_target",
        "off_on_delay": 2,
    }
    assert data["policy"] == {"type": "standard"}
    assert data["health"]["type"] == "entity_state"
    assert data["behavior"]["health_check"] is False


async def test_strategy_step_lists_eight_options(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """The strategy step offers notify first, then the seven recovery strategies."""
    entry = await setup_guards()
    result = await _init(hass, entry.entry_id)
    result = await _cfg(hass, result["flow_id"], {"source_type": "state_based"})
    result = await _cfg(
        hass,
        result["flow_id"],
        {"name": "Opt Guard", "assigned_device": {}, "state_check": STATE_HEALTH},
    )
    assert result["step_id"] == "strategy"
    schema = result["data_schema"].schema
    selector = next(v for k, v in schema.items() if str(k) == "strategy")
    options = selector.config["options"]
    assert options == [
        "notify",
        "switch",
        "switch_check",
        "action",
        "action_check",
        "actions",
        "actions_check",
        "poe_port",
    ]


async def test_notify_strategy_routes_to_notify_step(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """Picking 'notify' routes to the notify-only step, not a recovery step."""
    entry = await setup_guards()
    result = await _init(hass, entry.entry_id)
    result = await _cfg(hass, result["flow_id"], {"source_type": "state_based"})
    result = await _cfg(
        hass,
        result["flow_id"],
        {"name": "Notify Guard", "assigned_device": {}, "state_check": STATE_HEALTH},
    )
    result = await _cfg(hass, result["flow_id"], {"strategy": "notify"})
    assert result["step_id"] == "notify"


async def test_duplicate_name_rejected(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """A second guard reusing an existing name is rejected at the device step."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    entry = await setup_guards(make_guard("Existing", strategy="notify"))

    result = await _init(hass, entry.entry_id)
    result = await _cfg(hass, result["flow_id"], {"source_type": "state_based"})
    result = await _cfg(
        hass,
        result["flow_id"],
        {"name": "Existing", "assigned_device": {}, "state_check": STATE_HEALTH},
    )
    assert result["step_id"] == "device"
    assert result["errors"] == {"name": "duplicate_name"}


async def test_broken_template_rejected(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """An invalid Jinja health template is rejected by TemplateSelector validation.

    In-process the selector raises `InvalidData` (the REST layer turns this into a
    form `errors` dict); either way the guard is never created.
    """
    entry = await setup_guards()
    result = await _init(hass, entry.entry_id)
    result = await _cfg(hass, result["flow_id"], {"source_type": "template_based"})
    assert result["step_id"] == "device"
    with pytest.raises(InvalidData):
        await _cfg(
            hass,
            result["flow_id"],
            {
                "name": "Bad Jinja",
                "assigned_device": {},
                "template_check": {"template": "{{ 1 + }}"},
            },
        )


async def test_empty_action_rejected(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """An action guard with no action sequence is rejected at the action step."""
    hass.states.async_set("binary_sensor.guard_health", "on")
    entry = await setup_guards()
    result = await _init(hass, entry.entry_id)
    result = await _cfg(hass, result["flow_id"], {"source_type": "state_based"})
    result = await _cfg(
        hass,
        result["flow_id"],
        {"name": "Empty Action", "assigned_device": {}, "state_check": STATE_HEALTH},
    )
    result = await _cfg(hass, result["flow_id"], {"strategy": "action"})
    assert result["step_id"] == "action"
    result = await _cfg(
        hass,
        result["flow_id"],
        {
            "recovery_action": {"action": []},
            "behavior": {"debounce": 5, "cooldown": 10},
            "notification": {},
        },
    )
    assert result["step_id"] == "action"
    assert result["errors"] == {"action": "action_required"}
