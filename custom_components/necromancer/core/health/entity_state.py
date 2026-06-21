"""Generic health source: an entity's state/attribute against on/off values.

The most generic rung of the health ladder. Works with any entity: read its
`state` or any attribute, then map it to a verdict — a value in `on_value` means
healthy, one in `off_value` means unhealthy, anything else is `UNKNOWN` (no false
alarm). `unavailable`/`unknown` are `UNKNOWN` by default, but an explicit
`off_value` wins — list `unavailable` there to treat "entity gone" as the failure
signal on purpose. A group or technology-specific sources layer on top later.

`on_value`/`off_value` are lists (any of several states counts), but a bare
string is accepted too. Backward compatible: older guards stored only
`healthy_state` (== on_value, with no off_value, so anything that isn't healthy
counts as unhealthy).
"""

from __future__ import annotations

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from .base import Health, HealthSource


def _as_set(raw: object) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple, set)):
        return {str(v) for v in raw}
    return {str(raw)}


class EntityStateHealth(HealthSource):
    """Map an entity's state/attribute to OK / UNHEALTHY / UNKNOWN."""

    @property
    def entity_id(self) -> str:
        return self.config["entity_id"]

    @property
    def source(self) -> str:
        """`state`, or an attribute name to read instead of the state."""
        return self.config.get("source", "state")

    @property
    def on_values(self) -> set[str]:
        return _as_set(self.config.get("on_value")) or _as_set(
            self.config.get("healthy_state", "on")
        )

    @property
    def off_values(self) -> set[str] | None:
        """Values meaning unhealthy; `None`/empty (legacy) = anything but on."""
        return _as_set(self.config.get("off_value")) or None

    @property
    def watched_entities(self) -> list[str]:
        return [self.entity_id]

    def evaluate(self) -> Health:
        state = self.hass.states.get(self.entity_id)
        if state is None:
            return Health.UNKNOWN
        if self.source == "state":
            actual = state.state
        else:
            actual = state.attributes.get(self.source)
            if actual is None:
                return Health.UNKNOWN
        actual = str(actual)
        off = self.off_values
        # An explicit off_value wins — even unavailable/unknown — so a guard can
        # treat "entity gone" as the failure signal on purpose.
        if off is not None and actual in off:
            return Health.UNHEALTHY
        # Otherwise ambiguous states stay UNKNOWN (no false alarm).
        if actual in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return Health.UNKNOWN
        if actual in self.on_values:
            return Health.OK
        if off is None:
            return Health.UNHEALTHY
        return Health.UNKNOWN

    def describe(self) -> str:
        if self.source == "state":
            return self.entity_id
        return f"{self.entity_id}[{self.source}]"
