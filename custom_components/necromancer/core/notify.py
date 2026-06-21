"""Notification helper — resolve a message and run the user's action.

Kept out of the engine so the state machine stays focused. Log lines stay
English; the user-facing text comes from `NOTIFY_MESSAGES` (picked by the
instance language, English fallback).

Delivery is a user-defined action sequence (script syntax) rather than fixed
notify entities: the user decides whether and how to notify. The resolved text and
context are exposed to the sequence as variables so the user can either take the
ready-made `message` or compose their own from the parts:
  - `message`    — the full line, "<name>: <event_text>"
  - `name`       — the guard name
  - `event_text` — the localized event text without the name (e.g. "Reparatur erfolgreich.")
  - `event`      — the notify key (e.g. "recovery_success")
  - plus event params: `attempt`, `max`, `attempts` (plural-correct), `reason`
The sequence runs detached so a user delay never stalls the engine.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant

from ..const import LOGGER, NOTIFY_MESSAGES
from .actions import async_run


def _resolve(lang: str, name: str, key: str, params: dict) -> tuple[str, str]:
    """Return (message, event_text) for `key` in `lang`.

    Mutates `params` to add a localized, plural-correct `attempts` phrase whenever
    an `attempt` count is present (so messages stay TTS-friendly: "1 Versuch" /
    "3 Versuche", not "1 Versuchen").
    """
    messages = NOTIFY_MESSAGES.get(lang, NOTIFY_MESSAGES["en"])
    template = messages.get(key) or NOTIFY_MESSAGES["en"].get(key, key)
    if "attempt" in params:
        n = params["attempt"]
        if lang == "de":
            params["attempts"] = f"{n} Versuch" if n == 1 else f"{n} Versuche"
        else:
            params["attempts"] = f"{n} attempt" if n == 1 else f"{n} attempts"
    try:
        event_text = template.format(name=name, **params)
    except (KeyError, IndexError):
        event_text = key
    return f"{name}: {event_text}", event_text


async def async_notify(
    hass: HomeAssistant,
    name: str,
    action: list | dict | None,
    key: str,
    **params: object,
) -> None:
    """Resolve the message for `key` in the active language and run the action."""
    lang = (hass.config.language or "en").split("-")[0]
    message, event_text = _resolve(lang, name, key, params)
    LOGGER.debug("Notify %s: %s", name, message)

    if not action:
        return
    variables = {
        "message": message,
        "name": name,
        "event_text": event_text,
        "event": key,
        **params,
    }

    async def _run() -> None:
        try:
            await async_run(hass, action, f"{name} notify", variables)
        except vol.Invalid as err:
            LOGGER.error("Notify action invalid for %s: %s", name, err)
        except Exception:  # noqa: BLE001
            LOGGER.exception("Notify action failed for %s", name)

    hass.async_create_task(_run(), f"necromancer notify {name}")
