"""Shared fixtures for the Necromancer pytest suite.

Runs inside the Home Assistant core test harness: the component is symlinked into
`tests/testing_config/custom_components/necromancer` and loaded via the
`enable_custom_integrations` fixture, so `MockConfigEntry` + the `hass` fixture
exercise the real setup path (engines + all four platforms).

`make_guard()` builds a subentry `data` dict in exactly the shape the config flow
emits (`config_flow_helpers._build_data`), so setup/entity tests can declare guards
declaratively without driving the multi-step flow.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Mapping, Sequence
from typing import Any

import pytest

from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from tests.common import MockConfigEntry

DOMAIN = "necromancer"

_CHECK_STRATEGIES = {"switch_check", "action_check", "actions_check", "poe_port"}


@pytest.fixture(autouse=True)
def _enable_custom(enable_custom_integrations: None) -> None:
    """Make every test load custom_components from the test config dir."""


def make_guard(
    name: str,
    *,
    strategy: str = "switch_check",
    source: str = "state_based",
    health_entity: str = "binary_sensor.guard_health",
    template: str = "{{ true }}",
    switch_entity: str = "switch.guard_target",
    action: list | None = None,
    off_action: list | None = None,
    on_action: list | None = None,
    expected_id: str = "aa:bb:cc:dd:ee:ff",
    device_id: str | None = None,
    debounce: int = 1,
    cooldown: int = 1,
    boot_window: int = 1,
    max_attempts: int = 2,
    off_on_delay: int = 1,
    notify_action: list | None = None,
    linked_guards: Sequence[str] | None = None,
    notify_follower_success: bool = False,
    on_value: Sequence[str] = ("on",),
    off_value: Sequence[str] = ("off",),
    attribute: str | None = None,
) -> dict[str, Any]:
    """Build one guard subentry `data` dict matching `_build_data`'s output."""
    notify_only = strategy == "notify"
    check = strategy in _CHECK_STRATEGIES

    if source == "template_based":
        health: dict[str, Any] = {"type": "template", "template": template}
    else:
        health = {
            "type": "entity_state",
            "entity_id": health_entity,
            "source": attribute or "state",
            "on_value": list(on_value),
            "off_value": list(off_value),
        }

    behavior: dict[str, Any] = {"debounce": debounce, "notify_action": notify_action}
    data: dict[str, Any] = {
        "name": name,
        "health": health,
        "policy": {"type": "notify" if notify_only else "standard"},
        "behavior": behavior,
    }

    if notify_only:
        data["driver"] = {"type": "noop"}
    else:
        behavior["cooldown"] = cooldown
        behavior["health_check"] = check
        if check:
            behavior["boot_window"] = boot_window
            behavior["max_attempts"] = max_attempts
        if strategy in ("action", "action_check"):
            data["driver"] = {"type": "action_call", "action": action}
        elif strategy in ("actions", "actions_check"):
            data["driver"] = {
                "type": "action_cycle",
                "off_action": off_action,
                "on_action": on_action,
                "off_on_delay": off_on_delay,
            }
        elif strategy == "poe_port":
            data["driver"] = {"type": "poe_port", "expected_id": expected_id}
        else:
            data["driver"] = {
                "type": "switch_cycle",
                "switch_entity": switch_entity,
                "off_on_delay": off_on_delay,
            }
        if notify_follower_success:
            behavior["notify_follower_success"] = True

    if device_id:
        data["device_id"] = device_id
    if linked_guards and not notify_only:
        data["linked_guards"] = sorted(linked_guards)
    return data


type SetupGuards = Callable[..., Coroutine[Any, Any, MockConfigEntry]]


@pytest.fixture
def setup_guards(hass: HomeAssistant) -> SetupGuards:
    """Return an async factory: create + set up a service entry with given guards.

    Usage: `entry = await setup_guards(make_guard("A"), make_guard("B"))`.
    Each positional arg is a guard data dict; subentry ids are `guard0`, `guard1`…
    Pass `options=` for the flat PoE port list. Asserts the entry loaded.
    """

    async def _setup(
        *guards: Mapping[str, Any],
        options: dict[str, Any] | None = None,
        entry_id: str = "necro_test_entry",
        expect_loaded: bool = True,
    ) -> MockConfigEntry:
        subentries = [
            ConfigSubentryData(
                data=dict(g),
                subentry_id=f"guard{i}",
                subentry_type="device",
                title=g["name"],
                unique_id=None,
            )
            for i, g in enumerate(guards)
        ]
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Necromancer",
            data={},
            options=options or {},
            subentries_data=subentries,
            entry_id=entry_id,
        )
        entry.add_to_hass(hass)
        ok = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        if expect_loaded:
            assert ok
            assert entry.state is ConfigEntryState.LOADED
        return entry

    return _setup


def entity_id_for(
    hass: HomeAssistant, subentry_id: str, domain: str, key: str
) -> str | None:
    """Resolve a guard's view-entity id via the registry (unique_id = `<sid>_<key>`)."""
    return er.async_get(hass).async_get_entity_id(
        domain, DOMAIN, f"{subentry_id}_{key}"
    )
