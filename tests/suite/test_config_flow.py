"""Top-level service config-flow + options-flow tests.

Covers the single blank service entry (NOT the device subentries): creation,
single-instance enforcement, the supported `device` subentry type, and the
options-flow PoE-port menu.
"""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import DOMAIN, SetupGuards

from tests.common import MockConfigEntry


async def test_user_step_creates_blank_entry(hass: HomeAssistant) -> None:
    """The user step creates the single blank service entry with empty data."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Necromancer"
    assert result["data"] == {}


async def test_single_instance_aborts(hass: HomeAssistant) -> None:
    """A second user-initiated flow aborts when an entry already exists."""
    MockConfigEntry(domain=DOMAIN).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_supports_device_subentry_type(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """The service entry advertises the `device` subentry type."""
    entry = await setup_guards()

    flow_cls = config_entries.HANDLERS[DOMAIN]
    types = flow_cls.async_get_supported_subentry_types(entry)
    assert "device" in types


async def test_options_flow_shows_port_menu(
    hass: HomeAssistant, setup_guards: SetupGuards
) -> None:
    """The options flow opens on a menu offering add_port and save."""
    entry = await setup_guards()

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert {"add_port", "save"} <= set(result["menu_options"])
