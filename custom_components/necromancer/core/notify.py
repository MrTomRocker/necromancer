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

import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant

from ..const import NOTIFY_MESSAGES
from .actions import async_run

LOGGER = logging.getLogger(__name__)

# Optional alert variables default to "" so a notify action template can reference
# them (`{{ attempt }}`) without an undefined-variable error when they don't apply.
_OPTIONAL_DEFAULTS = {"attempt": "", "max": "", "attempts": "", "reason": ""}


def _add_attempts(lang: str, params: dict) -> None:
    """Add a localized, plural-correct `attempts` phrase when `attempt` is present.

    Keeps messages TTS-friendly ("1 Versuch" / "3 Versuche", not "1 Versuchen").
    """
    if "attempt" in params:
        n = params["attempt"]
        if lang == "de":
            params["attempts"] = f"{n} Versuch" if n == 1 else f"{n} Versuche"
        else:
            params["attempts"] = f"{n} attempt" if n == 1 else f"{n} attempts"


def _resolve(lang: str, name: str, key: str, params: dict) -> tuple[str, str]:
    """Return (message, event_text) for `key` in `lang` (adds the `attempts` phrase)."""
    messages = NOTIFY_MESSAGES.get(lang, NOTIFY_MESSAGES["en"])
    template = messages.get(key) or NOTIFY_MESSAGES["en"].get(key, key)
    _add_attempts(lang, params)
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
    _dispatch(
        hass,
        name,
        action,
        {
            **_OPTIONAL_DEFAULTS,
            **params,
            "message": message,
            "name": name,
            "event_text": event_text,
            "event": key,
        },
    )


def _dispatch(
    hass: HomeAssistant, name: str, action: list | dict, variables: dict
) -> None:
    """Run the notify action detached so a user delay never stalls the engine."""

    async def _run() -> None:
        try:
            await async_run(hass, action, f"{name} notify", variables)
        except vol.Invalid as err:
            LOGGER.error("Notify action invalid for %s: %s", name, err)
        except Exception:
            LOGGER.exception("Notify action failed for %s", name)

    hass.async_create_task(_run(), f"necromancer notify {name}")


async def async_notify_custom(
    hass: HomeAssistant,
    name: str,
    action: list | dict | None,
    message: str,
    event: str = "custom",
    event_text: str | None = None,
    **params: object,
) -> None:
    """Run the guard's notify action with a caller-supplied message.

    Unlike `async_notify`, the text is not resolved from `NOTIFY_MESSAGES` — the
    caller provides it (e.g. a recovery script reporting progress via the
    `necromancer.notify_guard` service). The same variables a built-in alert exposes
    are passed (`message`, `name`, `event_text`, `event`, plus optional `attempt` /
    `max` / `attempts` / `reason`), so an existing notify action template works
    unchanged. `event_text` defaults to `message` when omitted.
    """
    LOGGER.debug("Notify (custom) %s: %s", name, message)
    if not action:
        return
    lang = (hass.config.language or "en").split("-")[0]
    _add_attempts(lang, params)
    _dispatch(
        hass,
        name,
        action,
        {
            **_OPTIONAL_DEFAULTS,
            **params,
            "message": message,
            "name": name,
            "event_text": message if event_text is None else event_text,
            "event": event,
        },
    )
