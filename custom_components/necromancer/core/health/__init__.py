"""Health source registry + factory."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .base import Health, HealthSource
from .entity_state import EntityStateHealth
from .template import TemplateHealth

# Generic first; group + technology-specific sources layer on later.
HEALTH_TYPES: dict[str, type[HealthSource]] = {
    "entity_state": EntityStateHealth,
    "template": TemplateHealth,
}


def create_health(hass: HomeAssistant, config: dict) -> HealthSource:
    """Build a HealthSource from its config dict."""
    return HEALTH_TYPES[config["type"]](hass, config)


__all__ = ["HEALTH_TYPES", "Health", "HealthSource", "create_health"]
