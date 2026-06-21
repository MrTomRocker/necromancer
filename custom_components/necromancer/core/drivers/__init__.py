"""Recovery driver registry + factory."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .action_call import ActionCallDriver
from .action_cycle import ActionCycleDriver
from .base import RecoveryDriver
from .noop import NoopDriver
from .poe_port import PoePortDriver
from .switch_cycle import SwitchCycleDriver

DRIVER_TYPES: dict[str, type[RecoveryDriver]] = {
    "switch_cycle": SwitchCycleDriver,
    "action_call": ActionCallDriver,
    "action_cycle": ActionCycleDriver,
    "poe_port": PoePortDriver,
    "noop": NoopDriver,
}


def create_driver(hass: HomeAssistant, config: dict) -> RecoveryDriver:
    """Build a RecoveryDriver from its config dict."""
    return DRIVER_TYPES[config["type"]](hass, config)


__all__ = ["DRIVER_TYPES", "RecoveryDriver", "create_driver"]
