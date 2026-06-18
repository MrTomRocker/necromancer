"""Notification helper — resolve a message and run the user's action.

Kept out of the engine so the state machine stays focused. Log lines stay
English; the user-facing text comes from `NOTIFY_MESSAGES` (picked by the
instance language, English fallback).

Delivery is a user-defined action sequence (script syntax) rather than fixed
notify entities: the user decides whether and how to notify. The resolved
message and context are exposed to the sequence as the variables `message`,
`name`, `event` (the notify key) plus any event params (attempt, max, reason).
The sequence runs detached so a user delay never stalls the engine.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant

from .actions import async_run
from .const import LOGGER, NOTIFY_MESSAGES


async def async_notify(
    hass: HomeAssistant,
    name: str,
    action: list | dict | None,
    key: str,
    **params: object,
) -> None:
    """Resolve the message for `key` in the active language and run the action."""
    lang = (hass.config.language or "en").split("-")[0]
    messages = NOTIFY_MESSAGES.get(lang, NOTIFY_MESSAGES["en"])
    template = messages.get(key) or NOTIFY_MESSAGES["en"].get(key, "{name}: " + key)
    try:
        message = template.format(name=name, **params)
    except (KeyError, IndexError):
        message = f"{name}: {key}"
    LOGGER.debug("Notify %s: %s", name, message)

    if not action:
        return
    variables = {"message": message, "name": name, "event": key, **params}

    async def _run() -> None:
        try:
            await async_run(hass, action, f"{name} notify", variables)
        except vol.Invalid as err:
            LOGGER.error("Notify action invalid for %s: %s", name, err)
        except Exception:  # noqa: BLE001
            LOGGER.exception("Notify action failed for %s", name)

    hass.async_create_task(_run(), f"necromancer notify {name}")
