"""Health source: a Jinja template that evaluates to healthy/unhealthy.

The inline alternative to `entity_state` — instead of pointing at one on/off
entity (or building a template *entity* for complex conditions), the user writes
a boolean template, e.g. `{{ states('sensor.cpu') | float(0) < 90 }}`.

A template is a continuous expression, so unlike a trigger it can be checked any
time: `evaluate()` renders it on demand (used for the recovery VERIFY step too),
and `async_setup` tracks it so the engine re-evaluates whenever a referenced
entity changes. `result_as_boolean` accepts on/off, true/false, 1/0, yes/no; an
empty/unknown result or a render error is UNKNOWN (no false alarm).
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import TemplateError
from homeassistant.helpers.event import (
    TrackTemplate,
    TrackTemplateResult,
    async_track_template_result,
)
from homeassistant.helpers.template import Template, result_as_boolean

from .base import Health, HealthSource

_UNKNOWN_RESULTS = {"", "none", "unknown", "unavailable"}


class TemplateHealth(HealthSource):
    """Map a boolean Jinja template to OK / UNHEALTHY / UNKNOWN."""

    def __init__(self, hass: HomeAssistant, config: dict) -> None:
        super().__init__(hass, config)
        self._template = Template(config["template"], hass)

    @property
    def watched_entities(self) -> list[str]:
        return []

    def referenced_entities(self) -> list[str]:
        """The entities this template reads (so the engine can spot a self-loop)."""
        try:
            return list(self._template.async_render_to_info().entities)
        except TemplateError:
            return []

    def evaluate(self) -> Health:
        try:
            result = self._template.async_render(parse_result=True)
        except TemplateError:
            return Health.UNKNOWN
        if result is None or (
            isinstance(result, str) and result.strip().lower() in _UNKNOWN_RESULTS
        ):
            return Health.UNKNOWN
        return Health.OK if result_as_boolean(result) else Health.UNHEALTHY

    async def async_setup(
        self, on_change: Callable[[], None]
    ) -> Callable[[], None] | None:
        @callback
        def _changed(_event: Event | None, _updates: list[TrackTemplateResult]) -> None:
            on_change()

        info = async_track_template_result(
            self.hass, [TrackTemplate(self._template, None)], _changed
        )
        info.async_refresh()
        return info.async_remove

    def describe(self) -> str:
        return f"template: {self.config['template']}"
