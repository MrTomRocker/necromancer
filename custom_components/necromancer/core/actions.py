"""Run user-defined action sequences (script syntax) from the action selector.

The action selector stores its value raw, so before a `Script` can run it the
sequence needs the static schema (`cv.SCRIPT_SCHEMA`, which also normalises the
legacy `service` key to `action`) plus the async pass for dynamic actions
(device/condition/trigger). Shared by recovery drivers (blocking) and notify
(detached).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.script import Script, async_validate_actions_config

from ..const import DOMAIN


async def async_validate(hass: HomeAssistant, action: list | dict | None) -> list:
    """Normalise + validate a raw action-selector value into a runnable sequence."""
    if not action:
        return []
    sequence = action if isinstance(action, list) else [action]
    return await async_validate_actions_config(hass, cv.SCRIPT_SCHEMA(sequence))


async def async_run(
    hass: HomeAssistant,
    action: list | dict | None,
    name: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and run an action sequence, blocking until it finishes.

    Returns the script's final variable scope (including any set via a
    `variables:` action), so a caller can chain one run's variables into the
    next — e.g. `action_cycle` passing its off phase into its on phase. Empty
    dict when there is nothing to run or the run produced no result.
    """
    sequence = await async_validate(hass, action)
    if not sequence:
        return {}
    script = Script(hass, sequence, name, DOMAIN)
    result = await script.async_run(variables or {}, context=Context())
    if not result:
        return {}
    # `.variables` is the run's final scope; HA injects its run `context` there.
    # Carry only real variables forward, not the (stale) Context object.
    return {k: v for k, v in result.variables.items() if k != "context"}


def static_errors(action: list | dict | None) -> list[str]:
    """Sync, best-effort validation for startup config checks."""
    if not action:
        return []
    sequence = action if isinstance(action, list) else [action]
    try:
        cv.SCRIPT_SCHEMA(sequence)
    except vol.Invalid as err:
        return [f"invalid action: {err}"]
    return []
