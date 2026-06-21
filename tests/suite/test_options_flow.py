"""Options flow tests — the flat PoE-port list (add/edit/delete/import/export).

The service entry's options flow is a real-button menu: add / edit / delete a
port, import or export YAML, then "save" writes the flat list to `entry.options`.
A submitted port arrives nested under sections (power/identity/status/timing) and
is flattened to a single flat dict before being stored.
"""

from __future__ import annotations

from typing import Any

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import SetupGuards

# One port submit, sections required (the flow flattens these into a flat dict).
PORT_INPUT: dict[str, Any] = {
    "label": "P1",
    "power": {"actuator": "switch.guard_target"},
    "identity": {"id_static": "aa:bb"},
    "status": {
        "status_entity": "binary_sensor.guard_health",
        "status_on": ["on"],
        "status_off": ["off"],
    },
    "timing": {"off_on_delay": 2, "off_timeout": 10, "on_timeout": 20},
}

# A stored (flat) port, as it lives in entry.options["ports"].
FLAT_PORT: dict[str, Any] = {
    "label": "P1",
    "actuator": "switch.guard_target",
    "id_static": "aa:bb",
    "status_entity": "binary_sensor.guard_health",
    "status_on": ["on"],
    "status_off": ["off"],
    "off_on_delay": 2,
    "off_timeout": 10,
    "on_timeout": 20,
}


async def _menu(hass: HomeAssistant, entry_id: str) -> dict:
    """Open the options flow; assert the init menu and return the result."""
    result = await hass.config_entries.options.async_init(entry_id)
    assert result["type"] is FlowResultType.MENU
    return result


async def _pick(hass: HomeAssistant, flow_id: str, step: str) -> dict:
    return await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": step}
    )


async def _submit(hass: HomeAssistant, flow_id: str, user_input: dict) -> dict:
    return await hass.config_entries.options.async_configure(flow_id, user_input)


async def test_add_port_then_save_stores_flat_port(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """Add a port via the wizard, save, and find it flattened in the options."""
    entry = await setup_guards()

    result = await _menu(hass, entry.entry_id)
    result = await _pick(hass, result["flow_id"], "add_port")
    assert result["step_id"] == "add_port"

    result = await _submit(hass, result["flow_id"], PORT_INPUT)
    assert result["type"] is FlowResultType.MENU

    result = await _pick(hass, result["flow_id"], "save")
    assert result["type"] is FlowResultType.CREATE_ENTRY

    ports = result["data"]["ports"]
    assert len(ports) == 1
    port = ports[0]
    assert port["label"] == "P1"
    # Stored flat: top-level keys, NOT nested under power/timing/status.
    assert port["actuator"] == "switch.guard_target"
    assert port["status_entity"] == "binary_sensor.guard_health"
    assert port["off_on_delay"] == 2
    assert not any(k in port for k in ("power", "timing", "status", "identity"))

    await hass.async_block_till_done()
    assert entry.options["ports"][0]["label"] == "P1"


async def test_edit_port_replaces_not_appends(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """Editing a port replaces it in place: still one port, with the new value."""
    entry = await setup_guards(options={"ports": [dict(FLAT_PORT)]})

    result = await _menu(hass, entry.entry_id)
    result = await _pick(hass, result["flow_id"], "edit_port")
    assert result["step_id"] == "edit_port"

    result = await _submit(hass, result["flow_id"], {"port": "0"})
    assert result["step_id"] == "add_port"

    edited = {**PORT_INPUT, "timing": {**PORT_INPUT["timing"], "off_on_delay": 5}}
    result = await _submit(hass, result["flow_id"], edited)
    assert result["type"] is FlowResultType.MENU

    result = await _pick(hass, result["flow_id"], "save")
    ports = result["data"]["ports"]
    assert len(ports) == 1
    assert ports[0]["off_on_delay"] == 5


async def test_delete_port_removes_it(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """Deleting the only port leaves an empty list after save."""
    entry = await setup_guards(options={"ports": [dict(FLAT_PORT)]})

    result = await _menu(hass, entry.entry_id)
    result = await _pick(hass, result["flow_id"], "delete_port")
    assert result["step_id"] == "delete_port"

    result = await _submit(hass, result["flow_id"], {"port": "0"})
    assert result["type"] is FlowResultType.MENU

    result = await _pick(hass, result["flow_id"], "save")
    assert result["data"]["ports"] == []


@pytest.mark.parametrize(
    "ports_yaml",
    [
        pytest.param("this: [is: bad", id="malformed_yaml"),
        pytest.param("just a scalar", id="scalar_not_list"),
        pytest.param("- label: P1\n  actuator: switch.x", id="missing_status_entity"),
    ],
)
async def test_import_invalid_yaml_shows_error(
    hass: HomeAssistant, setup_guards: SetupGuards, ports_yaml: str
) -> None:
    """Invalid import YAML keeps the form open with errors['base'] == import_failed."""
    entry = await setup_guards()

    result = await _menu(hass, entry.entry_id)
    result = await _pick(hass, result["flow_id"], "import_ports")
    assert result["step_id"] == "import_ports"

    result = await _submit(
        hass,
        result["flow_id"],
        {"ports_yaml": ports_yaml, "import_mode": "merge"},
    )
    assert result["step_id"] == "import_ports"
    assert result["errors"] == {"base": "import_failed"}


async def test_import_replace_then_save(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """A valid import in replace mode overwrites the port list, then saves."""
    entry = await setup_guards(options={"ports": [dict(FLAT_PORT)]})

    yaml_text = (
        "- label: Imported\n"
        "  actuator: switch.guard_target\n"
        "  status_entity: binary_sensor.guard_health\n"
        "  off_on_delay: 3\n"
    )

    result = await _menu(hass, entry.entry_id)
    result = await _pick(hass, result["flow_id"], "import_ports")
    result = await _submit(
        hass,
        result["flow_id"],
        {"ports_yaml": yaml_text, "import_mode": "replace"},
    )
    assert result["type"] is FlowResultType.MENU

    result = await _pick(hass, result["flow_id"], "save")
    ports = result["data"]["ports"]
    assert len(ports) == 1
    assert ports[0]["label"] == "Imported"
    assert ports[0]["off_on_delay"] == 3.0


async def test_import_merge_upserts_by_label(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """Merge import updates a same-label port and appends a new one."""
    entry = await setup_guards(options={"ports": [dict(FLAT_PORT)]})

    yaml_text = (
        "- label: P1\n"
        "  actuator: switch.guard_target\n"
        "  status_entity: binary_sensor.guard_health\n"
        "  off_on_delay: 9\n"
        "- label: P2\n"
        "  actuator: switch.guard_target\n"
        "  status_entity: binary_sensor.guard_health\n"
    )

    result = await _menu(hass, entry.entry_id)
    result = await _pick(hass, result["flow_id"], "import_ports")
    result = await _submit(
        hass,
        result["flow_id"],
        {"ports_yaml": yaml_text, "import_mode": "merge"},
    )
    assert result["type"] is FlowResultType.MENU

    result = await _pick(hass, result["flow_id"], "save")
    ports = result["data"]["ports"]
    labels = [p["label"] for p in ports]
    assert labels == ["P1", "P2"]
    assert ports[0]["off_on_delay"] == 9.0


async def test_edit_and_export_appear_only_with_ports(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """The menu hides edit/delete/export until at least one port exists."""
    entry = await setup_guards()

    result = await _menu(hass, entry.entry_id)
    empty = set(result["menu_options"])
    assert "add_port" in empty
    assert {"edit_port", "delete_port", "export_ports"}.isdisjoint(empty)

    entry_with = await setup_guards(
        options={"ports": [dict(FLAT_PORT)]}, entry_id="necro_with_ports"
    )
    result = await _menu(hass, entry_with.entry_id)
    full = set(result["menu_options"])
    assert {"edit_port", "delete_port", "export_ports"} <= full
